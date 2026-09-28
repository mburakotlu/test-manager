"""Checklist template loading and rendering for the Smart Checklist plugin field.

Templates are stored as YAML files (see ``templates/``) and rendered to the
plain-text format the Jira "Smart Checklist" plugin expects in its custom
field: one item per line, prefixed ``+ `` for checked or ``- `` for unchecked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ChecklistItem:
    """A single checklist line item."""

    text: str
    checked: bool = False


@dataclass
class ChecklistGroup:
    """A named sub-list of items within a cascaded (grouped) template.

    Renders as a ``## Group Name`` sub-header under each day's top-level
    ``# {week}.{day}`` header (see :func:`render_cascaded_checklist`).
    """

    name: str
    items: list[ChecklistItem] = field(default_factory=list)


@dataclass
class ChecklistTemplate:
    """A named, reusable checklist definition.

    Either flat (``items``, rendered via :meth:`render`) or cascaded
    (``groups``, rendered via :func:`render_cascaded_checklist`) — a template
    should define one or the other, not both.
    """

    name: str
    items: list[ChecklistItem] = field(default_factory=list)
    groups: list[ChecklistGroup] = field(default_factory=list)
    source: str | None = None

    def render(self, **placeholders: str) -> str:
        """Render to Smart Checklist textarea format.

        ``placeholders`` are substituted into each item's text via
        ``str.format`` (e.g. ``vehicle="VAN-FZG"`` for a ``{vehicle}`` token).
        """
        lines = []
        for item in self.items:
            prefix = "+" if item.checked else "-"
            text = item.text.format(**placeholders) if placeholders else item.text
            lines.append(f"{prefix} {text}")
        return "\n".join(lines)


def render_cascaded_checklist(
    template: ChecklistTemplate,
    days: int = 5,
    week: str | None = None,
    day_labels: list[str] | None = None,
    **placeholders: str,
) -> str:
    """Render a template's ``groups:`` as a cascaded checklist, repeated once per day.

    Each day gets a top-level chapter header (``# {week}.{day}``), and each
    group within that day gets its own nested sub-header (``## Group Name``,
    its own collapsible progress badge) with plain, unprefixed item lines
    underneath. ``week`` has any trailing ``-YYYY`` year suffix stripped for
    the header (e.g. period ``KW38-2026`` -> header ``KW38.1``), since the
    year adds no value there.
    """
    if not template.groups:
        raise ValueError(f"Template {template.name!r} has no 'groups:' to render as a cascaded checklist.")
    week_label = re.sub(r"-\d{4}$", "", week) if week else week
    labels = day_labels or [f"{week_label}.{d}" if week_label else f"Day {d}" for d in range(1, days + 1)]
    if len(labels) != days:
        raise ValueError("day_labels must have exactly 'days' entries")
    lines = []
    for label in labels:
        lines.append(f"# {label}")
        for group in template.groups:
            lines.append(f"## {group.name}")
            for item in group.items:
                prefix = "+" if item.checked else "-"
                text = item.text.format(**placeholders) if placeholders else item.text
                lines.append(f"{prefix} {text}")
    return "\n".join(lines)


def render_daily_checklists(
    templates: list[ChecklistTemplate],
    days: int = 5,
    week: str | None = None,
    day_labels: list[str] | None = None,
    **placeholders: str,
) -> str:
    """Render ``templates`` repeated once per day (e.g. 2 templates x 5 days = 10 lists).

    Thin wrapper around :func:`render_cascaded_checklist`: treats each of
    ``templates`` as one group (its ``items``, named after the template).
    """
    synthetic = ChecklistTemplate(
        name="|".join(t.name for t in templates),
        groups=[ChecklistGroup(name=t.name, items=t.items) for t in templates],
    )
    return render_cascaded_checklist(synthetic, days=days, week=week, day_labels=day_labels, **placeholders)


# Kept for backward compatibility with existing vehicle YAML data (``template:
# daily_enna``) — now just a normal template name resolving to
# templates/daily_enna.yaml, which defines its own ``groups:`` cascade (see
# render_cascaded_checklist). No longer special-cased in render_test_checklist.
DAILY_ENNA_TEMPLATE_NAME = "daily_enna"


def render_test_checklist(
    template_name: str,
    templates_dir: str | Path,
    week: str | None = None,
    days: int = 5,
    **placeholders: str,
) -> str:
    """Render the checklist for a vehicle test.

    Loads ``{template_name}.yaml`` under ``templates_dir``. Templates that
    define ``groups:`` render as a cascaded, per-day checklist (see
    :func:`render_cascaded_checklist`); plain ``items:`` templates render as
    a normal flat list via :meth:`ChecklistTemplate.render`.
    """
    template = load_template(Path(templates_dir) / f"{template_name}.yaml")
    if template.groups:
        return render_cascaded_checklist(template, days=days, week=week, **placeholders)
    return template.render(week=week or "", **placeholders)


def load_template(path: str | Path) -> ChecklistTemplate:
    """Load a checklist template from a YAML file.

    Expected shape (flat)::

        name: TemplateName
        source: optional free-text provenance note
        items:
          - text: Item text, may contain {placeholders}
            checked: false

    Or cascaded (rendered per-day via :func:`render_cascaded_checklist`)::

        name: TemplateName
        groups:
          - name: Group Name
            items:
              - text: Item text
                checked: false
    """
    resolved = Path(path)
    data: dict[str, Any] = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if not data or ("items" not in data and "groups" not in data):
        raise ValueError(f"Template {resolved} is missing required 'items' or 'groups' key.")
    items = [
        ChecklistItem(text=i["text"], checked=bool(i.get("checked", False)))
        for i in data.get("items", [])
    ]
    groups = [
        ChecklistGroup(
            name=g["name"],
            items=[ChecklistItem(text=i["text"], checked=bool(i.get("checked", False))) for i in g.get("items", [])],
        )
        for g in data.get("groups", [])
    ]
    return ChecklistTemplate(name=data.get("name", resolved.stem), items=items, groups=groups, source=data.get("source"))
