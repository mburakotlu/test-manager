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
import hmac
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import uuid
from calendar import month_name
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, redirect, request, session, url_for
from app_paths import APP_DIR, APP_VERSION

# Loaded here (not just in main.py) so `python webapp.py` also works without
# manually exporting env vars into the shell first.
load_dotenv(APP_DIR / ".env")

from jira_agent.browser_checklist import save_pending_checklists, write_checklists_via_browser
from jira_agent.checklist import render_test_checklist
from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient
from jira_agent.asset_vehicles import (
    history_attribute_id, load_asset_vehicles, save_asset_history,
    save_asset_tickets, ticket_attribute_id,
)
from jira_agent.register import RegisterEntry, append_entry, load_entries
from jira_agent.vehicle import HistoryEntry, Vehicle, clear_ticket_reference, vehicle_link_fields
from import_jahreskalender import import_calendar
from booking_notifications import (
    build_booking_submitted_email,
    build_test_email,
    load_mail_settings,
    send_email,
)
from planner_store import (
    authenticate,
    change_password as set_user_password,
    claim_booking_creation,
    complete_booking_creation,
    create_booking,
    create_user,
    get_booking,
    get_user,
    get_vehicle,
    initialize_database,
    clear_login_failures,
    login_is_locked,
    list_audit_events,
    list_bookings,
    list_imported_activity_colors,
    list_users,
    list_vehicles,
    record_audit_event,
    record_login_failure,
    reapprove_failed_booking,
    review_booking,
    update_booking,
    update_booking_progress,
    PROGRESS_EDITABLE_STATUSES,
    update_user_access,
    update_vehicle,
    update_vehicle_comment,
    set_vehicle_active,
    count_vehicle_bookings,
    delete_vehicle,
    MAX_VEHICLE_COMMENT_LENGTH,
    add_vehicle,
    vehicle_name_exists,
    fail_booking_creation,
)
from sync_tests_to_insight import sync_all

TEAM_ENDPOINTS = {
    "index",
    "login",
    "identify_user",
    "logout",
    "log_view",
    "team_dashboard",
    "campaign_planner",
    "my_bookings",
    "activity_history",
    "vehicle_history",
    "booking_review",
    "vehicle_management",
    "submit_booking",
    "edit_booking",
    "review_booking_action",
    "booking_progress",
    "retry_failed_booking",
    "create_booking_ticket",
    "vehicle_create",
    "vehicle_edit",
    "vehicle_comment",
    "vehicle_status",
}

TEMPLATES_DIR = APP_DIR / "templates"
DEFAULT_BOARD_ID = 114663  # DLCM_Integration/Automation
DEFAULT_XLSX = APP_DIR / "DLCM_FZG_Planung.xlsx"
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
_CHECKLIST_LOCK = threading.Lock()
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
app.secret_key = os.environ.get("APP_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes"),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)
initialize_database()

PAGE = """<!doctype html>
<html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: "Segoe UI", Arial, sans-serif; margin: 0; background: #f3f6fa; color: #243447; line-height: 1.5; }}
nav {{ background: #1d3550; padding: 0.8em clamp(1em, 4vw, 3em); display: flex; align-items: center; flex-wrap: wrap; gap: 0.45em 1.15em; }}
nav a {{ color: #f3f7fb; text-decoration: none; font-weight: 550; padding: 0.3em 0; }}
nav a:hover {{ color: #9bd4ff; text-decoration: none; }}
nav form {{ margin-left: auto; }}
nav button {{ padding: 0.42em 0.8em; }}
.container {{ width: min(94%, 1440px); margin: clamp(1.25em, 3vw, 2.5em) auto; }}
.team-section {{ margin: 2.5em 0; scroll-margin-top: 1em; }}
nav a.workspace-tab[aria-current="page"] {{ color: #fff; box-shadow: inset 0 -3px #9bd4ff; }}
.workspace-welcome {{ color: #526579; max-width: 680px; margin: 0 0 1.5em; }}
.workspace-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1em; max-width: 1000px; }}
.workspace-card {{ display: flex; flex-direction: column; min-height: 190px; padding: 1.4em; border: 1px solid #d5e1ec; border-radius: 12px; background: #fff; color: #243447; text-decoration: none; box-shadow: 0 5px 18px rgba(29,53,80,.06); transition: border-color .15s, box-shadow .15s, transform .15s; }}
.workspace-card:hover {{ border-color: #3182ce; box-shadow: 0 8px 22px rgba(29,53,80,.14); transform: translateY(-2px); text-decoration: none; }}
.workspace-card:focus-visible {{ outline: 3px solid #3182ce; outline-offset: 3px; }}
.workspace-card strong {{ color: #1d3550; font-size: 1.2em; }}
.workspace-card span {{ color: #526579; margin: 0.6em 0 1.1em; }}
.workspace-card .workspace-card-action {{ color: #1769aa; font-weight: 700; margin: auto 0 0; }}
.login-form {{ max-width: 520px; display: grid; gap: 0.2em; }}
.password-input-row {{ display: flex; align-items: center; gap: 0.5em; }}
.password-input-row input {{ flex: 1; min-width: 0; width: auto; }}
.password-toggle {{ background: #e8eff6; color: #1d3550; }}
.password-toggle:hover {{ background: #d7e5f1; color: #1d3550; }}
.login-feedback {{ min-height: 1.2em; margin: 0; color: #77500b; font-size: 0.9em; }}
.login-submit:disabled {{ opacity: 0.7; cursor: wait; }}
h1 {{ color: #1d3550; font-size: clamp(1.65rem, 3vw, 2.15rem); line-height: 1.2; margin: 0 0 0.75em; }}
h3 {{ color: #1d3550; border-bottom: 1px solid #d6e0ea; padding-bottom: 0.45em; margin-top: 1.8em; }}
p {{ margin: 0.6em 0 1em; }}
a {{ color: #1769aa; }}
.card {{ background: #fff; border: 1px solid #e0e7ef; border-radius: 12px; padding: clamp(1.1em, 2.5vw, 1.7em); box-shadow: 0 5px 18px rgba(29,53,80,0.06); }}
.planner-intro {{ color: #526579; margin: -0.3em 0 1.2em; }}
.planner-form {{ max-width: 900px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.25em 1.4em; }}
.planner-form label {{ margin: 0.35em 0 0.7em; }}
.planner-form .wide {{ grid-column: 1 / -1; }}
.form-hint {{ display: block; color: #61758a; font-size: 0.9em; margin-top: 0.25em; }}
.current-week-preview {{ display: inline-flex; flex-wrap: wrap; align-items: center; gap: 0.3em 0.5em; margin-top: 0.4em; padding: 0.3em 0.65em; background: #fff8e6; border: 1px solid #f0d7a1; border-radius: 6px; color: #5c4400; font-size: 0.88em; font-weight: 500; }}
.current-week-preview strong {{ color: #805000; }}
.link-button {{ background: none; color: #1769aa; padding: 0; font-size: inherit; font-weight: 650; text-decoration: underline; }}
.link-button:hover {{ background: none; color: #12588f; }}
.activity-color-options {{ grid-column: 1 / -1; border: 1px solid #d5e1ec; border-radius: 8px; padding: 0.8em 1em; margin: 0.4em 0; }}
.activity-color-options legend {{ color: #354b60; font-weight: 600; padding: 0 0.3em; }}
.activity-color-options label {{ display: inline-flex; align-items: center; gap: 0.4em; margin: 0.4em 1em 0.4em 0; font-weight: 500; }}
.activity-color-swatch {{ width: 1.4em; height: 1.4em; display: inline-block; border: 1px solid #788999; border-radius: 3px; }}
.activity-color-options input[type=color] {{ width: 3em; height: 2em; padding: 0; border: 1px solid #c8d4e0; cursor: pointer; }}
.approver-dropdown {{ position: relative; max-width: 460px; }}
.approver-dropdown summary {{ list-style: none; cursor: pointer; border: 1px solid #c8d4e0; border-radius: 7px; padding: 0.6em 2.2em 0.6em 0.8em; background: #fff; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.approver-dropdown summary::-webkit-details-marker {{ display: none; }}
.approver-dropdown summary::after {{ content: "▾"; position: absolute; right: 0.8em; color: #526579; }}
.approver-dropdown[open] summary {{ border-color: #3182ce; box-shadow: 0 0 0 3px rgba(49,130,206,.16); }}
.approver-dropdown-options {{ position: absolute; z-index: 20; left: 0; right: 0; margin-top: 0.25em; max-height: 240px; overflow-y: auto; background: #fff; border: 1px solid #c8d4e0; border-radius: 7px; box-shadow: 0 8px 20px rgba(29,53,80,.15); padding: 0.3em 0; }}
.activity-color-options .approver-dropdown-options label {{ display: flex; margin: 0; padding: 0.45em 0.8em; }}
.approver-dropdown-options label:hover {{ background: #edf7ff; }}
.vehicle-comment summary {{ list-style: none; cursor: pointer; min-width: 9em; max-width: 18em; white-space: pre-wrap; overflow-wrap: anywhere; }}
.vehicle-comment summary::-webkit-details-marker {{ display: none; }}
.vehicle-comment textarea {{ min-width: 16em; }}
.vehicle-comment-field {{ grid-column: 1 / -1; }}
label {{ display: block; margin: 0.7em 0; font-weight: 600; color: #354b60; }}
input[type=text], input[type=password], input[type=date], textarea, select {{ width: 100%; border: 1px solid #c8d4e0; border-radius: 7px; padding: 0.68em 0.8em; margin-top: 0.3em; background: #fff; color: #243447; font: inherit; transition: border-color .15s, box-shadow .15s; }}
input:focus, textarea:focus, select:focus {{ outline: none; border-color: #3182ce; box-shadow: 0 0 0 3px rgba(49,130,206,.16); }}
button, .button {{ display: inline-block; background: #1769aa; color: #fff; border: 0; padding: 0.62em 1.05em; border-radius: 7px; cursor: pointer; font: inherit; font-weight: 650; text-decoration: none; transition: background .15s, transform .15s; }}
button:hover, .button:hover {{ background: #12588f; color: #fff; text-decoration: none; }}
button:active {{ transform: translateY(1px); }}
.primary-action {{ grid-column: 1 / -1; justify-self: start; margin-top: 0.35em; }}
form {{ margin: 0; }}
table {{ border-collapse: separate; border-spacing: 0; width: 100%; margin: 1em 0; background: #fff; border: 1px solid #e0e7ef; border-radius: 10px; overflow: hidden; box-shadow: 0 3px 12px rgba(29,53,80,0.05); }}
td, th {{ border-bottom: 1px solid #e7edf3; padding: 0.8em 0.9em; text-align: left; vertical-align: top; }}
th {{ background: #edf3f8; color: #304b66; font-size: 0.88em; letter-spacing: 0.02em; }}
tr:last-child td {{ border-bottom: 0; }}
tbody tr:hover td, table tr:hover td {{ background: #f7fbff; }}
.table-wrap {{ max-width: 100%; overflow-x: auto; border-radius: 10px; }}
.table-wrap table {{ min-width: 760px; }}
.vehicle-group-header th {{ background: #dce8f2; color: #1d3550; font-size: 0.96em; padding: 0.75em 0.9em; }}
.table-wrap form {{ display: inline-flex; flex-wrap: wrap; gap: 0.4em; margin: 0.35em 0; }}
.table-wrap button {{ padding: 0.48em 0.72em; font-size: 0.9em; }}
.review-filters {{ display: flex; flex-wrap: wrap; gap: 0.55em; margin: 1em 0 1.2em; }}
.review-filters a {{ border: 1px solid #cfdae6; background: #fff; border-radius: 999px; padding: 0.4em 0.85em; text-decoration: none; font-weight: 600; }}
.review-filters a:hover {{ border-color: #1769aa; background: #edf7ff; }}
.history-stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 0.8em; margin: 1.2em 0; }}
.history-stat {{ background: #fff; border: 1px solid #e0e7ef; border-radius: 10px; padding: 0.9em 1em; }}
.history-stat strong {{ display: block; color: #1d3550; font-size: 1.35em; }}
.history-stat span {{ color: #61758a; font-size: 0.9em; }}
.vehicle-usage-card {{ padding: 0; overflow: hidden; align-self: start; }}
.vehicle-usage-card summary {{ cursor: pointer; list-style: none; padding: 0.9em 1em; }}
.vehicle-usage-card summary:focus-visible {{ outline: 3px solid #1769aa; outline-offset: -3px; }}
.vehicle-usage-toggle {{ display: block; margin-top: 0.4em; color: #1769aa !important; font-weight: 600; }}
.vehicle-usage-card[open] .vehicle-usage-toggle {{ color: #61758a !important; }}
.vehicle-usage-list {{ max-height: 18em; overflow-y: auto; margin: 0; padding: 0 1em 0.8em; list-style: none; }}
.vehicle-usage-list li {{ display: flex; flex-wrap: wrap; justify-content: space-between; gap: 0.35em 1em; border-top: 1px solid #edf1f5; padding: 0.65em 0; }}
.vehicle-usage-list a {{ color: #1d3550; font-weight: 700; }}
.vehicle-usage-list span {{ color: #61758a; font-size: 0.9em; }}
.week-timeline {{ border-left: 3px solid #c9d9e8; margin: 1.5em 0 0 0.65em; padding-left: 1.2em; }}
.week-card {{ position: relative; background: #fff; border: 1px solid #e0e7ef; border-radius: 10px; margin: 0 0 1em; padding: 0.9em 1em; box-shadow: 0 3px 12px rgba(29,53,80,0.04); }}
.week-card a {{ font-weight: 700; }}
.week-tone-0 {{ background: #f0f7ff; border-color: #b8d7f0; }}
.week-tone-1 {{ background: #f0faf5; border-color: #b6dfc7; }}
.week-tone-2 {{ background: #fff8ed; border-color: #f0d7ae; }}
.week-tone-3 {{ background: #f7f3ff; border-color: #d3c4f0; }}
.week-tone-4 {{ background: #fff2f3; border-color: #edc3c8; }}
.week-card::before {{ content: ""; position: absolute; left: -1.72em; top: 1.25em; width: 0.65em; height: 0.65em; background: #1769aa; border: 3px solid #f3f6fa; border-radius: 50%; }}
.week-card summary {{ cursor: pointer; color: #1d3550; font-weight: 700; }}
.week-count {{ display: inline-block; margin-left: 0.5em; color: #61758a; font-size: 0.88em; font-weight: 500; }}
.history-entries {{ list-style: none; padding: 0; margin: 0.8em 0 0; }}
.history-entries li {{ padding: 0.75em 0; border-top: 1px solid #edf1f5; }}
.history-date {{ color: #61758a; font-size: 0.9em; margin-right: 0.7em; }}
.history-vehicle {{ color: #1d3550; font-weight: 700; }}
.history-vin {{ color: #61758a; font-size: 0.9em; margin-left: 0.4em; }}
.history-activity {{ display: block; margin: 0.25em 0 0 0; }}
.history-color-swatch {{ display: inline-block; width: 0.9em; height: 0.9em; margin-right: 0.45em; border: 1px solid #788999; border-radius: 2px; vertical-align: middle; }}
.vehicle-details {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 0.7em; margin: 1em 0; }}
.vehicle-detail {{ background: #f7f9fc; border: 1px solid #e3eaf1; border-radius: 8px; padding: 0.75em; }}
.vehicle-detail span {{ display: block; font-size: 0.82em; color: #61758a; }}
.vehicle-detail strong {{ display: block; color: #243f5b; overflow-wrap: anywhere; }}
.history-status {{ display: inline-block; border-radius: 999px; padding: 0.1em 0.55em; margin-left: 0.45em; background: #e5eef7; color: #36536f; font-size: 0.8em; }}
.vehicle-usage-table {{ width: 100%; border-collapse: collapse; }}
.vehicle-usage-table th, .vehicle-usage-table td {{ padding: 0.65em 0.8em; border-bottom: 1px solid #e5ebf1; text-align: left; }}
.vehicle-usage-table th {{ color: #61758a; font-size: 0.85em; }}
.history-grid-wrap {{ position: relative; max-width: 100%; overflow: auto; max-height: 75vh; border: 1px solid #dce5ed; border-radius: 10px; background: #fff; isolation: isolate; }}
.history-grid-shell:fullscreen {{ width: 100vw; height: 100vh; padding: 0.6em; background: #f3f6fa; }}
.history-grid-shell:fullscreen .history-grid-wrap {{ max-width: none; max-height: calc(100vh - 4.5em); height: calc(100vh - 4.5em); }}
.history-grid-shell:fullscreen .history-grid {{ min-width: max-content; }}
.history-grid-fullscreen-button {{ margin: 0.5em 0; }}
.history-grid {{ width: max-content; min-width: 100%; margin: 0; border: 0; border-radius: 0; box-shadow: none; overflow: visible; }}
.history-grid th, .history-grid td {{ min-width: 105px; padding: 0.3em 0.4em; text-align: center; vertical-align: middle; border-right: 1px solid #e1e8ef; }}
.history-grid th.grid-day-col, .history-grid td.grid-day-cell {{ width: 40px; min-width: 40px; max-width: 40px; padding: 0; text-align: center; }}
.history-grid td.grid-day-cell {{ border-right-color: #edf1f5; border-bottom-color: #edf1f5; }}
.history-grid .grid-activity-label {{ display: block; width: 100%; padding: 0.2em 0.3em; border: 0; border-radius: 0; color: #172a3d; background: #fff; font-size: 0.84em; line-height: 1.25; white-space: normal; overflow-wrap: break-word; }}
.history-grid .grid-activity-label small {{ display: block; color: #46596b; font-size: 0.9em; line-height: 1.2; }}
.history-grid thead .grid-week-col {{ border-right: 1px solid #879aab; }}
.history-grid thead .grid-day-col {{ font-size: 0.82em; }}
.history-grid .grid-weekend {{ background-color: #e8edf2; color: #31465b; }}
.history-grid thead .grid-day-col.grid-weekend {{ background-color: #cbd6e0; color: #21384e; }}
.history-grid td.grid-weekend.grid-filled {{ background-color: #e8edf2; }}
.history-grid .grid-today {{ outline: 2px solid #c57b00; outline-offset: -2px; }}
.history-grid .grid-vehicle-col {{ position: sticky !important; left: 0 !important; z-index: 3; width: 190px; min-width: 190px; max-width: 190px; padding: 0.3em 0.5em; text-align: left; background: #edf3f8; box-shadow: 2px 0 4px rgba(29,53,80,.12); }}
.history-grid thead {{ position: sticky; top: 0; z-index: 4; }}
.history-grid thead th.grid-week-col {{ position: sticky; top: 0; z-index: 4; }}
.history-grid thead th {{ background: #e7eff6; color: #1d3550; }}
.history-grid thead th.grid-vehicle-col {{ position: sticky !important; top: 0; left: 0 !important; z-index: 7; }}
.history-grid thead .grid-year {{ background: #d7e5f1; font-size: 0.98em; }}
.history-grid thead .grid-month {{ background: #e2ebf3; font-size: 0.9em; }}
.history-grid .grid-month-end {{ border-right: 2px solid #9aabba !important; }}
.history-grid thead .grid-week-col strong, .history-grid thead .grid-week-col span {{ display: block; }}
.history-grid thead .grid-week-col span, .history-grid .grid-vehicle-col span {{ color: #61758a; font-size: 0.82em; font-weight: 400; }}
.history-grid thead .grid-current-week {{ background: #ffe29a; box-shadow: inset 0 0 0 2px #c57b00; }}
.history-grid .grid-current-week {{ box-shadow: inset 1px 0 #c57b00, inset -1px 0 #c57b00; }}
.history-grid td.grid-current-week {{ background-color: #fff8e6; }}
.history-grid td.grid-filled.grid-current-week {{ background-color: #fff8e6; }}
.history-grid thead .grid-week-col .grid-current-label {{ display: block; color: #805000; font-size: 0.72em; font-weight: 700; }}
.history-grid .grid-section-row th {{ position: sticky; left: 0; z-index: 3; padding: 0.25em 0.55em; background: #dce8f2; text-align: left; }}
.history-grid td.grid-vehicle-col {{ background: #fff; }}
.history-grid td.grid-filled {{ font-size: 0.9em; }}
.vehicle-management-heading {{ display: flex; align-items: center; justify-content: space-between; gap: 1em; margin: 1.2em 0; }}
.vehicle-management-heading h2 {{ margin: 0; color: #1d3550; font-size: 1.2em; }}
.vehicle-status-form {{ display: flex; flex-wrap: wrap; align-items: center; gap: 0.5em; }}
.vehicle-status-form label {{ margin: 0; font-size: 0.9em; }}
.vehicle-status-form select {{ width: auto; min-width: 14em; margin: 0; padding: 0.4em 0.6em; }}
.vehicle-status-form button {{ padding: 0.45em 0.8em; font-size: 0.88em; white-space: nowrap; }}
.vehicle-reactivate-form {{ margin-left: 0.6em !important; }}
.vehicle-reactivate-form button {{ white-space: nowrap; }}
.vehicle-delete-card {{ max-width: 640px; margin: 1.5em 0; border-color: #f0c9c9; }}
.vehicle-delete-card h3 {{ margin-top: 0; color: #8a1f1f; border-bottom-color: #f0c9c9; }}
.danger-button {{ background: #b42318; }}
.danger-button:hover {{ background: #8a1f1f; }}
.progress-badge {{ display: inline-block; margin-left: 0.35em; padding: 0.05em 0.5em; border-radius: 999px; font-size: 0.8em; font-weight: 650; white-space: nowrap; }}
.progress-in-progress {{ background: #e3f4ea; color: #1f6b3a; border: 1px solid #a9dcbc; }}
.progress-closed {{ background: #eceff3; color: #4a5a6a; border: 1px solid #ccd5df; }}
.history-grid .grid-activity-label.grid-progress-closed, .activity-calendar-event.progress-closed-event {{ opacity: 0.55; }}
.progress-form select {{ width: auto; margin: 0; padding: 0.3em 0.45em; font-size: 0.88em; }}
.vehicle-list-switch {{ margin: 0 0 0.75em; font-size: 0.9em; }}
.vehicle-list-switch a {{ font-weight: 600; }}
.view-tabs {{ display: flex; flex-wrap: wrap; gap: 0.55em; margin: 1em 0; }}
.view-tabs a {{ border: 1px solid #cfdae6; background: #fff; border-radius: 999px; padding: 0.4em 0.9em; text-decoration: none; font-weight: 650; }}
.view-tabs a.selected {{ background: #1769aa; border-color: #1769aa; color: #fff; }}
.activity-calendar {{ overflow-x: auto; border: 1px solid #dce5ed; border-radius: 10px; background: #fff; }}
.activity-calendar-nav {{ display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 0.75em; padding: 0.75em; }}
.activity-calendar-nav h2 {{ margin: 0; color: #1d3550; font-size: 1.15em; }}
.activity-calendar-nav a {{ text-decoration: none; font-weight: 650; }}
.activity-calendar-nav form {{ display: flex; align-items: center; gap: 0.45em; }}
.activity-calendar-nav input {{ width: auto; margin: 0; padding: 0.35em 0.5em; }}
.activity-calendar-nav button {{ padding: 0.38em 0.65em; }}
.activity-calendar-scope {{ display: inline-flex; border: 1px solid #c8d4e0; border-radius: 7px; overflow: hidden; }}
.activity-calendar-scope a {{ padding: 0.35em 0.8em; color: #1769aa; background: #fff; }}
.activity-calendar-scope a + a {{ border-left: 1px solid #c8d4e0; }}
.activity-calendar-scope a.selected {{ background: #1769aa; color: #fff; }}
.activity-calendar-week .activity-calendar-table td {{ height: 320px; }}
.activity-calendar-week .activity-calendar-event {{ font-size: 0.86em; }}
.activity-calendar-table {{ width: 100%; min-width: 700px; margin: 0; border: 0; border-radius: 0; box-shadow: none; table-layout: fixed; }}
.activity-calendar-table th {{ padding: 0.4em; text-align: center; }}
.activity-calendar-table td {{ height: 118px; padding: 0.3em; vertical-align: top; border-right: 1px solid #e7edf3; }}
.activity-calendar-day-cell {{ background: #fff; }}
.activity-calendar-day {{ display: block; margin-bottom: 0.2em; color: #526579; font-size: 0.84em; text-align: right; }}
.activity-calendar-other-month {{ background: #f7f9fc; }}
.activity-calendar-other-month .activity-calendar-day {{ color: #a0adba; }}
.activity-calendar-weekend {{ background-color: #f4f7fa; }}
.activity-calendar-today {{ box-shadow: inset 0 0 0 2px #c57b00; }}
.activity-calendar-event {{ display: block; margin: 0.2em 0; padding: 0.25em 0.35em; border-left: 3px solid #7890a6; border-radius: 3px; background: #eaf1f7; color: #243447; font-size: 0.78em; line-height: 1.25; overflow-wrap: anywhere; }}
.activity-calendar-event strong, .activity-calendar-event small {{ display: block; }}
.activity-calendar-event small {{ margin-top: 0.12em; color: inherit; opacity: 0.85; }}
.sr-only {{ position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }}
.msg, .warn {{ padding: 0.85em 1em; margin: 1em 0; border-radius: 8px; }}
.msg {{ background: #eaf7ef; border: 1px solid #b9e2c7; color: #205b35; }}
.warn {{ background: #fff6e8; border: 1px solid #f0d7a8; color: #77500b; }}
fieldset {{ margin-bottom: 1em; border: 1px solid #d6e0ea; border-radius: 8px; display: flex; flex-wrap: wrap; gap: 0.5em 1.5em; padding: 1em; }}
fieldset label {{ width: 160px; }}
.badge {{ display: inline-block; padding: 2px 10px; border-radius: 10px; color: #fff; font-size: 0.85em; }}
.overdue {{ color: #c0392b; font-weight: bold; }}
.due-soon {{ color: #b9770e; font-weight: bold; }}
.log-strip {{ background: #fff9e8; border: 1px solid #f0dfab; border-radius: 8px; padding: 0.8em 1em; margin-bottom: 1.2em; font-size: 0.9em; }}
.log-strip ul {{ margin: 0.4em 0 0; padding-left: 1.2em; }}
.log-time {{ color: #718096; margin-right: 0.5em; }}
@media (max-width: 700px) {{
  nav {{ gap: 0.35em 0.85em; padding: 0.75em 1em; }}
  .vehicle-status-form select {{ min-width: 0; width: 100%; }}
  .vehicle-management-heading {{ flex-wrap: wrap; }}
  nav form {{ margin-left: 0; }}
  .container {{ width: calc(100% - 1.5em); margin: 1.2em auto; }}
  .workspace-grid {{ grid-template-columns: 1fr; }}
  .planner-form {{ grid-template-columns: 1fr; }}
  .planner-form .wide, .primary-action, .activity-color-options {{ grid-column: auto; }}
  td, th {{ padding: 0.65em; }}
  .week-timeline {{ margin-left: 0.35em; padding-left: 1em; }}
  .week-card::before {{ left: -1.43em; }}
}}
</style>
<script>
function setVehicleChecks(button, checked) {{
  const form = button.closest("form");
  form.querySelectorAll('input[name="vehicles"]').forEach(cb => cb.checked = checked);
}}
function confirmVehicleStatus(form, question) {{
  const select = form.querySelector('select[name="vehicle_id"]');
  const name = select.options[select.selectedIndex].text;
  return confirm(question + "?\\n\\n" + name);
}}
function updateApproverSummary(input) {{
  const dropdown = input.closest(".approver-dropdown");
  const summary = dropdown.querySelector("summary");
  const selected = Array.from(dropdown.querySelectorAll('input[name="notify_to"]:checked')).map(box => box.value);
  summary.textContent = selected.length ? selected.join(", ") : summary.dataset.placeholder;
}}
document.addEventListener("click", event => {{
  document.querySelectorAll(".approver-dropdown[open]").forEach(dropdown => {{
    if (!dropdown.contains(event.target)) dropdown.removeAttribute("open");
  }});
}});
function toggleLoginPassword(button) {{
  const input = document.getElementById(button.getAttribute("aria-controls"));
  const reveal = input.type === "password";
  input.type = reveal ? "text" : "password";
  button.textContent = reveal ? "Hide" : "Show";
  button.setAttribute("aria-pressed", reveal ? "true" : "false");
  button.setAttribute("aria-label", reveal ? "Hide password" : "Show password");
  input.focus();
}}
document.addEventListener("DOMContentLoaded", () => {{
  const form = document.querySelector("[data-login-form]");
  if (!form) return;
  const password = form.querySelector("#login-password");
  const feedback = form.querySelector("#login-feedback");
  password.addEventListener("keyup", event => {{
    feedback.textContent = event.getModifierState("CapsLock") ? "Caps Lock is on." : "";
  }});
  form.addEventListener("submit", () => {{
    const button = form.querySelector(".login-submit");
    button.disabled = true;
    button.textContent = "Signing in...";
  }});
}});
async function toggleHistoryGridFullscreen(button) {{
  const shell = button.closest(".history-grid-shell");
  try {{
    if (document.fullscreenElement === shell) {{
      await document.exitFullscreen();
    }} else {{
      await shell.requestFullscreen();
    }}
  }} catch (error) {{
    alert("Could not switch the activity grid to full screen. Use your browser's full-screen permission/settings and try again.");
  }}
}}
document.addEventListener("fullscreenchange", () => {{
  const button = document.querySelector(".history-grid-fullscreen-button");
  const shell = button && button.closest(".history-grid-shell");
  if (button && shell) {{
    button.textContent = document.fullscreenElement === shell ? "Exit full screen" : "View grid full screen";
  }}
}});
</script>
</head>
<body>
<nav>{nav}<span style="color:#c5d3e0;margin-left:auto;">v{app_version}</span></nav>
<div class="container">
<h1>{title}</h1>
{log_strip}
{body}
</div>
</body></html>"""


def esc(value: str) -> str:
    """Escape a value for safe interpolation into HTML."""
    return html.escape(str(value), quote=True)


def _current_actor() -> str:
    return session.get("display_name") or session.get("user", "unknown")


def _group_vehicles(vehicles: list[dict]) -> list[tuple[str, list[dict]]]:
    grouped: dict[str, list[dict]] = {}
    for vehicle in vehicles:
        group_name = vehicle.get("group_name") or "Other vehicles"
        grouped.setdefault(group_name, []).append(vehicle)
    return list(grouped.items())


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
    user = session.get("user")
    if not user:
        nav = '<a href="' + url_for("login") + '">Sign in</a>'
    else:
        nav = (
            f'<a class="workspace-tab" href="{url_for("team_dashboard")}" '
            f'{"aria-current=page" if request.endpoint == "team_dashboard" and request.args.get("section", "home") == "home" else ""}>Home</a> '
        )
        nav += (
            f'<form method="post" action="{url_for("logout")}" style="display:inline">'
            f'<input type="hidden" name="_csrf_token" value="{esc(session.get("csrf_token", ""))}">'
            f'<button type="submit">Sign out ({esc(session.get("display_name", user))})</button></form>'
        )
    secured_body = re.sub(
        r"(<form\b(?=[^>]*\bmethod=['\"]?post\b)[^>]*>)",
        lambda match: match.group(1)
        + f'<input type="hidden" name="_csrf_token" value="{esc(session.get("csrf_token", ""))}">',
        body,
        flags=re.IGNORECASE,
    )
    return PAGE.format(
        title=esc(title),
        body=secured_body,
        nav=nav,
        log_strip=log_strip,
        app_version=esc(APP_VERSION),
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


def _login_form(username: str = "", error: str = "") -> str:
    error_html = f'<p class="warn" role="alert">{esc(error)}</p>' if error else ""
    return (
        f'{error_html}<form class="card login-form" data-login-form method="post" action="{url_for("login")}">'
        '<label for="login-username">Username</label>'
        f'<input id="login-username" type="text" name="username" value="{esc(username)}" '
        'autocomplete="username" required autofocus>'
        '<label for="login-password">Password</label>'
        '<div class="password-input-row">'
        '<input id="login-password" type="password" name="password" autocomplete="current-password" required>'
        '<button class="password-toggle" type="button" aria-controls="login-password" '
        'aria-label="Show password" aria-pressed="false" onclick="toggleLoginPassword(this)">Show</button>'
        '</div><p id="login-feedback" class="login-feedback" aria-live="polite"></p>'
        '<button class="login-submit" type="submit">Sign in</button></form>'
    )


@app.before_request
def enforce_login_roles_and_csrf():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    if request.method == "POST":
        expected = session.get("csrf_token", "")
        supplied = request.form.get("_csrf_token", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            abort(400, description="Form expired or invalid. Reload the page and try again.")

    endpoint = request.endpoint or ""
    if endpoint == "static":
        return None

    user_name = session.get("user")
    user = get_user(user_name) if user_name else None
    if user is None or not user["active"]:
        session.pop("user", None)
        session.pop("role", None)
        session.pop("user_id", None)
        session.pop("display_name", None)
        session.pop("needs_identity", None)
        if endpoint in {"login"}:
            return None
        return redirect(url_for("login", next=request.path))

    session["role"] = user["role"]
    session["user_id"] = user["id"]
    session["must_change_password"] = bool(user["must_change_password"])
    if user["must_change_password"] and user["role"] == "admin" and endpoint not in {"change_password", "logout"}:
        return redirect(url_for("change_password"))
    if session.get("needs_identity") and endpoint not in {"identify_user", "logout"}:
        return redirect(url_for("identify_user"))

    if endpoint in {"refresh_form", "sprint_form"}:
        return render_page(
            "Legacy planning workflow retired",
            '<p class="warn">This workflow reads the legacy Jira Assets test queue. New campaign bookings are managed in Campaign Planner; review and create Jira tickets from the booking queue.</p>',
        ), 410

    if endpoint.startswith("admin_") and user["role"] != "admin":
        abort(403)
    if endpoint == "change_password" and user["role"] != "admin":
        abort(403)
    if user["role"] in ("planner", "automation") and endpoint not in TEAM_ENDPOINTS:
        abort(403)
    return None


@app.route("/")
def index() -> str:
    """Show the form to log an activity and select which vehicles it applies to."""
    if session.get("user"):
        return redirect(url_for("team_dashboard"))
    client = JiraClient(load_config())
    try:
        vehicles = load_asset_vehicles(client)
    finally:
        client.close()
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

    asset_client = JiraClient(load_config())
    try:
        all_vehicles = {v.label: v for v in load_asset_vehicles(asset_client)}
    finally:
        asset_client.close()
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
            vehicle = load_asset_vehicles(client, {label})[0]
            vehicle.history.append(HistoryEntry(
                date=entry_date, activity=activity, description=description, ticket=key,
                due_date=due_date, priority=priority, assignee=assignee,
            ))
            save_asset_history(client, vehicle, history_attribute_id(client, create=True))
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
    config = load_config()
    asset_client = JiraClient(config)
    try:
        vehicles = load_asset_vehicles(asset_client)
    finally:
        asset_client.close()

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

    client = JiraClient(config)
    log_event(f"Generating {len(built)} sprint ticket(s)...")
    try:
        created: list[tuple[str, str]] = []
        created_labels: dict[str, str] = {}
        tickets_id = ticket_attribute_id(client, create=True)
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
            save_asset_tickets(client, vehicle, tickets_id)
            ticket_url = f"{config.effective_web_base_url}/jira/browse/{key}"
            rows.append(
                f'<tr><td>{esc(vehicle.label)}</td><td>{esc(test.name)}</td><td>{esc(test.period)}</td>'
                f'<td><a href="{esc(ticket_url)}" target="_blank">{esc(key)}</a></td></tr>'
            )
            log_event(f"Created {key} ({test.name} / {vehicle.label}).")

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
        try:
            write_checklists_via_browser(config.effective_web_base_url, created)
        except Exception as exc:
            # Tickets (and sprint assignment) above already succeeded — don't lose
            # track of their checklists just because the browser session expired.
            save_pending_checklists(created)
            log_event(f"Checklist write failed, saved {len(created)} ticket(s) as pending: {exc}")
            body = f"""
            <p class="msg">Created {len(created)} ticket(s) (and sprint, if requested), but writing
            their checklist(s) failed: {esc(str(exc))}</p>
            <p>The pending checklist(s) were saved to <code>pending_checklists.json</code>. After
            fixing the browser session (e.g. <code>python browser_login.py</code>), run
            <code>python retry_pending_checklists.py</code> to write them.</p>
            {sprint_note}
            <table><tr><th>Vehicle</th><th>Test</th><th>Period</th><th>Ticket</th></tr>{''.join(rows)}</table>
            <p><a href="{url_for('sprint_form')}">Back</a></p>
            """
            return render_page("Tickets Created — Checklist Pending", body)
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
    """Refresh FZG Assets test queues from the Excel Jahreskalender sheet.

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
          <label><input type="checkbox" name="confirm" value="1"> Actually write changes now (uncheck to preview only)</label>
          <button type="submit">Submit</button>
        </form>
        """
        return render_page("Refresh from Excel", body)

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
    config = load_config()
    client = JiraClient(config)
    try:
        asset_vehicles = load_asset_vehicles(client)
    finally:
        client.close()
    calendar_result = import_calendar(xlsx_path, asset_vehicles)
    if confirm:
        log_event(f"Calendar refresh applied to {len(calendar_result['matched'])} vehicle(s).")

    rows = "".join(
        f"<tr><td>{esc(label)}</td><td>{existing}</td><td>{new}</td></tr>"
        for label, existing, new in calendar_result["matched"]
    )
    unmatched_html = (
        f'<p>New FZG Assets {"created" if confirm else "planned"}: '
        f'{esc(", ".join(v.label for v in calendar_result["new_vehicles"]))}</p>'
        if calendar_result["new_vehicles"] else ""
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
    if calendar_result["vehicles"]:
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
    client = JiraClient(load_config())
    try:
        vehicles = load_asset_vehicles(client)
    finally:
        client.close()
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

    client = JiraClient(config)
    try:
        vehicles = load_asset_vehicles(client)
        touched = clear_ticket_reference(vehicles, ticket)
        if touched:
            tickets_id = ticket_attribute_id(client)
            if tickets_id is None:
                raise RuntimeError("FZG Tickets attribute is missing")
            history_id = history_attribute_id(client)
            for vehicle in touched:
                if vehicle.tests:
                    save_asset_tickets(client, vehicle, tickets_id)
                if history_id is not None:
                    save_asset_history(client, vehicle, history_id)
    finally:
        client.close()
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
    """List live FZG Assets vehicles and their test counts."""
    client = JiraClient(load_config())
    try:
        vehicles = load_asset_vehicles(client)
    finally:
        client.close()
    rows = "".join(
        f"<tr><td>{esc(v.label)}</td><td>{esc(v.key)}</td><td>{esc(v.vin)}</td>"
        f"<td>{len(v.tests)}</td><td>{sum(bool(t.ticket) for t in v.tests)}</td></tr>"
        for v in vehicles
    )
    return render_page("Asset Vehicles", "<table><tr><th>Vehicle</th><th>Asset key</th>"
                       "<th>VIN</th><th>Tests</th><th>Tickets</th></tr>" + rows + "</table>")


@app.route("/repo/<filename>", methods=["GET", "POST"])
def repo_edit(filename: str) -> str:
    """Direct old vehicle-file links to the live Assets list."""
    return render_page("Asset Vehicles", f'<p>Vehicle files are managed in Assets. <a href="{url_for("repo_list")}">View Assets</a>.</p>')


def _booking_table(bookings: list[dict], *, review: bool = False) -> str:
    rows = []
    try:
        base_url = load_config().effective_web_base_url
    except RuntimeError:
        base_url = ""
    for booking in bookings:
        issue = esc(booking["jira_issue_key"]) if booking["jira_issue_key"] else ""
        if issue and base_url:
            issue = f'<a href="{esc(base_url)}/jira/browse/{issue}" target="_blank">{issue}</a>'
        status = esc(booking["status"].replace("_", " ").title())
        actions = ""
        edit_link = (
            f'<a href="{url_for("edit_booking", booking_id=booking["id"])}">Edit</a> '
            if review and booking["status"] in ("pending", "approved") and not booking["jira_issue_key"]
            else ""
        )
        if review and booking["status"] == "pending":
            actions = (
                edit_link
                + f'<form method="post" action="{url_for("review_booking_action", booking_id=booking["id"])}">'
                '<button name="decision" value="approved" type="submit">Approve</button> '
                '<button name="decision" value="rejected" type="submit">Reject</button></form>'
            )
        elif review and booking["status"] == "approved":
            actions = (
                edit_link
                + f'<a href="{url_for("create_booking_ticket", booking_id=booking["id"])}">Preview Jira ticket</a> '
                f'<form method="post" action="{url_for("review_booking_action", booking_id=booking["id"])}">'
                '<button name="decision" value="pending" type="submit">Return to approve/reject</button> '
                '<button name="decision" value="rejected" type="submit">Reject</button></form>'
            )
        elif review and booking["status"] in ("creating", "creation_error"):
            actions = '<span class="warn">Check Jira before taking further action.</span>'
            if booking["status"] == "creation_error":
                actions += (
                    f'<form method="post" action="{url_for("retry_failed_booking", booking_id=booking["id"])}">'
                    '<label><input type="checkbox" name="verified_no_ticket" value="1" required>'
                    "I checked Jira and no issue was created.</label>"
                    '<button type="submit">Re-approve for retry</button></form>'
                )
        rows.append(
            "<tr>"
            f"<td>{esc(booking['vehicle_label'])}</td>"
            f"<td>{esc(booking['vehicle_vin'] or '—')}</td>"
            f"<td>{esc(booking['activity'])}</td>"
            f"<td>{esc(booking['period'])}</td>"
            f"<td>{esc(_booking_date_range_label(booking))}</td>"
            + (f"<td>{esc(booking['checklist_template'] or 'Not selected')}</td>" if review else "")
            + f"<td>{status}</td>"
            f"<td>{_progress_control(booking, 'automation' if review else 'testing')}</td>"
            f"<td>{issue or '—'}</td>"
            f"<td>{esc(booking['created_by'])}</td>"
            f"<td>{actions}</td>"
            "</tr>"
        )
    if not rows:
        return "<p>No bookings found.</p>"
    return (
        '<div class="table-wrap"><table><tr><th>Vehicle</th><th>VIN</th><th>Planned activity</th><th>Week</th>'
        "<th>Date</th>" + ("<th>Checklist</th>" if review else "")
        + "<th>Status</th><th>Progress</th><th>Jira ticket</th><th>Submitted by</th><th>Actions</th></tr>"
        + "".join(rows)
        + "</table></div>"
    )


@app.route("/login", methods=["GET", "POST"])
def login() -> str:
    if session.get("user"):
        if session.get("needs_identity"):
            return redirect(url_for("identify_user"))
        return redirect(url_for("team_dashboard"))
    if request.method == "GET":
        if not list_users():
            body = (
                "<p>No app administrator has been configured yet. On the app host, run "
                "<code>python manage_app.py bootstrap-admin</code> once to create the first admin.</p>"
            )
        else:
            body = (
                '<p class="form-hint">Team members can use the shared Planner account. '
                'After signing in, enter your name so bookings and actions can be attributed. '
                'Names entered on a shared account are self-reported.</p>'
            )
        body += _login_form()
        return render_page("Sign in", body)

    username = request.form.get("username", "")
    remote_addr = request.remote_addr or ""
    user = None
    if not login_is_locked(username, remote_addr):
        user = authenticate(username, request.form.get("password", ""))
        if user is None:
            record_login_failure(username, remote_addr)
        else:
            clear_login_failures(username, remote_addr)
    if user is None:
        return render_page(
            "Sign in",
            _login_form(username, "Sign-in failed. Check the username and password, or contact an app admin."),
        ), 401

    session.clear()
    session["csrf_token"] = secrets.token_urlsafe(32)
    session["user"] = user["username"]
    session["user_id"] = user["id"]
    session["role"] = user["role"]
    session["must_change_password"] = bool(user["must_change_password"])
    session["next_after_login"] = (
        next_path
        if (next_path := request.args.get("next", "")).startswith("/")
        and not next_path.startswith("//")
        else ""
    )
    session.permanent = True
    if user["must_change_password"]:
        return redirect(url_for("change_password"))
    if user["role"] == "planner":
        session["needs_identity"] = True
        return redirect(url_for("identify_user"))
    session["display_name"] = user["username"]
    record_audit_event(user["username"], "Signed in")
    next_path = session.pop("next_after_login", "")
    if next_path:
        return redirect(next_path)
    return redirect(url_for("team_dashboard"))


@app.route("/identify", methods=["GET", "POST"])
def identify_user() -> str:
    if session.get("role") != "planner" or not session.get("needs_identity"):
        return redirect(url_for("team_dashboard"))
    if request.method == "GET":
        return render_page(
            "Identify yourself",
            '<p class="planner-intro">This shared Planner account needs your name for the activity log. '
            'This is self-reported and does not verify your identity.</p>'
            f'<form class="card" method="post" action="{url_for("identify_user")}">'
            '<label>Your name <input type="text" name="display_name" maxlength="80" autocomplete="name" required></label>'
            '<button type="submit">Continue</button></form>',
        )
    display_name = request.form.get("display_name", "").strip()
    if (
        not display_name
        or len(display_name) > 80
        or any(ord(character) < 32 or ord(character) == 127 for character in display_name)
    ):
        return render_page(
            "Identify yourself",
            '<p class="warn">Enter a name of 1–80 characters without control characters.</p>'
            f'<form class="card" method="post" action="{url_for("identify_user")}">'
            '<label>Your name <input type="text" name="display_name" maxlength="80" required></label>'
            '<button type="submit">Continue</button></form>',
        ), 400
    session["display_name"] = display_name
    session.pop("needs_identity", None)
    record_audit_event(display_name, "Signed in", "Shared Planner account")
    next_path = session.pop("next_after_login", "")
    if next_path:
        return redirect(next_path)
    return redirect(url_for("team_dashboard"))


@app.route("/logout", methods=["POST"])
def logout() -> str:
    if session.get("user"):
        record_audit_event(_current_actor(), "Signed out")
    session.clear()
    return redirect(url_for("login"), code=303)


@app.route("/change-password", methods=["GET", "POST"])
def change_password() -> str:
    if request.method == "GET":
        return render_page(
            "Change password",
            f"""<form method="post" action="{url_for('change_password')}">
              <label>Current password <input type="password" name="current_password" required></label>
              <label>New password <input type="password" name="new_password" minlength="12" required></label>
              <label>Confirm new password <input type="password" name="confirm_password" minlength="12" required></label>
              <button type="submit">Save password</button>
            </form>""",
        )

    username = session["user"]
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    if not authenticate(username, current_password):
        return render_page("Change password", '<p class="warn">Current password is incorrect.</p>'
                           + f'<p><a href="{url_for("change_password")}">Try again</a></p>'), 400
    if new_password != request.form.get("confirm_password", ""):
        return render_page("Change password", '<p class="warn">The new passwords do not match.</p>'
                           + f'<p><a href="{url_for("change_password")}">Try again</a></p>'), 400
    try:
        set_user_password(int(session["user_id"]), new_password)
    except ValueError as exc:
        return render_page("Change password", f'<p class="warn">{esc(exc)}</p><p><a href="{url_for("change_password")}">Try again</a></p>'), 400
    session["must_change_password"] = False
    record_audit_event(_current_actor(), "Changed password")
    return redirect(url_for("team_dashboard"))


@app.route("/admin/users")
def admin_users() -> str:
    users = list_users()
    rows = []
    for user in users:
        role_options = "".join(
            f'<option value="{role}" {"selected" if role == user["role"] else ""}>{role.title()}</option>'
            for role in ("admin", "automation", "planner")
        )
        checked = "checked" if user["active"] else ""
        reset_control = (
            f'<form method="post" action="{url_for("admin_user_reset_password", user_id=user["id"])}">'
            '<button type="submit">Generate reset password</button></form>'
            if user["active"]
            else '<span class="form-hint">Enable account to reset its password.</span>'
        )
        rows.append(
            f'<tr><td>{esc(user["username"])}</td><td>{esc(user["created_at"])}</td><td>'
            f'<form method="post" action="{url_for("admin_user_access", user_id=user["id"])}">'
            f'<select name="role">{role_options}</select> '
            f'<label><input type="checkbox" name="active" value="1" {checked}> Active</label> '
            '<button type="submit">Save access</button></form></td>'
            f'<td>{reset_control}</td></tr>'
        )
    body = f"""
    <p>Only app admins can manage access or reset passwords. Users cannot change their own passwords. For shared team access, create one Planner account and share its password securely. Every Planner sign-in asks for the person’s name; that self-reported name is recorded with their activity.</p>
    <form method="post" action="{url_for('admin_user_create')}">
      <label>Username <input type="text" name="username" required></label>
      <label>Role <select name="role"><option value="planner">Planner</option><option value="automation">Automation</option><option value="admin">Admin</option></select></label>
      <button type="submit">Create user</button>
    </form>
    <h3>Current users</h3>
    <table><tr><th>Username</th><th>Created</th><th>Role and access</th><th>Password</th></tr>{''.join(rows)}</table>
    """
    return render_page("Manage users", body)


@app.route("/admin/audit")
def admin_audit() -> str:
    events = list_audit_events()
    rows = "".join(
        f'<tr><td>{esc(event["created_at"])}</td><td>{esc(event["actor"])}</td>'
        f'<td>{esc(event["action"])}</td><td>{esc(event["details"])}</td></tr>'
        for event in events
    )
    body = (
        '<p>Sign-ins, sign-outs, booking changes, vehicle edits, and ticket actions. '
        'Names from the shared Planner account are self-reported.</p>'
        '<div class="table-wrap"><table><tr><th>Time</th><th>Person/account</th>'
        f'<th>Action</th><th>Details</th></tr>{rows}</table></div>'
        if rows
        else '<p class="form-hint">No access or change events have been recorded yet.</p>'
    )
    return render_page("Access & Change Log", body)


@app.route("/admin/users/create", methods=["POST"])
def admin_user_create() -> str:
    username = request.form.get("username", "")
    role = request.form.get("role", "")
    try:
        temporary_password = create_user(username, role)
    except (ValueError, sqlite3.IntegrityError) as exc:
        return render_page("Create user", f'<p class="warn">Could not create user: {esc(exc)}</p><p><a href="{url_for("admin_users")}">Back</a></p>'), 400
    record_audit_event(_current_actor(), "Created user account", f"Username {username}; role {role}")
    password_instructions = (
        "Share this password securely. Users cannot change their passwords; an admin can generate a reset password."
    )
    body = (
        f'<p class="msg">Created {esc(username)} with role {esc(role)}. {password_instructions}</p>'
        f'<p><strong>Temporary password:</strong> <code>{esc(temporary_password)}</code></p>'
        f'<p><a href="{url_for("admin_users")}">Back to user management</a></p>'
    )
    return render_page("User created", body)


@app.route("/admin/users/<int:user_id>/access", methods=["POST"])
def admin_user_access(user_id: int) -> str:
    role = request.form.get("role", "")
    active = request.form.get("active") == "1"
    try:
        update_user_access(user_id, role, active)
    except ValueError as exc:
        return render_page("Update access", f'<p class="warn">{esc(exc)}</p><p><a href="{url_for("admin_users")}">Back</a></p>'), 400
    record_audit_event(_current_actor(), "Updated user access", f"User ID {user_id}; role {role}; active {active}")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
def admin_user_reset_password(user_id: int) -> str:
    user = next((item for item in list_users() if item["id"] == user_id), None)
    if user is None:
        abort(404)
    if not user["active"]:
        return render_page(
            "Password not reset",
            '<p class="warn">Enable this account before resetting its password.</p>'
            f'<p><a href="{url_for("admin_users")}">Back to user management</a></p>',
        ), 400
    temporary_password = secrets.token_urlsafe(18)
    try:
        set_user_password(user_id, temporary_password, must_change=False)
    except ValueError as exc:
        return render_page(
            "Password not reset",
            f'<p class="warn">{esc(exc)}</p><p><a href="{url_for("admin_users")}">Back to user management</a></p>',
        ), 400
    record_audit_event(_current_actor(), "Reset user password", f"User ID {user_id}; {user['username']}")
    return render_page(
        "Password reset",
        '<p class="msg">A new temporary password was generated. Share it with the user through an approved secure channel.</p>'
        f'<p><strong>Temporary password:</strong> <code>{esc(temporary_password)}</code></p>'
        f'<p><a href="{url_for("admin_users")}">Back to user management</a></p>',
    )


@app.route("/planner")
def campaign_planner() -> str:
    return team_dashboard("testing")


def _campaign_planner_section() -> str:
    vehicles = list_vehicles()
    if not vehicles:
        return (
            '<section class="team-section" id="submit-booking"><h2>Testing Team: Submit an activity</h2>'
            '<div class="card"><p class="warn">No vehicles are configured yet. Add a vehicle below or run the workbook import.</p></div></section>'
        )
    body = f"""
    <p class="planner-intro">Plan by week or dates. Enter at least one; dates take precedence when both are provided, and a date range may cross weeks. Submission saves it for review and does not create a Jira ticket.</p>
    <form class="card planner-form" method="post" action="{url_for('submit_booking')}">
      {_booking_form_fields(vehicles)}
      {_notification_recipient_fields()}
      <button class="primary-action" type="submit">Submit booking for review</button>
    </form>
    """
    return f'<section class="team-section" id="submit-booking"><h2>Submit/Book an activity</h2>{body}</section>'


def _booking_form_fields(vehicles: list[dict], booking: dict | None = None) -> str:
    booking = booking or {}
    selected_vehicle_id = booking.get("vehicle_id")
    if selected_vehicle_id is not None and all(vehicle["id"] != selected_vehicle_id for vehicle in vehicles):
        current_vehicle = get_vehicle(selected_vehicle_id)
        if current_vehicle:
            vehicles = [current_vehicle, *vehicles]
    selected_color = (booking.get("activity_color") or "").upper()
    imported_colors = list_imported_activity_colors()
    is_custom_color = bool(selected_color) and selected_color not in imported_colors
    color_options = "".join(
        f'<label><input type="radio" name="activity_color" value="{color}"'
        f'{" checked" if color == selected_color else ""}>'
        f'<span class="activity-color-swatch" style="background-color:{color}" aria-hidden="true"></span>{color}</label>'
        for color in imported_colors
    )
    vehicle_options = "".join(
        f'<optgroup label="{esc(group_name)}">'
        + "".join(
            f'<option value="{vehicle["id"]}"{" selected" if vehicle["id"] == selected_vehicle_id else ""}>'
            f'{esc(vehicle["label"])} — VIN: {esc(vehicle["vin"] or "Not recorded")}</option>'
            for vehicle in group_vehicles
        )
        + "</optgroup>"
        for group_name, group_vehicles in _group_vehicles(vehicles)
    )
    today = date.today()
    current_year, current_week, _ = today.isocalendar()
    current_period = f"KW{current_week:02d}-{current_year}"
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    current_week_preview = (
        f'<span class="current-week-preview">Current week: <strong>{current_period}</strong> '
        f'({week_start:%a %d %b} – {week_end:%a %d %b %Y}) '
        f'<button type="button" class="link-button" onclick="this.form.period.value=\'{current_period}\'">'
        'Use current week</button></span>'
    )
    return f"""
      <label>Vehicle <select name="vehicle_id" required>{vehicle_options}</select></label>
      <label>Planned activity <input type="text" name="activity" value="{esc(booking.get("activity", ""))}" required></label>
      <label>Planned week (optional) <input type="text" name="period" value="{esc(booking.get("period", ""))}" placeholder="{current_period}" pattern="KW[0-9]{{1,2}}(-[0-9]{{4}})?">
        {current_week_preview}
        <span class="form-hint">Use the ISO week format, for example {current_period}. If no dates are entered, this books Monday-Friday.</span></label>
      <label>Start date (optional) <input type="date" name="planned_date" value="{esc(booking.get("planned_date", ""))}">
        <span class="form-hint">Choose a date to book a day or date range. If a week is also entered, the dates take precedence.</span></label>
      <label>End date <input type="date" name="planned_end_date" value="{esc(booking.get("planned_end_date", ""))}">
        <span class="form-hint">Optional. May fall in a different week; requires a start date.</span></label>
      <label class="wide">Notes <textarea name="description" rows="3" placeholder="Optional details for team">{esc(booking.get("description", ""))}</textarea></label>
      <fieldset class="activity-color-options"><legend>Activity color</legend>
        <label><input type="radio" name="activity_color" value=""{" checked" if not selected_color else ""}> Automatic</label>
        <span class="form-hint">Automatic reuses a matching activity color when available. Or choose an imported Excel color or a custom color.</span>
        {color_options}
        <label><input type="radio" name="activity_color" value="custom" id="activity-color-custom"{" checked" if is_custom_color else ""}>
          Custom <input type="color" name="custom_activity_color" value="{esc(selected_color.lower() if is_custom_color else "#1769aa")}" aria-label="Custom activity color"
          oninput="document.getElementById('activity-color-custom').checked = true"></label>
      </fieldset>
    """


@app.route("/planner/bookings", methods=["GET"])
def my_bookings() -> str:
    return team_dashboard("testing")


def _my_bookings_section() -> str:
    bookings = list_bookings(created_by=_current_actor(), limit=500)
    return f'<section class="team-section" id="my-bookings"><h2>My Bookings</h2>{_booking_table(bookings)}</section>'


def _activity_color_palette(bookings: list[dict]) -> dict[str, str]:
    palette_votes: dict[str, dict[str, int]] = {}
    for booking in bookings:
        activity_key = booking["activity"].strip().casefold()
        color = booking.get("activity_color", "").strip().upper()
        if re.fullmatch(r"#[0-9A-F]{6}", color):
            votes = palette_votes.setdefault(activity_key, {})
            votes[color] = votes.get(color, 0) + 1
    return {
        activity: max(colors, key=lambda color: (colors[color], color))
        for activity, colors in palette_votes.items()
    }


def _booking_activity_color(booking: dict, activity_colors: dict[str, str]) -> str:
    color = booking.get("activity_color", "").strip().upper()
    if not re.fullmatch(r"#[0-9A-F]{6}", color):
        color = activity_colors.get(booking["activity"].strip().casefold(), "")
    return color if re.fullmatch(r"#[0-9A-F]{6}", color) else ""


def _history_color_swatch(booking: dict, activity_colors: dict[str, str]) -> str:
    color = _booking_activity_color(booking, activity_colors)
    if not color:
        return ""
    return f'<span class="history-color-swatch" style="background-color:{color}" title="Activity color {color}"></span>'


def _activity_status_label(status: str) -> str:
    if status == "historical":
        return ""
    return status.replace("_", " ").title()


def _history_status_badge(status: str) -> str:
    label = _activity_status_label(status)
    return f'<span class="history-status">{esc(label)}</span>' if label else ""


PROGRESS_LABELS = {"not_started": "Not started", "in_progress": "In progress", "closed": "Closed"}


def _booking_progress(booking: dict) -> str:
    # Activities imported as historical are already finished.
    if booking["status"] == "historical":
        return "closed"
    progress = booking.get("progress") or "not_started"
    return progress if progress in PROGRESS_LABELS else "not_started"


def _progress_badge(booking: dict) -> str:
    """Badge for calendar/history views; historical imports stay unmarked to keep the views clean."""
    if booking["status"] == "historical":
        return ""
    progress = _booking_progress(booking)
    if progress == "in_progress":
        return '<span class="progress-badge progress-in-progress">▶ In progress</span>'
    if progress == "closed":
        return '<span class="progress-badge progress-closed">✓ Closed</span>'
    return ""


def _is_closed_display(booking: dict) -> bool:
    return booking["status"] != "historical" and _booking_progress(booking) == "closed"


def _progress_control(booking: dict, return_to: str) -> str:
    progress = _booking_progress(booking)
    if booking["status"] not in PROGRESS_EDITABLE_STATUSES:
        return esc(PROGRESS_LABELS[progress]) if booking["status"] == "historical" else '<span class="form-hint">After approval</span>'
    options = "".join(
        f'<option value="{value}"{" selected" if value == progress else ""}>{label}</option>'
        for value, label in PROGRESS_LABELS.items()
    )
    return (
        f'<form class="progress-form" method="post" action="{url_for("booking_progress", booking_id=booking["id"])}">'
        f'<input type="hidden" name="return_to" value="{return_to}">'
        f'<select name="progress" onchange="this.form.submit()" aria-label="Progress of {esc(booking["activity"])}">'
        f'{options}</select><noscript><button type="submit">Save</button></noscript></form>'
    )


def _activity_history_grid(
    dated_bookings: list[tuple[date, dict]],
    selected_year: str,
    activity_colors: dict[str, str],
) -> str:
    if not dated_bookings:
        return '<div class="card"><p>No activities were found for this selection.</p></div>'

    if selected_year == "all":
        first_date = min(booking_date for booking_date, _ in dated_bookings)
        last_date = max(
            _booking_activity_end_date(booking)
            for _, booking in dated_bookings
        )
        today = date.today()
        if today >= first_date:
            last_date = max(last_date, today)
        current_year_end = date(today.year, 12, 31)
        last_date = max(last_date, current_year_end)
        first_day = first_date
        last_day = last_date
    else:
        year = int(selected_year)
        first_day = date(year, 1, 1)
        last_day = date(year, 12, 31)
    calendar_days = [
        first_day + timedelta(days=offset)
        for offset in range((last_day - first_day).days + 1)
    ]

    def activity_color_style(color: str) -> str:
        color = color.upper()
        if not re.fullmatch(r"#[0-9A-F]{6}", color):
            return ""
        red, green, blue = (int(color[index:index + 2], 16) for index in (1, 3, 5))
        text_color = "#000000" if (red * 299 + green * 587 + blue * 114) / 1000 >= 145 else "#FFFFFF"
        return f' style="background-color:{color};color:{text_color}"'

    bookings_by_vehicle: dict[int, list[tuple[date, dict]]] = {}
    vehicle_ids = set()
    for booking_date, booking in dated_bookings:
        end_date = _booking_activity_end_date(booking)
        if end_date < first_day or booking_date > last_day:
            continue
        visible_start = max(booking_date, first_day)
        visible_booking = (
            booking
            if visible_start == booking_date
            else {**booking, "planned_date": visible_start.isoformat()}
        )
        vehicle_id = booking["vehicle_id"]
        vehicle_ids.add(vehicle_id)
        bookings_by_vehicle.setdefault(vehicle_id, []).append((visible_start, visible_booking))

    year_headers = []
    month_headers = []
    week_headers = []
    day_headers = []
    start = 0
    while start < len(calendar_days):
        year = calendar_days[start].year
        end = start + 1
        while end < len(calendar_days) and calendar_days[end].year == year:
            end += 1
        year_headers.append(f'<th class="grid-year" colspan="{end - start}">{year}</th>')
        start = end
    start = 0
    while start < len(calendar_days):
        day = calendar_days[start]
        month_key = (day.year, day.month)
        end = start + 1
        while end < len(calendar_days):
            next_day = calendar_days[end]
            if (next_day.year, next_day.month) != month_key:
                break
            end += 1
        month_headers.append(
            f'<th class="grid-month grid-month-end" colspan="{end - start}">{day.strftime("%B")}</th>'
        )
        start = end
    current_iso = date.today().isocalendar()
    current_week = (current_iso.year, current_iso.week)
    start = 0
    while start < len(calendar_days):
        day = calendar_days[start]
        iso = day.isocalendar()
        week_key = (iso.year, iso.week)
        end = start + 1
        while end < len(calendar_days):
            next_iso = calendar_days[end].isocalendar()
            if (next_iso.year, next_iso.week) != week_key:
                break
            end += 1
        current_class = " grid-current-week" if week_key == current_week else ""
        current_label = '<span class="grid-current-label">CURRENT WEEK</span>' if current_class else ""
        week_headers.append(
            f'<th class="grid-week-col{current_class}" colspan="{end - start}">'
            f'<strong>KW{iso.week:02d}</strong>{current_label}</th>'
        )
        start = end
    for day_index, day in enumerate(calendar_days):
        iso = day.isocalendar()
        classes = []
        if day.weekday() >= 5:
            classes.append("grid-weekend")
        if (iso.year, iso.week) == current_week:
            classes.append("grid-current-week")
        if day_index == len(calendar_days) - 1 or (
            calendar_days[day_index + 1].year,
            calendar_days[day_index + 1].month,
        ) != (day.year, day.month):
            classes.append("grid-month-end")
        if day == date.today():
            classes.append("grid-today")
        class_attribute = " " + " ".join(classes) if classes else ""
        day_headers.append(
            f'<th class="grid-day-col{class_attribute}" title="{day:%A, %d %B %Y}">{day.day}</th>'
        )

    vehicle_rows = []
    for group_name, group_vehicles in _group_vehicles(list_vehicles(active_only=False)):
        visible_vehicles = [vehicle for vehicle in group_vehicles if vehicle["id"] in vehicle_ids]
        if not visible_vehicles:
            continue
        vehicle_rows.append(
            f'<tr class="grid-section-row"><th colspan="{len(calendar_days) + 1}">{esc(group_name)}</th></tr>'
        )
        for vehicle in visible_vehicles:
            lanes: list[list[tuple[date, dict]]] = []
            lane_ends: list[date] = []
            entries = sorted(
                bookings_by_vehicle[vehicle["id"]],
                key=lambda entry: (entry[0], _booking_activity_end_date(entry[1])),
            )
            for entry in entries:
                booking_date, booking = entry
                end_date = _booking_activity_end_date(booking)
                lane_index = next(
                    (index for index, lane_end in enumerate(lane_ends) if lane_end < booking_date),
                    len(lanes),
                )
                if lane_index == len(lanes):
                    lanes.append([])
                    lane_ends.append(date.min)
                lanes[lane_index].append(entry)
                lane_ends[lane_index] = end_date

            for lane_index, lane in enumerate(lanes):
                cells = []
                if lane_index == 0:
                    cells.append(
                        f'<td class="grid-vehicle-col" rowspan="{len(lanes)}">'
                        f'<a href="{url_for("vehicle_history", vehicle_id=vehicle["id"])}">'
                        f'{esc(vehicle["label"])}</a><span>VIN: {esc(vehicle["vin"] or "Not recorded")}</span></td>'
                    )
                entries_by_date = {booking_date: booking for booking_date, booking in lane}
                day_index = 0
                while day_index < len(calendar_days):
                    day = calendar_days[day_index]
                    iso = day.isocalendar()
                    month_end = day_index == len(calendar_days) - 1 or (
                        calendar_days[day_index + 1].year,
                        calendar_days[day_index + 1].month,
                    ) != (day.year, day.month)
                    booking = entries_by_date.get(day)
                    if booking is None:
                        cell_class = ["grid-day-cell"]
                        if day.weekday() >= 5:
                            cell_class.append("grid-weekend")
                        if (iso.year, iso.week) == current_week:
                            cell_class.append("grid-current-week")
                        if month_end:
                            cell_class.append("grid-month-end")
                        if day == date.today():
                            cell_class.append("grid-today")
                        cells.append(f'<td class="{" ".join(cell_class)}" aria-label="No activity"></td>')
                        day_index += 1
                        continue

                    span_end = min(_booking_activity_end_date(booking), last_day)
                    max_span = max(1, span_end.toordinal() - day.toordinal() + 1)
                    activity = booking["activity"].strip()
                    color = _booking_activity_color(booking, activity_colors)
                    status = _activity_status_label(booking["status"])
                    status_markup = f'<small>{esc(status)}</small>' if status else ""
                    progress_markup = f"<small>{_progress_badge(booking)}</small>" if _progress_badge(booking) else ""
                    description_markup = (
                        f'<small> — {esc(booking["description"])}</small>'
                        if booking["description"]
                        else ""
                    )
                    closed_class = " grid-progress-closed" if _is_closed_display(booking) else ""
                    label = (
                        f'<span class="grid-activity-label{closed_class}"{activity_color_style(color)}>'
                        f'{esc(activity)}{status_markup}{progress_markup}{description_markup}</span>'
                    )
                    cell_class = ["grid-day-cell", "grid-filled"]
                    if day.weekday() >= 5:
                        cell_class.append("grid-weekend")
                    if (iso.year, iso.week) == current_week:
                        cell_class.append("grid-current-week")
                    if month_end:
                        cell_class.append("grid-month-end")
                    if day == date.today():
                        cell_class.append("grid-today")
                    cells.append(
                        f'<td colspan="{max_span}" class="{" ".join(cell_class)}" aria-label="Activity from '
                        f'{day:%d %B %Y} through {span_end:%d %B %Y}, KW{iso.week:02d}-{iso.year}">{label}</td>'
                    )
                    day_index += max_span
                vehicle_rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        '<div class="history-grid-shell">'
        '<button type="button" class="grid-fullscreen-button history-grid-fullscreen-button" '
        'onclick="toggleHistoryGridFullscreen(this)">View grid full screen</button>'
        '<div class="history-grid-wrap"><table class="history-grid"><thead><tr>'
        '<th class="grid-vehicle-col" rowspan="4">Vehicle / VIN</th>'
        + "".join(year_headers)
        + '</tr><tr>'
        + "".join(month_headers)
        + '</tr><tr>'
        + "".join(week_headers)
        + '</tr><tr>'
        + "".join(day_headers)
        + '</tr></thead><tbody>'
        + "".join(vehicle_rows)
        + "</tbody></table></div></div>"
    )


def _booking_activity_date(booking: dict) -> date | None:
    planned_date = booking["planned_date"].strip()
    if planned_date:
        return date.fromisoformat(planned_date)
    match = re.fullmatch(r"KW(\d{1,2})(?:-(\d{4}))?", booking["period"].strip().upper())
    if not match:
        return None
    week = int(match.group(1))
    year = int(match.group(2)) if match.group(2) else date.today().isocalendar().year
    try:
        return date.fromisocalendar(year, week, 1)
    except ValueError:
        return None


def _booking_activity_end_date(booking: dict) -> date:
    start_date = _booking_activity_date(booking)
    if start_date is None:
        raise ValueError("A booking end date requires a valid start date.")
    raw_end_date = booking.get("planned_end_date", "").strip()
    if not raw_end_date:
        if not booking["planned_date"].strip():
            return start_date + timedelta(days=4)
        return start_date
    end_date = date.fromisoformat(raw_end_date)
    if end_date < start_date:
        raise ValueError("A booking end date cannot be before its start date.")
    return end_date


def _booking_date_range_label(booking: dict) -> str:
    start_date = _booking_activity_date(booking)
    if start_date is None:
        return "—"
    end_date = _booking_activity_end_date(booking)
    if end_date > start_date:
        return f"{start_date.isoformat()} – {end_date.isoformat()}"
    return start_date.isoformat()


def _activity_history_calendar(
    dated_bookings: list[tuple[date, dict]],
    selected_year: str,
    activity_colors: dict[str, str],
    month_value: str,
    vehicle_id: int | None = None,
    scope: str = "month",
    week_value: str = "",
) -> str:
    if scope not in ("month", "week"):
        abort(400, description="Choose the month or week calendar.")
    if not re.fullmatch(r"\d{4}-\d{2}", month_value):
        abort(400, description="Choose a valid calendar month.")
    try:
        month_start = date.fromisoformat(f"{month_value}-01")
    except ValueError:
        abort(400, description="Choose a valid calendar month.")
    if selected_year != "all" and month_start.year != int(selected_year):
        abort(400, description="Choose a month within the selected year.")

    def in_selected_year(start: date, end: date) -> bool:
        return selected_year == "all" or start.year <= int(selected_year) <= end.year

    next_month = (
        date(month_start.year + 1, 1, 1)
        if month_start.month == 12
        else date(month_start.year, month_start.month + 1, 1)
    )
    month_last = next_month - timedelta(days=1)
    if scope == "week":
        if week_value:
            week_match = re.fullmatch(r"(\d{4})-W(\d{2})", week_value)
            try:
                if not week_match:
                    raise ValueError
                week_start = date.fromisocalendar(int(week_match.group(1)), int(week_match.group(2)), 1)
            except ValueError:
                abort(400, description="Choose a valid calendar week.")
        else:
            anchor = date.today() if month_start <= date.today() <= month_last else month_start
            week_start = anchor - timedelta(days=anchor.weekday())
        calendar_start = week_start
        calendar_end = week_start + timedelta(days=6)
        if not in_selected_year(calendar_start, calendar_end):
            abort(400, description="Choose a week within the selected year.")
        week_month = calendar_start if in_selected_year(calendar_start, calendar_start) else calendar_end
        month_start = date(week_month.year, week_month.month, 1)
    else:
        calendar_start = month_start - timedelta(days=month_start.weekday())
        calendar_end = month_last + timedelta(days=6 - month_last.weekday())
    bookings_by_day: dict[date, list[dict]] = {}
    for booking_date, booking in dated_bookings:
        activity_end = _booking_activity_end_date(booking)
        first_visible = max(booking_date, calendar_start)
        last_visible = min(activity_end, calendar_end)
        day = first_visible
        while day <= last_visible:
            bookings_by_day.setdefault(day, []).append(booking)
            day += timedelta(days=1)

    def event_style(booking: dict) -> str:
        color = _booking_activity_color(booking, activity_colors)
        if not color:
            return ""
        red, green, blue = (int(color[index:index + 2], 16) for index in (1, 3, 5))
        foreground = "#000000" if (red * 299 + green * 587 + blue * 114) / 1000 >= 145 else "#FFFFFF"
        return f' style="background-color:{color};border-color:{color};color:{foreground}"'

    day_names = "".join(
        f'<th scope="col">{name}</th>'
        for name in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    )
    rows = []
    day = calendar_start
    while day <= calendar_end:
        cells = []
        for _ in range(7):
            classes = ["activity-calendar-day-cell"]
            if scope == "month" and day.month != month_start.month:
                classes.append("activity-calendar-other-month")
            if day.weekday() >= 5:
                classes.append("activity-calendar-weekend")
            if day == date.today():
                classes.append("activity-calendar-today")
            events = []
            for booking in sorted(
                bookings_by_day.get(day, []),
                key=lambda item: (item["vehicle_label"].casefold(), item["activity"].casefold()),
            ):
                activity = booking["activity"].strip()
                start_date = _booking_activity_date(booking)
                end_date = _booking_activity_end_date(booking)
                date_label = start_date.strftime("%d %b")
                if end_date > start_date:
                    date_label += f" – {end_date:%d %b}"
                booked_by = booking.get("created_by", "")
                # Excel imports have no known booker, so the calendar omits it for them.
                meta = [] if booked_by.startswith("excel") else [f"Booked by: {esc(booked_by or 'Unknown')}"]
                for badge in (_history_status_badge(booking["status"]), _progress_badge(booking)):
                    if badge:
                        meta.append(badge)
                meta_markup = f'<small>{" · ".join(meta)}</small>' if meta else ""
                closed_class = " progress-closed-event" if _is_closed_display(booking) else ""
                events.append(
                    f'<span class="activity-calendar-event{closed_class}"{event_style(booking)}>'
                    f'<strong>{esc(activity)}</strong>'
                    f'<small>{esc(booking["vehicle_label"])} · {esc(date_label)}</small>'
                    f'{meta_markup}</span>'
                )
            cells.append(
                f'<td class="{" ".join(classes)}"><span class="activity-calendar-day">'
                f'{day.strftime("%d %b") if scope == "week" else day.day}</span>'
                f'{"".join(events)}</td>'
            )
            day += timedelta(days=1)
        rows.append("<tr>" + "".join(cells) + "</tr>")

    def calendar_url(calendar_scope: str, month: str, week: str = "") -> str:
        params = {"view": "calendar", "month": month}
        if calendar_scope == "week":
            params.update(calendar="week", week=week)
        if vehicle_id is not None:
            return url_for("vehicle_history", vehicle_id=vehicle_id, **params)
        return url_for(
            "team_dashboard",
            section="history",
            year=selected_year,
            _anchor="activity-history",
            **params,
        )

    def week_value_for(day: date) -> str:
        iso_year, iso_week, _ = day.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"

    current_month_value = month_start.strftime("%Y-%m")
    current_week_value = week_value_for(calendar_start if scope == "week" else (
        date.today() if month_start <= date.today() <= month_last else month_start
    ))
    if scope == "week":
        previous_start = calendar_start - timedelta(days=7)
        next_start = calendar_start + timedelta(days=7)
        previous_link = (
            f'<a href="{calendar_url("week", current_month_value, week_value_for(previous_start))}" aria-label="Previous week">Previous</a>'
            if in_selected_year(previous_start, previous_start + timedelta(days=6))
            else '<span aria-hidden="true"></span>'
        )
        next_link = (
            f'<a href="{calendar_url("week", current_month_value, week_value_for(next_start))}" aria-label="Next week">Next</a>'
            if in_selected_year(next_start, next_start + timedelta(days=6))
            else '<span aria-hidden="true"></span>'
        )
        iso_year, iso_week, _ = calendar_start.isocalendar()
        calendar_title = (
            f"Week {iso_week}, {iso_year} · {calendar_start:%d %b} – {calendar_end:%d %b %Y}"
        )
    else:
        previous_month = (
            date(month_start.year - 1, 12, 1)
            if month_start.month == 1
            else date(month_start.year, month_start.month - 1, 1)
        )
        previous_link = (
            f'<a href="{calendar_url("month", previous_month.strftime("%Y-%m"))}" aria-label="Previous month">Previous</a>'
            if selected_year == "all" or previous_month.year == int(selected_year)
            else '<span aria-hidden="true"></span>'
        )
        next_link = (
            f'<a href="{calendar_url("month", next_month.strftime("%Y-%m"))}" aria-label="Next month">Next</a>'
            if selected_year == "all" or next_month.year == int(selected_year)
            else '<span aria-hidden="true"></span>'
        )
        calendar_title = f"{month_name[month_start.month]} {month_start.year}"
    scope_tabs = (
        '<div class="activity-calendar-scope" aria-label="Calendar range">'
        f'<a class="{"selected" if scope == "month" else ""}" href="{calendar_url("month", current_month_value)}">Month</a>'
        f'<a class="{"selected" if scope == "week" else ""}" href="{calendar_url("week", current_month_value, current_week_value)}">Week</a>'
        "</div>"
    )
    calendar_action = (
        url_for("vehicle_history", vehicle_id=vehicle_id)
        if vehicle_id is not None
        else url_for("team_dashboard")
    )
    # GET forms drop the action's query string, so the section and year must be form fields.
    year_field = (
        ""
        if vehicle_id is not None
        else '<input type="hidden" name="section" value="history">'
        f'<input type="hidden" name="year" value="{esc(selected_year)}">'
    )
    if scope == "week":
        week_constraints = (
            f' min="{week_value_for(date(int(selected_year), 1, 1))}" max="{week_value_for(date(int(selected_year), 12, 31))}"'
            if selected_year != "all" and vehicle_id is None
            else ""
        )
        picker = (
            '<input type="hidden" name="calendar" value="week">'
            f'<input type="hidden" name="month" value="{current_month_value}">'
            '<label class="sr-only" for="activity-calendar-week">Choose week</label>'
            f'<input id="activity-calendar-week" type="week" name="week" value="{current_week_value}"{week_constraints}>'
        )
    else:
        month_constraints = (
            f' min="{selected_year}-01" max="{selected_year}-12"'
            if selected_year != "all" and vehicle_id is None
            else ""
        )
        picker = (
            '<label class="sr-only" for="activity-calendar-month">Choose month</label>'
            f'<input id="activity-calendar-month" type="month" name="month" value="{current_month_value}"{month_constraints}>'
        )
    return (
        f'<section class="activity-calendar activity-calendar-{scope}" aria-label="Vehicle activity calendar">'
        '<div class="activity-calendar-nav">'
        + previous_link
        + f'<h2>{esc(calendar_title)}</h2>'
        + next_link
        + scope_tabs
        + f'<form method="get" action="{calendar_action}">'
        '<input type="hidden" name="view" value="calendar">'
        + year_field
        + picker
        + '<button type="submit">Go</button></form></div>'
        '<table class="activity-calendar-table"><thead><tr>'
        + day_names
        + '</tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></section>"
    )


@app.route("/planner/history")
def activity_history() -> str:
    return team_dashboard("history")


def _activity_history_section() -> str:
    bookings = [booking for booking in list_bookings(limit=5000) if booking["status"] != "rejected"]
    activity_colors = _activity_color_palette(bookings)
    dated_bookings = []
    for booking in bookings:
        booking_date = _booking_activity_date(booking)
        if booking_date is not None:
            dated_bookings.append((booking_date, booking))
    vehicle_usage = _vehicle_usage_section(list_vehicles(active_only=False), dated_bookings, date.today())
    years = sorted(
        {
            year
            for booking_date, booking in dated_bookings
            for year in range(booking_date.year, _booking_activity_end_date(booking).year + 1)
        },
        reverse=True,
    )
    default_year = str(date.today().year)
    if default_year not in {str(year) for year in years}:
        default_year = str(years[0]) if years else "all"
    selected_year = request.args.get("year", default_year)
    if selected_year != "all":
        if not selected_year.isdecimal() or int(selected_year) not in years:
            abort(400, description="Choose a year with activities.")
        year_start = date(int(selected_year), 1, 1)
        year_end = date(int(selected_year), 12, 31)
        dated_bookings = [
            (booking_date, booking)
            for booking_date, booking in dated_bookings
            if booking_date <= year_end and _booking_activity_end_date(booking) >= year_start
        ]

    weeks: dict[str, list[tuple[date, dict]]] = {}
    for booking_date, booking in dated_bookings:
        iso_year, iso_week, _ = booking_date.isocalendar()
        period = f"KW{iso_week:02d}-{iso_year}"
        weeks.setdefault(period, []).append((booking_date, booking))

    week_cards = []
    for index, (period, entries) in enumerate(
        sorted(weeks.items(), key=lambda item: min(entry[0] for entry in item[1]), reverse=True)
    ):
        entry_rows = []
        for booking_date, booking in sorted(
            entries, key=lambda entry: (entry[0], entry[1]["vehicle_label"].casefold())
        ):
            end_date = _booking_activity_end_date(booking)
            date_label = booking_date.strftime("%d %b")
            if end_date > booking_date:
                date_label += f" – {end_date:%d %b}"
            details = (
                f'<span class="form-hint">{esc(booking["description"])}</span>'
                if booking["description"]
                else ""
            )
            entry_rows.append(
                f'<li><span class="history-date">{esc(date_label)}</span>'
                f'<a class="history-vehicle" href="{url_for("vehicle_history", vehicle_id=booking["vehicle_id"])}">'
                f'{esc(booking["vehicle_label"])}</a>'
                f'<span class="history-vin">VIN: {esc(booking["vehicle_vin"] or "Not recorded")}</span>'
                f'<span class="history-activity">{_history_color_swatch(booking, activity_colors)}{esc(booking["activity"])} '
                f'{_history_status_badge(booking["status"])}{_progress_badge(booking)}'
                f'</span>{details}</li>'
            )
        opened = " open" if index == 0 else ""
        week_cards.append(
            f'<details class="week-card week-tone-{index % 5}"{opened}><summary>{esc(period)}'
            f'<span class="week-count">{len(entries)} activities</span></summary>'
            f'<ul class="history-entries">{"".join(entry_rows)}</ul></details>'
        )

    options = '<option value="all">All years</option>' + "".join(
        f'<option value="{year}" {"selected" if selected_year == str(year) else ""}>{year}</option>'
        for year in years
    )
    vehicle_count = len({booking["vehicle_label"] for _, booking in dated_bookings})
    summary = (
        f'<div class="history-stats">'
        f'<div class="history-stat"><strong>{len(dated_bookings)}</strong><span>Activities</span></div>'
        f'<div class="history-stat"><strong>{len(weeks)}</strong><span>Weeks with activity</span></div>'
        f'<div class="history-stat"><strong>{vehicle_count}</strong><span>Vehicles</span></div></div>'
    )
    selected_view = request.args.get("view", "grid")
    if selected_view not in ("grid", "timeline", "calendar"):
        abort(400, description="Choose the grid, timeline, or calendar history view.")
    selected_month = request.args.get("month", "").strip()
    if not selected_month:
        if selected_year == "all":
            selected_month = date.today().strftime("%Y-%m")
        else:
            year_number = int(selected_year)
            default_month = date.today().month if year_number == date.today().year else 1
            selected_month = f"{year_number:04d}-{default_month:02d}"
    view_tabs = (
        '<div class="view-tabs">'
        f'<a class="{"selected" if selected_view == "grid" else ""}" href="{url_for("team_dashboard", section="history", year=selected_year, view="grid")}">Excel-style grid</a>'
        f'<a class="{"selected" if selected_view == "timeline" else ""}" href="{url_for("team_dashboard", section="history", year=selected_year, view="timeline")}">Weekly timeline</a>'
        f'<a class="{"selected" if selected_view == "calendar" else ""}" href="{url_for("team_dashboard", section="history", year=selected_year, view="calendar", month=selected_month)}">Calendar</a>'
        "</div>"
    )
    if selected_view == "grid":
        history_content = _activity_history_grid(dated_bookings, selected_year, activity_colors)
    elif selected_view == "calendar":
        history_content = _activity_history_calendar(
            dated_bookings,
            selected_year,
            activity_colors,
            selected_month,
            scope=request.args.get("calendar", "month"),
            week_value=request.args.get("week", "").strip(),
        )
    else:
        history_content = (
            '<div class="week-timeline">'
            + "".join(week_cards)
            + "</div>"
            if week_cards
            else '<div class="card"><p>No activities were found for this selection.</p></div>'
        )
    body = (
        '<p class="planner-intro">New bookings appear here while pending and update as they are reviewed. Rejected bookings are excluded. '
        'In the Excel-style grid, colored cells mark weeks with activities.</p>'
        + vehicle_usage
        + '<form class="card" method="get" action="'
        + url_for("team_dashboard")
        + '"><label for="history-year">Filter by year</label>'
        + '<input type="hidden" name="section" value="history">'
        + f'<input type="hidden" name="view" value="{esc(selected_view)}">'
        + f'<select id="history-year" name="year">{options}</select> '
        + '<button type="submit">Show year</button></form>'
        + summary
        + view_tabs
        + history_content
    )
    return f'<section class="team-section" id="activity-history"><h2>Vehicle Activity History</h2>{body}</section>'


def _vehicle_usage_section(
    vehicles: list[dict],
    dated_bookings: list[tuple[date, dict]],
    today: date,
) -> str:
    last_used: dict[int, date] = {}
    for start_date, booking in dated_bookings:
        if start_date > today:
            continue
        last_activity_date = min(_booking_activity_end_date(booking), today)
        vehicle_id = booking["vehicle_id"]
        if vehicle_id not in last_used or last_activity_date > last_used[vehicle_id]:
            last_used[vehicle_id] = last_activity_date

    labels = (
        "Used in the last 30 days",
        "No activity for 31–60 days",
        "No activity for over 60 days",
        "No recorded use",
    )
    categorized: dict[str, list[tuple[dict, date | None]]] = {label: [] for label in labels}
    for vehicle in vehicles:
        last_activity_date = last_used.get(vehicle["id"])
        if last_activity_date is None:
            category = labels[3]
        else:
            days_since_use = (today - last_activity_date).days
            category = labels[0] if days_since_use <= 30 else labels[1] if days_since_use <= 60 else labels[2]
        categorized[category].append((vehicle, last_activity_date))

    cards = []
    for label in labels:
        rows = []
        for vehicle, last_activity_date in sorted(
            categorized[label], key=lambda entry: entry[0]["label"].casefold()
        ):
            last_used_label = last_activity_date.strftime("%d %b %Y") if last_activity_date else "No recorded use"
            rows.append(
                f'<li><a href="{url_for("vehicle_history", vehicle_id=vehicle["id"])}">'
                f'{esc(vehicle["label"])}</a><span>Last used: {esc(last_used_label)}</span></li>'
            )
        contents = (
            f'<ul class="vehicle-usage-list">{"".join(rows)}</ul>'
            if rows
            else '<p class="form-hint">No vehicles in this category.</p>'
        )
        cards.append(
            '<details class="history-stat vehicle-usage-card">'
            f'<summary><strong>{len(categorized[label])}</strong><span>{esc(label)}</span>'
            '<span class="vehicle-usage-toggle">View vehicle list</span></summary>'
            + contents
            + "</details>"
        )
    return (
        '<section class="card" aria-labelledby="vehicle-usage-heading">'
        '<h3 id="vehicle-usage-heading">Vehicle usage</h3>'
        '<p class="form-hint">Based on recorded activity dates up to today. Future and rejected bookings are excluded.</p>'
        + '<div class="history-stats">'
        + "".join(cards)
        + "</div>"
        + "</section>"
    )


@app.route("/planner/history/vehicle/<int:vehicle_id>")
def vehicle_history(vehicle_id: int) -> str:
    vehicle = get_vehicle(vehicle_id)
    if vehicle is None:
        abort(404)
    bookings = [
        booking for booking in list_bookings(vehicle_id=vehicle_id, limit=5000)
        if booking["status"] != "rejected"
    ]
    activity_colors = _activity_color_palette(bookings)
    dated_bookings = []
    for booking in bookings:
        booking_date = _booking_activity_date(booking)
        if booking_date is not None:
            dated_bookings.append((booking_date, booking))
    weeks: dict[str, list[tuple[date, dict]]] = {}
    for booking_date, booking in dated_bookings:
        iso_year, iso_week, _ = booking_date.isocalendar()
        weeks.setdefault(f"KW{iso_week:02d}-{iso_year}", []).append((booking_date, booking))
    selected_view = request.args.get("view", "timeline")
    if selected_view not in ("timeline", "calendar"):
        abort(400, description="Choose the weekly history or calendar view.")
    selected_month = request.args.get("month", "").strip() or date.today().strftime("%Y-%m")

    fields = (
        ("Name", vehicle["label"]),
        ("Logger", vehicle["logger"]),
        ("Baureihe", vehicle["series"]),
        ("Cluster", vehicle["cluster"]),
        ("Markt", vehicle["market"]),
        ("VR-Stand", vehicle["vr_stand"]),
        ("Verwendungszweck", vehicle["purpose"]),
        ("VIN", vehicle["vin"]),
        ("Comment", vehicle.get("comment", "")),
    )
    details = "".join(
        f'<div class="vehicle-detail"><span>{esc(label)}</span><strong>{esc(value or "Not recorded")}</strong></div>'
        for label, value in fields
    )
    cards = []
    for index, (period, entries) in enumerate(
        sorted(weeks.items(), key=lambda item: min(entry[0] for entry in item[1]), reverse=True)
    ):
        entry_rows = []
        for booking_date, booking in sorted(entries, key=lambda entry: entry[0]):
            description = (
                f'<span class="form-hint">{esc(booking["description"])}</span>'
                if booking["description"]
                else ""
            )
            entry_rows.append(
                f'<li><span class="history-date">{booking_date.strftime("%a, %d %b %Y")}</span>'
                f'<span class="history-activity">{_history_color_swatch(booking, activity_colors)}{esc(booking["activity"])}'
                f'{_history_status_badge(booking["status"])}{_progress_badge(booking)}</span>'
                f'{description}</li>'
            )
        cards.append(
            f'<details class="week-card week-tone-{index % 5}"{" open" if index == 0 else ""}>'
            f'<summary>{esc(period)}<span class="week-count">{len(entries)} activities</span></summary>'
            f'<ul class="history-entries">{"".join(entry_rows)}</ul></details>'
        )
    view_tabs = (
        '<div class="view-tabs">'
        f'<a class="{"selected" if selected_view == "timeline" else ""}" '
        f'href="{url_for("vehicle_history", vehicle_id=vehicle_id, view="timeline")}">Weekly history</a>'
        f'<a class="{"selected" if selected_view == "calendar" else ""}" '
        f'href="{url_for("vehicle_history", vehicle_id=vehicle_id, view="calendar", month=selected_month)}">Calendar</a>'
        "</div>"
    )
    vehicle_history_content = (
        _activity_history_calendar(
            dated_bookings,
            "all",
            activity_colors,
            selected_month,
            vehicle_id=vehicle_id,
            scope=request.args.get("calendar", "month"),
            week_value=request.args.get("week", "").strip(),
        )
        if selected_view == "calendar"
        else (
            '<div class="week-timeline">' + "".join(cards) + "</div>"
            if cards
            else '<div class="card"><p>No dated activities were found for this vehicle.</p></div>'
        )
    )
    body = (
        f'<p class="planner-intro">Excel history and bookings for {esc(vehicle["label"])}, '
        f'shown by week with their current status.</p>'
        f'<div class="card"><h3>Vehicle details</h3><div class="vehicle-details">{details}</div></div>'
        f'<h3>Activity history ({len(dated_bookings)} activities)</h3>'
        + view_tabs
        + vehicle_history_content
        + f'<p><a class="button" href="{url_for("activity_history")}">Back to activity history</a></p>'
    )
    return render_page(f"Vehicle History — {vehicle['label']}", body)


def _parse_booking_form() -> tuple[dict, list[date]]:
    activity_color = request.form.get("activity_color", "").strip()
    if activity_color == "custom":
        activity_color = request.form.get("custom_activity_color", "").strip()
        if not activity_color:
            raise ValueError("Choose a valid activity color.")
    activity_color = activity_color.upper()
    if activity_color and not re.fullmatch(r"#[0-9A-F]{6}", activity_color):
        raise ValueError("Choose a valid activity color.")
    try:
        vehicle_id = int(request.form.get("vehicle_id", ""))
    except ValueError as exc:
        raise ValueError("Select an active vehicle.") from exc
    period = request.form.get("period", "").strip().upper()
    period_match = re.fullmatch(r"KW(\d{1,2})(?:-(\d{4}))?", period) if period else None
    if period and not period_match:
        raise ValueError("Week must look like KW47 or KW47-2026.")
    planned_date = request.form.get("planned_date", "").strip()
    planned_end_date = request.form.get("planned_end_date", "").strip()
    selected_date = None
    if planned_date:
        selected_date = date.fromisoformat(planned_date)
    if planned_end_date:
        if selected_date is None:
            raise ValueError("Choose an exact start date before setting an end date.")
        selected_end_date = date.fromisoformat(planned_end_date)
        if selected_end_date < selected_date:
            raise ValueError("The end date must be on or after the start date.")
    else:
        selected_end_date = selected_date
    if selected_date:
        iso_year, iso_week, _ = selected_date.isocalendar()
        period = f"KW{iso_week:02d}-{iso_year}"
    elif period_match:
        week = int(period_match.group(1))
        year = int(period_match.group(2)) if period_match.group(2) else date.today().isocalendar().year
        try:
            date.fromisocalendar(year, week, 1)
        except ValueError as exc:
            raise ValueError("Week is not valid for the specified year.") from exc
    else:
        raise ValueError("Enter a planned week or a start date.")
    weekend_dates = []
    if selected_date and selected_end_date:
        weekend_dates = [
            selected_date + timedelta(days=offset)
            for offset in range((selected_end_date - selected_date).days + 1)
            if (selected_date + timedelta(days=offset)).weekday() >= 5
        ]
    values = {
        "vehicle_id": vehicle_id,
        "activity": request.form.get("activity", ""),
        "period": period,
        "planned_date": planned_date,
        "planned_end_date": planned_end_date,
        "description": request.form.get("description", ""),
        "activity_color": activity_color,
    }
    return values, weekend_dates


def _weekend_confirmation_page(
    values: dict, weekend_dates: list[date], action: str, cancel_url: str, verb: str,
    notify_to: tuple[str, ...] = (),
) -> str:
    hidden_fields = "".join(
        f'<input type="hidden" name="{name}" value="{esc(value)}">'
        for name, value in values.items()
    ) + "".join(f'<input type="hidden" name="notify_to" value="{esc(address)}">' for address in notify_to)
    weekend_names = ", ".join(f'{day:%A, %d %B %Y}' for day in weekend_dates)
    return render_page(
        "Confirm weekend booking",
        '<div class="card"><p class="warn">This activity includes weekend date(s): '
        f'{weekend_names}. Weekend activities are included in the Excel-style calendar. '
        f'Do you want to {verb} this booking?</p>'
        f'<form method="post" action="{action}">{hidden_fields}'
        '<button type="submit" name="confirm_weekend" value="1">Confirm weekend booking</button> '
        f'<a class="button" href="{cancel_url}">Cancel</a></form></div>',
    )


def _optional_mail_settings():
    try:
        settings = load_mail_settings()
    except ValueError:
        return None
    return settings if settings.enabled else None


def _selected_notification_recipients() -> tuple[str, ...]:
    settings = _optional_mail_settings()
    if settings is None:
        return ()
    allowed = {address.casefold(): address for address in settings.recipients}
    selected = []
    for address in request.form.getlist("notify_to"):
        configured = allowed.get(address.strip().casefold())
        if configured is None:
            raise ValueError("Choose recipients from the approver list.")
        if configured not in selected:
            selected.append(configured)
    if not selected:
        raise ValueError("Choose at least one approver to notify by email.")
    return tuple(selected)


def _notification_recipient_fields() -> str:
    settings = _optional_mail_settings()
    if settings is None:
        return ""
    preselect = len(settings.recipients) == 1
    options = "".join(
        f'<label><input type="checkbox" name="notify_to" value="{esc(address)}"{" checked" if preselect else ""} '
        'onchange="updateApproverSummary(this)"> '
        f'{esc(address)}</label>'
        for address in settings.recipients
    )
    summary = esc(settings.recipients[0]) if preselect else "Select approver(s)"
    return (
        '<fieldset class="activity-color-options"><legend>Send email notification to</legend>'
        '<details class="approver-dropdown"><summary data-placeholder="Select approver(s)">'
        f'{summary}</summary><div class="approver-dropdown-options">{options}</div></details>'
        '<span class="form-hint">Choose at least one approver.</span></fieldset>'
    )


@app.route("/planner/bookings/create", methods=["POST"])
def submit_booking() -> str:
    try:
        values, weekend_dates = _parse_booking_form()
        notify_to = _selected_notification_recipients()
        if weekend_dates and request.form.get("confirm_weekend") != "1":
            return _weekend_confirmation_page(
                values, weekend_dates, url_for("submit_booking"), url_for("campaign_planner"), "submit",
                notify_to,
            )
        booking_id = create_booking(
            values["vehicle_id"],
            values["activity"],
            values["period"],
            values["planned_date"],
            values["description"],
            "",
            _current_actor(),
            planned_end_date=values["planned_end_date"],
            activity_color=values["activity_color"],
        )
    except (ValueError, sqlite3.IntegrityError) as exc:
        return render_page("Booking not submitted", f'<p class="warn">{esc(exc)}</p><p><a href="{url_for("campaign_planner")}">Back</a></p>'), 400
    actor = _current_actor()
    planned_date = values["planned_date"]
    planned_end_date = values["planned_end_date"]
    details = (
        f"Vehicle ID {values['vehicle_id']}; activity {values['activity'].strip()}; "
        f"dates {planned_date or values['period']}"
        + (f" through {planned_end_date}" if planned_end_date else "")
        + f"; color {values['activity_color'] or 'automatic'}"
        + (f"; email to {', '.join(notify_to)}" if notify_to else "")
    )
    record_audit_event(actor, f"Submitted booking #{booking_id}", details)
    log_event(f"Campaign booking #{booking_id} submitted by {actor}.")
    _notify_booking_submitted(booking_id, notify_to)
    session["team_notice"] = "Booking submitted and waiting for team review. No Jira ticket was created."
    return redirect(url_for("team_dashboard", section="testing", _anchor="my-bookings"))


def _app_url(path: str, base_url: str) -> str:
    return (base_url or request.url_root.rstrip("/")) + path


def _notify_booking_submitted(booking_id: int, recipients: tuple[str, ...]) -> None:
    if not recipients:
        return
    try:
        settings = load_mail_settings()
        if not settings.enabled:
            return
        booking = get_booking(booking_id)
        if booking is None:
            return
        booking["date_label"] = _booking_date_range_label(booking)
        message = build_booking_submitted_email(
            settings, booking, _app_url(url_for("booking_review"), settings.app_base_url), recipients
        )
    except Exception as exc:
        log_event(f"Booking #{booking_id} notification not prepared ({type(exc).__name__}: {exc}).", "error")
        return

    def deliver() -> None:
        try:
            send_email(settings, message)
            log_event(f"Booking #{booking_id} notification sent to {len(recipients)} approver(s).")
        except Exception as exc:
            log_event(f"Booking #{booking_id} notification failed ({type(exc).__name__}).", "error")

    if app.testing:
        deliver()
    else:
        threading.Thread(target=deliver, name=f"booking-{booking_id}-email", daemon=True).start()


@app.route("/planner/bookings/<int:booking_id>/edit", methods=["GET", "POST"])
def edit_booking(booking_id: int) -> str:
    booking = get_booking(booking_id)
    if booking is None:
        abort(404)
    back_url = url_for("team_dashboard", section="automation", _anchor="review-bookings")
    if booking["status"] not in ("pending", "approved") or booking["jira_issue_key"]:
        return render_page(
            "Booking cannot be edited",
            '<p class="warn">Only pending or approved bookings without a Jira ticket can be edited.</p>'
            f'<p><a href="{back_url}">Back to review</a></p>',
        ), 409
    if request.method == "GET":
        return render_page(
            f"Edit booking #{booking_id}",
            f'<p class="planner-intro">Submitted by {esc(booking["created_by"])}. Status: '
            f'{esc(booking["status"].title())}. Saving keeps the current review status.</p>'
            f'<form class="card planner-form" method="post" action="{url_for("edit_booking", booking_id=booking_id)}">'
            + _booking_form_fields(list_vehicles(), booking)
            + '<button class="primary-action" type="submit">Save booking changes</button></form>'
            f'<p><a href="{back_url}">Back to review</a></p>',
        )
    try:
        values, weekend_dates = _parse_booking_form()
        if weekend_dates and request.form.get("confirm_weekend") != "1":
            return _weekend_confirmation_page(
                values, weekend_dates, url_for("edit_booking", booking_id=booking_id),
                url_for("edit_booking", booking_id=booking_id), "save",
            )
        changed = update_booking(
            booking_id,
            values["vehicle_id"],
            values["activity"],
            values["period"],
            values["planned_date"],
            values["description"],
            planned_end_date=values["planned_end_date"],
            activity_color=values["activity_color"],
        )
    except (ValueError, sqlite3.IntegrityError) as exc:
        return render_page(
            "Booking not updated",
            f'<p class="warn">{esc(exc)}</p><p><a href="{url_for("edit_booking", booking_id=booking_id)}">Back</a></p>',
        ), 400
    actor = _current_actor()
    if changed:
        labels = {
            "vehicle_id": "vehicle", "activity": "activity", "period": "week", "planned_date": "start date",
            "planned_end_date": "end date", "description": "notes", "activity_color": "color",
        }
        record_audit_event(
            actor,
            f"Edited booking #{booking_id}",
            "Changed fields: " + ", ".join(labels[name] for name in changed),
        )
        log_event(f"Booking #{booking_id} edited by {actor}.")
        session["team_notice"] = f"Booking #{booking_id} updated."
    else:
        session["team_notice"] = f"No changes were made to booking #{booking_id}."
    return redirect(back_url)


@app.route("/planner/review")
def booking_review() -> str:
    return team_dashboard("automation")


def _booking_review_section() -> str:
    requested_status = request.args.get("status", "actionable")
    statuses = ("pending", "approved", "creating", "creation_error")
    if requested_status == "actionable":
        bookings = [
            booking for booking in list_bookings(statuses=statuses, limit=500)
            if _booking_progress(booking) != "closed"
        ]
    elif requested_status == "all":
        bookings = list_bookings(limit=1000)
    elif requested_status in ("pending", "approved", "rejected", "historical", "creating", "created", "creation_error"):
        bookings = list_bookings(status=requested_status, limit=1000)
    else:
        abort(400, description="Unknown booking status filter.")
    filters = "".join(
        f'<a href="{url_for("team_dashboard", section="automation", status=key)}">{label}</a>'
        for key, label in (
            ("actionable", "Actionable"), ("pending", "Pending"), ("approved", "Approved"),
            ("created", "Created"), ("rejected", "Rejected"), ("historical", "Historical"),
            ("all", "All"),
        )
    )
    return (
        '<section class="team-section" id="review-bookings"><h2>Automation Team: Review bookings and Create Jira tickets</h2>'
        f'<p class="planner-intro">Review weekly submissions. Approval does not create a Jira ticket; ticket creation requires a separate preview and confirmation.</p>'
        f'<div class="review-filters" aria-label="Filter bookings">{filters}</div>'
        + _booking_table(bookings, review=True)
        + "</section>"
    )


@app.route("/planner/bookings/<int:booking_id>/review", methods=["POST"])
def review_booking_action(booking_id: int) -> str:
    decision = request.form.get("decision", "")
    if decision not in ("approved", "rejected", "pending"):
        abort(400, description="Choose approve, reject, or return to pending.")
    try:
        review_booking(booking_id, decision, _current_actor())
    except ValueError as exc:
        return render_page("Review booking", f'<p class="warn">{esc(exc)}</p><p><a href="{url_for("booking_review")}">Back</a></p>'), 409
    actor = _current_actor()
    record_audit_event(actor, f"Booking {decision}", f"Booking #{booking_id}")
    log_event(f"Booking #{booking_id} {decision} by {actor}.")
    return redirect(url_for("team_dashboard", section="automation", _anchor="review-bookings"))


@app.route("/planner/bookings/<int:booking_id>/progress", methods=["POST"])
def booking_progress(booking_id: int) -> str:
    section = "automation" if request.form.get("return_to") == "automation" else "testing"
    back = url_for("team_dashboard", section=section,
                   _anchor="review-bookings" if section == "automation" else "my-bookings")
    progress = request.form.get("progress", "")
    if progress not in PROGRESS_LABELS:
        abort(400, description="Choose Not started, In progress, or Closed.")
    booking = get_booking(booking_id)
    if booking is None:
        abort(404)
    try:
        previous = update_booking_progress(booking_id, progress)
    except ValueError as exc:
        return render_page("Progress not updated", f'<p class="warn">{esc(exc)}</p><p><a href="{back}">Back</a></p>'), 409
    if previous != progress:
        actor = _current_actor()
        record_audit_event(
            actor,
            "Changed booking progress",
            f"Booking #{booking_id}; {PROGRESS_LABELS.get(previous, previous)} → {PROGRESS_LABELS[progress]}",
        )
        log_event(f"Booking #{booking_id} marked {PROGRESS_LABELS[progress]} by {actor}.")
        session["team_notice"] = f"{booking['activity']} ({booking['vehicle_label']}) marked {PROGRESS_LABELS[progress]}."
    return redirect(back)


@app.route("/planner/bookings/<int:booking_id>/retry", methods=["POST"])
def retry_failed_booking(booking_id: int) -> str:
    if request.form.get("verified_no_ticket") != "1":
        abort(400, description="Verify in Jira that no ticket was created before retrying.")
    try:
        reapprove_failed_booking(booking_id)
    except ValueError as exc:
        return render_page("Booking not re-approved", f'<p class="warn">{esc(exc)}</p><p><a href="{url_for("booking_review")}">Back</a></p>'), 409
    actor = _current_actor()
    record_audit_event(actor, "Booking re-approved for retry", f"Booking #{booking_id}; Jira absence verified")
    log_event(f"Booking #{booking_id} re-approved for retry by {actor} after Jira check.")
    return redirect(url_for("team_dashboard", section="automation", _anchor="review-bookings"))


@app.route("/planner/bookings/<int:booking_id>/create-ticket", methods=["GET", "POST"])
def create_booking_ticket(booking_id: int) -> str:
    booking = get_booking(booking_id)
    if booking is None:
        abort(404)
    if booking["status"] != "approved":
        return render_page("Cannot create Jira ticket", '<p class="warn">Only an approved booking can be turned into a Jira ticket.</p>'
                           + f'<p><a href="{url_for("booking_review")}">Back to review</a></p>'), 409
    template_names = sorted(
        path.stem for path in TEMPLATES_DIR.glob("*.yaml")
        if re.fullmatch(r"[a-zA-Z0-9_-]+", path.stem)
    )
    if request.method == "POST":
        if request.form.get("confirm") != "1":
            abort(400, description="Explicit confirmation is required to create a Jira ticket.")
        checklist_template = request.form.get("checklist_template", "").strip()
    else:
        checklist_template = request.args.get("checklist_template", booking["checklist_template"]).strip()
    if checklist_template and checklist_template not in template_names:
        return render_page(
            "Checklist selection failed",
            '<p class="warn">Select an available checklist template.</p>'
            f'<p><a href="{url_for("create_booking_ticket", booking_id=booking_id, checklist_template="")}">Choose a template</a></p>',
        ), 400
    if request.method == "POST" and not checklist_template:
        abort(400, description="Select and preview a checklist template before creating a ticket.")
    if not template_names:
        return render_page(
            "Checklist selection unavailable",
            '<p class="warn">No checklist templates are available. Contact an admin.</p>'
            f'<p><a href="{url_for("booking_review")}">Back to review</a></p>',
        ), 400

    template_options = "".join(
        f'<option value="{esc(name)}" {"selected" if name == checklist_template else ""}>{esc(name)}</option>'
        for name in template_names
    )
    selection_form = (
        f'<form method="get" action="{url_for("create_booking_ticket", booking_id=booking_id)}">'
        '<label>Checklist template <select name="checklist_template" required>'
        f'<option value="">Select a template</option>{template_options}</select></label> '
        '<button type="submit">Preview checklist</button></form>'
    )
    if not checklist_template:
        return render_page(
            "Choose checklist template",
            f'<p><strong>Vehicle:</strong> {esc(booking["vehicle_label"])}<br>'
            f'<strong>Activity:</strong> {esc(booking["activity"])}</p>'
            + selection_form
            + f'<p><a href="{url_for("booking_review")}">Back to review</a></p>',
        )
    project = os.environ.get("JIRA_PROJECT_KEY", "PHUB").strip() or "PHUB"
    summary = f"{booking['period']} - {booking['activity']} - {booking['vehicle_label']}"
    description = (
        "Created from an approved campaign booking in the Test Manager.\n"
        f"Vehicle: {booking['vehicle_label']}\n"
        f"VIN: {booking['vehicle_vin']}\n"
        f"Week: {booking['period']}\n"
        f"Planned date(s): {_booking_date_range_label(booking)}\n"
        f"Activity: {booking['activity']}\n"
        f"Description: {booking['description']}\n"
        f"Checklist template: {checklist_template}\n"
        f"Booking ID: {booking_id}\n"
        f"Jira Assets reference: {booking['jira_asset_key']}\n"
    )
    try:
        checklist = render_test_checklist(
            checklist_template,
            TEMPLATES_DIR,
            week=booking["period"],
            vehicle=booking["vehicle_label"],
            activity=booking["activity"],
        )
    except (OSError, ValueError, KeyError) as exc:
        return render_page("Checklist preview failed", f'<p class="warn">Could not render the selected checklist: {esc(exc)}</p>'
                           + f'<p><a href="{url_for("booking_review")}">Back to review</a></p>'), 400
    if request.method == "GET":
        return render_page(
            "Preview Jira ticket",
            f'<p><strong>Vehicle:</strong> {esc(booking["vehicle_label"])}<br>'
            f'<strong>Activity:</strong> {esc(booking["activity"])}<br>'
            f'<strong>Week/date:</strong> {esc(booking["period"])} / {esc(_booking_date_range_label(booking))}<br>'
            f'<strong>Project:</strong> {esc(project)}<br><strong>Summary:</strong> {esc(summary)}</p>'
            + selection_form
            + f'<h3>Checklist preview ({esc(checklist_template)})</h3><pre>{esc(checklist)}</pre>'
            "<p>This action creates a Jira issue and attempts to write its checklist. It does not create or assign an Agile sprint.</p>"
            f'<form method="post" action="{url_for("create_booking_ticket", booking_id=booking_id)}">'
            f'<input type="hidden" name="checklist_template" value="{esc(checklist_template)}">'
            '<label><input type="checkbox" name="confirm" value="1" required> I reviewed this booking and want to create one Jira ticket.</label>'
            '<button type="submit">Confirm and create Jira ticket</button></form>'
            f'<p><a href="{url_for("booking_review")}">Back to review</a></p>',
        )

    try:
        config = load_config()
    except RuntimeError as exc:
        return render_page("Jira not configured", f'<p class="warn">{esc(exc)}</p><p><a href="{url_for("booking_review")}">Back to review</a></p>'), 503
    client = JiraClient(config)
    if not claim_booking_creation(booking_id, checklist_template=checklist_template):
        client.close()
        return render_page("Ticket creation already started", '<p class="warn">This booking is no longer available for ticket creation. Check its status and Jira before retrying.</p>'
                           + f'<p><a href="{url_for("booking_review")}">Back to review</a></p>'), 409
    record_audit_event(_current_actor(), "Selected checklist template", f"Booking #{booking_id}; template {checklist_template}")
    try:
        vehicle = Vehicle(
            label=booking["vehicle_label"],
            vin=booking["vehicle_vin"],
            series=booking["series"],
            cluster=booking["cluster"],
            market=booking["market"],
            scope=booking["purpose"],
            key=booking["jira_asset_key"],
        )
        result = client.create_issue(
            project,
            "Task",
            summary,
            description,
            confirm=True,
            extra_fields=vehicle_link_fields(vehicle),
        )
        issue_key = result.get("key")
        if not issue_key:
            raise RuntimeError("Jira did not return a created issue key.")
        complete_booking_creation(booking_id, issue_key)
    except Exception as exc:
        fail_booking_creation(booking_id, type(exc).__name__)
        record_audit_event(_current_actor(), "Jira ticket creation failed", f"Booking #{booking_id}; {type(exc).__name__}")
        log_event(f"Ticket creation for booking #{booking_id} failed ({type(exc).__name__}); verify Jira before retrying.", "error")
        return render_page(
            "Ticket creation needs attention",
            '<p class="warn">The Jira request did not complete cleanly. The booking is locked against automatic retry to avoid duplicate tickets. Check Jira for a partially created issue before contacting an admin.</p>'
            f'<p>Error type: {esc(type(exc).__name__)}</p><p><a href="{url_for("booking_review")}">Back to review</a></p>',
        ), 502
    finally:
        client.close()

    checklist_pending = False
    try:
        with _CHECKLIST_LOCK:
            write_checklists_via_browser(config.effective_web_base_url, [(issue_key, checklist)])
            verification_client = JiraClient(config)
            try:
                if not verification_client.get_checklist(issue_key).strip():
                    raise RuntimeError("Jira checklist read-back was empty.")
            finally:
                verification_client.close()
    except Exception as exc:
        checklist_pending = True
        try:
            with _CHECKLIST_LOCK:
                save_pending_checklists([(issue_key, checklist)])
        except (OSError, ValueError) as save_error:
            log_event(
                f"Checklist write and pending-checklist save failed for {issue_key} "
                f"({type(exc).__name__}; {type(save_error).__name__}).",
                "error",
            )
        else:
            log_event(
                f"Checklist for {issue_key} queued for retry ({type(exc).__name__}).",
                "error",
            )

    actor = _current_actor()
    log_event(f"Created Jira ticket {issue_key} from booking #{booking_id} by {actor}.")
    record_audit_event(_current_actor(), "Created Jira ticket", f"Booking #{booking_id}; ticket {issue_key}")
    checklist_notice = (
        "<p class=\"warn\">The ticket was created, but its checklist was not confirmed. "
        "It was queued for retry; check the app host's pending_checklists.json and refresh its Jira browser login if needed.</p>"
        if checklist_pending else "<p class=\"msg\">Checklist written and verified.</p>"
    )
    return render_page(
        "Jira ticket created",
        f'<p class="msg">Created <a href="{esc(config.effective_web_base_url)}/jira/browse/{esc(issue_key)}" target="_blank">{esc(issue_key)}</a>. '
        "The ticket key is stored on the booking.</p>"
        f"{checklist_notice}"
        f'<p><a href="{url_for("booking_review")}">Back to review</a></p>',
    )


def _vehicle_fields_form(vehicle: dict | None = None, *, include_section: bool = False) -> str:
    vehicle = vehicle or {}
    field_specs = (
        ("label", "Name", True),
        ("series", "Baureihe", False),
        ("cluster", "Cluster", False),
        ("market", "Markt", False),
        ("vr_stand", "VR-Stand", False),
        ("purpose", "Verwendungszweck", False),
        ("vin", "VIN", False),
    )
    fields = "".join(
        f'<label>{label}<input type="text" name="{name}" value="{esc(vehicle.get(name, ""))}" {"required" if required else ""}></label>'
        for name, label, required in field_specs
    )
    fields += (
        f'<label class="vehicle-comment-field">Comment <textarea name="comment" rows="3" maxlength="{MAX_VEHICLE_COMMENT_LENGTH}" '
        f'placeholder="Optional notes about this vehicle">{esc(vehicle.get("comment", ""))}</textarea></label>'
    )
    logger = vehicle.get("logger", "")
    logger_options = "".join(
        f'<option value="{value}" {"selected" if logger == value else ""}>{label}</option>'
        for value, label in (("", "Not specified"), ("Yes", "Yes"), ("No", "No"))
    )
    section_field = ""
    if include_section:
        sections = list(dict.fromkeys(
            item["group_name"] for item in list_vehicles(active_only=False) if item["group_name"]
        ))
        options = "".join(f'<option value="{esc(section)}"></option>' for section in sections)
        section_field = (
            '<label>Section <input type="text" name="group_name" list="vehicle-sections" '
            f'value="{esc(vehicle.get("group_name", ""))}" placeholder="Choose or enter a section" required>'
            f'<datalist id="vehicle-sections">{options}</datalist></label>'
        )
    return fields + f'<label>Logger device attached? <select name="logger">{logger_options}</select></label>' + section_field


@app.route("/vehicles")
def vehicle_management() -> str:
    return team_dashboard("vehicles")


def _vehicle_comment_editor(vehicle: dict) -> str:
    comment = vehicle.get("comment", "")
    summary = (
        f'<span class="vehicle-comment-text">{esc(comment)}</span>'
        if comment
        else '<span class="form-hint">Add comment</span>'
    )
    return (
        f'<details class="vehicle-comment"><summary title="Edit comment">{summary}</summary>'
        f'<form method="post" action="{url_for("vehicle_comment", vehicle_id=vehicle["id"])}">'
        f'<textarea name="comment" rows="3" maxlength="{MAX_VEHICLE_COMMENT_LENGTH}" '
        f'aria-label="Comment for {esc(vehicle["label"])}">{esc(comment)}</textarea>'
        '<button type="submit">Save comment</button></form></details>'
    )


@app.route("/vehicles/<int:vehicle_id>/comment", methods=["POST"])
def vehicle_comment(vehicle_id: int) -> str:
    vehicle = get_vehicle(vehicle_id)
    if vehicle is None:
        abort(404)
    comment = request.form.get("comment", "").strip()
    try:
        update_vehicle_comment(vehicle_id, comment)
    except ValueError as exc:
        return render_page(
            "Comment not saved",
            f'<p class="warn">{esc(exc)}</p><p><a href="{url_for("vehicle_management")}">Back to vehicles</a></p>',
        ), 400
    if comment != vehicle.get("comment", ""):
        actor = _current_actor()
        record_audit_event(actor, "Updated vehicle comment", f"Vehicle ID {vehicle_id}; {vehicle['label']}")
        log_event(f"Vehicle #{vehicle_id} comment updated by {actor}.")
    show = {} if vehicle["active"] else {"show": "inactive"}
    return redirect(url_for("team_dashboard", section="vehicles", **show, _anchor="vehicle-details"))


def _vehicle_management_section() -> str:
    all_vehicles = list_vehicles(active_only=False)
    show_inactive = request.args.get("show") == "inactive"
    vehicles = [vehicle for vehicle in all_vehicles if bool(vehicle["active"]) != show_inactive]
    rows = []
    for group_name, group_vehicles in _group_vehicles(vehicles):
        rows.append(f'<tr class="vehicle-group-header"><th colspan="10">{esc(group_name)}</th></tr>')
        rows.extend(
            f'<tr><td>{esc(vehicle["label"])}</td><td>{esc(vehicle["logger"] or "—")}</td>'
            f'<td>{esc(vehicle["series"])}</td><td>{esc(vehicle["cluster"])}</td>'
            f'<td>{esc(vehicle["market"])}</td><td>{esc(vehicle["vr_stand"])}</td>'
            f'<td>{esc(vehicle["purpose"])}</td><td>{esc(vehicle["vin"])}</td>'
            f'<td>{_vehicle_comment_editor(vehicle)}</td><td><a href="{url_for("vehicle_edit", vehicle_id=vehicle["id"])}">Edit</a>'
            f'{_vehicle_reactivate_button(vehicle) if show_inactive else ""}</td></tr>'
            for vehicle in group_vehicles
        )
    list_title = "Inactive vehicles" if show_inactive else "Active vehicles"
    switch_url = url_for("vehicle_management") if show_inactive else url_for("vehicle_management", show="inactive")
    switch_label = "Show active vehicles" if show_inactive else "Show inactive vehicles"
    table_content = (
        f'<div class="table-wrap"><table><tr><th>Name</th><th>Logger</th><th>Baureihe</th>'
        f'<th>Cluster</th><th>Markt</th><th>VR-Stand</th><th>Verwendungszweck</th><th>VIN</th>'
        f'<th>Comment</th><th></th></tr>{"".join(rows)}</table></div>'
        if rows
        else f'<p class="form-hint">No {list_title.casefold()} to display.</p>'
    )
    status_control = (
        ""
        if show_inactive
        else _vehicle_deactivate_form([vehicle for vehicle in all_vehicles if vehicle["active"]])
    )
    body = f"""
    <p>Users maintain current/new vehicle records.</p>
    <div class="vehicle-management-heading"><h2>{list_title}</h2>{status_control}</div>
    <p class="vehicle-list-switch"><a href="{switch_url}">{switch_label}</a></p>
    {table_content}
    """
    return f'<section class="team-section" id="vehicle-details"><h2>Vehicle details</h2>{body}</section>'


def _vehicle_reactivate_button(vehicle: dict) -> str:
    return (
        f'<form class="vehicle-reactivate-form" method="post" action="{url_for("vehicle_status")}" '
        f'onsubmit="return confirm(\'Make this vehicle active again?\')">'
        f'<input type="hidden" name="vehicle_id" value="{vehicle["id"]}">'
        '<input type="hidden" name="active" value="1"><input type="hidden" name="return_to" value="inactive">'
        f'<button type="submit" aria-label="Make {esc(vehicle["label"])} active again">Make active again</button></form>'
    )


def _vehicle_deactivate_form(active_vehicles: list[dict]) -> str:
    if not active_vehicles:
        return ""
    options = "".join(
        f'<optgroup label="{esc(group_name)}">'
        + "".join(
            f'<option value="{vehicle["id"]}">{esc(vehicle["label"])}'
            f'{" — VIN: " + esc(vehicle["vin"]) if vehicle["vin"] else ""}</option>'
            for vehicle in group_vehicles
        )
        + "</optgroup>"
        for group_name, group_vehicles in _group_vehicles(active_vehicles)
    )
    return (
        f'<form class="vehicle-status-form" method="post" action="{url_for("vehicle_status")}" '
        'onsubmit="return confirmVehicleStatus(this, \'Move this vehicle to the inactive list\')">'
        '<input type="hidden" name="active" value="0"><input type="hidden" name="return_to" value="active">'
        '<label for="vehicle-deactivate">Move to inactive</label>'
        '<select id="vehicle-deactivate" name="vehicle_id" required><option value="">Select vehicle</option>'
        f'{options}</select><button type="submit">Move to inactive</button></form>'
    )


@app.route("/vehicles/status", methods=["POST"])
def vehicle_status() -> str:
    active = request.form.get("active") == "1"
    back = url_for("vehicle_management", show="inactive") if request.form.get("return_to") == "inactive" else url_for("vehicle_management")
    try:
        vehicle_id = int(request.form.get("vehicle_id", ""))
    except ValueError:
        return render_page(
            "Vehicle not updated", f'<p class="warn">Select a vehicle.</p><p><a href="{back}">Back to vehicles</a></p>'
        ), 400
    vehicle = get_vehicle(vehicle_id)
    if vehicle is None:
        abort(404)
    if set_vehicle_active(vehicle_id, active):
        state = "active" if active else "inactive"
        actor = _current_actor()
        record_audit_event(
            actor,
            "Reactivated vehicle" if active else "Deactivated vehicle",
            f"Vehicle ID {vehicle_id}; {vehicle['label']}",
        )
        log_event(f"Vehicle #{vehicle_id} ({vehicle['label']}) set {state} by {actor}.")
        session["team_notice"] = f"{vehicle['label']} moved to the {state} list."
    else:
        session["team_notice"] = f"{vehicle['label']} is already {'active' if active else 'inactive'}."
    return redirect(back)


@app.route("/team")
def team_dashboard(section_override: str | None = None) -> str:
    notice = session.pop("team_notice", "")
    notice_html = f'<p class="msg" role="status">{esc(notice)}</p>' if notice else ""
    section = section_override or request.args.get("section", "home")
    if section == "home":
        destinations = (
            ("testing", "Testing / Bookings", "Book a vehicle activity and see your submitted bookings.", "Open bookings"),
            ("automation", "Automation", "Review bookings, choose a checklist, and create Jira tickets.", "Open review"),
            ("vehicles", "Vehicles", "View and manage vehicle details and active or inactive records.", "Open vehicles"),
            ("history", "Activity History", "Explore vehicle activities in the grid, timeline, or calendar.", "Open history"),
        )
        cards = "".join(
            f'<a class="workspace-card" href="{url_for("team_dashboard", section=key)}">'
            f'<strong>{esc(title)}</strong><span>{esc(description)}</span>'
            f'<span class="workspace-card-action">{esc(action)} &rarr;</span></a>'
            for key, title, description, action in destinations
        )
        if session.get("role") == "admin":
            cards += (
                f'<a class="workspace-card" href="{url_for("admin_dashboard")}">'
                '<strong>Admin</strong><span>Manage users, review the access and change log, and change your password.</span>'
                '<span class="workspace-card-action">Open admin &rarr;</span></a>'
            )
        section_content = (
            f'<p class="workspace-welcome">Welcome, {esc(_current_actor())}. Choose where you would like to go.</p>'
            f'<div class="workspace-grid">{cards}</div>'
        )
    elif section == "testing":
        section_content = _campaign_planner_section() + _my_bookings_section()
    elif section == "automation":
        section_content = _booking_review_section()
    elif section == "vehicles":
        section_content = _vehicle_management_section()
    elif section == "history":
        section_content = _activity_history_section()
    else:
        abort(400, description="Choose Home, Testing, Automation, Vehicles, or Activity History.")
    body = notice_html + section_content
    return render_page("Team Workspace", body)


@app.route("/admin")
def admin_dashboard() -> str:
    destinations = (
        ("admin_users", "Manage Users", "Create accounts, manage access, and reset passwords."),
        ("admin_audit", "Access & Change Log", "Review sign-ins and recorded changes."),
        ("change_password", "Change Password", "Change your admin account password."),
        ("admin_email_settings", "Email Notifications", "Check approver email settings and send a test email."),
    )
    cards = "".join(
        f'<a class="workspace-card" href="{url_for(endpoint)}">'
        f'<strong>{esc(title)}</strong><span>{esc(description)}</span>'
        '<span class="workspace-card-action">Open &rarr;</span></a>'
        for endpoint, title, description in destinations
    )
    return render_page("Admin", f'<p class="workspace-welcome">Choose an admin option.</p><div class="workspace-grid">{cards}</div>')


@app.route("/admin/email", methods=["GET", "POST"])
def admin_email_settings() -> str:
    back = f'<p><a href="{url_for("admin_dashboard")}">Back to admin</a></p>'
    try:
        settings = load_mail_settings()
    except ValueError as exc:
        return render_page("Email Notifications", f'<p class="warn">Email settings are invalid: {esc(exc)}</p>{back}')
    if not settings.enabled:
        return render_page(
            "Email Notifications",
            '<p>Booking notifications are off. Set <code>BOOKING_NOTIFY_TO</code>, <code>MAIL_FROM</code>, and '
            '<code>SMTP_HOST</code> (or <code>MAIL_OUTBOX_DIR</code> for local testing) in the app host\'s '
            '<code>.env</code>, then restart the app.</p>' + back,
        )
    notice = ""
    if request.method == "POST":
        try:
            result = send_email(settings, build_test_email(settings, _current_actor()))
        except Exception as exc:
            notice = f'<p class="warn">Test email failed: {esc(type(exc).__name__)}. Check the SMTP settings.</p>'
            log_event(f"Test email failed ({type(exc).__name__}).", "error")
        else:
            notice = f'<p class="msg">Test email {esc(result)}.</p>'
            record_audit_event(_current_actor(), "Sent test email", f"{len(settings.recipients)} recipient(s)")
    delivery = (
        f"SMTP {settings.smtp_host}:{settings.smtp_port} ({settings.smtp_security})"
        if settings.smtp_host
        else f"Local outbox folder: {settings.outbox_dir}"
    )
    link_base = settings.app_base_url or f"{request.url_root.rstrip('/')} (APP_BASE_URL not set)"
    return render_page(
        "Email Notifications",
        notice
        + '<div class="card"><p>Approvers that people can choose when booking:</p><ul>'
        + "".join(f"<li>{esc(address)}</li>" for address in settings.recipients)
        + f"</ul><p><strong>From:</strong> {esc(settings.sender)}<br>"
        f"<strong>Delivery:</strong> {esc(delivery)}<br>"
        f"<strong>Links point to:</strong> {esc(link_base)}</p>"
        f'<form method="post" action="{url_for("admin_email_settings")}">'
        '<button type="submit">Send test email</button></form></div>' + back,
    )


@app.route("/vehicles/create", methods=["POST"])
def vehicle_create() -> str:
    vehicle_data = {
        key: request.form.get(key, "")
        for key in ("label", "logger", "series", "cluster", "market", "vr_stand", "purpose", "vin", "comment", "group_name")
    }
    if not vehicle_data["label"].strip():
        return render_page(
            "Vehicle not added",
            '<p class="warn">Vehicle name is required.</p><p><a href="' + url_for("vehicle_management") + '">Back</a></p>',
        ), 400
    if not vehicle_data["group_name"].strip():
        return render_page(
            "Vehicle not added",
            '<p class="warn">Choose or enter a vehicle section.</p><p><a href="' + url_for("vehicle_management") + '">Back</a></p>',
        ), 400
    if vehicle_name_exists(vehicle_data["label"]) and request.form.get("confirm_duplicate") != "1":
        hidden_fields = "".join(
            f'<input type="hidden" name="{key}" value="{esc(value)}">'
            for key, value in vehicle_data.items()
        )
        return render_page(
            "Confirm duplicate vehicle",
            '<div class="card"><p class="warn">A vehicle with this name already exists. '
            'Do you want to add another vehicle with the same name?</p>'
            f'<p><strong>Name:</strong> {esc(vehicle_data["label"])}<br>'
            f'<strong>Section:</strong> {esc(vehicle_data["group_name"])}<br>'
            f'<strong>VIN:</strong> {esc(vehicle_data["vin"] or "Not recorded")}</p>'
            f'<form method="post" action="{url_for("vehicle_create")}">{hidden_fields}'
            '<button type="submit" name="confirm_duplicate" value="1">Add another vehicle with this name</button> '
            f'<a class="button" href="{url_for("vehicle_management")}">Cancel</a></form></div>',
        )
    try:
        vehicle_id = add_vehicle(vehicle_data)
    except ValueError as exc:
        return render_page("Vehicle not added", f'<p class="warn">{esc(exc)}</p><p><a href="{url_for("vehicle_management")}">Back</a></p>'), 400
    actor = _current_actor()
    record_audit_event(actor, "Added vehicle", f"Vehicle ID {vehicle_id}; {vehicle_data['label'].strip()}")
    log_event(f"Vehicle #{vehicle_id} added by {actor}.")
    return redirect(url_for("team_dashboard", section="vehicles"))


def _vehicle_delete_section(vehicle: dict) -> str:
    booking_count = count_vehicle_bookings(vehicle["id"])
    if booking_count:
        body = (
            f'<p class="form-hint">This vehicle has {booking_count} booking(s), so it cannot be deleted. '
            'To stop using it, choose <strong>Move to inactive</strong> on the Vehicles page instead.</p>'
        )
    else:
        body = (
            f'<form method="post" action="{url_for("admin_vehicle_delete", vehicle_id=vehicle["id"])}" '
            f'onsubmit="return confirm(\'Permanently delete this vehicle? This cannot be undone.\')">'
            f'<button class="danger-button" type="submit">Delete {esc(vehicle["label"])}</button></form>'
        )
    return f'<div class="card vehicle-delete-card"><h3>Delete vehicle</h3>{body}</div>'


@app.route("/vehicles/<int:vehicle_id>/delete", methods=["POST"])
def admin_vehicle_delete(vehicle_id: int) -> str:
    vehicle = get_vehicle(vehicle_id)
    if vehicle is None:
        abort(404)
    try:
        delete_vehicle(vehicle_id)
    except ValueError as exc:
        return render_page(
            "Vehicle not deleted",
            f'<p class="warn">{esc(exc)}</p><p><a href="{url_for("vehicle_edit", vehicle_id=vehicle_id)}">Back</a></p>',
        ), 409
    actor = _current_actor()
    record_audit_event(actor, "Deleted vehicle", f"Vehicle ID {vehicle_id}; {vehicle['label']}")
    log_event(f"Vehicle #{vehicle_id} ({vehicle['label']}) deleted by {actor}.")
    session["team_notice"] = f"{vehicle['label']} was deleted."
    show = {} if vehicle["active"] else {"show": "inactive"}
    return redirect(url_for("vehicle_management", **show))


@app.route("/vehicles/<int:vehicle_id>/edit", methods=["GET", "POST"])
def vehicle_edit(vehicle_id: int) -> str:
    vehicle = get_vehicle(vehicle_id)
    if vehicle is None:
        abort(404)
    if request.method == "GET":
        return render_page(
            "Edit vehicle",
            f'<form method="post" action="{url_for("vehicle_edit", vehicle_id=vehicle_id)}">'
            f'{_vehicle_fields_form(vehicle, include_section=True)}<button type="submit">Save vehicle details</button></form>'
            + (_vehicle_delete_section(vehicle) if session.get("role") == "admin" else "")
            + f'<p><a href="{url_for("vehicle_management")}">Back to vehicles</a></p>',
        )
    updated_fields = {
        key: request.form.get(key, "").strip()
        for key in ("label", "logger", "series", "cluster", "market", "vr_stand", "purpose", "vin", "comment", "group_name")
    }
    changed_fields = [
        key for key, value in updated_fields.items()
        if value != str(vehicle.get(key, "")).strip()
    ]
    try:
        update_vehicle(vehicle_id, updated_fields)
    except ValueError as exc:
        return render_page("Vehicle not updated", f'<p class="warn">{esc(exc)}</p><p><a href="{url_for("vehicle_edit", vehicle_id=vehicle_id)}">Back</a></p>'), 400
    except sqlite3.IntegrityError as exc:
        if "vehicles.label" in str(exc):
            message = "Another vehicle already uses this name. Choose a unique name."
        else:
            raise
        return render_page("Vehicle not updated", f'<p class="warn">{esc(message)}</p><p><a href="{url_for("vehicle_edit", vehicle_id=vehicle_id)}">Back</a></p>'), 400
    actor = _current_actor()
    if changed_fields:
        record_audit_event(
            actor,
            "Edited vehicle",
            f"Vehicle ID {vehicle_id}; {vehicle['label']}; changed fields: {', '.join(changed_fields)}",
        )
    log_event(f"Vehicle #{vehicle_id} details updated by {actor}.")
    return redirect(url_for("team_dashboard", section="vehicles"))


if __name__ == "__main__":
    app.run(
        host=os.environ.get("APP_HOST", "127.0.0.1"),
        port=int(os.environ.get("APP_PORT", "5000")),
        debug=os.environ.get("APP_DEBUG", "").strip().lower() in ("1", "true", "yes"),
        use_reloader=False,
    )
