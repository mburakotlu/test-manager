"""Ordered vehicle test queues stored on the FZG Assets objects."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict
import json

from .jira_client import JiraClient
from .vehicle import HistoryEntry, TestAssignment, Vehicle

FZG_TYPE_ID = 4321
FZG_SCHEMA_ID = 607
TESTS_ID = 25059
PERIODS_ID = 25060
TICKETS_NAME = "Tickets"
HISTORY_NAME = "Historie (JSON)"
TEC_PC_NAME = "TEC PC"


def split_attribute(value: str) -> list[str]:
    return [part.strip() for part in value.split("|")] if value else []


def joined(values: list[str]) -> str:
    return " | ".join(values)


def asset_attribute_id(client: JiraClient, name: str, create: bool = False) -> int | None:
    definitions = client.get_object_type_attributes(FZG_TYPE_ID)
    matches = [a for a in definitions if a.get("name") == name]
    if len(matches) > 1:
        raise ValueError(f"FZG has more than one {name} attribute")
    if matches:
        return int(matches[0]["id"])
    if create:
        result = client.add_object_type_attribute(FZG_TYPE_ID, name, confirm=True)
        return int(result["attribute_id"])
    return None


def ticket_attribute_id(client: JiraClient, create: bool = False) -> int | None:
    return asset_attribute_id(client, TICKETS_NAME, create)


def history_attribute_id(client: JiraClient, create: bool = False) -> int | None:
    return asset_attribute_id(client, HISTORY_NAME, create)


def load_asset_vehicles(client: JiraClient, labels: set[str] | None = None) -> list[Vehicle]:
    """Load FZG vehicles and their three positional test fields from Assets."""
    objects = client.list_test_vehicles(
        iql="objectType = FZG", object_type_id=FZG_TYPE_ID,
        object_schema_id=FZG_SCHEMA_ID, max_results=1000,
    )
    vehicles = []
    for entry in objects:
        if labels is not None and entry["label"] not in labels:
            continue
        obj = client.get_insight_object(entry["key"])
        attrs = obj["attributes"]
        names = split_attribute((attrs.get("Tests") or [""])[0] or "")
        periods = split_attribute((attrs.get("Testplanung (KW)") or [""])[0] or "")
        tickets = split_attribute((attrs.get(TICKETS_NAME) or [""])[0] or "")
        if len(names) != len(periods):
            raise ValueError(f"{obj['key']}: Tests and Testplanung (KW) have different lengths")
        if tickets and len(tickets) != len(names):
            raise ValueError(f"{obj['key']}: Tickets has {len(tickets)} slots; expected {len(names)}")
        if not tickets:
            tickets = ["x"] * len(names)
        raw_history = (attrs.get(HISTORY_NAME) or [""])[0] or ""
        history = [HistoryEntry(**item) for item in json.loads(raw_history)] if raw_history else []
        vehicles.append(Vehicle(
            label=obj["label"], key=obj["key"], vin=(attrs.get("VIN") or [""])[0] or "",
            series=(attrs.get("Baureihe") or [""])[0] or "",
            market=(attrs.get("Markt") or [""])[0] or "",
            scope=(attrs.get("Scope") or [""])[0] or "",
            tec_pc=(attrs.get(TEC_PC_NAME) or [""])[0] or "",
            cluster=(attrs.get("Cluster") or [""])[0] or "",
            zielstand=(attrs.get("ZielStand") or [""])[0] or "",
            tests=[TestAssignment(name=name, period=period, template="daily_enna",
                                  ticket="" if ticket.lower() == "x" else ticket)
                     for name, period, ticket in zip(names, periods, tickets)],
            history=history,
        ))
    return vehicles


def save_asset_tickets(client: JiraClient, vehicle: Vehicle, attribute_id: int) -> None:
    """Persist one ordered ticket slot per test. Empty tickets become x."""
    if not vehicle.key:
        raise ValueError(f"Vehicle {vehicle.label} has no Assets key")
    value = joined([test.ticket or "x" for test in vehicle.tests])
    client.update_insight_object_attributes(
        int(client.get_insight_object(vehicle.key)["id"]), {attribute_id: [value]}, confirm=True,
    )


def save_asset_history(client: JiraClient, vehicle: Vehicle, attribute_id: int) -> None:
    """Persist activity history on the vehicle's FZG object."""
    value = json.dumps([asdict(entry) for entry in vehicle.history], ensure_ascii=False)
    client.update_insight_object_attributes(
        int(client.get_insight_object(vehicle.key)["id"]), {attribute_id: [value]}, confirm=True,
    )


def merge_existing_tickets(old: Vehicle, new: Vehicle) -> None:
    """Carry ticket keys through refresh and retain ticketed tests absent from it."""
    by_test: dict[tuple[str, str], deque[TestAssignment]] = defaultdict(deque)
    for test in old.tests:
        by_test[test.name, test.period].append(test)
    for test in new.tests:
        matches = by_test.get((test.name, test.period))
        if matches:
            test.ticket = matches.popleft().ticket or test.ticket
    new.tests.extend(test for matches in by_test.values() for test in matches if test.ticket)
