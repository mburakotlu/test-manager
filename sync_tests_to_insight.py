"""Sync each vehicle's local ``tests:`` queue (name + period) onto its Insight FZG object.

Writes two attributes on the "FZG" object type (schema 607, id 4321), each as a
SINGLE pipe-joined string value (e.g. "TA1 | TA2 | TA3") rather than a multi-value
list — Insight silently dedupes identical values within a multi-value attribute,
which would desync "Tests" from "Testplanung (KW)" whenever an activity repeats
across weeks (common in this data). Pipe-joining keeps both attributes' token
count/order aligned and side-steps that dedup entirely:
  - "Tests" (id 25059): "<name1> | <name2> | ..."
  - "Testplanung (KW)" (id 25060): "<period1> | <period2> | ..."

Only vehicles with a known FZG object id (found by VIN via IQL) are updated; vehicles
without one are listed but skipped (creating new Insight objects is a separate,
explicitly-confirmed step).

Usage:
    python sync_tests_to_insight.py [--vehicles-dir DIR] [--vehicles A,B] [--confirm]

Dry-run by default; pass --confirm to actually write to Jira/Insight.
"""

from __future__ import annotations

import argparse

from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient
from jira_agent.vehicle import load_vehicles

TESTS_ATTR_ID = 25059
TESTPLANUNG_ATTR_ID = 25060
FZG_OBJECT_TYPE_ID = 4321
FZG_SCHEMA_ID = 607


def sync_all(vehicles: list, client: JiraClient, confirm: bool = False) -> dict[str, object]:
    """Push each vehicle's ``tests:`` (name+period) onto its Insight FZG object.

    Shared core used by both this script's CLI and the web UI's /refresh route.
    Only vehicles with a non-empty ``tests`` list and a matching FZG object
    (found by VIN) are touched. Returns ``{"updated": [(label, key, count)],
    "skipped": [labels]}``.
    """
    updated: list[tuple[str, str, int]] = []
    skipped: list[str] = []
    for vehicle in vehicles:
        if not vehicle.tests:
            continue
        iql = f'objectType = FZG AND VIN = "{vehicle.vin}"'
        matches = client.list_test_vehicles(iql=iql, object_type_id=FZG_OBJECT_TYPE_ID, object_schema_id=FZG_SCHEMA_ID)
        if not matches:
            skipped.append(vehicle.label)
            continue
        object_id = matches[0]["id"]
        # Single pipe-joined string per attribute (see module docstring for why).
        names_joined = " | ".join(t.name for t in vehicle.tests)
        periods_joined = " | ".join(t.period for t in vehicle.tests)
        client.update_insight_object_attributes(
            object_id,
            {TESTS_ATTR_ID: [names_joined], TESTPLANUNG_ATTR_ID: [periods_joined]},
            confirm=confirm,
        )
        updated.append((vehicle.label, matches[0]["key"], len(vehicle.tests)))
    return {"updated": updated, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vehicles-dir", default="vehicles")
    parser.add_argument("--vehicles", help="Comma-separated list of vehicle labels to limit to.")
    parser.add_argument("--confirm", action="store_true", help="Actually write to Insight (default: dry-run).")
    args = parser.parse_args()

    vehicles = load_vehicles(args.vehicles_dir)
    if args.vehicles:
        wanted = {v.strip() for v in args.vehicles.split(",")}
        vehicles = [v for v in vehicles if v.label in wanted]

    config = load_config()
    client = JiraClient(config)
    try:
        result = sync_all(vehicles, client, confirm=args.confirm)
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
