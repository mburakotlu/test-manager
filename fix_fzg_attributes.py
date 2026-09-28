"""Fill in missing standard attributes on FZG Insight objects from DLCM_FZG_Planung.xlsx.

Companion to audit_fzg_attributes.py: for every FZG object missing VIN/Scope/
Baureihe/Cluster/ZielStand/Tests/Testplanung (KW), looks up the matching row(s)
in the Jahreskalender sheet (by vehicle label, same as import_jahreskalender.py)
and writes only the attributes that are (a) currently missing and (b) have real
source data in the Excel. Never fabricates Markt/Aktivität/Testart — these have
no source anywhere in this repo, so they're left alone if missing.

Usage:
    python fix_fzg_attributes.py [--xlsx DLCM_FZG_Planung.xlsx] [--confirm]

Dry-run by default: prints the planned attribute writes per vehicle without
touching Jira. Pass --confirm to actually write them.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import openpyxl

from import_jahreskalender import extract_entries
from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient

DEFAULT_XLSX = "DLCM_FZG_Planung.xlsx"

# Insight FZG object type attribute ids (see jira_agent/jira_client.py + repo memory).
ATTR_IDS = {
    "VIN": 25048,
    "Scope": 25049,
    "Markt": 25050,
    "Baureihe": 25052,
    "ZielStand": 25053,
    "Cluster": 25055,
    "Tests": 25059,
    "Testplanung (KW)": 25060,
}
STANDARD_ATTRIBUTES = [
    "Key", "Bezeichnung", "Created", "Updated", "VIN", "Scope", "Markt",
    "Aktivität", "Baureihe", "ZielStand", "Testart", "Tests", "Testplanung (KW)",
]
ATTR_ID_TO_NAME = {v: k for k, v in ATTR_IDS.items()}


def extract_static_attrs(xlsx_path: Path) -> dict[str, dict[str, str]]:
    """Return {vehicle_name: {"Baureihe":..., "Cluster":..., "Markt":..., "ZielStand":..., "Scope":..., "VIN":...}}.

    Only read from each vehicle block's own header row (where the name cell is
    actually set), matching how the name "carries down" for day-cell rows below it.
    """
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb["Jahreskalender"]
    result: dict[str, dict[str, str]] = {}
    for r in range(9, ws.max_row + 1):
        name = ws.cell(row=r, column=2).value
        if not name:
            continue
        name = str(name).split(" (")[0].strip()
        values = {
            "Baureihe": ws.cell(row=r, column=4).value,
            "Cluster": ws.cell(row=r, column=5).value,
            "Markt": ws.cell(row=r, column=6).value,
            "ZielStand": ws.cell(row=r, column=7).value,
            "Scope": ws.cell(row=r, column=8).value,
            "VIN": ws.cell(row=r, column=9).value,
        }
        result[name] = {k: str(v).strip() for k, v in values.items() if v is not None and str(v).strip()}
    return result


def extract_test_queues(xlsx_path: Path) -> dict[str, tuple[str, str]]:
    """Return {vehicle_name: (pipe_joined_names, pipe_joined_periods)} from day-cell entries."""
    entries = extract_entries(xlsx_path)
    by_name: dict[str, list[tuple[object, str]]] = {}
    for name, date, text in entries:
        stripped = name.split(" (")[0].strip()
        by_name.setdefault(stripped, []).append((date, text))

    result = {}
    for name, date_text_list in by_name.items():
        names, periods = [], []
        for date, text in date_text_list:
            iso_year, iso_week, _ = date.isocalendar()
            periods.append(f"KW{iso_week:02d}-{iso_year}")
            names.append(" / ".join(line.strip() for line in text.splitlines() if line.strip()))
        result[name] = (" | ".join(names), " | ".join(periods))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--xlsx", default=DEFAULT_XLSX)
    parser.add_argument("--confirm", action="store_true", help="Actually write to Insight (default: dry-run only).")
    args = parser.parse_args()

    xlsx_path = Path(args.xlsx)
    static_attrs = extract_static_attrs(xlsx_path)
    test_queues = extract_test_queues(xlsx_path)

    config = load_config()
    client = JiraClient(config)
    try:
        vehicles = client.list_test_vehicles(
            iql="objectType = FZG",
            object_type_id=client.INSIGHT_VEHICLE_ASSET_OBJECT_TYPE_ID,
            object_schema_id=client.INSIGHT_VEHICLE_ASSET_SCHEMA_ID,
            max_results=200,
        )

        planned: list[tuple[str, str, int, dict[int, list[str]]]] = []
        unmatched: list[str] = []
        no_gaps: list[str] = []
        for v in vehicles:
            obj = client.get_insight_object(v["key"])
            label = obj["label"]
            missing = {
                name for name in STANDARD_ATTRIBUTES
                if not obj["attributes"].get(name) or not obj["attributes"][name][0]
            }
            if not missing:
                no_gaps.append(label)
                continue

            static = static_attrs.get(label, {})
            names_joined, periods_joined = test_queues.get(label, ("", ""))

            attrs: dict[int, list[str]] = {}
            for field, value in static.items():
                if field in missing and field in ATTR_IDS:
                    attrs[ATTR_IDS[field]] = [value]
            if "Tests" in missing and names_joined:
                attrs[ATTR_IDS["Tests"]] = [names_joined]
            if "Testplanung (KW)" in missing and periods_joined:
                attrs[ATTR_IDS["Testplanung (KW)"]] = [periods_joined]

            if not attrs:
                unmatched.append(label)
                continue
            planned.append((label, obj["key"], obj["id"], attrs))

        print(f"{len(vehicles)} FZG object(s) checked, {len(no_gaps)} already complete, "
              f"{len(unmatched)} with gaps but no Excel data found, {len(planned)} fixable from Excel.\n")
        if unmatched:
            print(f"No Excel match / no fixable data for: {unmatched}\n")

        for label, key, _object_id, attrs in planned:
            filled = {ATTR_ID_TO_NAME[a_id]: val[0] for a_id, val in attrs.items()}
            print(f"{label} ({key}): would set {filled}")

        if not args.confirm:
            print(f"\nDRY-RUN only — {len(planned)} object(s) would be updated. Re-run with --confirm to execute.")
            return

        for label, key, object_id, attrs in planned:
            client.update_insight_object_attributes(object_id, attrs, confirm=True)
            print(f"Updated {label} ({key})")
    finally:
        client.close()


if __name__ == "__main__":
    main()
