# jira_manager

A vehicle campaign planner and Jira automation toolkit. Team members submit
vehicle/activity/week bookings, review them, and explicitly confirm Jira ticket
creation in one shared workspace. Vehicle details and campaign bookings are
stored in the app's SQLite database. The planning workbook is supported as an
initial import and for deliberate, incremental updates; the planner database
remains the source of truth.

## What's here

| Piece | Purpose |
|---|---|
| `jira_agent/` | Core Jira client (read/search/comment/link/attachment/issue-create), MCP server, checklist templating, browser-automation workaround |
| `planner_store.py` | App accounts, roles, vehicle details, campaign bookings, and SQLite persistence |
| `manage_app.py` | App-user bootstrap, password reset, and application-secret helper |
| `import_planner_workbook.py` | Dry-run/one-time migration of vehicle details and calendar activities from Excel |
| `tests/` | Planner database tests |
| `templates/*.yaml` | Reusable checklist templates (Smart Checklist plugin format) |
| `generate_sprint_tickets.py` | Creates one ticket per test vehicle for a sprint, with a checklist applied |
| `apply_checklist_template.py` | Apply a checklist template to one ticket via the direct API (currently blocked — see Known limitation below) |
| `apply_checklist_via_browser.py` | Apply a checklist template to one ticket via browser automation (works) |
| `browser_login.py` | One-time interactive SSO login for the browser-automation workaround |
| `test_connection.py` | Smoke test for the Jira connection |

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium   # only needed for checklist/browser automation
Copy-Item .env.example .env
python manage_app.py generate-secret
# Put the printed value in APP_SECRET_KEY in .env.
```

Set the Jira values in `.env` for the automation account, then perform the
one-time workbook migration. It imports the vehicle fields and dated activities;
past activities are retained as historical/read-only, while today/future
activities await review:

```powershell
python import_planner_workbook.py
python import_planner_workbook.py --confirm
python manage_app.py bootstrap-admin
python main.py
```

Admins can generate a reset password from **Manage Users**. For host-level
recovery, stop the local app and use `python manage_app.py reset-password
USERNAME`; the command prompts for a new password twice without displaying it
and uses the database configured by `APP_DATABASE_PATH`.

For checklist automation, run `python browser_login.py` on the app host once
using an authorized Jira browser account. If the browser session is unavailable
or expires, the Jira ticket is still recorded and its checklist is queued in
`pending_checklists.json` for a later retry.
Testing team members submit activities without choosing a checklist. After
approval, the automation team selects and previews a checklist template on the
Jira ticket preview page before confirming ticket creation. Existing bookings
keep their previously selected templates, which can still be changed there.
Reviewers can use **Edit** on pending or approved bookings to correct the
vehicle, activity, week/dates, notes, or color before Jira creation starts.
Edits keep the current review status and are recorded in the Access & Change Log.

When `BOOKING_NOTIFY_TO` is configured, the booking form lists those approvers
and the person booking must tick at least one. Only the ticked approvers are
emailed the booking details and a link to the review page; addresses outside
the configured list are rejected. Emails are sent in the background;
a mail failure never blocks the booking and is shown in the activity log.
Admins can check the settings and send a test email from **Admin → Email
Notifications**. For a local trial without a mail server, set
`MAIL_OUTBOX_DIR` and open the saved `.eml` files in Outlook. Links work for
other people only after the app is hosted and `APP_BASE_URL` points to it.
The automation section is part of the shared workspace, so moving this choice
does not restrict access to it by role.

The initial importer merges into the planner database once. Vehicles are
matched by name; existing non-empty vehicle fields are preserved and blank
fields can be filled from the workbook. Vehicles absent from the database are
added. Activities from the workbook are recorded as imported entries, and any
initial workbook import already recorded in the database blocks another initial
import. To add changes from a subsequently updated workbook, preview with
`python import_planner_workbook.py --sync-updates`, then apply with
`python import_planner_workbook.py --sync-updates --confirm`. This operation
adds missing vehicles and activity bookings, fills only blank vehicle fields,
and applies workbook section/order changes. It does not delete bookings or
overwrite populated vehicle fields; matching existing bookings are skipped.
Back up the database before confirming and keep the original workbook as an
archive/backup. Do not use the legacy Excel refresh afterward.
The main calendar import does not include notes from the individual vehicle
worksheets. Preview those additional rows with
`python import_planner_workbook.py --import-vehicle-tabs`; use
`python import_planner_workbook.py --import-vehicle-tabs --confirm` to add them
once to existing vehicles as read-only history, retaining the recorded person
in the description.

The importer preserves the workbook's vehicle section headings and row order
(for example, MLBevo, E³ 1.2, and Neu ab 1.1.2026). To apply those headings and
ordering to vehicles already imported into an older database without importing
activities again, preview with `python import_planner_workbook.py
--update-groups`, then apply with `python import_planner_workbook.py
--update-groups --confirm`.
If the workbook was imported before activity colors were stored, preview a color
backfill with `python import_planner_workbook.py --update-colors`, then apply it
with `python import_planner_workbook.py --update-colors --confirm`.
On the testing booking form, **Automatic** reuses a matching activity color
when available. Testers can instead choose an imported Excel color or a custom
color; the choice is stored with that booking and shown in activity history.
Excel colors come from imported workbook data, so import or sync new activities
before expecting their colors to appear. Use the color backfill above when
colors on already imported activities have changed.
If the workbook was imported before merged activity ranges were stored, preview
the date-span backfill with `python import_planner_workbook.py --update-spans`,
then apply it with `python import_planner_workbook.py --update-spans --confirm`.

The first admin creates accounts in **Manage Users**. The app generates a
temporary password for each new account. Only admins can change passwords:
they can change their own password or generate a reset password for another
active account from **Manage Users**. Share temporary/reset passwords through
an approved secure channel. Sign-in is temporarily locked after five failed
attempts for the same username and source address; a successful sign-in clears
the failed-attempt counter. Planner and Automation accounts share one Team
Workspace with a home page linking to activity submission/bookings, booking
review, Jira ticket creation, vehicle management, and activity history. The
top navigation provides a shortcut back to Home. Admins can open the **Admin**
card on Home to manage users, review the access/change log, or change their
password. VINs appear in the
vehicle selector, vehicle list, and booking lists. Team users do not need Jira
accounts or tokens.

For shared team access, create one Planner account in **Manage Users** and
distribute its password securely. Each Planner sign-in asks for the person's
name; the name is self-reported, not independently verified, and is used to
attribute their bookings and recorded actions. Admins can review these events
in **Access & Change Log** and reset the shared password when needed. Use
individual accounts instead if verified per-person attribution is required.

For an individual local trial, SQLite defaults to `data/planner.sqlite3`.
For team access, deploy behind company-approved HTTPS and sign-in/network
controls on one persistent host, configure a stable `APP_SECRET_KEY`, and
keep the SQLite database on that host's local persistent disk (not a shared
network drive). Set `SESSION_COOKIE_SECURE=true` when HTTPS is enabled. The
development server defaults to `127.0.0.1`; do not expose Flask's built-in
server directly to a network. Plan regular encrypted backups of the database.

### Windows executable

On a Windows build machine, install `requirements.txt` into `.venv` as above,
then install PyInstaller (`python -m pip install pyinstaller`) and run
`python build_exe.py`. The distributable folder is `dist/JiraManager/`.
Keep that whole folder together. Copy its `.env.example` to `.env`, set the Jira
credentials and generated app secret, then run `JiraManager.exe
--import-workbook` for a preview, `JiraManager.exe --import-workbook --confirm`
to import once, and `JiraManager.exe --bootstrap-admin` to create the first
admin. Double-clicking the EXE opens the local app at
`http://127.0.0.1:5000/`. The EXE does not require Python on the destination
machine. Browser checklist features need a separately installed Playwright
Chromium browser. This EXE is for local use; shared team deployment requires an
approved hosted setup and should not expose Flask's built-in server.

Required env vars (see `.env.example`):

| Var | Required | Notes |
|---|---|---|
| `JIRA_BASE_URL` | yes | REST API domain, e.g. `https://api.skyway.porsche.com` |
| `JIRA_EMAIL` | yes | your Jira account email |
| `JIRA_API_TOKEN` | yes | Personal Access Token — **never hardcode this in source** |
| `JIRA_AUTH_MODE` | no | `bearer` (default) or `basic` |
| `JIRA_IS_TEC_NET` | no | `false` to route through the corporate proxy |
| `JIRA_VERIFY_SSL` | no | `false` for corporate SSL interception |
| `JIRA_WEB_BASE_URL` | no | Browser-facing domain (e.g. `https://skyway.porsche.com`), used only by the browser-automation scripts. Defaults to stripping `api.` from `JIRA_BASE_URL` |
| `WORKSPACE_ROOT` | no | defaults to CWD; used by `code_search`/`code_read` |
| `JIRA_PROJECT_KEY` | no | Jira project for approved campaign tickets; defaults to `PHUB` |
| `APP_SECRET_KEY` | required for stable/shared sessions | Generate with `python manage_app.py generate-secret`; keep private |
| `APP_DATABASE_PATH` | no | SQLite path; defaults to `data/planner.sqlite3` |
| `SESSION_COOKIE_SECURE` | no | Set to `true` behind HTTPS; keep `false` for localhost HTTP |
| `BOOKING_NOTIFY_TO` | no | Comma-separated approver addresses that people can select on the booking form; notifications are off when empty |
| `MAIL_FROM` | with notifications | Sender address for notification emails |
| `SMTP_HOST` / `SMTP_PORT` | with notifications | Company mail server; port defaults to 587 (465 for `ssl`) |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | no | Mail-server credentials, if required; keep private |
| `SMTP_SECURITY` | no | `starttls` (default), `ssl`, or `none` |
| `MAIL_OUTBOX_DIR` | no | Local testing: save emails as `.eml` files in this folder instead of using SMTP |
| `APP_BASE_URL` | recommended when hosted | Public app address used in email links, e.g. `https://planner.example.com` |

## Verify the connection

```powershell
$env:JIRA_API_TOKEN = "<your-token>"   # set directly in your shell, not in a file you commit
python test_connection.py PHUB-1
```

## Campaign booking workflow

1. A planner signs in and enters a vehicle, activity, and either a week (`KWnn`
   or `KWnn-YYYY`) or a start date; at least one is required. A week without
   dates books Monday-Friday of that ISO week. A start date books one day, or
   the start of an optional date range that may cross weeks. Dates take
   precedence if both week and dates are entered. Submission creates a pending
   booking in SQLite only; it does not call Jira.
2. An automation user reviews the booking and approves or rejects it.
3. For an approved booking, the automation user reviews a ticket and checklist
   preview and explicitly confirms ticket creation. The ticket includes the
   vehicle label and VIN where available. Its Jira key is recorded on the booking.
4. The first version does not automatically create or assign Agile sprints.
   The Jira instance has documented sprint persistence issues; group tickets
   manually in Jira until that behavior is verified.

Vehicle master data (Name, Logger, Baureihe, Cluster, Markt, VR-Stand,
Verwendungszweck, VIN, and optional existing Jira Assets key) is edited by
automation users in the app. When adding a vehicle, choose an existing workbook
section or enter a new section name. Existing vehicle names are allowed; the app
asks for confirmation before adding a duplicate, and each record keeps its own
vehicle ID and booking history. A Jira Assets key is a reference only; the app
database is authoritative for the planner's vehicle data and bookings.
Workbook activities dated before import are retained as read-only history.
The Testing and Automation views include a year-filtered Excel-style weekly grid,
a weekly timeline, and a month calendar. The calendar shows each activity on
every booked day with its vehicle, date range, status, and submitter, and allows
month-by-month navigation. In the grid, month names group daily columns across the
top, ISO week numbers group the date row, and each day is labeled with its
calendar number (1, 2, 3, ...). Strong vertical separators mark month ends;
Saturdays and Sundays are shaded to distinguish weekends. A full-screen control
expands the grid for a wider view. Vehicle names and VINs run down the left.
The week/date headers stay visible while scrolling vertically,
and the vehicle column stays visible while scrolling horizontally. The grid
extends through the end of the current year, and the current ISO week is
highlighted and labeled. Activities are displayed directly in the day cells
using their original Excel fill colors; matching new team bookings reuse that
activity color. Imported merged Excel cells are shown across their full inclusive
date range, including weekends. New bookings can set an end date to display a
multi-day span, including across week boundaries; leaving the start date blank
displays the booking Monday-Friday of its selected ISO week. Overlapping
activities are placed on separate rows rather than covering one another.
Saturday and Sunday workbook activities remain in their exact calendar-day
cells. New bookings can also include weekend dates; submitters must confirm if
any date in the selected activity range falls on a weekend before the booking is
saved.
Select a vehicle name to open its full Excel-imported record, including all
vehicle fields and dated activities grouped into color-coded ISO weeks, or
switch to a calendar showing that vehicle's bookings by date. That per-vehicle
view includes every dated imported entry, including future-dated workbook
entries, and is read-only.

The old `/refresh` and `/sprint` web workflows are retired because they read
the former Excel/Assets queue. CLI scripts such as `import_jahreskalender.py`
and `generate_from_vehicles.py` remain for legacy/manual use but should not be
used to process new planner bookings.

## Use as an MCP server

Copy `.vscode/mcp.example.json` to `.vscode/mcp.json`, fill in your real values, and
it will be picked up by MCP-capable clients (GitHub Copilot, Claude Desktop, Cline).
`.vscode/mcp.json` is gitignored — do not commit real tokens.

Run standalone: `python -m jira_agent.server`

| Tool | Purpose | Writes to Jira? |
|---|---|---|
| `jira_get_issue` | Fetch issue details | no |
| `jira_get_comments` | List comments | no |
| `jira_get_attachments` | List attachment metadata | no |
| `jira_search` | JQL search | no |
| `jira_add_comment` | Post a comment (dry-run by default) | only with `confirm=true` |
| `jira_add_link` | Add a remote web link | only with `confirm=true` |
| `jira_add_attachment` | Upload a file | only with `confirm=true` |
| `code_search` | ripgrep search in `WORKSPACE_ROOT` | no |
| `code_read` | Read a file slice | no |

## Known limitation: checklist field can't be written via the direct API

The Smart Checklist plugin field (`customfield_17802`) can be read via the normal
API, but Jira rejects writes to it (`PUT /issue/{key}` → 400: "not on the appropriate
screen"), and the plugin's own REST API only accepts a real browser session cookie,
not our Bearer/Basic PAT auth.

**Workaround (what we actually use):** `jira_agent/browser_checklist.py` drives the
real Smart Checklist editor UI via Playwright, using a saved browser session
(`.auth/jira_browser_state.json`, gitignored, refreshed automatically on each run).
One-time interactive login via `browser_login.py`, fully headless afterward.

**Permanent fix (not done):** ask a Jira admin to add the "Checklists" field to the
relevant project's issue-type screens. Once that's done, `JiraClient.set_checklist()`
(already implemented, dry-run by default) works directly via the normal PAT with no
browser dependency.

## Known limitation: live Jira sprint objects are unusable on this instance

Creating and activating a sprint via the Agile REST API works (`201`/`200`), but the
sprint is deleted cluster-wide within seconds of activation — reproduced 3/3 times,
confirmed not to be a caching artifact. This appears to be a server-side automation
on this Jira instance (e.g. an anti-clutter rule for empty sprints). As a result,
sprint identifiers are always plain free-text strings, never live Jira sprint objects.

## Security notes

- No credentials are hardcoded anywhere in this repo. All secrets come from
  environment variables (`.env`, real `.vscode/mcp.json`, `.auth/`) which are gitignored.
- `jira_add_comment` / `jira_add_link` / `jira_add_attachment` / `create_issue` /
  `set_checklist` are dry-run by default and require an explicit `confirm=true` (or
  `--confirm` on CLI scripts) to write to Jira.
- `code_search` / `code_read` are sandboxed to `WORKSPACE_ROOT` and reject path
  traversal.
- `.auth/jira_browser_state.json` contains a live session cookie — treat it as a
  secret, same as an API token.
