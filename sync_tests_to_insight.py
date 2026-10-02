"""Refresh FZG Assets test queues and activity status from the Excel calendar.

Writes aligned attributes on the "FZG" object type (schema 607, id 4321), each as a
SINGLE pipe-joined string value (e.g. "TA1 | TA2 | TA3") rather than a multi-value
list — Insight silently dedupes identical values within a multi-value attribute,
which would desync "Tests" from "Testplanung (KW)" whenever an activity repeats
across weeks (common in this data). Pipe-joining keeps both attributes' token
count/order aligned and side-steps that dedup entirely:
  - "Tests" (id 25059): "<name1> | <name2> | ..."
  - "Testplanung (KW)" (id 25060): "<period1> | <period2> | ..."

"Status" is Active when a test is planned within the last two weeks, otherwise
Passive. Ticket links do not affect this status.

Usage:
    python sync_tests_to_insight.py [--xlsx PATH] [--vehicles A,B] [--confirm]

Dry-run by default; pass --confirm to actually write to Jira/Insight.
"""

from __future__ import annotations

import argparse

from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient
from jira_agent.asset_vehicles import (
    asset_attribute_id, joined, load_asset_vehicles,
    merge_existing_tickets, ticket_attribute_id,
)
from jira_agent.vehicle import is_vehicle_inactive

TESTS_ATTR_ID = 25059
TESTPLANUNG_ATTR_ID = 25060
FZG_OBJECT_TYPE_ID = 4321
FZG_SCHEMA_ID = 607


def sync_all(vehicles: list, client: JiraClient, confirm: bool = False,
             include_unmatched: bool = True) -> dict[str, object]:
    """Push each vehicle's ``tests:`` (name+period) onto its Insight FZG object.

    Shared core used by both this script's CLI and the web UI's /refresh route.
    Existing FZG objects are updated; missing calendar vehicles are created on
    confirmation. Ticketed tests absent from the calendar remain linked. All
    existing FZG assets receive a status update, even without calendar entries.
    """
    updated: list[tuple[str, str, int]] = []
    skipped: list[str] = []
    existing = {v.key: v for v in load_asset_vehicles(client)}
    tickets_id = ticket_attribute_id(client, create=confirm)
    status_id = asset_attribute_id(client, "Status", create=confirm)
    processed_keys = set()
    for vehicle in vehicles:
        if vehicle.key:
            obj = client.get_insight_object(vehicle.key)
            object_id, object_key = obj["id"], obj["key"]
            processed_keys.add(object_key)
        else:
            if not vehicle.tests:
                continue
            object_id, object_key = None, "(new FZG Asset)"
        # Single pipe-joined string per attribute (see module docstring for why).
        old = existing.get(object_key)
        if old:
            merge_existing_tickets(old, vehicle)
        if not vehicle.tests:
            if status_id is not None:
                client.update_insight_object_attributes(
                    object_id, {status_id: ["Passive"]}, confirm=confirm,
                )
                updated.append((vehicle.label, object_key, 0))
            continue
        names_joined = joined([t.name for t in vehicle.tests])
        periods_joined = joined([t.period for t in vehicle.tests])
        attrs = {TESTS_ATTR_ID: [names_joined], TESTPLANUNG_ATTR_ID: [periods_joined]}
        if tickets_id is not None:
            attrs[tickets_id] = [joined([t.ticket or "x" for t in vehicle.tests])]
        if status_id is not None:
            attrs[status_id] = ["Passive" if is_vehicle_inactive(vehicle) else "Active"]
        if object_id is None:
            if confirm:
                attrs[25045] = [vehicle.label]
                for value, attr_id in ((vehicle.vin, 25048), (vehicle.scope, 25049),
                                       (vehicle.market, 25050), (vehicle.series, 25052),
                                       (vehicle.zielstand, 25053), (vehicle.cluster, 25055)):
                    if value:
                        attrs[attr_id] = [value]
                created = client.create_insight_object(FZG_OBJECT_TYPE_ID, attrs, confirm=True)
                vehicle.key = object_key = created["objectKey"]
        else:
            client.update_insight_object_attributes(object_id, attrs, confirm=confirm)
        updated.append((vehicle.label, object_key, len(vehicle.tests)))
    if status_id is not None and include_unmatched:
        for key, vehicle in existing.items():
            if key not in processed_keys:
                object_id = client.get_insight_object(key)["id"]
                status = "Passive" if is_vehicle_inactive(vehicle) else "Active"
                client.update_insight_object_attributes(object_id, {status_id: [status]}, confirm=confirm)
                updated.append((vehicle.label, key, len(vehicle.tests)))
    return {"updated": updated, "skipped": skipped}


def main() -> None:
    from pathlib import Path
    from dotenv import load_dotenv
    from import_jahreskalender import import_calendar

    load_dotenv(Path(__file__).parent / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xlsx", default="DLCM_FZG_Planung.xlsx")
    parser.add_argument("--vehicles", help="Comma-separated list of vehicle labels to limit to.")
    parser.add_argument("--confirm", action="store_true", help="Actually write to Insight (default: dry-run).")
    args = parser.parse_args()

    config = load_config()
    client = JiraClient(config)
    try:
        vehicles = load_asset_vehicles(client)
        result_calendar = import_calendar(Path(args.xlsx), vehicles)
        vehicles = result_calendar["vehicles"]
        if args.vehicles:
            wanted = {v.strip() for v in args.vehicles.split(",")}
            vehicles = [v for v in vehicles if v.label in wanted]
        result = sync_all(vehicles, client, confirm=args.confirm,
                  include_unmatched=not bool(args.vehicles))
        for label, key, count in result["updated"]:
            print(f"{label} ({key}): {count} test(s) {'updated' if args.confirm else 'would update'}")

        print(f"\n{'Updated' if args.confirm else 'Would update'} {len(result['updated'])} vehicle(s).")
        if result["skipped"]:
            print(f"Skipped (no FZG Insight object found): {result['skipped']}")
        if not args.confirm:
            print("\nDry-run only. Re-run with --confirm to write changes to Insight.")
    finally:
        client.close()


if __name__ == "__main__":
    main()
