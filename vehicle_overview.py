"""Show an overview of which sprints (periods) include which vehicles/tests.

Reads vehicles/*.yaml (see jira_agent/vehicle.py) and prints a grouped view,
including the linked Jira ticket key/URL for any test that's already been
generated (see generate_from_vehicles.py, which persists ticket keys back onto
each test entry after creation).

Usage:
    python vehicle_overview.py [--group-by period|vehicle] [--period "KW37"]
        [--vehicles "SE0400,SE0607"] [--vehicles-dir vehicles]
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from jira_agent.config import load_config
from jira_agent.vehicle import load_vehicles

VEHICLES_DIR = Path(__file__).parent / "vehicles"


def main() -> None:
    """Parse CLI args and print the requested overview grouping."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--group-by", choices=["period", "vehicle"], default="period",
                        help="Group the overview by sprint/period (default) or by vehicle")
    parser.add_argument("--period", help='Only show this period, e.g. "KW37" (default: all)')
    parser.add_argument("--vehicles", help="Comma-separated vehicle labels to include (default: all)")
    parser.add_argument("--vehicles-dir", default=str(VEHICLES_DIR), help="Directory of local vehicle YAML files (default: vehicles/)")
    args = parser.parse_args()

    vehicles = load_vehicles(args.vehicles_dir)
    if args.vehicles:
        wanted = {v.strip() for v in args.vehicles.split(",") if v.strip()}
        vehicles = [v for v in vehicles if v.label in wanted]

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
