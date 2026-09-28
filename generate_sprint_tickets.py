"""Generate one sprint-checklist ticket per test vehicle, from a template.

Test vehicles come from local YAML files under vehicles/*.yaml by default (see
jira_agent/vehicle.py) — this repo is the source of truth rather than the
read-only-for-us Jira Insight/Assets plugin. Pass --from-assets to instead query
Insight live (JiraClient.list_test_vehicles). Checklists are written via the
browser-automation workaround (see jira_agent/browser_checklist.py) since the
Smart Checklist field can't be set through the normal issue API on this instance.

One-time setup (if not already done):
    python browser_login.py

Usage:
    python generate_sprint_tickets.py --sprint "KW37" [--template analysis_checklist]
        [--vehicles "PT1023,PT1123"] [--vehicles-dir vehicles] [--from-assets]
        [--vehicle-iql "objectType = Autos"] [--limit N] [--confirm] [--headed]

Dry-run by default: prints the full plan (tickets to create + checklist preview)
without touching Jira. Pass --confirm to actually create tickets and write checklists.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from jira_agent.browser_checklist import write_checklists_via_browser
from jira_agent.checklist import load_template
from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient
from jira_agent.vehicle import load_vehicles

TEMPLATES_DIR = Path(__file__).parent / "templates"
VEHICLES_DIR = Path(__file__).parent / "vehicles"


def main() -> None:
    """Parse CLI args, build the ticket plan, and preview/execute it."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sprint", required=True, help='Free-text sprint identifier, e.g. "KW37"')
    parser.add_argument("--template", default="analysis_checklist", help="Template file stem under templates/")
    parser.add_argument("--project", default="PHUB", help="Jira project key (default: PHUB)")
    parser.add_argument("--issue-type", default="Task", help="Jira issue type name (default: Task)")
    parser.add_argument("--vehicles", help="Comma-separated vehicle labels to include (default: all found)")
    parser.add_argument("--vehicles-dir", default=str(VEHICLES_DIR), help="Directory of local vehicle YAML files (default: vehicles/)")
    parser.add_argument("--from-assets", action="store_true", help="Query the Insight/Assets API live instead of local vehicles/*.yaml")
    parser.add_argument("--vehicle-iql", default="objectType = Autos", help="Insight IQL filter for vehicles (only with --from-assets)")
    parser.add_argument("--limit", type=int, default=10, help="Safety cap on number of vehicles processed (default 10)")
    parser.add_argument("--confirm", action="store_true", help="Actually create tickets + write checklists (default: dry-run only)")
    parser.add_argument("--headed", action="store_true", help="Show the browser window instead of headless")
    args = parser.parse_args()

    template_path = TEMPLATES_DIR / f"{args.template}.yaml"
    if not template_path.is_file():
        raise SystemExit(f"Template not found: {template_path}")
    template = load_template(template_path)

    config = load_config()
    client = JiraClient(config)
    try:
        if args.from_assets:
            vehicles = client.list_test_vehicles(iql=args.vehicle_iql, max_results=max(args.limit * 2, 50))
        else:
            vehicles = [{"label": v.label, "key": v.key} for v in load_vehicles(args.vehicles_dir)]
        if args.vehicles:
            wanted = {v.strip() for v in args.vehicles.split(",") if v.strip()}
            vehicles = [v for v in vehicles if v["label"] in wanted]
        vehicles = vehicles[: args.limit]

        if not vehicles:
            raise SystemExit("No matching test vehicles found.")

        print(f"Sprint: {args.sprint}  Template: {template.name}  Project: {args.project}  Issue type: {args.issue_type}")
        print(f"Vehicles ({len(vehicles)} of up to --limit={args.limit}):")

        plan = []
        for v in vehicles:
            summary = f"{args.sprint} Test Vehicle Sprint Checklist - {v['label']}"
            key_note = f" ({v['key']})" if v.get("key") else ""
            description = (
                "Automatically generated sprint checklist ticket.\n"
                f"Sprint: {args.sprint}\n"
                f"Test vehicle: {v['label']}{key_note}"
            )
            checklist = template.render(vehicle=v["label"], sprint=args.sprint)
            plan.append((v, summary, description, checklist))
            print(f"  - {v['label']}{key_note}  ->  \"{summary}\"")

        print("-" * 60)
        print("Checklist preview (identical per vehicle unless the template uses {vehicle}/{sprint}):")
        print(plan[0][3])
        print("-" * 60)

        if not args.confirm:
            print(f"DRY-RUN only — {len(plan)} ticket(s) would be created. Re-run with --confirm to execute.")
            return

        created: list[tuple[str, str]] = []
        for v, summary, description, checklist in plan:
            result = client.create_issue(args.project, args.issue_type, summary, description, confirm=True)
            key = result["key"]
            created.append((key, checklist))
            print(f"Created {key}: {summary}")

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
