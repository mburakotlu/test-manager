"""Local web UI for logging vehicle activities and creating Jira issues for them.

Run with:
    python main.py
(or ``python webapp.py`` directly — both work; see main.py for details)
Then open http://127.0.0.1:5000/ in a browser.

Lets you pick one or more vehicles, describe an activity (HW change, PUDIS pull,
charging, completed test, ...), preview it, and optionally create a real Jira
issue per selected vehicle — same dry-run-by-default convention as the other
CLI scripts in this repo (generate_from_vehicles.py, generate_history_tickets.py),
just through a form instead of flags.
"""

from __future__ import annotations

import html
import tempfile
import threading
import uuid
from datetime import date, datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv
from flask import Flask, request, url_for

# Loaded here (not just in main.py) so `python webapp.py` also works without
# manually exporting env vars into the shell first.
load_dotenv(Path(__file__).parent / ".env")

from jira_agent.browser_checklist import write_checklists_via_browser
from jira_agent.checklist import render_test_checklist
from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient
from jira_agent.register import RegisterEntry, append_entry, load_entries
from jira_agent.vehicle import HistoryEntry, clear_ticket_reference, load_vehicle, load_vehicles, save_vehicle_history, save_vehicle_tests, vehicle_link_fields
from import_jahreskalender import import_calendar
from sync_tests_to_insight import sync_all

VEHICLES_DIR = Path(__file__).parent / "vehicles"
TEMPLATES_DIR = Path(__file__).parent / "templates"
DEFAULT_BOARD_ID = 114663  # DLCM_Integration/Automation
DEFAULT_XLSX = Path(__file__).parent / "DLCM_FZG_Planung.xlsx"
REPO_TICKET = "PHUB-2200"  # "Jira-Manager" Xray Test Repository reference ticket
ACTIVITY_SUGGESTIONS = ["HW change", "PUDIS ziehen", "Charging", "Test"]
PRIORITIES = ["Highest", "High", "Medium", "Low", "Lowest"]
PRIORITY_COLORS = {"Highest": "#c0392b", "High": "#e67e22", "Medium": "#d4ac0d", "Low": "#2e86c1", "Lowest": "#7f8c8d"}
# Fixed local candidate list (resolved once via JiraClient.search_users) instead of a
# live Jira-wide user search, which returned too many ambiguous/ unrelated matches.
ASSIGNEE_CANDIDATES = [
    {"name": "VKB0A6G", "display_name": "Namik"},
    {"name": "VN5ZNR6", "display_name": "Youssef"},
    {"name": "UC7B2IR", "display_name": "Mustafa Burak"},
]

# In-memory log of slow/background operations (browser automation, Insight sync, ...)
# so the UI can show that something is running/ran, without needing a job queue.
_LOG_LOCK = threading.Lock()
_LOG: list[dict[str, str]] = []
_LOG_MAX_ENTRIES = 300


def log_event(message: str, status: str = "info") -> None:
    """Record a timestamped background-operation event, shown on the /log page."""
    with _LOG_LOCK:
        _LOG.append({
            "time": datetime.now().isoformat(timespec="seconds"),
            "status": status,
            "message": message,
        })
        del _LOG[:-_LOG_MAX_ENTRIES]


def recent_log_entries(limit: int = 5) -> list[dict[str, str]]:
    """Return the most recent log entries, newest first."""
    with _LOG_LOCK:
        return list(reversed(_LOG[-limit:]))

app = Flask(__name__)

PAGE = """<!doctype html>
<html><head><title>{title}</title>
<style>
body {{ font-family: sans-serif; margin: 0; padding: 0; background: #f7f9fb; color: #222; }}
nav {{ background: #2c3e50; padding: 0.8em 2em; }}
nav a {{ color: #ecf0f1; margin-right: 1.2em; text-decoration: none; font-weight: 500; }}
nav a:hover {{ text-decoration: underline; }}
.container {{ width: 94%; max-width: 1400px; margin: 1.5em auto; }}
h1 {{ color: #2c3e50; }}
h3 {{ color: #2c3e50; border-bottom: 2px solid #d6e0ea; padding-bottom: 0.2em; }}
table {{ border-collapse: collapse; width: 100%; margin: 1em 0; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
td, th {{ border: 1px solid #e0e6ec; padding: 6px 10px; text-align: left; }}
th {{ background: #eaf0f6; color: #2c3e50; }}
tr:nth-child(even) td {{ background: #f7fafc; }}
.msg {{ padding: 0.6em 1em; background: #eaf4ea; border-left: 4px solid #4caf6f; margin-bottom: 1em; border-radius: 3px; }}
.warn {{ padding: 0.6em 1em; background: #fdf3e3; border-left: 4px solid #b9770e; margin-bottom: 1em; border-radius: 3px; }}
fieldset {{ margin-bottom: 1em; border: 1px solid #d6e0ea; border-radius: 4px; display: flex; flex-wrap: wrap; gap: 0.2em 1.5em; }}
fieldset label {{ width: 160px; }}
label {{ display: block; margin: 0.4em 0; }}
textarea, input[type=text], input[type=date], select {{ width: 100%; box-sizing: border-box; border: 1px solid #ccd6e0; border-radius: 3px; padding: 4px 6px; }}
button {{ background: #2980b9; color: #fff; border: none; padding: 0.5em 1.2em; border-radius: 4px; cursor: pointer; font-size: 1em; }}
button:hover {{ background: #21618c; }}
.badge {{ display: inline-block; padding: 2px 10px; border-radius: 10px; color: #fff; font-size: 0.85em; }}
.overdue {{ color: #c0392b; font-weight: bold; }}
.due-soon {{ color: #b9770e; font-weight: bold; }}
.log-strip {{ background: #fffbe6; border: 1px solid #f0e0a0; border-radius: 4px; padding: 0.5em 1em; margin-bottom: 1em; font-size: 0.9em; }}
.log-strip ul {{ margin: 0.3em 0 0 0; padding-left: 1.2em; }}
.log-time {{ color: #7f8c8d; margin-right: 0.5em; }}
</style>
<script>
function setVehicleChecks(button, checked) {{
  const form = button.closest("form");
  form.querySelectorAll('input[name="vehicles"]').forEach(cb => cb.checked = checked);
}}
</script>
</head>
<body>
<nav><a href="{index_url}">Create Activity</a> <a href="{sprint_url}">Generate Sprint Tickets</a> <a href="{refresh_url}">Refresh from Excel</a> <a href="{history_url}">History Overview</a> <a href="{comment_url}">Add Comment</a> <a href="{delete_ticket_url}">Delete Ticket</a> <a href="{register_url}">Edit Register</a> <a href="{repo_url}">Test Repo Files</a> <a href="{log_url}">Activity Log</a></nav>
<div class="container">
<h1>{title}</h1>
{log_strip}
{body}
</div>
</body></html>"""


def esc(value: str) -> str:
    """Escape a value for safe interpolation into HTML."""
    return html.escape(str(value), quote=True)


def render_page(title: str, body: str) -> str:
    """Wrap ``body`` (already-escaped HTML) in the shared page layout, with a strip
    showing the most recent background-operation log entries (see log_event())."""
    entries = recent_log_entries(5)
    if entries:
        items = "".join(
            f'<li><span class="log-time">{esc(e["time"])}</span>{esc(e["message"])}</li>'
            for e in entries
        )
        log_strip = (
            f'<div class="log-strip"><strong>Recent background activity</strong> '
            f'(<a href="{url_for("log_view")}">full log</a>)<ul>{items}</ul></div>'
        )
    else:
        log_strip = ""
    return PAGE.format(
        title=esc(title),
        body=body,
        log_strip=log_strip,
        index_url=url_for("index"),
        sprint_url=url_for("sprint_form"),
        refresh_url=url_for("refresh_form"),
        history_url=url_for("history_view"),
        comment_url=url_for("comment_form"),
        delete_ticket_url=url_for("delete_ticket_form"),
        register_url=url_for("register_view"),
        repo_url=url_for("repo_list"),
        log_url=url_for("log_view"),
    )


@app.route("/")
def index() -> str:
    """Show the form to log an activity and select which vehicles it applies to."""
    vehicles = load_vehicles(VEHICLES_DIR)
    checkboxes = "\n".join(
        f'<label><input type="checkbox" name="vehicles" value="{esc(v.label)}"> {esc(v.label)}</label>'
        for v in vehicles
    )
    datalist_opts = "".join(f'<option value="{esc(a)}">' for a in ACTIVITY_SUGGESTIONS)
    priority_opts = "".join(f'<option value="{esc(p)}" {"selected" if p == "Medium" else ""}>{esc(p)}</option>' for p in PRIORITIES)
    assignee_opts = "".join(f'<option value="{esc(a["name"])}">{esc(a["display_name"])}</option>' for a in ASSIGNEE_CANDIDATES)
    body = f"""
    <form method="post" action="{url_for('create')}">
      <div><button type="button" onclick="setVehicleChecks(this,true)">Select all</button>
      <button type="button" onclick="setVehicleChecks(this,false)">Select none</button></div>
      <fieldset><legend>Vehicles</legend>{checkboxes or '<p>No vehicles found.</p>'}</fieldset>
      <label>Activity
        <input type="text" list="activities" name="activity" required>
      </label>
      <datalist id="activities">{datalist_opts}</datalist>
      <label>Date <input type="date" name="date" value="{esc(date.today().isoformat())}"></label>
      <label>Due date <input type="date" name="due_date"></label>
      <label>Priority <select name="priority">{priority_opts}</select></label>
      <label>Assignee
        <select name="assignee">
          <option value="">(unassigned)</option>
          {assignee_opts}
        </select>
      </label>
      <label>Description <textarea name="description" rows="3"></textarea></label>
      <label>Project <input type="text" name="project" value="PHUB"></label>
      <label>Issue type <input type="text" name="issue_type" value="Task"></label>
      <label><input type="checkbox" name="confirm" value="1"> Create real Jira issue(s) now (uncheck to preview only)</label>
      <button type="submit">Submit</button>
    </form>
    """
    return render_page("Create Vehicle Activity", body)


@app.route("/create", methods=["POST"])
def create() -> str:
    """Handle the activity form: preview only, or actually create Jira issues + persist history."""
    vehicle_labels = request.form.getlist("vehicles")
    activity = request.form.get("activity", "").strip()
    entry_date = request.form.get("date", "").strip()
    due_date = request.form.get("due_date", "").strip()
    priority = request.form.get("priority", "").strip()
    assignee = request.form.get("assignee", "").strip()
    description = request.form.get("description", "").strip()
    project = request.form.get("project", "PHUB").strip() or "PHUB"
    issue_type = request.form.get("issue_type", "Task").strip() or "Task"
    confirm = request.form.get("confirm") == "1"

    if not vehicle_labels:
        return render_page("Error", f'<p class="msg">Select at least one vehicle.</p><p><a href="{url_for("index")}">Back</a></p>')
    if not activity:
        return render_page("Error", f'<p class="msg">Activity is required.</p><p><a href="{url_for("index")}">Back</a></p>')

    all_vehicles = {v.label: v for v in load_vehicles(VEHICLES_DIR)}
    rows = []

    if not confirm:
        for label in vehicle_labels:
            summary = f"{entry_date} - {activity} - {label}"
            rows.append(f"<tr><td>{esc(label)}</td><td>{esc(summary)}</td><td>(preview only, not created)</td></tr>")
        hidden_vehicles = "".join(f'<input type="hidden" name="vehicles" value="{esc(l)}">' for l in vehicle_labels)
        body = f"""
        <p class="msg">Preview only — nothing was created or saved yet.</p>
        <table><tr><th>Vehicle</th><th>Summary</th><th>Status</th></tr>{''.join(rows)}</table>
        <form method="post" action="{url_for('create')}">
          {hidden_vehicles}
          <input type="hidden" name="activity" value="{esc(activity)}">
          <input type="hidden" name="date" value="{esc(entry_date)}">
          <input type="hidden" name="due_date" value="{esc(due_date)}">
          <input type="hidden" name="priority" value="{esc(priority)}">
          <input type="hidden" name="assignee" value="{esc(assignee)}">
          <input type="hidden" name="description" value="{esc(description)}">
          <input type="hidden" name="project" value="{esc(project)}">
          <input type="hidden" name="issue_type" value="{esc(issue_type)}">
          <input type="hidden" name="confirm" value="1">
          <button type="submit">Confirm &amp; Create in Jira</button>
        </form>
        <p><a href="{url_for('index')}">Back</a></p>
        """
        return render_page("Preview", body)

    config = load_config()
    client = JiraClient(config)
    log_event(f"Creating {len(vehicle_labels)} activity issue(s) ({activity})...")
    try:
        for label in vehicle_labels:
            vehicle = all_vehicles.get(label)
            if vehicle is None:
                rows.append(f'<tr><td>{esc(label)}</td><td colspan="2">Vehicle not found</td></tr>')
                continue
            summary = f"{entry_date} - {activity} - {label}"
            issue_description = (
                "Created via jira_manager web UI.\n"
                f"Vehicle: {label}\nDate: {entry_date}\nActivity: {activity}\n"
                + (f"Description: {description}\n" if description else "")
            )
            extra_fields = vehicle_link_fields(vehicle)
            if due_date:
                extra_fields["duedate"] = due_date
            if priority:
                extra_fields["priority"] = {"name": priority}
            if assignee:
                extra_fields["assignee"] = {"name": assignee}
            result = client.create_issue(
                project, issue_type, summary, issue_description, confirm=True,
                extra_fields=extra_fields,
            )
            key = result["key"]
            # Re-read from disk right before writing so a prior save in this same
            # batch (or from another request) is never clobbered by a stale copy.
            vehicle = load_vehicle(vehicle.source_path)
            vehicle.history.append(HistoryEntry(
                date=entry_date, activity=activity, description=description, ticket=key,
                due_date=due_date, priority=priority, assignee=assignee,
            ))
            save_vehicle_history(vehicle)
            ticket_url = f"{config.effective_web_base_url}/jira/browse/{key}"
            rows.append(
                f'<tr><td>{esc(label)}</td><td>{esc(summary)}</td>'
                f'<td><a href="{esc(ticket_url)}" target="_blank">{esc(key)}</a></td></tr>'
            )
            log_event(f"Created {key} for {label}.")
    finally:
        client.close()

    body = f"""
    <p class="msg">Done — created issue(s) and saved to vehicle history.</p>
    <table><tr><th>Vehicle</th><th>Summary</th><th>Ticket</th></tr>{''.join(rows)}</table>
    <p><a href="{url_for('index')}">Create another</a> &middot; <a href="{url_for('history_view')}">View history</a></p>
    """
    return render_page("Created", body)


@app.route("/sprint", methods=["GET", "POST"])
def sprint_form() -> str:
    """Preview/create tickets (+ checklists, + optional Agile sprint) for vehicles' queued tests.

    Web-UI equivalent of generate_from_vehicles.py: filters each vehicle's `tests:`
    queue by period(s), skips tests that already have a ticket, and on confirm
    creates one Jira issue + checklist per remaining test. Same dry-run-by-default
    convention as every other write path in this app.
    """
    vehicles = load_vehicles(VEHICLES_DIR)

    if request.method == "GET":
        checkboxes = "\n".join(
            f'<label><input type="checkbox" name="vehicles" value="{esc(v.label)}" checked> {esc(v.label)}</label>'
            for v in vehicles
        )
        assignee_opts = "".join(f'<option value="{esc(a["name"])}">{esc(a["display_name"])}</option>' for a in ASSIGNEE_CANDIDATES)
        body = f"""
        <p><a href="{url_for('new_sprint_form')}">&rarr; Just generate a new (empty) sprint, without tickets</a></p>
        <form method="post" action="{url_for('sprint_form')}">
          <div><button type="button" onclick="setVehicleChecks(this,true)">Select all</button>
          <button type="button" onclick="setVehicleChecks(this,false)">Select none</button></div>
          <fieldset><legend>Vehicles</legend>{checkboxes or '<p>No vehicles found.</p>'}</fieldset>
          <label>Periods (comma-separated, e.g. "KW38-2026,KW39-2026" — blank = all periods)
            <input type="text" name="periods">
          </label>
          <label>Sprint label <input type="text" name="sprint" placeholder="DLCM Tests KW38+KW39"></label>
          <label>Project <input type="text" name="project" value="PHUB"></label>
          <label>Issue type <input type="text" name="issue_type" value="Task"></label>
          <label>Assignee
            <select name="assignee">
              <option value="">(unassigned)</option>
              {assignee_opts}
            </select>
          </label>
          <label><input type="checkbox" name="no_sprint_assignment" value="1"> Skip assigning tickets to a real Agile sprint (only create plain tickets)</label>
          <label>Board id <input type="text" name="board_id" value="{DEFAULT_BOARD_ID}"></label>
          <label><input type="checkbox" name="confirm" value="1"> Create real Jira issue(s) + checklists now (uncheck to preview only)</label>
          <button type="submit">Submit</button>
        </form>
        """
        return render_page("Generate Sprint Tickets", body)

    vehicle_labels = request.form.getlist("vehicles")
    periods_raw = request.form.get("periods", "").strip()
    wanted_periods = {p.strip() for p in periods_raw.split(",") if p.strip()} if periods_raw else None
    sprint_label = request.form.get("sprint", "").strip()
    project = request.form.get("project", "PHUB").strip() or "PHUB"
    issue_type = request.form.get("issue_type", "Task").strip() or "Task"
    assignee = request.form.get("assignee", "").strip()
    no_sprint_assignment = request.form.get("no_sprint_assignment") == "1"
    board_id_raw = request.form.get("board_id", "").strip()
    confirm = request.form.get("confirm") == "1"

    if not vehicle_labels:
        return render_page("Error", f'<p class="msg">Select at least one vehicle.</p><p><a href="{url_for("sprint_form")}">Back</a></p>')

    selected = {v.label: v for v in vehicles if v.label in set(vehicle_labels)}

    built = []
    skipped_existing = 0
    for vehicle in selected.values():
        for test in vehicle.tests:
            if wanted_periods and test.period not in wanted_periods:
                continue
            if test.ticket:
                skipped_existing += 1
                continue
            template_path = TEMPLATES_DIR / f"{test.template}.yaml"
            if not template_path.is_file():
                return render_page("Error", f'<p class="msg">Template not found: {esc(str(template_path))} (vehicle: {esc(vehicle.label)}, test: {esc(test.name)})</p><p><a href="{url_for("sprint_form")}">Back</a></p>')
            label = sprint_label or test.period
            summary = f"{label} - {test.name} - {vehicle.label}"
            description = (
                "Automatically generated from the vehicle's assigned test queue (via web UI).\n"
                f"Vehicle: {vehicle.label}\nPeriod: {test.period}\nTest: {test.name}\n"
                + (f"Description: {test.description}\n" if test.description else "")
            )
            checklist = render_test_checklist(test.template, TEMPLATES_DIR, week=test.period, vehicle=vehicle.label, sprint=label, activity=test.name)
            built.append((vehicle, test, test.template, summary, description, checklist, label))

    if not built:
        return render_page(
            "No matching tests",
            f'<p class="msg">No matching assigned tests found for the selected vehicles/periods '
            f'(skipped {skipped_existing} test(s) that already have a ticket).</p>'
            f'<p><a href="{url_for("sprint_form")}">Back</a></p>',
        )

    if not confirm:
        rows = "".join(
            f"<tr><td>{esc(vehicle.label)}</td><td>{esc(test.name)}</td><td>{esc(test.period)}</td><td>{esc(summary)}</td></tr>"
            for vehicle, test, _template, summary, _description, _checklist, _label in built
        )
        hidden_vehicles = "".join(f'<input type="hidden" name="vehicles" value="{esc(l)}">' for l in vehicle_labels)
        body = f"""
        <p class="msg">Preview only — nothing was created yet. {len(built)} ticket(s) would be created
        (skipped {skipped_existing} already-ticketed test(s)).</p>
        <table><tr><th>Vehicle</th><th>Test</th><th>Period</th><th>Summary</th></tr>{rows}</table>
        <form method="post" action="{url_for('sprint_form')}">
          {hidden_vehicles}
          <input type="hidden" name="periods" value="{esc(periods_raw)}">
          <input type="hidden" name="sprint" value="{esc(sprint_label)}">
          <input type="hidden" name="project" value="{esc(project)}">
          <input type="hidden" name="issue_type" value="{esc(issue_type)}">
          <input type="hidden" name="assignee" value="{esc(assignee)}">
          <input type="hidden" name="no_sprint_assignment" value="{'1' if no_sprint_assignment else '0'}">
          <input type="hidden" name="board_id" value="{esc(board_id_raw)}">
          <input type="hidden" name="confirm" value="1">
          <button type="submit">Confirm &amp; Create in Jira</button>
        </form>
        <p><a href="{url_for('sprint_form')}">Back</a></p>
        """
        return render_page("Preview Sprint Tickets", body)

    config = load_config()
    client = JiraClient(config)
    log_event(f"Generating {len(built)} sprint ticket(s)...")
    try:
        created: list[tuple[str, str]] = []
        created_labels: dict[str, str] = {}
        touched_vehicles: dict[str, object] = {}
        rows = []
        for vehicle, test, _template, summary, description, checklist, label in built:
            extra_fields = vehicle_link_fields(vehicle)
            if assignee:
                extra_fields["assignee"] = {"name": assignee}
            result = client.create_issue(
                project, issue_type, summary, description, confirm=True,
                extra_fields=extra_fields,
            )
            key = result["key"]
            created.append((key, checklist))
            created_labels[key] = label
            test.ticket = key
            touched_vehicles[vehicle.label] = vehicle
            ticket_url = f"{config.effective_web_base_url}/jira/browse/{key}"
            rows.append(
                f'<tr><td>{esc(vehicle.label)}</td><td>{esc(test.name)}</td><td>{esc(test.period)}</td>'
                f'<td><a href="{esc(ticket_url)}" target="_blank">{esc(key)}</a></td></tr>'
            )
            log_event(f"Created {key} ({test.name} / {vehicle.label}).")

        # Persist ticket keys BEFORE the checklist step, which can throw (e.g. expired
        # browser session) — otherwise real tickets get created but never recorded locally.
        for vehicle in touched_vehicles.values():
            save_vehicle_tests(vehicle)

        sprint_note = ""
        if not no_sprint_assignment:
            board_id = int(board_id_raw) if board_id_raw else DEFAULT_BOARD_ID
            log_event(f"Assigning {len(created)} ticket(s) to their Agile sprint (board {board_id})...")
            keys_by_label: dict[str, list[str]] = {}
            for key, key_label in created_labels.items():
                keys_by_label.setdefault(key_label, []).append(key)
            sprint_lines = []
            for key_label, keys in keys_by_label.items():
                sprint = client.get_or_create_sprint(board_id, key_label, confirm=True)
                client.add_issues_to_sprint(sprint["id"], keys, confirm=True)
                action = "Reused" if sprint["reused"] else "Created"
                sprint_lines.append(f"{action} sprint \"{esc(sprint['name'])}\" (id {sprint['id']}, {esc(sprint['state'])} state) \u2014 {len(keys)} ticket(s)")
                log_event(f'{action} sprint "{sprint["name"]}" (id {sprint["id"]}) with {len(keys)} ticket(s).')
            sprint_note = "<p class=\"msg\">" + "<br>".join(sprint_lines) + f" on board {board_id}.</p>"

        log_event(f"Writing checklists via browser automation for {len(created)} ticket(s) — this runs in the background and can take a while...")
        write_checklists_via_browser(config.effective_web_base_url, created)
        log_event(f"Checklist write finished for {len(created)} ticket(s).")
    finally:
        client.close()

    body = f"""
    <p class="msg">Done — created {len(created)} ticket(s) + checklist(s).</p>
    {sprint_note}
    <table><tr><th>Vehicle</th><th>Test</th><th>Period</th><th>Ticket</th></tr>{''.join(rows)}</table>
    <p><a href="{url_for('sprint_form')}">Generate more</a></p>
    """
    return render_page("Sprint Tickets Created", body)


@app.route("/sprint/new", methods=["GET", "POST"])
def new_sprint_form() -> str:
    """Create a standalone (empty) Agile sprint, without generating any tickets.

    Quick-action counterpart to the "Also create/group into an Agile sprint"
    checkbox on /sprint — useful when you just want the sprint to exist first
    and add tickets to it later. Also lists existing sprints on the board with
    a delete action (see delete_sprint_action).
    """
    if request.method == "GET":
        board_id = int(request.args.get("board_id") or DEFAULT_BOARD_ID)
        sprints: list[dict] = []
        sprints_error = ""
        try:
            config = load_config()
        except RuntimeError as exc:
            sprints_error = str(exc)
        else:
            client = JiraClient(config)
            try:
                sprints = client.list_sprints(board_id)
            finally:
                client.close()

        if sprints_error:
            sprints_table = f'<p class="msg">Could not load existing sprints: {esc(sprints_error)}</p>'
        elif sprints:
            sprint_rows = "".join(
                f'<tr><td>{s["id"]}</td><td>{esc(s.get("name", ""))}</td><td>{esc(s.get("state", ""))}</td>'
                f'<td><form method="post" action="{url_for("delete_sprint_action")}" style="margin:0">'
                f'<input type="hidden" name="sprint_id" value="{s["id"]}">'
                f'<input type="hidden" name="board_id" value="{board_id}">'
                f'<button type="submit">Delete</button></form></td></tr>'
                for s in sprints
            )
            sprints_table = f'<table><tr><th>ID</th><th>Name</th><th>State</th><th></th></tr>{sprint_rows}</table>'
        else:
            sprints_table = f"<p>No sprints found on board {board_id}.</p>"

        body = f"""
        <form method="post" action="{url_for('new_sprint_form')}">
          <label>Sprint name <input type="text" name="sprint" placeholder="DLCM Tests KW40" required></label>
          <label>Board id <input type="text" name="board_id" value="{board_id}"></label>
          <label><input type="checkbox" name="confirm" value="1"> Create this sprint now (uncheck to preview only)</label>
          <button type="submit">Submit</button>
        </form>
        <h3>Existing sprints on board {board_id}</h3>
        {sprints_table}
        <p><a href="{url_for('sprint_form')}">Back to Generate Sprint Tickets</a></p>
        """
        return render_page("Generate New Sprint", body)

    sprint_label = request.form.get("sprint", "").strip()
    board_id_raw = request.form.get("board_id", "").strip()
    confirm = request.form.get("confirm") == "1"

    if not sprint_label:
        return render_page("Error", f'<p class="msg">A sprint name is required.</p><p><a href="{url_for("new_sprint_form")}">Back</a></p>')
    board_id = int(board_id_raw) if board_id_raw else DEFAULT_BOARD_ID

    config = load_config()
    client = JiraClient(config)
    try:
        result = client.create_sprint(board_id, sprint_label, confirm=confirm)
    finally:
        client.close()

    if not confirm:
        body = f"""
        <p class="msg">Preview only — nothing was created yet.</p>
        <p><strong>Sprint name:</strong> {esc(sprint_label)} &middot; <strong>Board id:</strong> {board_id}</p>
        <form method="post" action="{url_for('new_sprint_form')}">
          <input type="hidden" name="sprint" value="{esc(sprint_label)}">
          <input type="hidden" name="board_id" value="{board_id}">
          <input type="hidden" name="confirm" value="1">
          <button type="submit">Confirm &amp; Create Sprint</button>
        </form>
        <p><a href="{url_for('new_sprint_form', board_id=board_id)}">Back</a></p>
        """
        return render_page("Preview New Sprint", body)

    log_event(f'Creating sprint "{sprint_label}" on board {board_id}...')
    log_event(f'Sprint "{result["name"]}" (id {result["id"]}, {result["state"]} state) created on board {board_id}.')
    body = f"""
    <p class="msg">Sprint "{esc(result["name"])}" (id {result["id"]}, {esc(result["state"])} state) created on board {board_id}.</p>
    <p>Add tickets to it from <a href="{url_for('sprint_form')}">Generate Sprint Tickets</a> (check "Also create/group into an Agile sprint" with the same name/board), or via Jira directly.</p>
    <p><a href="{url_for('new_sprint_form', board_id=board_id)}">Create another</a></p>
    """
    return render_page("Sprint Created", body)


@app.route("/sprint/delete", methods=["POST"])
def delete_sprint_action() -> str:
    """Delete an Agile sprint by id (dry-run preview, then confirm), reached from
    the sprint list on /sprint/new. Issues in the sprint go back to the backlog,
    they are not deleted — but the sprint itself cannot be recovered once deleted.
    """
    sprint_id_raw = request.form.get("sprint_id", "").strip()
    board_id_raw = request.form.get("board_id", "").strip()
    confirm = request.form.get("confirm") == "1"

    if not sprint_id_raw.isdigit():
        return render_page("Error", f'<p class="msg">A valid sprint id is required.</p><p><a href="{url_for("new_sprint_form")}">Back</a></p>')
    sprint_id = int(sprint_id_raw)
    board_id = int(board_id_raw) if board_id_raw else DEFAULT_BOARD_ID

    config = load_config()
    client = JiraClient(config)
    try:
        result = client.delete_sprint(sprint_id, confirm=confirm)
    finally:
        client.close()

    if not confirm:
        body = f"""
        <p class="msg">Preview only — nothing was deleted yet. Any issues currently in this
        sprint will be returned to the backlog (not deleted); the sprint itself cannot
        be recovered once deleted.</p>
        <p><strong>Sprint id:</strong> {sprint_id}</p>
        <form method="post" action="{url_for('delete_sprint_action')}">
          <input type="hidden" name="sprint_id" value="{sprint_id}">
          <input type="hidden" name="board_id" value="{board_id}">
          <input type="hidden" name="confirm" value="1">
          <button type="submit">Confirm &amp; Delete Sprint</button>
        </form>
        <p><a href="{url_for('new_sprint_form', board_id=board_id)}">Back</a></p>
        """
        return render_page("Preview Delete Sprint", body)

    log_event(f"Deleting sprint id {sprint_id} on board {board_id}...")
    log_event(f"Sprint id {sprint_id} deleted.")
    body = f"""
    <p class="msg">Sprint id {sprint_id} deleted. Any of its issues were returned to the backlog.</p>
    <p><a href="{url_for('new_sprint_form', board_id=board_id)}">Back to sprints</a></p>
    """
    return render_page("Sprint Deleted", body)


@app.route("/refresh", methods=["GET", "POST"])
def refresh_form() -> str:
    """Re-import DLCM_FZG_Planung.xlsx's Jahreskalender sheet into vehicles/*.yaml,
    and optionally push the refreshed tests onto each vehicle's Insight FZG object.

    Web-UI equivalent of running import_jahreskalender.py then sync_tests_to_insight.py.
    Optionally accepts a replacement .xlsx upload (kept as a new copy, does not
    overwrite DEFAULT_XLSX unless explicitly confirmed) — otherwise re-reads the
    repo's existing DLCM_FZG_Planung.xlsx.
    """
    if request.method == "GET":
        body = f"""
        <form method="post" action="{url_for('refresh_form')}" enctype="multipart/form-data">
          <label>Replace Excel file (optional — leave empty to reuse {esc(DEFAULT_XLSX.name)})
            <input type="file" name="xlsx_file" accept=".xlsx">
          </label>
          <label><input type="checkbox" name="sync_insight" value="1" checked> Also push refreshed tests onto Insight FZG objects</label>
          <label><input type="checkbox" name="confirm" value="1"> Actually write changes now (uncheck to preview only)</label>
          <button type="submit">Submit</button>
        </form>
        """
        return render_page("Refresh from Excel", body)

    sync_insight = request.form.get("sync_insight") == "1"
    confirm = request.form.get("confirm") == "1"

    # An uploaded file can't be re-attached to the "Confirm & Apply" resubmit form
    # (browsers don't let you prefill <input type=file>), so a first-time upload is
    # copied to a stable temp path and that path is carried forward via a hidden
    # field across the preview -> confirm round trip.
    xlsx_path = DEFAULT_XLSX
    uploaded = request.files.get("xlsx_file")
    carried_tmp_path = request.form.get("xlsx_tmp_path", "").strip()
    if uploaded and uploaded.filename:
        if not uploaded.filename.endswith(".xlsx"):
            return render_page("Error", f'<p class="msg">Uploaded file must be a .xlsx.</p><p><a href="{url_for("refresh_form")}">Back</a></p>')
        tmp_path = Path(tempfile.gettempdir()) / f"jira_manager_refresh_{uuid.uuid4().hex}.xlsx"
        uploaded.save(tmp_path)
        xlsx_path = tmp_path
    elif carried_tmp_path and Path(carried_tmp_path).is_file():
        xlsx_path = Path(carried_tmp_path)

    if confirm and xlsx_path != DEFAULT_XLSX:
        # Persist the new calendar as the repo's tracked copy for future refreshes.
        DEFAULT_XLSX.write_bytes(xlsx_path.read_bytes())
        xlsx_path.unlink(missing_ok=True)
        xlsx_path = DEFAULT_XLSX

    if confirm:
        log_event(f"Refreshing vehicle tests from {xlsx_path.name}...")
    calendar_result = import_calendar(xlsx_path, VEHICLES_DIR, confirm=confirm)
    if confirm:
        log_event(f"Calendar refresh applied to {len(calendar_result['matched'])} vehicle(s).")

    rows = "".join(
        f"<tr><td>{esc(label)}</td><td>{existing}</td><td>{new}</td></tr>"
        for label, existing, new in calendar_result["matched"]
    )
    unmatched_html = (
        f'<p>Unmatched calendar names (no corresponding vehicles/*.yaml): {esc(", ".join(calendar_result["unmatched"]))}</p>'
        if calendar_result["unmatched"] else ""
    )

    if calendar_result["diffs"]:
        diff_items = "".join(
            f"<li><strong>{esc(d['label'])}</strong>: "
            + "".join(f'<span style="color:#2e7d32">+ [{esc(period)}] {esc(name)}</span><br>' for name, period in d["added"])
            + "".join(f'<span style="color:#c0392b">- [{esc(period)}] {esc(name)}</span><br>' for name, period in d["removed"])
            + "</li>"
            for d in calendar_result["diffs"]
        )
        diff_html = f"<h3>Changes vs. previous import</h3><ul>{diff_items}</ul>"
    else:
        diff_html = "<p>No changes vs. previous import.</p>"

    inactive_html = (
        f'<p class="warn">&#9888; Inactive (no test planned in the last 2 weeks): '
        f'{esc(", ".join(calendar_result["inactive"]))}</p>'
        if calendar_result["inactive"] else ""
    )

    insight_html = ""
    if sync_insight and calendar_result["vehicles"]:
        config = load_config()
        client = JiraClient(config)
        if confirm:
            log_event(f"Syncing {len(calendar_result['vehicles'])} vehicle(s) to Insight FZG objects...")
        try:
            insight_result = sync_all(calendar_result["vehicles"], client, confirm=confirm)
        finally:
            client.close()
        if confirm:
            log_event(f"Insight sync finished: {len(insight_result['updated'])} updated, {len(insight_result['skipped'])} skipped.")
        insight_rows = "".join(
            f"<tr><td>{esc(label)}</td><td>{esc(key)}</td><td>{count}</td></tr>"
            for label, key, count in insight_result["updated"]
        )
        insight_skipped_html = (
            f'<p>Skipped (no FZG Insight object found): {esc(", ".join(insight_result["skipped"]))}</p>'
            if insight_result["skipped"] else ""
        )
        insight_html = f"""
        <h3>Insight sync {'(applied)' if confirm else '(preview)'}</h3>
        <table><tr><th>Vehicle</th><th>Insight key</th><th>Test count</th></tr>{insight_rows}</table>
        {insight_skipped_html}
        """

    body = f"""
    <p class="msg">{'Changes written.' if confirm else 'Preview only — nothing was written yet.'}</p>
    <h3>Vehicle tests {'(updated)' if confirm else '(would update)'}</h3>
    <table><tr><th>Vehicle</th><th>Existing tests</th><th>New tests</th></tr>{rows}</table>
    {unmatched_html}
    {diff_html}
    {inactive_html}
    {insight_html}
    <form method="post" action="{url_for('refresh_form')}" enctype="multipart/form-data">
      <input type="hidden" name="sync_insight" value="{'1' if sync_insight else '0'}">
      <input type="hidden" name="xlsx_tmp_path" value="{esc(str(xlsx_path)) if xlsx_path != DEFAULT_XLSX else ''}">
      <input type="hidden" name="confirm" value="1">
      <button type="submit">Confirm &amp; Apply</button>
    </form>
    <p><a href="{url_for('refresh_form')}">Back</a></p>
    """
    return render_page("Refresh from Excel" + (" — Applied" if confirm else " — Preview"), body)


@app.route("/history")
def history_view() -> str:
    """Show every vehicle's history log, with links to any linked Jira tickets."""
    vehicles = load_vehicles(VEHICLES_DIR)
    try:
        base_url = load_config().effective_web_base_url
    except RuntimeError:
        base_url = None

    sections = []
    today = date.today().isoformat()
    for vehicle in vehicles:
        if not vehicle.history:
            continue
        rows = []
        for entry in vehicle.history:
            if entry.ticket and base_url:
                ticket_html = f'<a href="{esc(base_url)}/jira/browse/{esc(entry.ticket)}" target="_blank">{esc(entry.ticket)}</a>'
            else:
                ticket_html = esc(entry.ticket) or "(no ticket)"
            if entry.due_date and entry.due_date < today:
                due_html = f'<span class="overdue">{esc(entry.due_date)}</span>'
            elif entry.due_date:
                due_html = f'<span class="due-soon">{esc(entry.due_date)}</span>'
            else:
                due_html = "–"
            if entry.priority:
                color = PRIORITY_COLORS.get(entry.priority, "#7f8c8d")
                priority_html = f'<span class="badge" style="background:{color}">{esc(entry.priority)}</span>'
            else:
                priority_html = "–"
            rows.append(
                f"<tr><td>{esc(entry.date)}</td><td>{esc(entry.activity)}</td>"
                f"<td>{esc(entry.description)}</td><td>{due_html}</td><td>{priority_html}</td>"
                f"<td>{esc(entry.assignee) or '–'}</td><td>{ticket_html}</td></tr>"
            )
        sections.append(
            f"<h3>{esc(vehicle.label)}</h3>"
            f"<table><tr><th>Date</th><th>Activity</th><th>Description</th><th>Due</th><th>Priority</th><th>Assignee</th><th>Ticket</th></tr>{''.join(rows)}</table>"
        )

    body = "".join(sections) or "<p>No history entries yet.</p>"
    return render_page("Vehicle History Overview", body)


@app.route("/comment", methods=["GET", "POST"])
def comment_form() -> str:
    """Show a form to add a comment to an existing Jira ticket, dry-run by default."""
    if request.method == "GET":
        body = f"""
        <form method="post" action="{url_for('comment_form')}">
          <label>Ticket key <input type="text" name="ticket" placeholder="PHUB-1234" required></label>
          <label>Comment <textarea name="body" rows="4" required></textarea></label>
          <label><input type="checkbox" name="confirm" value="1"> Post this comment now (uncheck to preview only)</label>
          <button type="submit">Submit</button>
        </form>
        """
        return render_page("Add Comment to Ticket", body)

    ticket = request.form.get("ticket", "").strip()
    comment_body = request.form.get("body", "").strip()
    confirm = request.form.get("confirm") == "1"

    if not ticket or not comment_body:
        return render_page("Error", f'<p class="msg">Ticket key and comment are both required.</p><p><a href="{url_for("comment_form")}">Back</a></p>')

    config = load_config()
    client = JiraClient(config)
    try:
        result = client.add_comment(ticket, comment_body, confirm=confirm)
    finally:
        client.close()

    if not confirm:
        hidden = (
            f'<input type="hidden" name="ticket" value="{esc(ticket)}">'
            f'<input type="hidden" name="body" value="{esc(comment_body)}">'
            f'<input type="hidden" name="confirm" value="1">'
        )
        body = f"""
        <p class="msg">Preview only — nothing was posted yet.</p>
        <p><strong>Ticket:</strong> {esc(ticket)}</p>
        <p><strong>Comment preview:</strong></p>
        <pre>{esc(result['preview'])}</pre>
        <form method="post" action="{url_for('comment_form')}">
          {hidden}
          <button type="submit">Confirm &amp; Post Comment</button>
        </form>
        <p><a href="{url_for('comment_form')}">Back</a></p>
        """
        return render_page("Preview Comment", body)

    append_entry(RegisterEntry(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        ticket=ticket,
        action="comment",
        details=comment_body,
    ))
    ticket_url = f"{config.effective_web_base_url}/jira/browse/{ticket}"
    body = f"""
    <p class="msg">Comment posted to <a href="{esc(ticket_url)}" target="_blank">{esc(ticket)}</a> and recorded in the edit register.</p>
    <p><a href="{url_for('comment_form')}">Add another</a> &middot; <a href="{url_for('register_view')}">View register</a></p>
    """
    return render_page("Comment Posted", body)


@app.route("/ticket/delete", methods=["GET", "POST"])
def delete_ticket_form() -> str:
    """Show a form to permanently delete an existing Jira ticket, dry-run by default."""
    if request.method == "GET":
        body = f"""
        <form method="post" action="{url_for('delete_ticket_form')}">
          <label>Ticket key <input type="text" name="ticket" placeholder="PHUB-1234" required></label>
          <label><input type="checkbox" name="confirm" value="1"> Delete this ticket now (uncheck to preview only) \u2014 this cannot be undone</label>
          <button type="submit">Submit</button>
        </form>
        """
        return render_page("Delete Ticket", body)

    ticket = request.form.get("ticket", "").strip()
    confirm = request.form.get("confirm") == "1"

    if not ticket:
        return render_page("Error", f'<p class="msg">Ticket key is required.</p><p><a href="{url_for("delete_ticket_form")}">Back</a></p>')

    config = load_config()
    client = JiraClient(config)
    try:
        result = client.delete_issue(ticket, confirm=confirm)
    finally:
        client.close()

    if not confirm:
        body = f"""
        <p class="msg">Preview only \u2014 nothing was deleted yet. This action cannot be undone.</p>
        <p><strong>Ticket:</strong> {esc(ticket)}</p>
        <form method="post" action="{url_for('delete_ticket_form')}">
          <input type="hidden" name="ticket" value="{esc(ticket)}">
          <input type="hidden" name="confirm" value="1">
          <button type="submit">Confirm &amp; Delete Ticket</button>
        </form>
        <p><a href="{url_for('delete_ticket_form')}">Back</a></p>
        """
        return render_page("Preview Delete Ticket", body)

    log_event(f"Deleting ticket {ticket}...")
    append_entry(RegisterEntry(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        ticket=ticket,
        action="delete_issue",
        details="Ticket permanently deleted via web UI.",
    ))
    log_event(f"Ticket {ticket} deleted.")

    vehicles = load_vehicles(VEHICLES_DIR)
    touched = clear_ticket_reference(vehicles, ticket)
    for vehicle in touched:
        save_vehicle_tests(vehicle)
        save_vehicle_history(vehicle)
    vehicle_note = (
        f'<p class="msg">Cleared the ticket reference from: {esc(", ".join(v.label for v in touched))}.</p>'
        if touched else ""
    )
    if touched:
        log_event(f"Cleared {ticket} reference from {len(touched)} vehicle(s): {', '.join(v.label for v in touched)}.")

    body = f"""
    <p class="msg">Ticket {esc(ticket)} deleted and recorded in the edit register.</p>
    {vehicle_note}
    <p><a href="{url_for('delete_ticket_form')}">Delete another</a> &middot; <a href="{url_for('register_view')}">View register</a></p>
    """
    return render_page("Ticket Deleted", body)



@app.route("/register")
def register_view() -> str:
    """Show the local register of manual edits made to existing Jira tickets via this app."""
    entries = load_entries()
    try:
        base_url = load_config().effective_web_base_url
    except RuntimeError:
        base_url = None

    rows = []
    for entry in reversed(entries):  # most recent first
        if base_url:
            ticket_html = f'<a href="{esc(base_url)}/jira/browse/{esc(entry.ticket)}" target="_blank">{esc(entry.ticket)}</a>'
        else:
            ticket_html = esc(entry.ticket)
        rows.append(
            f"<tr><td>{esc(entry.timestamp)}</td><td>{ticket_html}</td>"
            f"<td>{esc(entry.action)}</td><td>{esc(entry.details)}</td></tr>"
        )

    body = (
        f"<table><tr><th>Timestamp</th><th>Ticket</th><th>Action</th><th>Details</th></tr>{''.join(rows)}</table>"
        if rows else "<p>No edits recorded yet.</p>"
    )
    return render_page("Edit Register", body)


@app.route("/log")
def log_view() -> str:
    """Show the full in-memory log of background operations (browser checklist writes,
    Insight sync, sprint creation, ...) started by this app since it was last restarted."""
    with _LOG_LOCK:
        entries = list(reversed(_LOG))
    rows = "".join(
        f'<tr><td>{esc(e["time"])}</td><td>{esc(e["status"])}</td><td>{esc(e["message"])}</td></tr>'
        for e in entries
    )
    body = (
        f"<table><tr><th>Time</th><th>Status</th><th>Message</th></tr>{rows}</table>"
        if rows else "<p>No background operations logged yet in this run.</p>"
    )
    return render_page("Activity Log", body)


@app.route("/repo")
def repo_list() -> str:
    """List the vehicle YAML files attached to the test-repo reference ticket."""
    config = load_config()
    client = JiraClient(config)
    try:
        attachments = client.get_attachments(REPO_TICKET)
    finally:
        client.close()
    yaml_attachments = [a for a in attachments if a["filename"].endswith(".yaml")]

    ticket_url = f"{config.effective_web_base_url}/jira/browse/{REPO_TICKET}"
    rows = []
    for att in sorted(yaml_attachments, key=lambda a: a["filename"]):
        edit_url = url_for("repo_edit", filename=att["filename"])
        local_marker = "" if (VEHICLES_DIR / att["filename"]).is_file() else ' <span class="badge" style="background:#7f8c8d">no local copy</span>'
        rows.append(
            f"<tr><td><a href=\"{esc(edit_url)}\">{esc(att['filename'])}</a>{local_marker}</td>"
            f"<td>{att['size']} bytes</td><td>{esc(att['created'])}</td></tr>"
        )
    body = (
        f'<p>Reference ticket: <a href="{esc(ticket_url)}" target="_blank">{esc(REPO_TICKET)}</a></p>'
        f"<table><tr><th>File</th><th>Size</th><th>Created</th></tr>{''.join(rows)}</table>"
        if rows else f'<p>No .yaml attachments found on <a href="{esc(ticket_url)}" target="_blank">{esc(REPO_TICKET)}</a>.</p>'
    )
    return render_page("Test Repo Files", body)


@app.route("/repo/<filename>", methods=["GET", "POST"])
def repo_edit(filename: str) -> str:
    """Read (and optionally save) one vehicle YAML file attached to the test-repo reference ticket."""
    if "/" in filename or "\\" in filename or not filename.endswith(".yaml"):
        return render_page("Error", f'<p class="msg">Invalid filename.</p><p><a href="{url_for("repo_list")}">Back</a></p>')

    config = load_config()
    client = JiraClient(config)
    try:
        if request.method == "GET":
            attachments = client.get_attachments(REPO_TICKET)
            match = next((a for a in attachments if a["filename"] == filename), None)
            if match is None:
                return render_page("Error", f'<p class="msg">{esc(filename)} is not attached to {esc(REPO_TICKET)}.</p><p><a href="{url_for("repo_list")}">Back</a></p>')
            content = client.download_attachment(match["content_url"]).decode("utf-8")
        else:
            content = request.form.get("content", "")
            confirm = request.form.get("confirm") == "1"

            try:
                parsed = yaml.safe_load(content)
            except yaml.YAMLError as exc:
                return render_page("Error", f'<p class="msg">Invalid YAML: {esc(str(exc))}</p><p><a href="{url_for("repo_edit", filename=filename)}">Back</a></p>')
            if not parsed or "label" not in parsed:
                return render_page("Error", f'<p class="msg">YAML must have a top-level \"label:\" key (vehicle files only).</p><p><a href="{url_for("repo_edit", filename=filename)}">Back</a></p>')

            if not confirm:
                body = f"""
                <p class="msg">Preview only \u2014 nothing was saved yet.</p>
                <form method="post" action="{url_for('repo_edit', filename=filename)}">
                  <textarea name="content" rows="20">{esc(content)}</textarea>
                  <input type="hidden" name="confirm" value="1">
                  <button type="submit">Confirm &amp; Save (local + Jira attachment)</button>
                </form>
                <p><a href="{url_for('repo_list')}">Back</a></p>
                """
                return render_page(f"Preview: {filename}", body)

            local_path = VEHICLES_DIR / filename
            local_path.write_text(content, encoding="utf-8")

            attachments = client.get_attachments(REPO_TICKET)
            existing = next((a for a in attachments if a["filename"] == filename), None)
            if existing is not None:
                client.delete_attachment(existing["id"], confirm=True)
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir) / filename
                tmp_path.write_text(content, encoding="utf-8")
                client.add_attachment(REPO_TICKET, str(tmp_path), confirm=True)

            append_entry(RegisterEntry(
                timestamp=datetime.now().isoformat(timespec="seconds"),
                ticket=REPO_TICKET,
                action="edit_vehicle_yaml",
                details=filename,
            ))
            body = f"""
            <p class="msg">Saved {esc(filename)} \u2014 updated locally in vehicles/ and re-uploaded to {esc(REPO_TICKET)}.</p>
            <p><a href="{url_for('repo_edit', filename=filename)}">Edit again</a> &middot; <a href="{url_for('repo_list')}">Back to file list</a></p>
            """
            return render_page("Saved", body)
    finally:
        client.close()

    body = f"""
    <form method="post" action="{url_for('repo_edit', filename=filename)}">
      <textarea name="content" rows="20">{esc(content)}</textarea>
      <input type="hidden" name="confirm" value="0">
      <button type="submit">Preview Changes</button>
    </form>
    <p><a href="{url_for('repo_list')}">Back</a></p>
    """
    return render_page(f"Edit: {filename}", body)


if __name__ == "__main__":
    app.run(debug=True)
