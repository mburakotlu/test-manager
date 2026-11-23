"""Thin Jira Server/Data Center REST v2 client used by the Jira agent.

All HTTP I/O goes through a single ``httpx.Client`` instance so tests can
inject a mock transport.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import BOT_COMMENT_PREFIX, JiraConfig


logger = logging.getLogger(__name__)


@dataclass
class IssueSummary:
    """Compact view of a Jira issue suitable for LLM consumption."""

    key: str
    summary: str
    description: str
    status: str
    assignee: str | None
    reporter: str | None
    priority: str | None
    labels: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    fix_versions: list[str] = field(default_factory=list)
    issue_type: str | None = None
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict."""
        return {
            "key": self.key,
            "summary": self.summary,
            "description": self.description,
            "status": self.status,
            "assignee": self.assignee,
            "reporter": self.reporter,
            "priority": self.priority,
            "labels": self.labels,
            "components": self.components,
            "fix_versions": self.fix_versions,
            "issue_type": self.issue_type,
            "url": self.url,
        }


def adf_to_text(node: Any) -> str:  # noqa: ANN401 — recursive ADF is heterogenous
    """Flatten an Atlassian Document Format (ADF) node tree to plain text.

    ADF is a JSON tree of ``{type, content, text, ...}`` nodes. We only care
    about extracting human-readable text for the LLM. Lists and headings are
    preserved as line breaks; unknown nodes are descended into.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(filter(None, (adf_to_text(n) for n in node)))
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type")
    if node_type == "text":
        return str(node.get("text", ""))
    if node_type == "hardBreak":
        return "\n"
    content = node.get("content", [])
    inner = adf_to_text(content)
    if node_type in {"paragraph", "heading", "listItem", "bulletList", "orderedList", "codeBlock"}:
        return inner + "\n"
    return inner


class JiraClient:
    """Jira REST client supporting both Cloud (v3) and Server/Data Center (v2)."""

    # Jira Insight/Assets (Riadalabs, self-hosted) — test vehicle fleet.
    # Instance-specific: re-verify via GET /jira/rest/insight/1.0/objectschema/list
    # if pointed at a different Jira instance.
    INSIGHT_TEST_VEHICLE_SCHEMA_ID = 81  # "PEDSFLEET Schema"
    INSIGHT_TEST_VEHICLE_OBJECT_TYPE_ID = 411  # "Autos" object type
    INSIGHT_VEHICLE_ASSET_SCHEMA_ID = 607  # secondary schema seen for some vehicles
    INSIGHT_VEHICLE_ASSET_OBJECT_TYPE_ID = 4321  # "FZG" object type

    def __init__(self, config: JiraConfig, http_client: httpx.Client | None = None) -> None:
        """Initialise with a config and an optional injected ``httpx.Client``."""
        self._config = config
        if http_client:
            self._http = http_client
        else:
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            proxy = config.proxies
            verify = config.verify_ssl
            if config.use_bearer_token:
                headers["Authorization"] = f"Bearer {config.api_token}"
                self._http = httpx.Client(
                    base_url=config.base_url,
                    timeout=30.0,
                    headers=headers,
                    follow_redirects=True,
                    proxy=proxy,
                    verify=verify,
                )
            else:
                self._http = httpx.Client(
                    base_url=config.base_url,
                    auth=config.auth,
                    timeout=30.0,
                    headers=headers,
                    follow_redirects=True,
                    proxy=proxy,
                    verify=verify,
                )

    # -- internals -------------------------------------------------------
    @property
    def _api(self) -> str:
        """Return the REST API path prefix — always v2 for Jira Server."""
        return "/jira/rest/api/2"

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._http.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        response = self._http.post(path, json=json)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _user_name(user: dict[str, Any] | None) -> str | None:
        if not user:
            return None
        return user.get("displayName") or user.get("emailAddress") or user.get("accountId")

    # -- public API ------------------------------------------------------
    def _parse_description(self, raw_desc: Any) -> str:
        """Parse description from either ADF (Cloud) or plain text (Server)."""
        if raw_desc is None:
            return ""
        if isinstance(raw_desc, str):
            return raw_desc.strip()
        return adf_to_text(raw_desc).strip()

    def _parse_comment_body(self, raw_body: Any) -> str:
        """Parse comment body from either ADF (Cloud) or plain text (Server)."""
        if raw_body is None:
            return ""
        if isinstance(raw_body, str):
            return raw_body.strip()
        return adf_to_text(raw_body).strip()

    def get_issue(self, key: str) -> IssueSummary:
        """Fetch ``key`` and return an :class:`IssueSummary`."""
        data = self._get(
            f"{self._api}/issue/{key}",
            params={"fields": "summary,description,status,assignee,reporter,priority,"
                              "labels,components,fixVersions,issuetype"},
        )
        fields = data.get("fields", {})
        return IssueSummary(
            key=data.get("key", key),
            summary=fields.get("summary", "") or "",
            description=self._parse_description(fields.get("description")),
            status=(fields.get("status") or {}).get("name", "") or "",
            assignee=self._user_name(fields.get("assignee")),
            reporter=self._user_name(fields.get("reporter")),
            priority=(fields.get("priority") or {}).get("name"),
            labels=list(fields.get("labels") or []),
            components=[c.get("name", "") for c in fields.get("components") or []],
            fix_versions=[v.get("name", "") for v in fields.get("fixVersions") or []],
            issue_type=(fields.get("issuetype") or {}).get("name"),
            url=f"{self._config.effective_web_base_url}/jira/browse/{data.get('key', key)}",
        )

    def get_comments(self, key: str) -> list[dict[str, Any]]:
        """Return all comments on ``key`` as a list of compact dicts."""
        data = self._get(f"{self._api}/issue/{key}/comment", params={"maxResults": 100})
        out: list[dict[str, Any]] = []
        for comment in data.get("comments", []):
            out.append({
                "id": comment.get("id"),
                "author": self._user_name(comment.get("author")),
                "created": comment.get("created"),
                "updated": comment.get("updated"),
                "body": self._parse_comment_body(comment.get("body")),
            })
        return out

    def get_attachments(self, key: str) -> list[dict[str, Any]]:
        """Return attachment metadata for ``key`` (no file download)."""
        data = self._get(f"{self._api}/issue/{key}", params={"fields": "attachment"})
        attachments = (data.get("fields") or {}).get("attachment") or []
        return [
            {
                "id": att.get("id"),
                "filename": att.get("filename"),
                "size": att.get("size"),
                "mime_type": att.get("mimeType"),
                "created": att.get("created"),
                "author": self._user_name(att.get("author")),
                "content_url": att.get("content"),
            }
            for att in attachments
        ]

    def search(self, jql: str, max_results: int = 25) -> list[dict[str, Any]]:
        """Run a JQL query and return compact issue dicts."""
        data = self._get(
            f"{self._api}/search",
            params={"jql": jql, "maxResults": max_results,
                    "fields": "summary,status,assignee,priority,issuetype"},
        )
        out: list[dict[str, Any]] = []
        for issue in data.get("issues", []):
            fields = issue.get("fields", {})
            out.append({
                "key": issue.get("key"),
                "summary": fields.get("summary"),
                "status": (fields.get("status") or {}).get("name"),
                "assignee": self._user_name(fields.get("assignee")),
                "priority": (fields.get("priority") or {}).get("name"),
                "issue_type": (fields.get("issuetype") or {}).get("name"),
                "url": f"{self._config.effective_web_base_url}/jira/browse/{issue.get('key')}",
            })
        return out

    def search_users(self, query: str, max_results: int = 10) -> list[dict[str, str]]:
        """Search Jira users by name/email/username for an assignee picker (read-only)."""
        if not query or not query.strip():
            return []
        data = self._get(
            f"{self._api}/user/picker",
            params={"query": query.strip(), "maxResults": max_results, "showAvatar": False},
        )
        return [
            {"name": u.get("name", ""), "display_name": u.get("displayName", u.get("name", ""))}
            for u in data.get("users", [])
        ]

    def add_comment(
        self,
        key: str,
        body: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Post a comment to ``key`` (dry-run unless ``confirm=True``).

        The body is always prefixed with :data:`BOT_COMMENT_PREFIX` so humans
        can audit and filter agent-authored comments.
        """
        if not body or not body.strip():
            raise ValueError("Comment body must be non-empty.")
        rendered = body.strip()
        if not rendered.startswith(BOT_COMMENT_PREFIX):
            rendered = f"{BOT_COMMENT_PREFIX}\n\n{rendered}"

        if not confirm:
            return {
                "posted": False,
                "dry_run": True,
                "issue_key": key,
                "preview": rendered,
                "hint": "Re-call with confirm=True to actually post this comment.",
            }

        if self._config.use_bearer_token:
            post_body: dict[str, Any] = {"body": rendered}
        else:
            post_body = {
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": line}],
                        }
                        for line in rendered.split("\n") if line
                    ] or [{"type": "paragraph", "content": [{"type": "text", "text": rendered}]}],
                },
            }
        data = self._post(f"{self._api}/issue/{key}/comment", json=post_body)
        return {
            "posted": True,
            "dry_run": False,
            "issue_key": key,
            "comment_id": data.get("id"),
            "preview": rendered,
        }

    def add_remote_link(
        self,
        key: str,
        url: str,
        title: str,
        relationship: str = "relates to",
        icon_url: str = "",
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Add a remote (web) link to a Jira issue (dry-run unless ``confirm=True``).

        Uses the ``/rest/api/2/issue/{key}/remotelink`` endpoint to attach
        a clickable URL in the ticket's "Links" section.

        :param key: Issue key (e.g. PHUB-123).
        :param url: The URL to link (e.g. a GitLab MR).
        :param title: Display title for the link.
        :param relationship: Relationship label (default: "relates to").
        :param icon_url: Optional icon URL for the link.
        :param confirm: If False, returns a dry-run preview; if True, posts.
        :return: Result dict with posted status and link ID.
        """
        if not url or not url.strip():
            raise ValueError("Link URL must be non-empty.")
        if not title or not title.strip():
            raise ValueError("Link title must be non-empty.")

        payload: dict[str, Any] = {
            "relationship": relationship,
            "object": {
                "url": url.strip(),
                "title": title.strip(),
            },
        }
        if icon_url:
            payload["object"]["icon"] = {"url16x16": icon_url, "title": title.strip()}

        if not confirm:
            return {
                "posted": False,
                "dry_run": True,
                "issue_key": key,
                "preview": payload,
                "hint": "Re-call with confirm=True to actually post this link.",
            }

        response = self._http.post(
            f"{self._api}/issue/{key}/remotelink", json=payload
        )
        response.raise_for_status()
        data = response.json()
        return {
            "posted": True,
            "dry_run": False,
            "issue_key": key,
            "link_id": data.get("id"),
            "url": url.strip(),
            "title": title.strip(),
        }

    def add_attachment(
        self,
        key: str,
        file_path: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Upload a file attachment to a Jira issue (dry-run unless ``confirm=True``).

        Uses the ``/rest/api/2/issue/{key}/attachments`` endpoint with
        multipart/form-data upload.

        :param key: Issue key (e.g. PHUB-123).
        :param file_path: Absolute or relative path to the file to upload.
        :param confirm: If False, returns a dry-run preview; if True, uploads.
        :return: Result dict with posted status and attachment metadata.
        """
        if not file_path or not file_path.strip():
            raise ValueError("File path must be non-empty.")

        resolved_path = os.path.abspath(file_path.strip())
        if not os.path.isfile(resolved_path):
            raise FileNotFoundError(f"Attachment file not found: {resolved_path}")

        filename = os.path.basename(resolved_path)
        file_size = os.path.getsize(resolved_path)

        if not confirm:
            return {
                "posted": False,
                "dry_run": True,
                "issue_key": key,
                "preview": {
                    "filename": filename,
                    "path": resolved_path,
                    "size_bytes": file_size,
                },
                "hint": "Re-call with confirm=True to actually upload this attachment.",
            }

        # Attachment upload requires special headers (no Content-Type json,
        # must include X-Atlassian-Token: no-check). The client's default
        # Content-Type: application/json header must be removed so httpx can
        # set the correct multipart/form-data boundary automatically.
        upload_headers = {
            "Accept": "application/json",
            "X-Atlassian-Token": "no-check",
        }
        if self._config.use_bearer_token:
            upload_headers["Authorization"] = f"Bearer {self._config.api_token}"

        original_content_type = self._http.headers.pop("content-type", None)
        try:
            with open(resolved_path, "rb") as f:
                response = self._http.post(
                    f"{self._api}/issue/{key}/attachments",
                    files={"file": (filename, f)},
                    headers=upload_headers,
                )
        finally:
            if original_content_type is not None:
                self._http.headers["content-type"] = original_content_type
        response.raise_for_status()
        data = response.json()

        # Response is a list of attachment objects
        attachments = data if isinstance(data, list) else [data]
        return {
            "posted": True,
            "dry_run": False,
            "issue_key": key,
            "attachments": [
                {
                    "id": att.get("id"),
                    "filename": att.get("filename"),
                    "size": att.get("size"),
                    "mime_type": att.get("mimeType"),
                    "content_url": att.get("content"),
                }
                for att in attachments
            ],
        }

    def download_attachment(self, content_url: str) -> bytes:
        """Download raw attachment bytes from a ``content_url`` returned by :meth:`get_attachments`."""
        response = self._http.get(content_url)
        response.raise_for_status()
        return response.content

    def delete_attachment(self, attachment_id: str, confirm: bool = False) -> dict[str, Any]:
        """Permanently delete an attachment by id (dry-run unless ``confirm=True``). Cannot be undone.

        Jira has no "update attachment in place" endpoint — refreshing a file
        means deleting the old attachment and uploading the new one.
        """
        if not confirm:
            return {
                "posted": False,
                "dry_run": True,
                "preview": {"attachment_id": attachment_id},
                "hint": "Re-call with confirm=True to actually delete this attachment.",
            }
        response = self._http.delete(f"{self._api}/attachment/{attachment_id}")
        response.raise_for_status()
        return {"posted": True, "dry_run": False, "attachment_id": attachment_id, "deleted": True}

    def get_checklist(self, key: str) -> str:
        """Return the raw Smart Checklist textarea content for ``key`` (empty if none)."""
        data = self._get(f"{self._api}/issue/{key}", params={"fields": self._config.checklist_field_id})
        return (data.get("fields") or {}).get(self._config.checklist_field_id) or ""

    def set_checklist(
        self,
        key: str,
        checklist_text: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Overwrite the Smart Checklist field on ``key`` (dry-run unless ``confirm=True``).

        ``checklist_text`` must already be in the plugin's textarea format
        (one item per line, prefixed ``+ `` for checked / ``- `` for unchecked).
        See :mod:`jira_agent.checklist` to render a template into this format.

        :param key: Issue key (e.g. PHUB-2189).
        :param checklist_text: Rendered checklist text to write.
        :param confirm: If False, returns a dry-run preview; if True, writes.
        :return: Result dict with posted status and the text that was (or would be) written.
        """
        if not checklist_text or not checklist_text.strip():
            raise ValueError("Checklist text must be non-empty.")

        if not confirm:
            return {
                "posted": False,
                "dry_run": True,
                "issue_key": key,
                "field_id": self._config.checklist_field_id,
                "preview": checklist_text,
                "hint": "Re-call with confirm=True to actually write this checklist.",
            }

        response = self._http.put(
            f"{self._api}/issue/{key}",
            json={"fields": {self._config.checklist_field_id: checklist_text}},
        )
        response.raise_for_status()
        return {
            "posted": True,
            "dry_run": False,
            "issue_key": key,
            "field_id": self._config.checklist_field_id,
            "preview": checklist_text,
        }

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    def delete_issue(self, key: str, confirm: bool = False) -> dict[str, Any]:
        """Permanently delete a Jira issue (dry-run unless ``confirm=True``). Cannot be undone.

        :param key: Issue key, e.g. "PHUB-2208".
        :param confirm: If False, returns a dry-run preview; if True, deletes.
        """
        if not confirm:
            return {
                "posted": False,
                "dry_run": True,
                "preview": {"key": key},
                "hint": "Re-call with confirm=True to actually delete this issue. This cannot be undone.",
            }
        response = self._http.delete(f"{self._api}/issue/{key}")
        response.raise_for_status()
        return {"posted": True, "dry_run": False, "key": key, "deleted": True}

    # -- issue creation ----------------------------------------------------
    def get_issue_create_fields(self, project_key: str, issue_type: str) -> dict[str, Any]:
        """Return fields available on the project's issue creation screen."""
        data = self._get(
            f"{self._api}/issue/createmeta",
            params={
                "projectKeys": project_key,
                "issuetypeNames": issue_type,
                "expand": "projects.issuetypes.fields",
            },
        )
        for project in data.get("projects", []):
            if project.get("key") != project_key:
                continue
            for kind in project.get("issuetypes", []):
                if kind.get("name") == issue_type and isinstance(kind.get("fields"), dict):
                    return kind["fields"]
        raise ValueError(f"Jira did not return creation fields for {project_key} {issue_type}.")

    def create_issue(
        self,
        project_key: str,
        issue_type: str,
        summary: str,
        description: str = "",
        confirm: bool = False,
        extra_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a Jira issue (dry-run unless ``confirm=True``).

        :param project_key: Project key, e.g. "PHUB".
        :param issue_type: Issue type name, e.g. "Task".
        :param summary: Issue summary/title.
        :param description: Plain-text description (Jira Server uses plain text, not ADF).
        :param confirm: If False, returns a dry-run preview; if True, creates.
        :param extra_fields: Additional raw fields to merge into the create payload.
        :return: Result dict with posted status and (if created) the new issue key/url.
        """
        if not summary or not summary.strip():
            raise ValueError("Summary must be non-empty.")

        from jira_agent.config import DEFAULT_REPORTER_USERNAME

        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "issuetype": {"name": issue_type},
            "summary": summary.strip(),
            # Default reporter/assignee — board 114663's saved filter only shows
            # tickets reported by a whitelisted account (see config.py notes).
            "reporter": {"name": DEFAULT_REPORTER_USERNAME},
            "assignee": {"name": DEFAULT_REPORTER_USERNAME},
        }
        if description:
            fields["description"] = description
        if extra_fields:
            fields.update(extra_fields)

        if not confirm:
            return {
                "posted": False,
                "dry_run": True,
                "preview": fields,
                "hint": "Re-call with confirm=True to actually create this issue.",
            }

        data = self._post(f"{self._api}/issue", json={"fields": fields})
        key = data.get("key")
        return {
            "posted": True,
            "dry_run": False,
            "key": key,
            "url": f"{self._config.effective_web_base_url}/jira/browse/{key}",
        }

    def update_issue_fields(
        self,
        key: str,
        fields: dict[str, Any],
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Update fields on an existing issue (dry-run unless ``confirm=True``).

        Fields must already be on the issue's edit screen — check via
        ``GET .../issue/{key}/editmeta`` first if unsure (e.g. VIN_FIELD_ID /
        VEHICLE_DESCRIPTION_FIELD_ID from config.py are confirmed present on PHUB tasks).
        """
        if not fields:
            raise ValueError("fields must be non-empty.")
        if not confirm:
            return {
                "posted": False,
                "dry_run": True,
                "key": key,
                "preview": fields,
                "hint": "Re-call with confirm=True to actually update this issue.",
            }
        response = self._http.put(f"{self._api}/issue/{key}", json={"fields": fields})
        response.raise_for_status()
        return {"posted": True, "dry_run": False, "key": key, "updated_fields": list(fields)}

    # -- Agile sprints (grouping only — see jira_client module notes) -------
    def create_sprint(self, board_id: int, name: str, confirm: bool = False) -> dict[str, Any]:
        """Create a new sprint on ``board_id`` in the default ``future`` state (dry-run unless ``confirm=True``).

        IMPORTANT: never transition a sprint created here to ``active`` — on this
        Jira instance, activating a sprint via the Agile API causes it to vanish
        (404) cluster-wide within seconds (confirmed via repeated testing). Sprints
        left in ``future`` state are stable and can safely hold issues for grouping/
        planning purposes, just not as a live "in progress" sprint.
        """
        payload = {"name": name, "originBoardId": board_id}
        if not confirm:
            return {
                "created": False,
                "dry_run": True,
                "preview": payload,
                "hint": "Re-call with confirm=True to actually create this sprint.",
            }
        response = self._http.post("/jira/rest/agile/1.0/sprint", json=payload)
        response.raise_for_status()
        data = response.json()
        return {"created": True, "dry_run": False, "id": data.get("id"), "name": data.get("name"), "state": data.get("state")}

    def add_issues_to_sprint(self, sprint_id: int, issue_keys: list[str], confirm: bool = False) -> dict[str, Any]:
        """Move ``issue_keys`` into ``sprint_id`` (dry-run unless ``confirm=True``)."""
        if not issue_keys:
            raise ValueError("issue_keys must be non-empty.")
        if not confirm:
            return {
                "moved": False,
                "dry_run": True,
                "sprint_id": sprint_id,
                "preview": issue_keys,
                "hint": "Re-call with confirm=True to actually move these issues.",
            }
        response = self._http.post(f"/jira/rest/agile/1.0/sprint/{sprint_id}/issue", json={"issues": issue_keys})
        response.raise_for_status()
        return {"moved": True, "dry_run": False, "sprint_id": sprint_id, "issues": issue_keys}

    def rename_sprint(self, sprint_id: int, name: str, confirm: bool = False) -> dict[str, Any]:
        """Rename an existing sprint (dry-run unless ``confirm=True``). Does not touch its state."""
        if not confirm:
            return {
                "renamed": False,
                "dry_run": True,
                "sprint_id": sprint_id,
                "preview": {"name": name},
                "hint": "Re-call with confirm=True to actually rename this sprint.",
            }
        response = self._http.post(f"/jira/rest/agile/1.0/sprint/{sprint_id}", json={"name": name})
        response.raise_for_status()
        data = response.json()
        return {"renamed": True, "dry_run": False, "id": data.get("id"), "name": data.get("name")}

    def list_sprints(self, board_id: int, state: str | None = None) -> list[dict[str, Any]]:
        """Return sprints on ``board_id`` (optionally filtered, e.g. ``state="future"``).

        Read-only, no confirm needed. ``state`` matches the Agile API's own
        comma-separated filter (``future``, ``active``, ``closed``).
        """
        params = {"state": state} if state else {}
        sprints: list[dict[str, Any]] = []
        start_at = 0
        while True:
            response = self._http.get(
                f"/jira/rest/agile/1.0/board/{board_id}/sprint",
                params={**params, "startAt": start_at, "maxResults": 50},
            )
            response.raise_for_status()
            data = response.json()
            sprints.extend(data.get("values", []))
            if data.get("isLast", True):
                break
            start_at += len(data.get("values", []))
        return sprints

    def get_or_create_sprint(self, board_id: int, name: str, confirm: bool = False) -> dict[str, Any]:
        """Reuse an existing sprint named ``name`` on ``board_id`` if one exists, else create it.

        Avoids spawning duplicate same-named sprints when ticket generation is
        re-run for the same sprint label. Dry-run unless ``confirm=True`` (the
        existing-sprint lookup itself is read-only and always happens).
        """
        existing = next((s for s in self.list_sprints(board_id) if s.get("name") == name), None)
        if existing is not None:
            return {
                "created": False, "reused": True, "dry_run": not confirm,
                "id": existing["id"], "name": existing["name"], "state": existing["state"],
            }
        result = self.create_sprint(board_id, name, confirm=confirm)
        result["reused"] = False
        return result

    def delete_sprint(self, sprint_id: int, confirm: bool = False) -> dict[str, Any]:
        """Permanently delete a sprint (dry-run unless ``confirm=True``).

        Any issues in the sprint are returned to the backlog by Jira, they are
        not deleted. Irreversible — the sprint itself cannot be recovered.
        """
        if not confirm:
            return {
                "deleted": False,
                "dry_run": True,
                "sprint_id": sprint_id,
                "hint": "Re-call with confirm=True to actually delete this sprint.",
            }
        response = self._http.delete(f"/jira/rest/agile/1.0/sprint/{sprint_id}")
        response.raise_for_status()
        return {"deleted": True, "dry_run": False, "sprint_id": sprint_id}

    # -- Insight/Assets (test vehicles) ------------------------------------
    def list_test_vehicles(
        self,
        iql: str = "objectType = Autos",
        max_results: int = 50,
        object_type_id: int | None = None,
        object_schema_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return objects from the Insight/Assets plugin matching an IQL filter.

        Defaults to the PEDSFLEET schema's "Autos" object type (instance-specific IDs —
        see :data:`INSIGHT_TEST_VEHICLE_SCHEMA_ID` / :data:`INSIGHT_TEST_VEHICLE_OBJECT_TYPE_ID`);
        pass ``object_type_id``/``object_schema_id`` to query a different object type
        (e.g. :data:`INSIGHT_VEHICLE_ASSET_OBJECT_TYPE_ID`/:data:`INSIGHT_VEHICLE_ASSET_SCHEMA_ID`
        for the "FZG" object type).

        :param iql: Insight Query Language filter (default: all "Autos" objects).
        :param max_results: Max objects to return in one page.
        :return: List of ``{"id", "key", "label"}`` dicts (label is the free-text vehicle name).
        """
        payload = {
            "objectTypeId": object_type_id if object_type_id is not None else self.INSIGHT_TEST_VEHICLE_OBJECT_TYPE_ID,
            "objectSchemaId": object_schema_id if object_schema_id is not None else self.INSIGHT_TEST_VEHICLE_SCHEMA_ID,
            "iql": iql,
            "page": 1,
            "asc": 1,
            "resultsPerPage": max_results,
            "includeAttributes": False,
        }
        response = self._http.post("/jira/rest/insight/1.0/object/navlist/iql", json=payload)
        response.raise_for_status()
        data = response.json()
        return [
            {"id": o["id"], "key": o["objectKey"], "label": o["label"]}
            for o in data.get("objectEntries", [])
        ]

    def get_insight_object(self, object_key: str) -> dict[str, Any]:
        """Fetch a single Insight/Assets object (any schema) by its object key or numeric id.

        Works for object keys of any schema/object type (not just "Autos" — e.g. the
        "FZG" object type on schema 607, keyed like ``PHUB-444909``), since Insight's
        ``/object/{key}`` endpoint accepts both the objectKey string and the numeric id.

        :param object_key: Insight object key (e.g. ``"PHUB-444909"``) or numeric id.
        :return: ``{"id", "key", "label", "object_type", "attributes"}`` where
            ``attributes`` maps attribute name -> list of display values.
        """
        response = self._http.get(f"/jira/rest/insight/1.0/object/{object_key}")
        response.raise_for_status()
        obj = response.json()
        attrs_response = self._http.get(f"/jira/rest/insight/1.0/object/{obj['id']}/attributes")
        attrs_response.raise_for_status()
        attributes = {
            a["objectTypeAttribute"]["name"]: [
                v.get("displayValue") for v in a.get("objectAttributeValues", [])
            ]
            for a in attrs_response.json()
        }
        return {
            "id": obj["id"],
            "key": obj["objectKey"],
            "label": obj["label"],
            "object_type": obj["objectType"]["name"],
            "attributes": attributes,
        }

    def add_object_type_attribute(
        self,
        object_type_id: int,
        name: str,
        attr_type: int = 0,
        max_cardinality: int = 1,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Create a new attribute definition on an Insight object type (dry-run unless ``confirm=True``).

        This is a SHARED, schema-level change — visible to every object of this type
        on the instance, not just one vehicle. Confirm the object type/schema before
        calling with ``confirm=True``.

        :param object_type_id: Insight object type id (e.g. 4321 for "FZG").
        :param name: Attribute name (e.g. "Tests").
        :param attr_type: Insight attribute type id (0 = Text).
        :param max_cardinality: Max values per object for this attribute (1 = single
         value, -1 = unlimited/list — 0 is rejected by the API).
        """
        payload = {
            "name": name,
            "type": attr_type,
            "defaultTypeId": 0,
            "maximumCardinality": max_cardinality,
        }
        if not confirm:
            return {
                "created": False,
                "dry_run": True,
                "object_type_id": object_type_id,
                "preview": payload,
                "hint": "Re-call with confirm=True to actually create this attribute.",
            }

        response = self._http.post(
            f"/jira/rest/insight/1.0/objecttypeattribute/{object_type_id}", json=payload
        )
        response.raise_for_status()
        data = response.json()
        return {
            "created": True,
            "dry_run": False,
            "object_type_id": object_type_id,
            "attribute_id": data.get("id"),
            "name": data.get("name"),
        }

    def get_object_type_attributes(self, object_type_id: int) -> list[dict[str, Any]]:
        """Return the attribute definitions for an Insight object type."""
        response = self._http.get(
            f"/jira/rest/insight/1.0/objecttype/{object_type_id}/attributes"
        )
        response.raise_for_status()
        return response.json()

    def create_insight_object(self, object_type_id: int,
                              attribute_values: dict[int, list[str]],
                              confirm: bool = False) -> dict[str, Any]:
        """Create an Assets object with explicit object type attribute values."""
        payload = {
            "objectTypeId": object_type_id,
            "attributes": [
                {"objectTypeAttributeId": attr_id,
                 "objectAttributeValues": [{"value": value} for value in values]}
                for attr_id, values in attribute_values.items()
            ],
        }
        if not confirm:
            return {"created": False, "preview": payload}
        response = self._http.post("/jira/rest/insight/1.0/object/create", json=payload)
        response.raise_for_status()
        return response.json()

    def update_insight_object_attributes(
        self,
        object_id: int,
        attribute_values: dict[int, list[str]],
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Set attribute values on an existing Insight object (dry-run unless ``confirm=True``).

        :param object_id: Numeric Insight object id (not the object key).
        :param attribute_values: Maps ``objectTypeAttributeId`` -> list of string values
            (multiple entries only valid for attributes with ``maximumCardinality`` != 1).
            When writing multiple multi-value attributes together (e.g. "Tests" and
            "Testplanung (KW)"), pass matching-length lists in the same call so the
            index-based pairing between them stays aligned.
        """
        payload = {
            "attributes": [
                {
                    "objectTypeAttributeId": attr_id,
                    "objectAttributeValues": [{"value": v} for v in values],
                }
                for attr_id, values in attribute_values.items()
            ]
        }
        if not confirm:
            return {
                "updated": False,
                "dry_run": True,
                "object_id": object_id,
                "preview": payload,
                "hint": "Re-call with confirm=True to actually update this object.",
            }

        response = self._http.put(f"/jira/rest/insight/1.0/object/{object_id}", json=payload)
        response.raise_for_status()
        return {"updated": True, "dry_run": False, "object_id": object_id}

    def add_object_comment(self, object_id: int, body: str, confirm: bool = False) -> dict[str, Any]:
        """Post a comment on an Insight/Assets object (dry-run unless ``confirm=True``).

        Uses ``POST /jira/rest/insight/1.0/comment/create`` — separate from Jira issue
        comments and :data:`BOT_COMMENT_PREFIX`-tagged the same way for auditability.

        :param object_id: Numeric Insight object id (not the object key).
        :param body: Comment text.
        """
        if not body or not body.strip():
            raise ValueError("Comment body must be non-empty.")
        rendered = body.strip()
        if not rendered.startswith(BOT_COMMENT_PREFIX):
            rendered = f"{BOT_COMMENT_PREFIX}\n\n{rendered}"

        if not confirm:
            return {
                "posted": False,
                "dry_run": True,
                "object_id": object_id,
                "preview": rendered,
                "hint": "Re-call with confirm=True to actually post this comment.",
            }

        response = self._http.post(
            "/jira/rest/insight/1.0/comment/create",
            json={"objectId": object_id, "comment": rendered},
        )
        response.raise_for_status()
        data = response.json()
        return {
            "posted": True,
            "dry_run": False,
            "object_id": object_id,
            "comment_id": data.get("id"),
            "preview": rendered,
        }
