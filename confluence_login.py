"""One-time interactive SSO login to enable headless Confluence page reads.

Run this once (and again whenever the saved session eventually expires):
    python confluence_login.py

Opens a real browser window. Log in via SSO yourself; this script detects
when you reach a Confluence page and saves the session for reuse by
read_confluence_page.py — no further manual logins needed until the
underlying SSO session expires. Confluence has its own separate SSO session
from Jira, so this must be run even if you've already logged in for Jira.
"""

from __future__ import annotations

from jira_agent.browser_confluence import save_login_state
from jira_agent.config import load_config


def main() -> None:
    """Load config for the base URL, then run the interactive login flow."""
    config = load_config()
    save_login_state(config.effective_web_base_url)


if __name__ == "__main__":
    main()
