"""Audit every Insight "FZG" object (vehicle) for the standard attribute set.

Standard attributes every vehicle's FZG object is expected to carry: Key,
Bezeichnung, Created, Updated, VIN, Scope, Markt, Aktivität, Baureihe,
ZielStand, Testart, Tests, Testplanung (KW). Read-only — reports which
attributes are missing/blank per vehicle, doesn't write anything.

Usage:
    python audit_fzg_attributes.py [--max-results 200]
"""

from __future__ import annotations

import argparse

from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient

STANDARD_ATTRIBUTES = [
    "Key", "Bezeichnung", "Created", "Updated", "VIN", "Scope", "Markt",
    "Aktivität", "Baureihe", "ZielStand", "Testart", "Tests", "Testplanung (KW)", "Tickets",
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-results", type=int, default=200, help="Max FZG objects to fetch (default 200)")
    args = parser.parse_args()

    config = load_config()
    client = JiraClient(config)
    try:
        vehicles = client.list_test_vehicles(
            iql="objectType = FZG",
            object_type_id=client.INSIGHT_VEHICLE_ASSET_OBJECT_TYPE_ID,
            object_schema_id=client.INSIGHT_VEHICLE_ASSET_SCHEMA_ID,
            max_results=args.max_results,
        )
        print(f"Found {len(vehicles)} FZG object(s). Checking {len(STANDARD_ATTRIBUTES)} standard attribute(s) each...\n")

        complete = 0
        for v in vehicles:
            obj = client.get_insight_object(v["key"])
            missing = [
                name for name in STANDARD_ATTRIBUTES
                if not obj["attributes"].get(name) or not obj["attributes"][name][0]
            ]
            if missing:
                print(f"{obj['label']} ({obj['key']}): missing {missing}")
            else:
                complete += 1

        print(f"\n{complete}/{len(vehicles)} object(s) have every standard attribute populated.")
    finally:
        client.close()


if __name__ == "__main__":
    main()
