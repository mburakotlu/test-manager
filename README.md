# jira_manager

A Jira automation toolkit, originally ported from the `enna_mh` repo's
`.github/agents/jira-agent.md` + `ide_tools/jira_mcp/`, extended with a sprint
ticket generator that creates a checklist ticket per test vehicle from a reusable
template.

## What's here

| Piece | Purpose |
|---|---|
| `jira_agent/` | Core Jira client (read/search/comment/link/attachment/issue-create), MCP server, checklist templating, browser-automation workaround |
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
copy .env.example .env   # then edit .env with your own values — never commit it
```

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

## Verify the connection

```powershell
$env:JIRA_API_TOKEN = "<your-token>"   # set directly in your shell, not in a file you commit
python test_connection.py PHUB-1
```

## Generate sprint checklist tickets for test vehicles

Test vehicles come from the Jira Insight/Assets plugin (schema `PEDSFLEET`, object
type `Autos`). Checklists come from `templates/*.yaml` and are written via the
browser-automation workaround (see Known limitation below).

One-time setup:

```powershell
python browser_login.py
```

Preview (dry-run, default — creates nothing):

```powershell
python generate_sprint_tickets.py --sprint "KW37" --vehicles "PT1023,CARIAD_1"
```

Run for real:

```powershell
python generate_sprint_tickets.py --sprint "KW37" --vehicles "PT1023,CARIAD_1" --confirm
```

Options: `--template NAME` (default `analysis_checklist`), `--project` (default `PHUB`),
`--issue-type` (default `Task`), `--vehicle-iql` (Insight query, default all `Autos`),
`--limit N` (safety cap, default 10), `--headed` (show the browser instead of headless).

Sprint is always a plain free-text label (e.g. `"KW37"`) — see Known limitation below
for why this doesn't use live Jira sprint objects.

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
