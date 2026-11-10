"""Fast path: one deterministic tool call for simple, unambiguous commands.

The fast path exists to keep trivial requests fast and predictable. It matches a
small set of read-only commands (read a file, list a directory, list processes)
and delegates to the normal ToolExecutor with a short-lived grant. It never uses
a shell, never builds a command string and returns ``None`` for anything it does
not fully recognise so the deep path can plan instead.

Path traversal is not special-cased here: the filesystem tools run every path
through Protected Zones, so ``read ..\\secret`` is denied by the same authority
that protects the deep path.
"""

from __future__ import annotations

import re
from uuid import uuid4

from kivi.safety.permissions import PermissionEngine
from kivi.tools import ToolExecutor, ToolRegistry, ToolResult

_READ = re.compile(r"^(?:прочитай|read)\s+(.+)$", re.IGNORECASE)
_LIST = re.compile(r"^(?:список|list)\s+(.+)$", re.IGNORECASE)
_PROCESSES = re.compile(r"^(?:процессы|processes)$", re.IGNORECASE)

_QUOTE_PAIRS = {'"': '"', "'": "'", "«": "»", "“": "”", "`": "`"}
_TAIL = frozenset({"", ".", "!", "?", ";", ","})
_ALLOWED_RISKS = frozenset({"R0", "R1"})


def _extract_path(rest: str) -> str | None:
    """Return a path from the command tail, respecting one pair of quotes."""
    text = rest.strip()
    if not text:
        return None
    if any(char in text for char in "\r\n\x00"):
        return None
    opening = text[0]
    if opening in _QUOTE_PAIRS:
        closing = _QUOTE_PAIRS[opening]
        end = text.find(closing, 1)
        if end == -1:
            return None
        path = text[1:end].strip()
        tail = text[end + 1 :].strip()
        if tail and tail not in _TAIL:
            return None
        return path or None
    return text or None


class FastPath:
    """Runs a whitelisted simple command through the normal executor."""

    def __init__(
        self,
        registry: ToolRegistry,
        executor: ToolExecutor,
        permissions: PermissionEngine,
    ) -> None:
        self.registry = registry
        self.executor = executor
        self.permissions = permissions

    def _run(self, name: str, args: dict[str, object], task_id: str) -> ToolResult | None:
        tool = self.registry.get(name)
        if tool is None or tool.risk not in _ALLOWED_RISKS:
            return None
        actual_task = task_id or f"fast-{uuid4().hex[:12]}"
        grant = self.permissions.issue(actual_task, tool.capabilities)
        return self.executor.run(name, args, grant=grant)

    def try_run(self, goal: str, task_id: str) -> ToolResult | None:
        """Return a result for a recognised simple command, else ``None``."""
        text = goal.strip()
        if not text:
            return None

        match = _READ.match(text)
        if match is not None:
            path = _extract_path(match.group(1))
            if path is None:
                return None
            return self._run("fs.read", {"path": path}, task_id)

        match = _LIST.match(text)
        if match is not None:
            path = _extract_path(match.group(1))
            if path is None:
                return None
            return self._run("fs.list", {"path": path}, task_id)

        if _PROCESSES.match(text.rstrip(" .!?")):
            return self._run("proc.list", {"limit": 50}, task_id)
        return None


__all__ = ["FastPath"]
