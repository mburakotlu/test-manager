"""Browser-driven Smart Checklist writer.

Workaround for the Smart Checklist plugin's write API only accepting a real
browser session cookie (not our Bearer PAT). Requires the ``playwright``
package (``pip install playwright`` then ``playwright install chromium``).

Flow:
    1. One-time: :func:`save_login_state` opens a real browser for interactive
       SSO login and saves the resulting session cookies to disk.
    2. Every run after that: :func:`write_checklist_via_browser` reuses (and
       refreshes) that saved session headlessly — no further manual login
       needed until the underlying SSO session itself expires.

The saved session state file contains live authentication cookies — treat it
exactly like a secret. It must never be committed (see .gitignore's ``.auth/``).
"""

from __future__ import annotations

from pathlib import Path
from app_paths import APP_DIR

DEFAULT_STATE_PATH = APP_DIR / ".auth" / "jira_browser_state.json"


def save_login_state(base_url: str, state_path: Path = DEFAULT_STATE_PATH, timeout_ms: int = 300_000) -> None:
    """Open a real (headed) browser for one-time interactive SSO login.

    Blocks until the user reaches a Jira page (login complete) or ``timeout_ms``
    elapses, then saves the session cookies to ``state_path``.
    """
    from playwright.sync_api import sync_playwright  # local import: optional heavy dep

    state_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{base_url}/jira/secure/MyJiraHome.jspa")
        print("Please log in via SSO in the opened browser window.")
        print("Waiting for you to reach a Jira page (dashboard/ticket)...")
        page.wait_for_url("**/jira/**", timeout=timeout_ms)
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
        context.storage_state(path=str(state_path))
        browser.close()
    print(f"Session saved to {state_path}. Treat this file as a secret (already gitignored).")


def _type_checklist_lines(page, checklist_text: str) -> None:
    """Type ``checklist_text`` into the currently-focused Ace editor, one line at a time.

    The Ace editor auto-continues the previous line's ``+``/``-`` bullet prefix on
    Enter, which would corrupt lines that need a different prefix (e.g. a ``# header``
    line right after a bullet item). Selecting and clearing each new line before
    typing its real content sidesteps that regardless of what Ace auto-inserted.
    """
    lines = [ln for ln in checklist_text.strip().split("\n") if ln.strip()]
    if not lines:
        raise ValueError("checklist_text must be non-empty")
    for i, line in enumerate(lines):
        if i > 0:
            page.keyboard.press("Enter")
            page.keyboard.press("Home")
            page.keyboard.press("Shift+End")
            page.keyboard.press("Delete")
        page.keyboard.type(line)


def write_checklist_via_browser(
    base_url: str,
    issue_key: str,
    checklist_text: str,
    state_path: Path = DEFAULT_STATE_PATH,
    headless: bool = True,
) -> None:
    """Write ``checklist_text`` (Smart Checklist textarea format) to ``issue_key`` via the real UI.

    Requires a saved session from :func:`save_login_state`. Thin wrapper around
    :func:`write_checklists_via_browser` for the single-issue case.
    """
    write_checklists_via_browser(base_url, [(issue_key, checklist_text)], state_path, headless)


def write_checklists_via_browser(
    base_url: str,
    items: list[tuple[str, str]],
    state_path: Path = DEFAULT_STATE_PATH,
    headless: bool = True,
) -> None:
    """Write checklists for multiple issues in a single browser session.

    ``items`` is a list of ``(issue_key, checklist_text)`` pairs. Reusing one
    browser/session across all items is much faster than launching a new
    browser per issue. Refreshes the saved session cookies after the run.
    """
    from playwright.sync_api import sync_playwright  # local import: optional heavy dep

    if not state_path.is_file():
        raise RuntimeError(
            f"No saved browser session at {state_path}. Run save_login_state() first "
            "(one-time interactive SSO login)."
        )
    if not items:
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=str(state_path))
        page = context.new_page()

        for issue_key, checklist_text in items:
            page.goto(f"{base_url}/jira/browse/{issue_key}")
            page.wait_for_load_state("networkidle")

            # The saved session expires after about a week — Jira then silently
            # redirects here instead of showing the issue, which would otherwise
            # surface as an opaque 10s Playwright timeout on the "edit" button below.
            if "login" in page.url.lower() or "saml" in page.url.lower():
                browser.close()
                raise RuntimeError(
                    f"Browser session appears expired — {issue_key} redirected to "
                    f"{page.url!r} instead of showing the issue. Run "
                    "`python browser_login.py` to refresh the saved session "
                    f"({state_path}), then retry."
                )

            frame = page.frame_locator("iframe").first
            try:
                frame.locator('button:has-text("edit")').first.click(timeout=10_000)
            except Exception as exc:
                browser.close()
                raise RuntimeError(
                    f"Could not find the checklist 'edit' button on {issue_key} "
                    f"(page url: {page.url!r}). The saved browser session may be "
                    "expired or the issue may not have a Checklists field — run "
                    "`python browser_login.py` to refresh the session, then retry."
                ) from exc
            page.wait_for_timeout(800)

            editor_frame = page.frame_locator("#rw-checklist-editor-iframe")
            editor_frame.locator(".ace_content").click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            page.wait_for_timeout(300)
            _type_checklist_lines(page, checklist_text)
            page.wait_for_timeout(500)
            editor_frame.get_by_role("button", name="Save").click(timeout=5_000)
            page.wait_for_timeout(1000)

        context.storage_state(path=str(state_path))
        browser.close()
