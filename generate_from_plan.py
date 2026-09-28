"""Generate one ticket per planned test activity, from a local vehicle test plan.

The plan file mirrors the real Jira Insight "Planung" object type (see
jira_agent/test_plan.py) — a local stand-in since our account only has read
access to that Insight schema. Each activity becomes one ticket with its own
checklist template, written via the browser-automation workaround.

One-time setup (if not already done):
    python browser_login.py

Usage:
    python generate_from_plan.py plans/PT1023_example.yaml [--project PHUB]
        [--issue-type Task] [--confirm] [--headed]

Dry-run by default: prints the full plan (tickets to create + checklist previews)
without touching Jira. Pass --confirm to actually create tickets and write checklists.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from jira_agent.browser_checklist import write_checklists_via_browser
from jira_agent.checklist import load_template
from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient
from jira_agent.test_plan import load_plan

TEMPLATES_DIR = Path(__file__).parent / "templates"


def main() -> None:
    """Parse CLI args, build the ticket plan from the plan file, and preview/execute it."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("plan_file", help="Path to a vehicle test plan YAML file, e.g. plans/PT1023_example.yaml")
    parser.add_argument("--project", default="PHUB", help="Jira project key (default: PHUB)")
    parser.add_argument("--issue-type", default="Task", help="Jira issue type name (default: Task)")
    parser.add_argument("--confirm", action="store_true", help="Actually create tickets + write checklists (default: dry-run only)")
    parser.add_argument("--headed", action="store_true", help="Show the browser window instead of headless")
    args = parser.parse_args()

    plan = load_plan(args.plan_file)
    if not plan.activities:
        raise SystemExit(f"Plan {args.plan_file} has no activities.")

    config = load_config()
    client = JiraClient(config)
    try:
        print(f"Vehicle: {plan.vehicle}  Sprint: {plan.sprint}  Project: {args.project}  Issue type: {args.issue_type}")
        print(f"Activities ({len(plan.activities)}):")

        built = []
        for activity in plan.activities:
            template_path = TEMPLATES_DIR / f"{activity.template}.yaml"
            if not template_path.is_file():
                raise SystemExit(f"Template not found: {template_path} (activity: {activity.name})")
            template = load_template(template_path)

            summary = f"{plan.sprint} - {activity.name} - {plan.vehicle}"
            description = (
                "Automatically generated from a vehicle test plan.\n"
                f"Vehicle: {plan.vehicle}\n"
                f"Sprint: {plan.sprint}\n"
                f"Activity: {activity.name}\n"
                + (f"Description: {activity.description}\n" if activity.description else "")
            )
            checklist = template.render(vehicle=plan.vehicle, sprint=plan.sprint, activity=activity.name)
            built.append((activity, template, summary, description, checklist))

            print(f"  - {activity.name}  ->  \"{summary}\"  (template: {template.name})")

        print("-" * 60)
        for activity, template, _summary, _description, checklist in built:
            print(f"Checklist for \"{activity.name}\" ({template.name}):")
            print(checklist)
            print("-" * 60)

        if not args.confirm:
            print(f"DRY-RUN only — {len(built)} ticket(s) would be created. Re-run with --confirm to execute.")
            return

        created: list[tuple[str, str]] = []
        for activity, _template, summary, description, checklist in built:
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
