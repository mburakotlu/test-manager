"""Import planned test activities from the Jahreskalender sheet of DLCM_FZG_Planung.xlsx
into the matching vehicles/*.yaml files' ``tests:`` block.

The calendar has one week-column per day (col headers are dates); each vehicle row
block can have free-text entries in any day-cell describing that week's planned
activity. This script:
  - reads every non-empty day-cell per vehicle row,
  - converts its date to an ISO calendar week label "KWnn-YYYY" (year-suffixed since
    the sheet spans 2025-2026 and week numbers repeat across years),
  - matches the calendar's vehicle name to a local vehicles/*.yaml file (by VIN,
    since calendar names sometimes differ from the yaml `label`, e.g. "SE0800" vs
    "SEP800"),
  - REPLACES that vehicle's whole `tests:` block with one entry per non-empty week
    (previous dummy entries are discarded, per user direction).

Usage:
    python import_jahreskalender.py [--xlsx PATH] [--vehicles-dir DIR] [--confirm]

Dry-run by default (prints planned changes); pass --confirm to actually write files.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import openpyxl

from jira_agent.vehicle import TestAssignment, is_vehicle_inactive, load_vehicles, save_vehicle_tests

DEFAULT_XLSX = "DLCM_FZG_Planung.xlsx"
DEFAULT_VEHICLES_DIR = "vehicles"


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


def import_calendar(xlsx_path: Path, vehicles_dir: str | Path, confirm: bool = False) -> dict[str, object]:
    """Refresh each matched vehicle's ``tests:`` block from the Jahreskalender sheet.

    Shared core used by both this script's CLI and the web UI's /refresh route.
    Returns a summary dict: ``{"matched": [(label, existing_count, new_count)],
    "unmatched": [names], "vehicles": [updated Vehicle objects],
    "diffs": [{"label", "added": [(name, period)], "removed": [(name, period)]}],
    "inactive": [labels]}``.
    ``diffs`` entries are only included for vehicles whose (name, period) set
    actually changed, so callers can highlight what's new/gone since last import.
    ``inactive`` lists every vehicle (matched or not) with no test planned in the
    last 2 weeks up to today, so callers can flag it as possibly stale/abandoned.
    """
    entries = extract_entries(xlsx_path)
    vin_to_calendar_name = build_vin_to_name(xlsx_path)

    vehicles = load_vehicles(vehicles_dir)

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
    diffs: list[dict[str, object]] = []
    unmatched_names: set[str] = set()
    for calendar_name, date_text_list in entries_by_calendar_name.items():
        vehicle = calendar_name_to_vehicle.get(calendar_name)
        if vehicle is None:
            unmatched_names.add(calendar_name)
            continue
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
        matched.append((vehicle.label, len(vehicle.tests), len(new_tests)))

        old_keys = set(old_by_key.keys())
        new_keys = {(t.name, t.period) for t in new_tests}
        added = sorted(new_keys - old_keys, key=lambda k: (k[1], k[0]))
        removed = sorted(old_keys - new_keys, key=lambda k: (k[1], k[0]))
        if added or removed:
            diffs.append({"label": vehicle.label, "added": added, "removed": removed})

        vehicle.tests = new_tests
        updated_vehicles.append(vehicle)
        if confirm:
            save_vehicle_tests(vehicle)

    inactive = sorted(v.label for v in vehicles if is_vehicle_inactive(v))

    return {
        "matched": matched,
        "unmatched": sorted(unmatched_names),
        "vehicles": updated_vehicles,
        "diffs": diffs,
        "inactive": inactive,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xlsx", default=DEFAULT_XLSX)
    parser.add_argument("--vehicles-dir", default=DEFAULT_VEHICLES_DIR)
    parser.add_argument("--confirm", action="store_true", help="Actually write the YAML files (default: dry-run).")
    args = parser.parse_args()

    result = import_calendar(Path(args.xlsx), args.vehicles_dir, confirm=args.confirm)
    for label, existing_count, new_count in result["matched"]:
        print(f"{label}: {existing_count} existing test(s) -> {new_count} imported test(s)")

    print(f"\nMatched {len(result['matched'])} vehicle(s) from calendar.")
    if result["unmatched"]:
        print(f"Unmatched calendar names (no corresponding vehicles/*.yaml): {result['unmatched']}")

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
        print(f"\n\u26a0 INACTIVE (no test planned in the last 2 weeks): {', '.join(result['inactive'])}")

    if not args.confirm:
        print("\nDry-run only. Re-run with --confirm to write changes to vehicles/*.yaml.")


if __name__ == "__main__":
    main()
