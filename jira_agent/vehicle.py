"""Local test-vehicle registry.

Mirrors the shape of the real Jira Insight "Autos" object type (schema PEDSFLEET,
id 411: Bezeichnung, Hersteller, Modell, Interne Kennung, VIN, Standort,
Erprobungsgebiet, Bemerkung) so vehicle data can be managed directly in this repo
instead of querying the (read-only for us) Insight/Assets API each time.

Each vehicle can also carry a queue of assigned tests, each with its own planned
period (sprint) and checklist template — see :class:`TestAssignment`.

See vehicles/*.yaml for examples.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml


@dataclass
class TestAssignment:
    """A single test/activity assigned to a vehicle for a specific period (sprint)."""

    name: str
    period: str
    description: str = ""
    template: str = "analysis_checklist"
    ticket: str = ""  # Jira issue key, filled in after the ticket is created


@dataclass
class HistoryEntry:
    """A past activity on a vehicle (HW change, PUDIS pull, charging, completed test, ...)."""

    date: str
    activity: str
    description: str = ""
    ticket: str = ""  # Jira issue key, filled in once created for this entry
    due_date: str = ""
    priority: str = ""
    assignee: str = ""  # Jira username


@dataclass
class Vehicle:
    """A test vehicle, mirroring the real Insight "Autos" object attributes."""

    label: str
    key: str = ""
    manufacturer: str = ""
    model: str = ""
    internal_id: str = ""
    vin: str = ""
    location: str = ""
    test_area: str = ""
    notes: str = ""
    series: str = ""
    market: str = ""
    scope: str = ""
    tec_pc: str = ""
    tests: list[TestAssignment] = field(default_factory=list)
    history: list[HistoryEntry] = field(default_factory=list)
    source_path: Path | None = None  # not persisted; set by load_vehicle/load_vehicles


def load_vehicle(path: str | Path) -> Vehicle:
    """Load a single vehicle from a YAML file.

    Expected shape::

        label: PT1023
        key: PEDSFLEET-26314   # optional, for traceability back to Insight
        manufacturer: ""
        model: ""
        internal_id: ""
        vin: ""
        location: ""
        test_area: ""
        notes: ""
        tests:                  # optional queue of assigned tests, in order
          - name: "X2X Update HCP3"
            period: "KW37"      # sprint identifier this test is planned for
            description: "Perform X2X update on the HCP3 hardware component."
            template: analysis_checklist
          - name: "Regression Suite A"
            period: "KW38"
            template: analysis_checklist
    """
    resolved = Path(path)
    data: dict[str, Any] = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if not data or "label" not in data:
        raise ValueError(f"Vehicle file {resolved} is missing required 'label' key.")
    tests = [
        TestAssignment(
            name=t["name"],
            period=t["period"],
            description=t.get("description", ""),
            template=t.get("template", "analysis_checklist"),
            ticket=t.get("ticket", ""),
        )
        for t in (data.get("tests") or [])
    ]
    history = [
        HistoryEntry(
            date=h["date"],
            activity=h["activity"],
            description=h.get("description", ""),
            ticket=h.get("ticket", ""),
            due_date=h.get("due_date", ""),
            priority=h.get("priority", ""),
            assignee=h.get("assignee", ""),
        )
        for h in (data.get("history") or [])
    ]
    return Vehicle(
        label=data["label"],
        key=data.get("key", ""),
        manufacturer=data.get("manufacturer", ""),
        model=data.get("model", ""),
        internal_id=data.get("internal_id", ""),
        vin=data.get("vin", ""),
        location=data.get("location", ""),
        test_area=data.get("test_area", ""),
        notes=data.get("notes", ""),
        series=data.get("series", ""),
        market=data.get("market", ""),
        scope=data.get("scope", ""),
        tec_pc=data.get("tec_pc", ""),
        tests=tests,
        history=history,
        source_path=resolved,
    )


def load_vehicles(directory: str | Path) -> list[Vehicle]:
    """Load all vehicle YAML files from ``directory``, sorted by filename."""
    resolved = Path(directory)
    if not resolved.is_dir():
        raise ValueError(f"Vehicles directory not found: {resolved}")
    return [load_vehicle(p) for p in sorted(resolved.glob("*.yaml"))]


_PERIOD_RE = re.compile(r"KW(\d{1,2})-(\d{4})$")


def _period_to_week_range(period: str) -> tuple[date, date] | None:
    """Return (monday, sunday) for a "KWnn-YYYY" period label, or None if unparseable."""
    match = _PERIOD_RE.match(period.strip())
    if not match:
        return None
    week, year = int(match.group(1)), int(match.group(2))
    try:
        monday = date.fromisocalendar(year, week, 1)
    except ValueError:
        return None
    return monday, monday + timedelta(days=6)


def is_vehicle_inactive(vehicle: Vehicle, today: date | None = None, weeks: int = 2) -> bool:
    """Return True if none of ``vehicle``'s tests fall within the last ``weeks`` week(s) up to ``today``.

    Used to flag vehicles that look abandoned/stale after a calendar refresh. Periods
    that can't be parsed as "KWnn-YYYY" are skipped (they neither count as recent
    activity nor cause a false inactive flag on their own).
    """
    today = today or date.today()
    cutoff = today - timedelta(weeks=weeks)
    for test in vehicle.tests:
        week_range = _period_to_week_range(test.period)
        if week_range is None:
            continue
        start, end = week_range
        if end >= cutoff and start <= today:
            return False
    return True


def vehicle_link_fields(vehicle: Vehicle) -> dict[str, Any]:
    """Build ``extra_fields`` for ``JiraClient.create_issue`` that link the new issue to this vehicle.

    Uses PHUB's plain VIN / Vehicle description fields (see config.py) since the real
    Insight object-picker field isn't on the create screen. Pass the result as
    ``extra_fields`` so the created issue is filterable/searchable by vehicle
    (e.g. ``VIN ~ \"...\"``) instead of only mentioning the vehicle in free text.
    Also sets "Product category" — required by board 114663's saved filter for a
    ticket to show up there at all (unrelated to vehicles, but every ticket this
    module creates currently targets that board's project).
    """
    from jira_agent.config import (
        PRODUCT_CATEGORY_FIELD_ID,
        PRODUCT_CATEGORY_VALUE,
        VEHICLE_DESCRIPTION_FIELD_ID,
        VIN_FIELD_ID,
    )

    fields: dict[str, Any] = {
        VEHICLE_DESCRIPTION_FIELD_ID: vehicle.label,
        PRODUCT_CATEGORY_FIELD_ID: PRODUCT_CATEGORY_VALUE,
    }
    if vehicle.vin:
        fields[VIN_FIELD_ID] = vehicle.vin
    return fields


def _replace_block(raw: str, key: str, block_lines: list[str]) -> str:
    """Replace the ``key:`` list block in ``raw`` with ``block_lines``, keeping everything else untouched.

    Locates the block by its "key:" line and consumes all following lines that are
    blank or indented (i.e. belong to that list); the first non-indented, non-blank
    line afterwards (or a different top-level key) ends the block. If ``key`` isn't
    present yet, the new block is appended at the end of the file.
    """
    lines = raw.splitlines(keepends=True)
    start: int | None = None
    end = len(lines)
    for i, line in enumerate(lines):
        if start is None and line.startswith(f"{key}:"):
            start = i
            continue
        if start is not None and line.strip() != "" and not line.startswith((" ", "\t", "-")):
            end = i
            break
    new_block = "".join(block_lines)
    if start is None:
        if raw and not raw.endswith("\n"):
            raw += "\n"
        return raw + new_block
    return "".join(lines[:start]) + new_block + "".join(lines[end:])


def save_vehicle_tests(vehicle: Vehicle) -> None:
    """Rewrite the ``tests:`` block of ``vehicle``'s source file, preserving other fields.

    Used to persist Jira ticket keys back onto the vehicle's test entries after
    ``generate_from_vehicles.py`` creates them, so the local file stays linked to
    what was actually generated in Jira. Requires ``vehicle.source_path`` to be
    set (done automatically by :func:`load_vehicle`/:func:`load_vehicles`).
    """
    if vehicle.source_path is None:
        raise ValueError(f"Vehicle {vehicle.label!r} has no source_path; load it via load_vehicle() first.")
    lines = ["tests:\n"]
    for t in vehicle.tests:
        lines.append(f'  - name: "{t.name}"\n')
        lines.append(f'    period: "{t.period}"\n')
        if t.description:
            lines.append(f'    description: "{t.description}"\n')
        lines.append(f"    template: {t.template}\n")
        if t.ticket:
            lines.append(f"    ticket: {t.ticket}\n")
    raw = vehicle.source_path.read_text(encoding="utf-8")
    vehicle.source_path.write_text(_replace_block(raw, "tests", lines), encoding="utf-8")


def save_vehicle_history(vehicle: Vehicle) -> None:
    """Rewrite the ``history:`` block of ``vehicle``'s source file, preserving other fields.

    Used to persist Jira ticket keys back onto history entries after a ticket is
    created for one (see ``generate_history_tickets.py``). Requires
    ``vehicle.source_path`` to be set (done automatically by :func:`load_vehicle`).
    """
    if vehicle.source_path is None:
        raise ValueError(f"Vehicle {vehicle.label!r} has no source_path; load it via load_vehicle() first.")
    lines = ["history:\n"]
    for h in vehicle.history:
        lines.append(f'  - date: "{h.date}"\n')
        lines.append(f'    activity: "{h.activity}"\n')
        if h.description:
            lines.append(f'    description: "{h.description}"\n')
        if h.due_date:
            lines.append(f'    due_date: "{h.due_date}"\n')
        if h.priority:
            lines.append(f'    priority: "{h.priority}"\n')
        if h.assignee:
            lines.append(f'    assignee: "{h.assignee}"\n')
        if h.ticket:
            lines.append(f"    ticket: {h.ticket}\n")
    raw = vehicle.source_path.read_text(encoding="utf-8")
    vehicle.source_path.write_text(_replace_block(raw, "history", lines), encoding="utf-8")


def clear_ticket_reference(vehicles: list[Vehicle], ticket: str) -> list[Vehicle]:
    """Clear ``ticket`` from any test/history entry across ``vehicles`` (in memory only).

    Used after a Jira ticket is deleted, so local YAML stops pointing at a
    ticket that no longer exists (which would otherwise block re-generating
    it, since generators skip tests/entries that already have a ``ticket``).
    Returns the vehicles that were actually changed; caller is responsible for
    persisting them (:func:`save_vehicle_tests`/:func:`save_vehicle_history`).
    """
    touched = []
    for vehicle in vehicles:
        changed = False
        for test in vehicle.tests:
            if test.ticket == ticket:
                test.ticket = ""
                changed = True
        for entry in vehicle.history:
            if entry.ticket == ticket:
                entry.ticket = ""
                changed = True
        if changed:
            touched.append(vehicle)
    return touched
