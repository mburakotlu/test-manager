"""Configuration loader for the Jira agent.

Reads connection parameters from environment variables. Fails fast with a clear
message if any required variable is missing. No secrets are ever logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BOT_COMMENT_PREFIX = "🤖 [jira-agent]"

# Corporate proxy hostname (not a secret) — override via JIRA_PROXY_URL if needed.
DEFAULT_PROXY = "http://http-proxy.porsche.org:3133"

# Smart Checklist plugin field on this Jira instance — verified against PHUB-2182.
# Custom field IDs are instance-specific; re-verify via GET /jira/rest/api/2/field
# if pointed at a different Jira instance/project.
DEFAULT_CHECKLIST_FIELD_ID = "customfield_17802"

# Plain fields on PHUB's Task create screen used to link an issue back to a vehicle.
# The "real" Insight object-picker field ("Asset", customfield_15815) exists on this
# instance but is NOT on PHUB's Task create screen (same class of problem as the
# Checklists field — would need a Jira admin to add it). These two plain text fields
# ARE on the screen and settable via the normal issue API. Verified 2026-09-09.
VIN_FIELD_ID = "customfield_12602"  # "VIN"
VEHICLE_DESCRIPTION_FIELD_ID = "customfield_18800"  # "Vehicle description"

# Cascading-select field required by board 114663's saved filter
# ("Product category" in cascadeOption(337005)) — tickets missing this value never
# show up on that Agile board's backlog, regardless of sprint. Verified 2026-09-14
# against PHUB-1348 (a real board ticket). Re-verify option ids if reused elsewhere.
PRODUCT_CATEGORY_FIELD_ID = "customfield_11801"
PRODUCT_CATEGORY_VALUE = {"value": "AP1_Automatisierung", "child": {"value": "R4 Automatisierung"}}


@dataclass(frozen=True)
class JiraConfig:
    """Runtime configuration for the Jira agent."""

    base_url: str
    email: str
    api_token: str
    workspace_root: Path
    use_bearer_token: bool = True
    is_tec_net: bool = True
    verify_ssl: bool = True
    proxy_url: str = DEFAULT_PROXY
    checklist_field_id: str = DEFAULT_CHECKLIST_FIELD_ID
    web_base_url: str = ""

    @property
    def effective_web_base_url(self) -> str:
        """Return the browser-facing Jira base URL.

        The REST API often lives on a separate subdomain (e.g. ``api.skyway...``)
        with different TLS/proxy handling than the human-facing web UI (e.g.
        ``skyway...``). Defaults to stripping an ``api.`` prefix from ``base_url``
        unless ``JIRA_WEB_BASE_URL`` is explicitly set.
        """
        if self.web_base_url:
            return self.web_base_url
        return self.base_url.replace("://api.", "://", 1)

    @property
    def auth_mode(self) -> str:
        """Return the effective auth mode: 'bearer' or 'basic'."""
        if self.use_bearer_token:
            return "bearer"
        return "basic"

    @property
    def auth(self) -> tuple[str, str]:
        """Return the basic-auth tuple for ``httpx`` (Cloud mode)."""
        return (self.email, self.api_token)

    @property
    def proxies(self) -> str | None:
        """Return the proxy URL if not on tec-net, else None."""
        if not self.is_tec_net:
            return self.proxy_url
        return None


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Environment variable {name!r} is required for the Jira agent. "
            "Copy .env.example to .env and fill in your values, or set it directly "
            "in your shell. See README.md for setup instructions."
        )
    return value


def load_config() -> JiraConfig:
    """Load and validate config from environment variables.

    Required:
      - ``JIRA_BASE_URL`` (e.g. ``https://api.skyway.porsche.com``)
      - ``JIRA_EMAIL``
      - ``JIRA_API_TOKEN`` (Personal Access Token — never hardcode this)

    Optional:
      - ``WORKSPACE_ROOT`` — defaults to the current working directory.
      - ``JIRA_AUTH_MODE`` — ``bearer`` (default) or ``basic``.
      - ``JIRA_IS_TEC_NET`` — ``true``/``false`` (default ``true``, no proxy).
      - ``JIRA_VERIFY_SSL`` — ``true``/``false`` (default ``true``).
      - ``JIRA_PROXY_URL`` — overrides the default corporate proxy.
    """
    base_url = _require_env("JIRA_BASE_URL").rstrip("/")
    email = _require_env("JIRA_EMAIL")
    token = _require_env("JIRA_API_TOKEN")
    root = Path(os.environ.get("WORKSPACE_ROOT", os.getcwd())).resolve()
    use_bearer = os.environ.get("JIRA_AUTH_MODE", "bearer").strip().lower() == "bearer"
    is_tec_net = os.environ.get("JIRA_IS_TEC_NET", "true").strip().lower() in ("true", "1", "yes")
    verify_ssl = os.environ.get("JIRA_VERIFY_SSL", "true").strip().lower() in ("true", "1", "yes")
    proxy_url = os.environ.get("JIRA_PROXY_URL", DEFAULT_PROXY).strip()
    checklist_field_id = os.environ.get("JIRA_CHECKLIST_FIELD_ID", DEFAULT_CHECKLIST_FIELD_ID).strip()
    web_base_url = os.environ.get("JIRA_WEB_BASE_URL", "").strip().rstrip("/")
    if not root.is_dir():
        raise RuntimeError(f"WORKSPACE_ROOT does not exist or is not a directory: {root}")
    return JiraConfig(
        base_url=base_url, email=email, api_token=token,
        workspace_root=root, use_bearer_token=use_bearer,
        is_tec_net=is_tec_net, verify_ssl=verify_ssl, proxy_url=proxy_url,
        checklist_field_id=checklist_field_id, web_base_url=web_base_url,
    )
