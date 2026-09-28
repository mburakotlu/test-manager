"""Append-only local log of manual Jira edits made via this repo's tools (e.g. comments).

Separate from jira_agent/vehicle.py's per-vehicle history: this register is
ticket-centric (any ticket, not just ones tied to a vehicle) and just records
what was done and when, for later review.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

REGISTER_PATH = Path(__file__).parent.parent / "register.yaml"


@dataclass
class RegisterEntry:
    """One recorded edit against an existing Jira ticket."""

    timestamp: str  # ISO 8601, e.g. "2026-09-09T14:30:00"
    ticket: str
    action: str  # e.g. "comment"
    details: str = ""


def load_entries(path: Path = REGISTER_PATH) -> list[RegisterEntry]:
    """Load all recorded entries, oldest first. Returns an empty list if the file doesn't exist yet."""
    if not path.is_file():
        return []
    raw: list[dict[str, Any]] = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [RegisterEntry(**e) for e in raw]


def append_entry(entry: RegisterEntry, path: Path = REGISTER_PATH) -> None:
    """Append ``entry`` to the register file, preserving all existing entries."""
    entries = load_entries(path)
    entries.append(entry)
    path.write_text(
        yaml.safe_dump([asdict(e) for e in entries], sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
