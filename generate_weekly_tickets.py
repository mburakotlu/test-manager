"""Create one ticket per week with the daily checklists repeated for each day.

Each ticket gets the "Daily Maintenance Enna TestSystem/object" and "Daily
Automation Monitoring" checklists (templates/daily_maintenance_enna.yaml,
templates/daily_automation_monitoring.yaml) repeated once per day (default 5
days) - 2 templates x 5 days = 10 lists total, per the project's Smart
Checklist template groups.

Checklists are written via the browser-automation workaround (see
jira_agent/browser_checklist.py) since the Smart Checklist field can't be set
through the normal issue API on this instance.

One-time setup (if not already done):
    python browser_login.py

Usage:
    python generate_weekly_tickets.py --week "KW38" [--days 5]
        [--project PHUB] [--issue-type Task] [--confirm] [--headed]

Dry-run by default: prints the ticket + full checklist preview without
touching Jira. Pass --confirm to actually create the ticket and write the checklist.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from jira_agent.browser_checklist import write_checklists_via_browser
from jira_agent.checklist import load_template, render_daily_checklists
from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient

TEMPLATES_DIR = Path(__file__).parent / "templates"
DAILY_TEMPLATE_NAMES = ["daily_maintenance_enna", "daily_automation_monitoring"]


def main() -> None:
    """Parse CLI args, build the weekly ticket plan, and preview/execute it."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--week", required=True, help='Week identifier, e.g. "KW38"')
    parser.add_argument("--days", type=int, default=5, help="Number of days to repeat the checklists for (default 5)")
    parser.add_argument("--project", default="PHUB", help="Jira project key (default: PHUB)")
    parser.add_argument("--issue-type", default="Task", help="Jira issue type name (default: Task)")
    parser.add_argument("--confirm", action="store_true", help="Actually create the ticket + write the checklist (default: dry-run only)")
    parser.add_argument("--headed", action="store_true", help="Show the browser window instead of headless")
    args = parser.parse_args()

    templates = [load_template(TEMPLATES_DIR / f"{name}.yaml") for name in DAILY_TEMPLATE_NAMES]

    summary = f"{args.week} - Daily Maintenance & Automation Monitoring"
    description = (
        "Automatically generated weekly maintenance ticket.\n"
        f"Week: {args.week}\n"
        f"Checklists: {', '.join(t.name for t in templates)} x {args.days} day(s)"
    )
    checklist = render_daily_checklists(templates, days=args.days, week=args.week)

    print(f"Week: {args.week}  Project: {args.project}  Issue type: {args.issue_type}")
    print(f"Ticket: \"{summary}\"")
    print("-" * 60)
    print(checklist)
    print("-" * 60)

    if not args.confirm:
        print("DRY-RUN only — 1 ticket would be created. Re-run with --confirm to execute.")
        return

    config = load_config()
    client = JiraClient(config)
    try:
        result = client.create_issue(args.project, args.issue_type, summary, description, confirm=True)
        key = result["key"]
        print(f"Created {key}: {summary}")

        print("Writing checklist via browser automation...")
        write_checklists_via_browser(config.effective_web_base_url, [(key, checklist)], headless=not args.headed)

        print("Verifying checklist via API...")
        actual = client.get_checklist(key)
        print(f"  {key}: {'OK' if actual.strip() else 'EMPTY?!'}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
