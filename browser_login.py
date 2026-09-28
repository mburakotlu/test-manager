"""One-time interactive SSO login to enable headless checklist automation.

Run this once (and again whenever the saved session eventually expires):
    python browser_login.py

Opens a real browser window. Log in via SSO yourself; this script detects
when you reach a Jira page and saves the session for reuse by
apply_checklist_via_browser.py — no further manual logins needed until the
underlying SSO session expires.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from jira_agent.browser_checklist import save_login_state
from jira_agent.config import load_config


def main() -> None:
    """Load config for the base URL, then run the interactive login flow."""
    config = load_config()
    save_login_state(config.effective_web_base_url)


if __name__ == "__main__":
    main()
