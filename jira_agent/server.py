"""MCP stdio server exposing Jira + code-search tools.

Run as ``python -m jira_agent.server``. Add to your MCP client config
(Claude Desktop / Cline / Copilot) — see ``README.md``.

The server intentionally contains no LLM calls. It only exposes tools; the
"agent" is the prompt executed by the host LLM client.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .code_search import CodeSearch
from .config import JiraConfig, load_config
from .jira_client import JiraClient


logger = logging.getLogger(__name__)


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "jira_get_issue",
        "description": "Fetch a Jira issue (summary, description, status, assignee, "
                       "reporter, priority, labels, components, fix versions).",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string", "description": "Issue key, e.g. PHUB-123"}},
            "required": ["key"],
        },
    },
    {
        "name": "jira_get_comments",
        "description": "Return all comments on a Jira issue.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "jira_get_attachments",
        "description": "Return attachment metadata (no file content) for a Jira issue.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "jira_search",
        "description": "Run a JQL query and return up to max_results compact issue dicts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "jql": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
            },
            "required": ["jql"],
        },
    },
    {
        "name": "jira_add_comment",
        "description": "Add a comment to a Jira issue. DRY-RUN by default — call with "
                       "confirm=true to actually post. Body is auto-prefixed with "
                       "'🤖 [jira-agent]' so humans can audit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "body": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["key", "body"],
        },
    },
    {
        "name": "jira_add_link",
        "description": "Add a remote web link to a Jira issue (e.g. GitLab MR, docs page). "
                       "DRY-RUN by default — call with confirm=true to actually post. "
                       "The link appears in the ticket's 'Links' section.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Issue key, e.g. PHUB-123"},
                "url": {"type": "string", "description": "URL to link (e.g. MR URL)"},
                "title": {"type": "string", "description": "Display title for the link"},
                "relationship": {"type": "string", "default": "relates to",
                                 "description": "Relationship label (e.g. 'relates to', 'is implemented by')"},
                "icon_url": {"type": "string", "default": "",
                             "description": "Optional 16x16 icon URL"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["key", "url", "title"],
        },
    },
    {
        "name": "jira_add_attachment",
        "description": "Upload a file attachment to a Jira issue. "
                       "DRY-RUN by default — call with confirm=true to actually upload. "
                       "File must exist on the local filesystem.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Issue key, e.g. PHUB-123"},
                "file_path": {"type": "string",
                              "description": "Absolute or relative path to the file to upload"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["key", "file_path"],
        },
    },
    {
        "name": "code_search",
        "description": "ripgrep-backed search inside the workspace root. Returns "
                       "[{path,line,text}, ...].",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path_glob": {"type": ["string", "null"], "default": None},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "code_read",
        "description": "Read a slice of a workspace file. Lines are 1-based and inclusive. "
                       "Output is prefixed with 'LINENO | TEXT'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "relative_path": {"type": "string"},
                "start": {"type": "integer", "minimum": 1, "default": 1},
                "end": {"type": "integer", "minimum": 1, "default": 400},
            },
            "required": ["relative_path"],
        },
    },
]


class JiraMcpHandlers:
    """Plain-Python tool handlers (transport-agnostic).

    Kept separate from the MCP framing layer so unit tests can call the
    handlers directly without spinning up stdio.
    """

    def __init__(self, jira: JiraClient, code: CodeSearch) -> None:
        """Initialise with a Jira client and a code-search helper."""
        self._jira = jira
        self._code = code

    @classmethod
    def from_config(cls, config: JiraConfig) -> "JiraMcpHandlers":
        """Build handlers from a :class:`JiraConfig`."""
        return cls(JiraClient(config), CodeSearch(config.workspace_root))

    @property
    def tool_names(self) -> list[str]:
        """Return the list of registered tool names."""
        return [t["name"] for t in TOOL_SPECS]

    def dispatch(self, name: str, arguments: dict[str, Any]) -> Any:  # noqa: ANN401
        """Dispatch a tool call by name."""
        args = arguments or {}
        if name == "jira_get_issue":
            return self._jira.get_issue(args["key"]).to_dict()
        if name == "jira_get_comments":
            return self._jira.get_comments(args["key"])
        if name == "jira_get_attachments":
            return self._jira.get_attachments(args["key"])
        if name == "jira_search":
            return self._jira.search(args["jql"], int(args.get("max_results", 25)))
        if name == "jira_add_comment":
            return self._jira.add_comment(
                args["key"], args["body"], bool(args.get("confirm", False)),
            )
        if name == "jira_add_link":
            return self._jira.add_remote_link(
                args["key"],
                args["url"],
                args["title"],
                args.get("relationship", "relates to"),
                args.get("icon_url", ""),
                bool(args.get("confirm", False)),
            )
        if name == "jira_add_attachment":
            return self._jira.add_attachment(
                args["key"],
                args["file_path"],
                bool(args.get("confirm", False)),
            )
        if name == "code_search":
            return self._code.search(
                args["pattern"],
                args.get("path_glob"),
                int(args.get("max_results", 50)),
            )
        if name == "code_read":
            return self._code.read(
                args["relative_path"],
                int(args.get("start", 1)),
                int(args.get("end", 400)),
            )
        raise ValueError(f"Unknown tool: {name}")


def _run_stdio(handlers: JiraMcpHandlers) -> None:
    """Run the MCP stdio server using the official ``mcp`` SDK if available.

    Imported lazily so unit tests don't require the ``mcp`` package.
    """
    try:
        from mcp.server import Server  # type: ignore[import-not-found]
        from mcp.server.stdio import stdio_server  # type: ignore[import-not-found]
        from mcp.types import TextContent, Tool  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover — import-time guard
        raise SystemExit(
            "The 'mcp' package is required to run the server. "
            "Install with: pip install -r requirements.txt"
        ) from exc

    import asyncio

    server: Server = Server("jira-agent")

    @server.list_tools()
    async def list_tools() -> list[Tool]:  # type: ignore[no-untyped-def]
        return [
            Tool(name=spec["name"], description=spec["description"],
                 inputSchema=spec["inputSchema"])
            for spec in TOOL_SPECS
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:  # type: ignore[no-untyped-def]
        try:
            result = handlers.dispatch(name, arguments)
            payload = json.dumps(result, indent=2, default=str, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 — surface any error to the client
            payload = json.dumps({"error": type(exc).__name__, "message": str(exc)})
        return [TextContent(type="text", text=payload)]

    async def _main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(_main())


def main() -> None:
    """Entry point for ``python -m jira_agent.server``."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    handlers = JiraMcpHandlers.from_config(config)
    logger.info("Starting jira-agent MCP server (workspace=%s)", config.workspace_root)
    _run_stdio(handlers)


if __name__ == "__main__":  # pragma: no cover
    main()
