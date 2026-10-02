"""Show an overview of which sprints (periods) include which vehicles/tests.

Reads FZG Assets and prints a grouped view, including linked Jira ticket keys.

Usage:
    python vehicle_overview.py [--group-by period|vehicle] [--period "KW37"]
        [--vehicles "SE0400,SE0607"]
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from dotenv import load_dotenv

from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient
from jira_agent.asset_vehicles import load_asset_vehicles



def main() -> None:
    """Parse CLI args and print the requested overview grouping."""
    load_dotenv(Path(__file__).parent / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--group-by", choices=["period", "vehicle"], default="period",
                        help="Group the overview by sprint/period (default) or by vehicle")
    parser.add_argument("--period", help='Only show this period, e.g. "KW37" (default: all)')
    parser.add_argument("--vehicles", help="Comma-separated vehicle labels to include (default: all)")
    args = parser.parse_args()

    client = JiraClient(load_config())
    try:
        wanted = {v.strip() for v in args.vehicles.split(",") if v.strip()} if args.vehicles else None
        vehicles = load_asset_vehicles(client, wanted)
    finally:
        client.close()

    try:
        base_url = load_config().effective_web_base_url
    except RuntimeError:
        base_url = None  # env not configured; still show the overview, just without links

    def ticket_note(ticket: str) -> str:
        if not ticket:
            return "(not yet generated)"
        if base_url:
            return f"{ticket}  {base_url}/jira/browse/{ticket}"
        return ticket

    if args.group_by == "period":
        by_period: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        for vehicle in vehicles:
            for test in vehicle.tests:
                if args.period and test.period != args.period:
                    continue
                by_period[test.period].append((vehicle.label, test.name, ticket_note(test.ticket)))

        if not by_period:
            print("No matching tests found.")
            return
        for period in sorted(by_period):
            entries = by_period[period]
            print(f"\n{period}  ({len(entries)} vehicle(s)):")
            for label, name, note in entries:
                print(f"  - {label:10s} {name:50s} {note}")
    else:
        for vehicle in vehicles:
            tests = [t for t in vehicle.tests if not args.period or t.period == args.period]
            if not tests:
                continue
            print(f"\n{vehicle.label}  ({len(tests)} test(s)):")
            for test in tests:
                print(f"  - {test.period:8s} {test.name:50s} {ticket_note(test.ticket)}")


if __name__ == "__main__":
    main()
