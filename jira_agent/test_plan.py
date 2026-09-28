"""Local test-activity plan loader.

Mirrors the shape of the real Jira Insight "Planung" object type (schema PEDSFLEET,
id 481: Projektname, Beschreibung, Geplante Woche, Auto reference) so migrating to
real Insight objects later — if write permission is ever granted — is a close match.
We use local YAML files instead because our account only has read access to that
Insight schema (create is blocked with 403 on both "Autos" and "Planung" object types).

See plans/*.yaml for examples.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class PlannedActivity:
    """A single planned test activity for a vehicle (maps to one generated ticket)."""

    name: str
    description: str = ""
    template: str = "analysis_checklist"


@dataclass
class VehicleTestPlan:
    """Planned test activities for one vehicle in one sprint."""

    vehicle: str
    sprint: str
    activities: list[PlannedActivity] = field(default_factory=list)


def load_plan(path: str | Path) -> VehicleTestPlan:
    """Load a vehicle test plan from a YAML file.

    Expected shape::

        vehicle: PT1023
        sprint: KW37
        activities:
          - name: "X2X Update HCP3"
            description: "Perform X2X update on HCP3 hardware component"
            template: analysis_checklist
    """
    resolved = Path(path)
    data: dict[str, Any] = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if not data or "vehicle" not in data or "sprint" not in data:
        raise ValueError(f"Plan {resolved} is missing required 'vehicle'/'sprint' keys.")
    activities = [
        PlannedActivity(
            name=a["name"],
            description=a.get("description", ""),
            template=a.get("template", "analysis_checklist"),
        )
        for a in data.get("activities", [])
    ]
    return VehicleTestPlan(vehicle=data["vehicle"], sprint=data["sprint"], activities=activities)
