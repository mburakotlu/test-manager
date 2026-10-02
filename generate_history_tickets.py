"""Create Jira issues for vehicle history entries that don't have one yet.

Reads the activity history on FZG Assets objects. This script creates a plain
Jira issue per entry missing a ticket and writes the key back to Assets.

Usage:
    python generate_history_tickets.py [--vehicles "SE0400,SE0607"]
        [--project PHUB] [--issue-type Task] [--confirm]

Dry-run by default: prints the issues that would be created without touching
Jira. Pass --confirm to actually create them.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from dotenv import load_dotenv

from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient
from jira_agent.vehicle import vehicle_link_fields
from jira_agent.asset_vehicles import load_asset_vehicles, save_asset_history, history_attribute_id



def main() -> None:
    """Parse CLI args, build the ticket plan from vehicle history logs, and preview/execute it."""
    load_dotenv(Path(__file__).parent / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vehicles", help="Comma-separated vehicle labels to include (default: all)")
    parser.add_argument("--project", default="PHUB", help="Jira project key (default: PHUB)")
    parser.add_argument("--issue-type", default="Task", help="Jira issue type name (default: Task)")
    parser.add_argument("--confirm", action="store_true", help="Actually create tickets (default: dry-run only)")
    args = parser.parse_args()

    config = load_config()
    client = JiraClient(config)
    try:
        wanted = {v.strip() for v in args.vehicles.split(",") if v.strip()} if args.vehicles else None
        vehicles = load_asset_vehicles(client, wanted)
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

        history_id = history_attribute_id(client, create=True)
        for vehicle, entry, summary, description in built:
            result = client.create_issue(
                args.project, args.issue_type, summary, description, confirm=True,
                extra_fields=vehicle_link_fields(vehicle),
            )
            entry.ticket = result["key"]
            save_asset_history(client, vehicle, history_id)
            print(f"Created {result['key']}: {summary}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
