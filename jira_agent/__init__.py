"""Jira agent package.

Provides read-only Jira access, guarded comment/link/attachment posting, and
workspace-sandboxed code search, exposed both as an MCP stdio server and as a
plain Python client usable from scripts/terminal.
"""

__all__ = ["config", "jira_client", "code_search", "server"]
