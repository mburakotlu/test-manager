"""Browser-driven Confluence reader.

Workaround for Confluence's REST API being gated by a reverse-proxy-level
Crowd/SAML SSO redirect that ignores the Authorization header entirely (see
CONFLUENCE_API_TOKEN notes) — a real browser session cookie is required
instead. Same pattern as jira_agent/browser_checklist.py, but Confluence has
its own separate SSO session (the saved Jira session does NOT carry over).

Flow:
    1. One-time: :func:`save_login_state` opens a real browser for interactive
       SSO login against Confluence and saves the resulting session cookies.
    2. Every run after that: :func:`fetch_content` reuses that saved session
       headlessly via Playwright's APIRequestContext (shares the browser
       context's cookie jar, no page navigation needed for the actual fetch).

The saved session state file contains live authentication cookies — treat it
exactly like a secret. It must never be committed (see .gitignore's ``.auth/``).
"""

from __future__ import annotations

from pathlib import Path
from app_paths import APP_DIR

DEFAULT_STATE_PATH = APP_DIR / ".auth" / "confluence_browser_state.json"


def save_login_state(base_url: str, state_path: Path = DEFAULT_STATE_PATH, timeout_ms: int = 300_000) -> None:
    """Open a real (headed) browser for one-time interactive SSO login to Confluence.

    Blocks until the user reaches a Confluence page (login complete) or
    ``timeout_ms`` elapses, then saves the session cookies to ``state_path``.
    """
    from playwright.sync_api import sync_playwright  # local import: optional heavy dep

    state_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.goto(f"{base_url}/confluence/")
        print("Please log in via SSO in the opened browser window.")
        print("Waiting for you to reach a Confluence page (dashboard/space)...")
        page.wait_for_url("**/confluence/**", timeout=timeout_ms)
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
        context.storage_state(path=str(state_path))
        browser.close()
    print(f"Session saved to {state_path}. Treat this file as a secret (already gitignored).")


def fetch_content(
    base_url: str,
    content_id: str,
    expand: str = "body.storage,version",
    state_path: Path = DEFAULT_STATE_PATH,
    headless: bool = True,
) -> dict:
    """Fetch a Confluence content object (page) by ID via the REST API.

    Requires a saved session from :func:`save_login_state`. Uses Playwright's
    ``APIRequestContext`` so the request reuses the browser context's session
    cookies without needing to render/navigate an actual page.
    """
    from playwright.sync_api import sync_playwright  # local import: optional heavy dep

    if not state_path.is_file():
        raise RuntimeError(
            f"No saved Confluence browser session at {state_path}. Run "
            "confluence_login.py first (one-time interactive SSO login)."
        )

    url = f"{base_url}/confluence/rest/api/content/{content_id}"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=str(state_path), ignore_https_errors=True)
        resp = context.request.get(url, params={"expand": expand})
        status = resp.status
        body_text = resp.text()
        if status != 200:
            browser.close()
            raise RuntimeError(f"Confluence fetch failed: HTTP {status} — {body_text[:300]}")
        data = resp.json()
        context.storage_state(path=str(state_path))
        browser.close()
    return data
