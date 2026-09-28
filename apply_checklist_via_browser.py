"""Apply a checklist template to a Jira ticket via browser automation.

Workaround for the Smart Checklist plugin's write API requiring a real browser
session (see jira_agent/browser_checklist.py and repo memory notes).

One-time setup:
    python browser_login.py

Usage:
    python apply_checklist_via_browser.py <ISSUE-KEY> <template-name> [--confirm] [--set key=value ...] [--headed]

Dry-run by default — always shows a preview before touching the browser. Pass
--confirm to actually open the browser and write to Jira.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from jira_agent.browser_checklist import DEFAULT_STATE_PATH, write_checklist_via_browser
from jira_agent.checklist import load_template, render_test_checklist
from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient

TEMPLATES_DIR = Path(__file__).parent / "templates"


def main() -> None:
    """Parse CLI args, render the template, and preview/apply it via the browser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("issue_key", help="Jira issue key, e.g. PHUB-2189")
    parser.add_argument("template_name", help="Template file stem under templates/, e.g. analysis_checklist")
    parser.add_argument("--confirm", action="store_true", help="Actually write via browser (default: dry-run preview only)")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                         help="Placeholder substitution for template items, e.g. --set vehicle=VAN-FZG")
    parser.add_argument("--week", help='Week/sprint label for cascaded templates (e.g. daily_enna), e.g. "KW39-2026"')
    parser.add_argument("--days", type=int, default=5, help="Number of day-chapters for cascaded templates (default 5)")
    parser.add_argument("--headed", action="store_true", help="Show the browser window instead of running headless")
    args = parser.parse_args()

    template_path = TEMPLATES_DIR / f"{args.template_name}.yaml"
    if not template_path.is_file():
        raise SystemExit(f"Template not found: {template_path}")

    placeholders = {}
    for pair in args.set:
        if "=" not in pair:
            raise SystemExit(f"Invalid --set value {pair!r}, expected KEY=VALUE")
        k, v = pair.split("=", 1)
        placeholders[k] = v

    template = load_template(template_path)
    rendered = render_test_checklist(args.template_name, TEMPLATES_DIR, week=args.week, days=args.days, **placeholders)

    print(f"Template: {template.name} ({template.source or 'no source noted'})")
    print(f"Issue: {args.issue_key}  (via browser automation)")
    print("-" * 60)
    print(rendered)
    print("-" * 60)

    if not args.confirm:
        print("DRY-RUN only — re-run with --confirm to write this via the browser.")
        return

    if not DEFAULT_STATE_PATH.is_file():
        raise SystemExit(
            f"No saved browser session at {DEFAULT_STATE_PATH}. Run 'python browser_login.py' first."
        )

    config = load_config()
    write_checklist_via_browser(config.effective_web_base_url, args.issue_key, rendered, headless=not args.headed)

    # Verify via the read-only API, which works fine with our normal PAT.
    client = JiraClient(config)
    try:
        actual = client.get_checklist(args.issue_key)
        print("WRITTEN. Verified via API read-back:")
        print(actual)
    finally:
        client.close()


if __name__ == "__main__":
    main()
