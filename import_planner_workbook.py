"""One-time, dry-run-by-default import of vehicle plans into the planner database."""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

import openpyxl
from openpyxl.styles.colors import COLOR_INDEX
from openpyxl.utils.datetime import from_excel

from dotenv import load_dotenv
from app_paths import APP_DIR
from planner_store import (
    import_vehicle_tab_history,
    import_workbook_data,
    initialize_database,
    sync_workbook_updates,
    update_imported_activity_colors,
    update_imported_activity_spans,
    update_vehicle_groups,
)

DEFAULT_WORKBOOK = APP_DIR / "DLCM_FZG_Planung.xlsx"
load_dotenv(APP_DIR / ".env")
VEHICLE_COLUMNS = {
    2: "label",
    3: "logger",
    4: "series",
    5: "cluster",
    6: "market",
    7: "vr_stand",
    8: "purpose",
    9: "vin",
}


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _date_value(value: object, epoch: datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        converted = from_excel(value, epoch)
        if isinstance(converted, datetime):
            return converted.date()
        if isinstance(converted, date):
            return converted
    if isinstance(value, str):
        return date.fromisoformat(value.strip())
    raise ValueError(f"Unrecognized calendar date value: {value!r}")


def _theme_palette(workbook) -> list[str]:
    theme = getattr(workbook, "loaded_theme", None)
    if not theme:
        return []
    root = ET.fromstring(theme)
    namespace = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    scheme = root.find(".//a:clrScheme", namespace)
    if scheme is None:
        return []
    palette = []
    for theme_color in list(scheme):
        color = next(iter(theme_color), None)
        if color is None:
            palette.append("")
        else:
            value = color.get("lastClr") or color.get("val") or ""
            palette.append(value.upper() if re.fullmatch(r"[0-9A-Fa-f]{6}", value) else "")
    return palette


def _apply_tint(rgb: str, tint: float) -> str:
    if not tint:
        return rgb.upper()
    red, green, blue = (int(rgb[index:index + 2], 16) / 255 for index in (0, 2, 4))
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    lightness = (
        lightness * (1 + tint)
        if tint < 0
        else lightness * (1 - tint) + (1 - (1 - tint))
    )
    tinted = colorsys.hls_to_rgb(hue, min(1, max(0, lightness)), saturation)
    return "".join(f"{round(channel * 255):02X}" for channel in tinted)


def _activity_fill_color(cell, theme_palette: list[str]) -> str:
    fill = cell.fill
    color = fill.fgColor
    if fill.fill_type != "solid":
        return ""
    if color.type == "rgb" and color.rgb:
        value = color.rgb[-6:]
    elif color.type == "theme" and color.theme < len(theme_palette):
        value = theme_palette[color.theme]
    elif color.type == "indexed" and color.indexed < len(COLOR_INDEX):
        value = COLOR_INDEX[color.indexed][-6:]
    else:
        return ""
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", value):
        return ""
    return f"#{_apply_tint(value, color.tint)}"


def read_workbook(path: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if not path.is_file():
        raise FileNotFoundError(f"Workbook not found: {path}")
    workbook = openpyxl.load_workbook(path, data_only=True, read_only=False)
    try:
        return _parse_workbook(workbook)
    finally:
        workbook.close()


def read_vehicle_tab_history(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"Workbook not found: {path}")
    vehicles, _ = read_workbook(path)
    vehicle_labels = {vehicle["label"].casefold(): vehicle["label"] for vehicle in vehicles}
    workbook = openpyxl.load_workbook(path, data_only=True, read_only=False)
    try:
        entries: list[dict[str, str]] = []
        non_vehicle_sheets = {"jahreskalender", "fzgtovin", "info", "tabelle1"}
        for sheet in workbook.worksheets:
            if sheet.title.casefold() in non_vehicle_sheets:
                continue
            canonical_label = vehicle_labels.get(sheet.title.strip().casefold())
            has_history_rows = any(
                any(sheet.cell(row=row, column=column).value is not None for column in range(1, 4))
                for row in range(2, sheet.max_row + 1)
            )
            if not has_history_rows:
                continue
            if canonical_label is None:
                raise ValueError(f"Vehicle history sheet {sheet.title!r} does not match a vehicle in Jahreskalender.")
            headers = tuple(_text(sheet.cell(1, column).value).casefold() for column in range(1, 4))
            if headers != ("wer?", "was?", "wann?"):
                raise ValueError(f"Unexpected history columns on vehicle sheet {sheet.title!r}.")
            for row in range(2, sheet.max_row + 1):
                who = _text(sheet.cell(row, 1).value)
                activity = _text(sheet.cell(row, 2).value)
                raw_date = sheet.cell(row, 3).value
                if not (who or activity or raw_date):
                    continue
                if not who or not activity or raw_date is None:
                    raise ValueError(
                        f"Incomplete history record on {sheet.title!r} row {row}; expected who, activity, and date."
                    )
                planned_date = _date_value(raw_date, workbook.epoch)
                iso_year, iso_week, _ = planned_date.isocalendar()
                entries.append(
                    {
                        "vehicle_label": canonical_label,
                        "activity": " / ".join(line.strip() for line in activity.splitlines() if line.strip()),
                        "period": f"KW{iso_week:02d}-{iso_year}",
                        "planned_date": planned_date.isoformat(),
                        "description": f"Recorded by: {who}",
                        "checklist_template": "analysis_checklist",
                    }
                )
        return entries
    finally:
        workbook.close()


def _parse_workbook(workbook) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if "Jahreskalender" not in workbook.sheetnames:
        raise ValueError("Workbook is missing the Jahreskalender worksheet.")
    sheet = workbook["Jahreskalender"]
    theme_palette = _theme_palette(workbook)
    headers = {
        _text(sheet.cell(row=8, column=column).value).casefold(): column
        for column in range(1, sheet.max_column + 1)
    }
    required_headers = {
        "name": 2,
        "logger": 3,
        "baureihe": 4,
        "cluster": 5,
        "markt": 6,
        "vr-stand": 7,
        "verwendungszweck": 8,
        "vin": 9,
    }
    for header, expected_column in required_headers.items():
        if headers.get(header) != expected_column:
            raise ValueError(f"Unexpected workbook layout: expected {header!r} in column {expected_column}.")

    vehicles_by_label: dict[str, dict[str, str]] = {}
    entries: list[dict[str, str]] = []
    merged_activity_ranges = {
        (merged.min_row, merged.min_col): merged.max_col
        for merged in sheet.merged_cells.ranges
        if merged.min_row >= 9 and merged.min_col >= 10 and merged.max_col > merged.min_col
    }
    current_label = ""
    current_group = ""
    group_orders: dict[str, int] = {}
    vehicle_orders: dict[str, int] = {}
    for row in range(9, sheet.max_row + 1):
        raw_label = _text(sheet.cell(row=row, column=2).value)
        row_values = [_text(sheet.cell(row=row, column=column).value) for column in range(3, 10)]
        raw_activities = [
            sheet.cell(row=row, column=column).value
            for column in range(10, sheet.max_column + 1)
        ]
        if raw_label and not any(row_values) and not any(_text(value) for value in raw_activities):
            current_group = raw_label
            group_orders.setdefault(current_group, len(group_orders))
            current_label = ""
            continue
        if raw_label:
            current_label = raw_label.split(" (", 1)[0].strip()
        if not current_label:
            continue

        vehicle = vehicles_by_label.setdefault(
            current_label.casefold(),
            {
                "label": current_label,
                "group_name": current_group,
                "group_order": str(group_orders.get(current_group, len(group_orders))),
                "display_order": str(vehicle_orders.setdefault(current_label.casefold(), len(vehicle_orders))),
                **{field: "" for field in VEHICLE_COLUMNS.values() if field != "label"},
            },
        )
        for column, field in VEHICLE_COLUMNS.items():
            value = _text(sheet.cell(row=row, column=column).value)
            if value and not vehicle[field]:
                if field == "logger":
                    normalized = value.casefold()
                    if normalized in ("ja", "yes", "y", "true", "1"):
                        value = "Yes"
                    elif normalized in ("nein", "no", "n", "false", "0"):
                        value = "No"
                vehicle[field] = value

        for column, raw_activity in enumerate(raw_activities, start=10):
            activity = _text(raw_activity)
            if not activity:
                continue
            raw_date = sheet.cell(row=8, column=column).value
            if raw_date is None:
                raise ValueError(f"Activity at {sheet.cell(row=row, column=column).coordinate} has no date header.")
            planned_date = _date_value(raw_date, workbook.epoch)
            end_column = merged_activity_ranges.get((row, column), column)
            raw_end_date = sheet.cell(row=8, column=end_column).value
            if raw_end_date is None:
                raise ValueError(
                    f"Activity at {sheet.cell(row=row, column=column).coordinate} has no end-date header."
                )
            planned_end_date = _date_value(raw_end_date, workbook.epoch)
            if planned_end_date < planned_date:
                raise ValueError(
                    f"Activity at {sheet.cell(row=row, column=column).coordinate} ends before it starts."
                )
            iso_year, iso_week, _ = planned_date.isocalendar()
            entries.append(
                {
                    "vehicle_label": current_label,
                    "activity": " / ".join(line.strip() for line in activity.splitlines() if line.strip()),
                    "period": f"KW{iso_week:02d}-{iso_year}",
                    "planned_date": planned_date.isoformat(),
                    "planned_end_date": planned_end_date.isoformat(),
                    "description": "",
                    "activity_color": _activity_fill_color(
                        sheet.cell(row=row, column=column), theme_palette
                    ),
                    "checklist_template": "analysis_checklist",
                }
            )

    # Grouping rows such as "MLBevo" are not vehicles unless they have vehicle
    # attributes or at least one dated activity.
    labels_with_bookings = {entry["vehicle_label"].casefold() for entry in entries}
    vehicles = [
        vehicle for key, vehicle in vehicles_by_label.items()
        if key in labels_with_bookings or any(
            vehicle[field] for field in ("logger", "series", "cluster", "market", "vr_stand", "purpose", "vin")
        )
    ]
    valid_labels = {vehicle["label"].casefold() for vehicle in vehicles}
    entries = [entry for entry in entries if entry["vehicle_label"].casefold() in valid_labels]
    return vehicles, entries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xlsx", type=Path, default=DEFAULT_WORKBOOK, help="Workbook to import.")
    parser.add_argument(
        "--update-groups",
        action="store_true",
        help="Preview/apply the workbook's group headings and row order to existing vehicles only.",
    )
    parser.add_argument(
        "--update-colors",
        action="store_true",
        help="Preview/apply Excel activity fill colors to already imported calendar activities.",
    )
    parser.add_argument(
        "--update-spans",
        action="store_true",
        help="Preview/apply merged Excel date ranges to already imported calendar activities.",
    )
    parser.add_argument(
        "--import-vehicle-tabs",
        action="store_true",
        help="Preview/import the additional dated history rows on vehicle-specific worksheets.",
    )
    parser.add_argument(
        "--sync-updates",
        action="store_true",
        help="Preview/add workbook changes without deleting bookings or overwriting populated vehicle fields.",
    )
    parser.add_argument("--confirm", action="store_true", help="Apply the requested database change.")
    args = parser.parse_args()

    workbook_operations = (
        args.update_groups,
        args.update_colors,
        args.update_spans,
        args.import_vehicle_tabs,
        args.sync_updates,
    )
    if sum(workbook_operations) > 1:
        parser.error("Choose only one workbook operation at a time.")

    vehicles, bookings = read_workbook(args.xlsx)
    print(f"Workbook: {args.xlsx.name}")
    digest = hashlib.sha256(args.xlsx.read_bytes()).hexdigest()
    if args.sync_updates:
        initialize_database()
        counts = sync_workbook_updates(vehicles, bookings, apply=args.confirm)
        print(
            "Workbook update: "
            f"{counts['vehicles_added']} vehicle(s) added, "
            f"{counts['vehicles_updated']} existing vehicle(s) updated, "
            f"{counts['bookings_added']} activity booking(s) added."
        )
        if not args.confirm:
            print("DRY-RUN only. No planner data was changed; add --confirm to apply these additions.")
        return
    if args.update_colors:
        initialize_database()
        matching = sum(bool(booking.get("activity_color")) for booking in bookings)
        print(f"Colored workbook activity records available: {matching}")
        if not args.confirm:
            print("DRY-RUN only. No planner data was changed; add --confirm to update imported activities.")
            return
        updated = update_imported_activity_colors(bookings)
        print(f"Updated Excel fill color on {updated} imported activity record(s).")
        return
    if args.update_spans:
        initialize_database()
        spanning = sum(
            booking.get("planned_end_date", "") > booking.get("planned_date", "")
            for booking in bookings
        )
        print(f"Multi-day workbook activity records available: {spanning}")
        if not args.confirm:
            print("DRY-RUN only. No planner data was changed; add --confirm to update imported activities.")
            return
        updated = update_imported_activity_spans(bookings)
        print(f"Updated Excel date range on {updated} imported activity record(s).")
        return
    if args.import_vehicle_tabs:
        entries = read_vehicle_tab_history(args.xlsx)
        print(f"Additional vehicle-tab history records: {len(entries)}")
        for entry in entries:
            print(
                f"  {entry['planned_date']} {entry['period']} | {entry['vehicle_label']} | "
                f"{entry['activity']} | {entry['description']}"
            )
        if not args.confirm:
            print("DRY-RUN only. No planner data was changed; add --confirm to import these records once.")
            return
        initialize_database()
        count = import_vehicle_tab_history(digest, args.xlsx.name, entries)
        print(f"Imported {count} additional vehicle-tab history record(s).")
        return
    print(f"Vehicle records: {len(vehicles)}")
    group_names = list(dict.fromkeys(vehicle["group_name"] for vehicle in vehicles if vehicle["group_name"]))
    print("Vehicle sections:", ", ".join(group_names) if group_names else "None")
    if args.update_groups:
        initialize_database()
        if not args.confirm:
            print("DRY-RUN only. No planner data was changed; add --confirm to apply section names and workbook order.")
            return
        updated = update_vehicle_groups(vehicles)
        print(f"Updated workbook section and display order for {updated} existing vehicle(s).")
        return
    print(f"Planned activity bookings: {len(bookings)}")
    historical = sum(bool(item["planned_date"]) and item["planned_date"] < date.today().isoformat() for item in bookings)
    print(f"Historical/read-only activities: {historical}")
    print(f"Current/future activities awaiting review: {len(bookings) - historical}")
    print("Sample vehicles:", ", ".join(vehicle["label"] for vehicle in vehicles[:10]))
    if not args.confirm:
        print("DRY-RUN only. No planner data was changed; add --confirm to merge this workbook once.")
        print("Existing vehicle details are preserved; blank fields may be filled from the workbook.")
        return

    initialize_database()
    counts = import_workbook_data(digest, args.xlsx.name, vehicles, bookings)
    print(f"Imported {counts[0]} vehicle(s) and {counts[1]} booking(s).")


if __name__ == "__main__":
    main()
