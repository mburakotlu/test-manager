"""Smoke test — verify Confluence connectivity using CONFLUENCE_API_TOKEN.

Confluence requires its own separate PAT (the Jira Bearer token does not work
against Confluence on this instance). Reuses the Jira config's host/proxy/SSL
settings since both live behind the same corporate network setup.

Usage (PowerShell):
    python test_confluence_connection.py [SPACE_KEY]
"""

from __future__ import annotations

import json
import os
import sys

import httpx

from jira_agent.config import load_config


def main() -> None:
    space_key = sys.argv[1] if len(sys.argv) > 1 else None

    token = os.environ.get("CONFLUENCE_API_TOKEN", "").strip()
    if not token:
        print("ERROR: CONFLUENCE_API_TOKEN is not set (add it to .env).")
        sys.exit(1)

    try:
        config = load_config()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    base_url = f"{config.effective_web_base_url}/confluence"
    url = f"{base_url}/rest/api/space" if not space_key else f"{base_url}/rest/api/space/{space_key}"

    print(f"Connecting to: {url}")
    print(f"Proxy: {config.proxies or 'None (direct)'}")
    print("-" * 60)

    client = httpx.Client(
        verify=config.verify_ssl,
        proxy=config.proxies,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    try:
        resp = client.get(url, params={"limit": 5} if not space_key else None)
        print(f"HTTP {resp.status_code}")
        try:
            print(json.dumps(resp.json(), indent=2, ensure_ascii=False)[:2000])
        except ValueError:
            print(resp.text[:2000])
        resp.raise_for_status()
        print("\nSUCCESS")
    except httpx.HTTPStatusError as e:
        print(f"\nFAILED: HTTP {e.response.status_code}")
        sys.exit(1)
    except Exception as e:
        print(f"\nFAILED: {type(e).__name__}: {e}")
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
