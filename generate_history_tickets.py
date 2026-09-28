"""Create Jira issues for vehicle history entries that don't have one yet.

Reads vehicles/*.yaml (see jira_agent/vehicle.py) — each vehicle can carry a
``history:`` log of past activities (HW change, PUDIS pull, charging, completed
test, ...). This script creates a plain Jira issue per entry that's missing a
``ticket``, and persists the resulting key back onto that entry.

Usage:
    python generate_history_tickets.py [--vehicles "SE0400,SE0607"]
        [--vehicles-dir vehicles] [--project PHUB] [--issue-type Task] [--confirm]

Dry-run by default: prints the issues that would be created without touching
Jira. Pass --confirm to actually create them.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient
from jira_agent.vehicle import load_vehicles, save_vehicle_history, vehicle_link_fields

VEHICLES_DIR = Path(__file__).parent / "vehicles"


def main() -> None:
    """Parse CLI args, build the ticket plan from vehicle history logs, and preview/execute it."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vehicles", help="Comma-separated vehicle labels to include (default: all)")
    parser.add_argument("--vehicles-dir", default=str(VEHICLES_DIR), help="Directory of local vehicle YAML files (default: vehicles/)")
    parser.add_argument("--project", default="PHUB", help="Jira project key (default: PHUB)")
    parser.add_argument("--issue-type", default="Task", help="Jira issue type name (default: Task)")
    parser.add_argument("--confirm", action="store_true", help="Actually create tickets (default: dry-run only)")
    args = parser.parse_args()

    vehicles = load_vehicles(args.vehicles_dir)
    if args.vehicles:
        wanted = {v.strip() for v in args.vehicles.split(",") if v.strip()}
        vehicles = [v for v in vehicles if v.label in wanted]

    config = load_config()
    client = JiraClient(config)
    try:
        built = []
        skipped_existing = 0
        for vehicle in vehicles:
            for entry in vehicle.history:
                if entry.ticket:
                    skipped_existing += 1
                    continue
                summary = f"{entry.date} - {entry.activity} - {vehicle.label}"
                description = (
                    "Automatically generated from the vehicle's history log.\n"
                    f"Vehicle: {vehicle.label}\n"
                    f"Date: {entry.date}\n"
                    f"Activity: {entry.activity}\n"
                    + (f"Description: {entry.description}\n" if entry.description else "")
                )
                built.append((vehicle, entry, summary, description))

        if not built:
            raise SystemExit(
                f"No matching history entries found (check --vehicles filter). "
                f"Skipped {skipped_existing} entr(y/ies) that already have a ticket."
            )

        print(f"Project: {args.project}  Issue type: {args.issue_type}")
        if skipped_existing:
            print(f"(Skipping {skipped_existing} entr(y/ies) that already have a ticket assigned.)")
        print(f"Tickets to generate ({len(built)}):")
        for vehicle, entry, summary, _description in built:
            print(f"  - {vehicle.label} / {entry.activity} ({entry.date})  ->  \"{summary}\"")

        if not args.confirm:
            print(f"DRY-RUN only — {len(built)} ticket(s) would be created. Re-run with --confirm to execute.")
            return

        touched_vehicles = {}
        for vehicle, entry, summary, description in built:
            result = client.create_issue(
                args.project, args.issue_type, summary, description, confirm=True,
                extra_fields=vehicle_link_fields(vehicle),
            )
            entry.ticket = result["key"]
            touched_vehicles[vehicle.label] = vehicle
            print(f"Created {result['key']}: {summary}")

        print("Persisting ticket keys back to vehicle YAML files...")
        for vehicle in touched_vehicles.values():
            save_vehicle_history(vehicle)
            print(f"  Updated {vehicle.source_path}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
