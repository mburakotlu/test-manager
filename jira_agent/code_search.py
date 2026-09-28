"""Workspace-sandboxed code search and file reading for the Jira agent.

The agent uses these helpers to correlate Jira ticket text with the actual
codebase. All paths are resolved relative to ``WORKSPACE_ROOT`` and any
attempt to escape that root raises :class:`ValueError`.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".venv", "venv", "build", "dist", "__pycache__", ".git", "node_modules",
)


def _resolve_inside(root: Path, candidate: str | Path) -> Path:
    """Resolve ``candidate`` and ensure it stays inside ``root``."""
    if ".." in Path(candidate).parts:
        raise ValueError(f"Path traversal not allowed: {candidate!r}")
    resolved = (root / candidate).resolve() if not Path(candidate).is_absolute() \
        else Path(candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"Path {resolved} is outside the workspace root {root}"
        ) from exc
    return resolved


def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a subprocess. Extracted for test injection."""
    return subprocess.run(  # noqa: S603 — args are constructed, not shell-interpreted
        cmd, check=False, capture_output=True, text=True, timeout=30,
    )


class CodeSearch:
    """Workspace-scoped code search and reader."""

    def __init__(self, workspace_root: Path, runner=_runner) -> None:
        """Initialise with the workspace root and an optional subprocess runner."""
        self._root = workspace_root.resolve()
        self._runner = runner

    @property
    def root(self) -> Path:
        """Return the resolved workspace root."""
        return self._root

    def search(
        self,
        pattern: str,
        path_glob: str | None = None,
        max_results: int = 50,
    ) -> list[dict[str, Any]]:
        """Run ``rg`` for ``pattern`` and return ``[{path,line,text}, ...]``.

        ``path_glob`` is forwarded to ``rg --glob`` and validated against
        path-traversal. Falls back to ``grep -rn`` if ripgrep is not installed.
        """
        if not pattern:
            raise ValueError("pattern must be non-empty")
        if path_glob and ".." in path_glob:
            raise ValueError(f"Path traversal not allowed in glob: {path_glob!r}")

        rg = shutil.which("rg")
        if rg:
            cmd = [rg, "--no-heading", "--with-filename", "--line-number",
                   "--max-count", str(max_results), "--color", "never"]
            for excl in DEFAULT_EXCLUDES:
                cmd += ["--glob", f"!**/{excl}/**"]
            if path_glob:
                cmd += ["--glob", path_glob]
            cmd += ["--", pattern, str(self._root)]
        else:
            logger.warning("ripgrep not found; falling back to grep")
            cmd = ["grep", "-rn", "--", pattern, str(self._root)]

        result = self._runner(cmd)
        # rg/grep exit 1 means "no matches"; only fail on >=2
        if result.returncode >= 2:
            raise RuntimeError(
                f"code search failed (rc={result.returncode}): {result.stderr.strip()}"
            )

        hits: list[dict[str, Any]] = []
        for raw_line in (result.stdout or "").splitlines():
            parsed = _parse_grep_line(raw_line, self._root)
            if parsed is not None:
                hits.append(parsed)
            if len(hits) >= max_results:
                break
        return hits

    def read(self, relative_path: str, start: int = 1, end: int = 400) -> dict[str, Any]:
        """Return a slice of a workspace file.

        ``start``/``end`` are 1-based, inclusive. The returned ``content``
        preserves the original line numbers in the ``LINENO | TEXT`` format
        so the LLM can reference them when drafting clarifications.
        """
        if start < 1:
            raise ValueError("start must be >= 1")
        if end < start:
            raise ValueError("end must be >= start")

        target = _resolve_inside(self._root, relative_path)
        if not target.is_file():
            raise FileNotFoundError(f"Not a file: {relative_path}")

        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        sliced = lines[start - 1:end]
        numbered = "\n".join(f"{start + i} | {line}" for i, line in enumerate(sliced))
        return {
            "path": str(target.relative_to(self._root)).replace("\\", "/"),
            "start": start,
            "end": min(end, len(lines)),
            "total_lines": len(lines),
            "content": numbered,
        }


def _parse_grep_line(line: str, root: Path) -> dict[str, Any] | None:
    """Parse a ``path:lineno:text`` line. Tolerates Windows drive letters."""
    if not line:
        return None
    # Windows: "C:\path\file.py:12:text" — split from the right twice.
    parts = line.rsplit(":", 2)
    if len(parts) < 3:
        return None
    path_part, lineno_part, text_part = parts
    try:
        lineno = int(lineno_part)
    except ValueError:
        return None
    path = Path(path_part)
    try:
        rel = path.resolve().relative_to(root)
        path_out = str(rel).replace("\\", "/")
    except (ValueError, OSError):
        path_out = path_part.replace("\\", "/")
    return {"path": path_out, "line": lineno, "text": text_part}
