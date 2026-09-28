"""Apply a checklist template (from templates/*.yaml) to a Jira ticket.

Usage (PowerShell):
    python apply_checklist_template.py <ISSUE-KEY> <template-name> [--confirm] [--set key=value ...]

Examples:
    python apply_checklist_template.py PHUB-2189 analysis_checklist
    python apply_checklist_template.py PHUB-2189 analysis_checklist --confirm
    python apply_checklist_template.py PHUB-2189 test_vehicle_sprint --set vehicle=VAN-FZG --confirm

Dry-run by default — always shows a preview before writing. Pass --confirm to
actually write to Jira.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from jira_agent.checklist import load_template
from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient

TEMPLATES_DIR = Path(__file__).parent / "templates"


def main() -> None:
    """Parse CLI args, render the template, and preview/apply it to the given issue."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("issue_key", help="Jira issue key, e.g. PHUB-2189")
    parser.add_argument("template_name", help="Template file stem under templates/, e.g. analysis_checklist")
    parser.add_argument("--confirm", action="store_true", help="Actually write to Jira (default: dry-run preview only)")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                         help="Placeholder substitution for template items, e.g. --set vehicle=VAN-FZG")
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
    rendered = template.render(**placeholders)

    config = load_config()
    client = JiraClient(config)
    try:
        result = client.set_checklist(args.issue_key, rendered, confirm=args.confirm)
        print(f"Template: {template.name} ({template.source or 'no source noted'})")
        print(f"Issue: {args.issue_key}  Field: {result['field_id']}")
        print("-" * 60)
        print(result["preview"])
        print("-" * 60)
        if result["posted"]:
            print("WRITTEN to Jira.")
        else:
            print("DRY-RUN only — re-run with --confirm to write this to Jira.")
    finally:
        client.close()


if __name__ == "__main__":
    main()
