"""Fetch a single Jira Insight/Assets object by key and print its attributes.

Usage:
    python read_insight_asset.py <OBJECT_KEY>
    python read_insight_asset.py PHUB-444909
"""

from __future__ import annotations

import json
import sys

from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient


def main() -> None:
    """Fetch the Insight object given as the first CLI argument and print its attributes."""
    if len(sys.argv) < 2:
        print("Usage: python read_insight_asset.py <OBJECT_KEY>")
        sys.exit(1)
    object_key = sys.argv[1]

    try:
        config = load_config()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    client = JiraClient(config)
    try:
        obj = client.get_insight_object(object_key)
        print(json.dumps(obj, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
