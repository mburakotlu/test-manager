"""Generate one ticket per assigned test, across all (or selected) vehicles.

Reads vehicles/*.yaml (see jira_agent/vehicle.py) — each vehicle can carry a queue
of assigned tests, each with its own planned period (sprint) and checklist
template. This is the consolidated successor to generate_from_plan.py: instead of
separate plan files, tests live directly on the vehicle that owns them.

Checklists are written via the browser-automation workaround (see
jira_agent/browser_checklist.py) since the Smart Checklist field can't be set
through the normal issue API on this instance.

One-time setup (if not already done):
    python browser_login.py

Usage:
    python generate_from_vehicles.py [--vehicles "SE0400,SE0607"] [--period "KW37"]
        [--vehicles-dir vehicles] [--project PHUB] [--issue-type Task]
        [--confirm] [--headed]

Dry-run by default: prints the full plan (tickets to create + checklist previews)
without touching Jira. Pass --confirm to actually create tickets and write checklists.
--period filters to only tests planned for that sprint; omit to include all periods.

Each created ticket is also grouped into a real Jira Agile "Sprint" field, using a
future-state sprint (named after the effective sprint label) that's reused if one
with that name already exists on --board-id, or created otherwise. Pass
--no-sprint-assignment to skip this and only create plain tickets.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from jira_agent.browser_checklist import write_checklists_via_browser
from jira_agent.checklist import render_test_checklist
from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient
from jira_agent.vehicle import load_vehicles, save_vehicle_tests, vehicle_link_fields

TEMPLATES_DIR = Path(__file__).parent / "templates"
VEHICLES_DIR = Path(__file__).parent / "vehicles"


def main() -> None:
    """Parse CLI args, build the ticket plan from vehicle test queues, and preview/execute it."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vehicles", help="Comma-separated vehicle labels to include (default: all)")
    parser.add_argument("--period", help='Only include tests planned for this period, e.g. "KW37" (default: all periods)')
    parser.add_argument("--periods", help='Comma-separated list of periods to include, e.g. "KW38-2026,KW39-2026" (default: all periods)')
    parser.add_argument("--sprint", help="Free-text sprint label for the created tickets (default: derived from the matched periods)")
    parser.add_argument("--vehicles-dir", default=str(VEHICLES_DIR), help="Directory of local vehicle YAML files (default: vehicles/)")
    parser.add_argument("--project", default="PHUB", help="Jira project key (default: PHUB)")
    parser.add_argument("--issue-type", default="Task", help="Jira issue type name (default: Task)")
    parser.add_argument("--confirm", action="store_true", help="Actually create tickets + write checklists (default: dry-run only)")
    parser.add_argument("--headed", action="store_true", help="Show the browser window instead of headless")
    parser.add_argument("--no-sprint-assignment", action="store_true", help="Skip grouping created tickets into a real Agile sprint (only the plain ticket is created).")
    parser.add_argument("--board-id", type=int, default=114663, help="Agile board id to create/reuse the sprint on (default: 114663, DLCM_Integration/Automation)")
    args = parser.parse_args()

    vehicles = load_vehicles(args.vehicles_dir)
    if args.vehicles:
        wanted = {v.strip() for v in args.vehicles.split(",") if v.strip()}
        vehicles = [v for v in vehicles if v.label in wanted]

    wanted_periods = {p.strip() for p in args.periods.split(",") if p.strip()} if args.periods else None

    config = load_config()
    client = JiraClient(config)
    try:
        built = []
        skipped_existing = 0
        for vehicle in vehicles:
            for test in vehicle.tests:
                if args.period and test.period != args.period:
                    continue
                if wanted_periods and test.period not in wanted_periods:
                    continue
                if test.ticket:
                    skipped_existing += 1
                    continue
                template_path = TEMPLATES_DIR / f"{test.template}.yaml"
                if not template_path.is_file():
                    raise SystemExit(f"Template not found: {template_path} (vehicle: {vehicle.label}, test: {test.name})")

                sprint_label = args.sprint or test.period
                summary = f"{sprint_label} - {test.name} - {vehicle.label}"
                description = (
                    "Automatically generated from the vehicle's assigned test queue.\n"
                    f"Vehicle: {vehicle.label}\n"
                    f"Period: {test.period}\n"
                    f"Test: {test.name}\n"
                    + (f"Description: {test.description}\n" if test.description else "")
                )
                checklist = render_test_checklist(test.template, TEMPLATES_DIR, week=test.period, vehicle=vehicle.label, sprint=sprint_label, activity=test.name)
                built.append((vehicle, test, test.template, summary, description, checklist, sprint_label))

        if not built:
            raise SystemExit(
                f"No matching assigned tests found (check --vehicles/--period filters). "
                f"Skipped {skipped_existing} test(s) that already have a ticket."
            )

        print(f"Project: {args.project}  Issue type: {args.issue_type}")
        if skipped_existing:
            print(f"(Skipping {skipped_existing} test(s) that already have a ticket assigned.)")
        print(f"Tickets to generate ({len(built)}):")
        for vehicle, test, template_name, summary, _description, _checklist, _sprint_label in built:
            print(f"  - {vehicle.label} / {test.name} ({test.period})  ->  \"{summary}\"  (template: {template_name})")

        print("-" * 60)
        for vehicle, test, template_name, _summary, _description, checklist, _sprint_label in built:
            print(f"Checklist for {vehicle.label} / \"{test.name}\" ({template_name}):")
            print(checklist)
            print("-" * 60)

        if not args.confirm:
            print(f"DRY-RUN only — {len(built)} ticket(s) would be created. Re-run with --confirm to execute.")
            return

        created: list[tuple[str, str]] = []
        created_labels: dict[str, str] = {}
        touched_vehicles = {}
        for vehicle, test, _template, summary, description, checklist, sprint_label in built:
            result = client.create_issue(
                args.project, args.issue_type, summary, description, confirm=True,
                extra_fields=vehicle_link_fields(vehicle),
            )
            key = result["key"]
            created.append((key, checklist))
            created_labels[key] = sprint_label
            test.ticket = key
            touched_vehicles[vehicle.label] = vehicle
            print(f"Created {key}: {summary}")

        # Persist ticket keys BEFORE the checklist step, which can throw (e.g. expired
        # browser session) — otherwise real tickets get created but never recorded locally.
        print("Persisting ticket keys back to vehicle YAML files...")
        for vehicle in touched_vehicles.values():
            save_vehicle_tests(vehicle)
            print(f"  Updated {vehicle.source_path}")

        if not args.no_sprint_assignment:
            print(f"Assigning tickets to their Agile sprint (board {args.board_id})...")
            keys_by_label: dict[str, list[str]] = {}
            for key, label in created_labels.items():
                keys_by_label.setdefault(label, []).append(key)
            for label, keys in keys_by_label.items():
                sprint = client.get_or_create_sprint(args.board_id, label, confirm=True)
                action = "Reusing" if sprint["reused"] else "Created"
                print(f"  {action} sprint {sprint['id']} ({sprint['name']!r}, {sprint['state']}) for {len(keys)} ticket(s)")
                client.add_issues_to_sprint(sprint["id"], keys, confirm=True)

        print("Writing checklists via browser automation...")
        write_checklists_via_browser(config.effective_web_base_url, created, headless=not args.headed)

        print("Verifying checklists via API...")
        for key, _ in created:
            actual = client.get_checklist(key)
            status = "OK" if actual.strip() else "EMPTY?!"
            print(f"  {key}: {status}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
