"""Smoke test — verify Jira connectivity by fetching a single issue.

Usage (PowerShell):
    $env:JIRA_BASE_URL   = "https://api.skyway.porsche.com"
    $env:JIRA_EMAIL      = "you@example.com"
    $env:JIRA_API_TOKEN  = "<your-personal-access-token>"   # never hardcode this
    $env:JIRA_AUTH_MODE  = "bearer"
    $env:JIRA_IS_TEC_NET = "false"
    $env:JIRA_VERIFY_SSL = "false"
    python test_connection.py PHUB-1
"""

from __future__ import annotations

import json
import sys

from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient


def main() -> None:
    """Fetch a Jira issue given as the first CLI argument to verify connectivity."""
    if len(sys.argv) < 2:
        print("Usage: python test_connection.py <ISSUE-KEY>")
        sys.exit(1)
    issue_key = sys.argv[1]

    try:
        config = load_config()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print(f"Connecting to: {config.base_url}")
    print(f"Auth mode: {'Bearer token' if config.use_bearer_token else 'Basic auth'}")
    print(f"Proxy: {config.proxies or 'None (direct)'}")
    print(f"Email: {config.email}")
    print("-" * 60)

    client = JiraClient(config)
    try:
        issue = client.get_issue(issue_key)
        print(f"\nSUCCESS — Fetched issue {issue.key}")
        print(json.dumps(issue.to_dict(), indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"\nFAILED: {type(e).__name__}: {e}")
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
