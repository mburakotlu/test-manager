"""Retry writing checklists that failed earlier (e.g. expired browser session).

When the sprint-ticket web form creates issues but the subsequent checklist
write fails (see jira_agent/browser_checklist.py), the pending
``(issue_key, checklist_text)`` pairs are saved to ``pending_checklists.json``
instead of being lost. Run this script after fixing the problem (usually
``python browser_login.py`` to refresh the saved SSO session) to write them.

Usage:
    python retry_pending_checklists.py
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from jira_agent.browser_checklist import (
    clear_pending_checklists, load_pending_checklists, write_checklists_via_browser,
)
from jira_agent.config import load_config


def main() -> None:
    """Load pending checklist items and write them, clearing the file on success."""
    pending = load_pending_checklists()
    if not pending:
        print("No pending checklists.")
        return

    config = load_config()
    print(f"Writing {len(pending)} pending checklist(s): {', '.join(key for key, _ in pending)}")
    write_checklists_via_browser(config.effective_web_base_url, pending)
    clear_pending_checklists()
    print("Done — pending_checklists.json cleared.")


if __name__ == "__main__":
    main()
