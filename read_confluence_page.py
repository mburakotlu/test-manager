"""Read a Confluence page's content via the saved browser session.

Usage:
    python read_confluence_page.py <PAGE_ID_OR_URL>

Requires a saved session — run confluence_login.py once first.
"""

from __future__ import annotations

import json
import re
import sys

from jira_agent.browser_confluence import fetch_content
from jira_agent.config import load_config


def _extract_content_id(page_id_or_url: str) -> str:
    """Pull the numeric content ID out of a full Confluence page URL, if given."""
    match = re.search(r"/pages/(\d+)", page_id_or_url)
    if match:
        return match.group(1)
    if page_id_or_url.isdigit():
        return page_id_or_url
    raise ValueError(f"Could not find a page ID in: {page_id_or_url!r}")


def _storage_to_text(storage_html: str) -> str:
    """Strip Confluence storage-format XHTML tags down to readable plain text."""
    text = re.sub(r"<[^>]+>", "\n", storage_html)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python read_confluence_page.py <PAGE_ID_OR_URL>")
        sys.exit(1)

    content_id = _extract_content_id(sys.argv[1])
    config = load_config()

    try:
        data = fetch_content(config.effective_web_base_url, content_id)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    title = data.get("title", "(no title)")
    version = data.get("version", {}).get("number", "?")
    storage_html = data.get("body", {}).get("storage", {}).get("value", "")

    print(f"Title: {title} (v{version})")
    print("-" * 60)
    print(_storage_to_text(storage_html))

    if "--raw" in sys.argv:
        print("-" * 60)
        print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
