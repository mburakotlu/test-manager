"""Import planned test activities from Jahreskalender into FZG Assets.

The calendar has one week-column per day (col headers are dates); each vehicle row
block can have free-text entries in any day-cell describing that week's planned
activity. This script:
  - reads every non-empty day-cell per vehicle row,
  - converts its date to an ISO calendar week label "KWnn-YYYY" (year-suffixed since
    the sheet spans 2025-2026 and week numbers repeat across years),
  - matches the calendar's vehicle name to an FZG Asset by VIN or label,
    - updates the aligned Tests, Testplanung (KW), Tickets, and Status
    attributes. Existing ticketed tests stay linked if absent from the calendar.

Usage:
    python import_jahreskalender.py [--xlsx PATH] [--confirm]

Dry-run by default; pass --confirm to update Assets.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import openpyxl
from dotenv import load_dotenv

from jira_agent.vehicle import TestAssignment, Vehicle, is_vehicle_inactive
from jira_agent.asset_vehicles import load_asset_vehicles
from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient
from sync_tests_to_insight import sync_all

DEFAULT_XLSX = "DLCM_FZG_Planung.xlsx"


def extract_entries(xlsx_path: Path) -> list[tuple[str, object, str]]:
    """Return (vehicle_name, date, text) for every non-empty day-cell in Jahreskalender."""
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb["Jahreskalender"]
    date_row = 8
    name_col = 2
    entries: list[tuple[str, object, str]] = []
    current_name: str | None = None
    for r in range(9, ws.max_row + 1):
        n = ws.cell(row=r, column=name_col).value
        if n:
            current_name = str(n).strip()
        if current_name is None:
            continue
        for c in range(10, ws.max_column + 1):
            v = ws.cell(row=r, column=c).value
            if v is not None and str(v).strip():
                d = ws.cell(row=date_row, column=c).value
                entries.append((current_name, d, str(v).strip()))
    return entries


def build_vin_to_name(xlsx_path: Path) -> dict[str, str]:
    """Read the FZGToVIN sheet: VIN -> calendar vehicle name."""
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb["FZGToVIN"]
    result: dict[str, str] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        name, vin = row[0], row[1]
        if name and vin:
            result[str(vin).strip()] = str(name).strip()
    return result


def calendar_static_attrs(xlsx_path: Path) -> dict[str, dict[str, str]]:
    """Read vehicle details from each Jahreskalender vehicle header row."""
    workbook = openpyxl.load_workbook(xlsx_path, data_only=True)
    sheet = workbook["Jahreskalender"]
    fields = {4: "series", 5: "cluster", 6: "market", 7: "zielstand",
              8: "scope", 9: "vin"}
    result = {}
    for row in range(9, sheet.max_row + 1):
        raw_name = sheet.cell(row=row, column=2).value
        if not raw_name:
            continue
        name = str(raw_name).split(" (")[0].strip()
        details = {
            field: str(value).strip()
            for column, field in fields.items()
            if (value := sheet.cell(row=row, column=column).value) is not None
            and str(value).strip()
        }
        result.setdefault(name, {}).update(details)
    return result


def import_calendar(xlsx_path: Path, vehicles: list) -> dict[str, object]:
    """Refresh each matched vehicle's ``tests:`` block from the Jahreskalender sheet.

    Shared core used by both this script's CLI and the web UI's /refresh route.
    Returns a summary dict: ``{"matched": [(label, existing_count, new_count)],
    "new_vehicles": [new Vehicle objects], "vehicles": [updated Vehicle objects],
    "diffs": [{"label", "added": [(name, period)], "removed": [(name, period)]}],
    "inactive": [labels]}``.
    ``diffs`` entries are only included for vehicles whose (name, period) set
    actually changed, so callers can highlight what's new/gone since last import.
    ``inactive`` lists every vehicle (matched or not) with no test planned in the
    last 2 weeks up to today, so callers can flag it as possibly stale/abandoned.
    """
    entries = extract_entries(xlsx_path)
    vin_to_calendar_name = build_vin_to_name(xlsx_path)
    static_attrs = calendar_static_attrs(xlsx_path)
    calendar_name_to_vin = {name: vin for vin, name in vin_to_calendar_name.items()}


    # calendar name -> vehicle (matched via VIN, falls back to stripped label match)
    calendar_name_to_vehicle = {}
    for vehicle in vehicles:
        calendar_name = vin_to_calendar_name.get(vehicle.vin)
        if calendar_name:
            calendar_name_to_vehicle[calendar_name] = vehicle
        calendar_name_to_vehicle.setdefault(vehicle.label, vehicle)

    # group entries by vehicle
    entries_by_calendar_name: dict[str, list[tuple[object, str]]] = {}
    for name, date, text in entries:
        stripped = name.split(" (")[0].strip()  # e.g. "SE0534 (H2)" -> "SE0534"
        entries_by_calendar_name.setdefault(stripped, []).append((date, text))

    matched: list[tuple[str, int, int]] = []
    updated_vehicles = []
    new_vehicles = []
    diffs: list[dict[str, object]] = []
    for calendar_name, date_text_list in entries_by_calendar_name.items():
        vehicle = calendar_name_to_vehicle.get(calendar_name)
        if vehicle is None:
            details = static_attrs.get(calendar_name, {})
            vehicle = Vehicle(label=calendar_name, **details)
            vehicle.vin = vehicle.vin or calendar_name_to_vin.get(calendar_name, "")
            if not vehicle.vin and len(calendar_name) == 17 and calendar_name.startswith("WP"):
                vehicle.vin = calendar_name
            new_vehicles.append(vehicle)
        # Preserve template/ticket/description for entries unchanged since the last
        # import (matched by name+period) instead of resetting them to defaults —
        # otherwise a re-refresh silently wipes out manual template overrides (e.g.
        # "daily_enna") and, worse, existing ticket links, risking duplicate tickets.
        old_by_key = {(t.name, t.period): t for t in vehicle.tests}
        new_tests = []
        for date, text in date_text_list:
            iso_year, iso_week, _ = date.isocalendar()
            period = f"KW{iso_week:02d}-{iso_year}"
            clean_text = " / ".join(line.strip() for line in text.splitlines() if line.strip())
            old = old_by_key.get((clean_text, period))
            new_tests.append(TestAssignment(
                name=clean_text,
                period=period,
                description=old.description if old else "",
                template=old.template if old else "analysis_checklist",
                ticket=old.ticket if old else "",
            ))
        remaining = Counter((test.name, test.period) for test in new_tests)
        for old_test in vehicle.tests:
            key = (old_test.name, old_test.period)
            if remaining[key]:
                remaining[key] -= 1
            elif old_test.ticket:
                new_tests.append(old_test)
        matched.append((vehicle.label, len(vehicle.tests), len(new_tests)))

        old_keys = set(old_by_key.keys())
        new_keys = {(t.name, t.period) for t in new_tests}
        added = sorted(new_keys - old_keys, key=lambda k: (k[1], k[0]))
        removed = sorted(old_keys - new_keys, key=lambda k: (k[1], k[0]))
        if added or removed:
            diffs.append({"label": vehicle.label, "added": added, "removed": removed})

        vehicle.tests = new_tests
        updated_vehicles.append(vehicle)

    inactive = sorted(v.label for v in vehicles if is_vehicle_inactive(v))

    return {
        "matched": matched,
        "unmatched": [],
        "vehicles": updated_vehicles,
        "new_vehicles": new_vehicles,
        "diffs": diffs,
        "inactive": inactive,
    }


def main() -> None:
    load_dotenv(Path(__file__).parent / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xlsx", default=DEFAULT_XLSX)
    parser.add_argument("--confirm", action="store_true", help="Actually update Assets (default: dry-run).")
    args = parser.parse_args()

    client = JiraClient(load_config())
    try:
        vehicles = load_asset_vehicles(client)
        result = import_calendar(Path(args.xlsx), vehicles)
        if args.confirm:
            sync_all(result["vehicles"], client, confirm=True)
    finally:
        client.close()
    for label, existing_count, new_count in result["matched"]:
        print(f"{label}: {existing_count} existing test(s) -> {new_count} imported test(s)")

    print(f"\nMatched {len(result['matched'])} vehicle(s) from calendar.")
    if result["new_vehicles"]:
        print(f"New FZG Assets {'created' if args.confirm else 'planned'}: "
              + ", ".join(v.label for v in result["new_vehicles"]))

    if result["diffs"]:
        print(f"\nChanges vs. previous import ({len(result['diffs'])} vehicle(s) affected):")
        for diff in result["diffs"]:
            print(f"  {diff['label']}:")
            for name, period in diff["added"]:
                print(f"    + [{period}] {name}")
            for name, period in diff["removed"]:
                print(f"    - [{period}] {name}")
    else:
        print("\nNo changes vs. previous import.")

    if result["inactive"]:
        print(f"\nINACTIVE (no test planned in the last 2 weeks): {', '.join(result['inactive'])}")

    if not args.confirm:
        print("\nDry-run only. Re-run with --confirm to update Assets.")


if __name__ == "__main__":
    main()
