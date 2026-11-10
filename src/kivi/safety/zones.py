"""Protected Zones: hard-deny paths and escape checks.

The built-in zone list lives in code and cannot be widened by a tool call: a
task may only *add* zones (through configuration read by the application), and
the workspace boundary is checked after symlinks and junctions are resolved.
That keeps the rule "the agent cannot edit its own safety core, audit or
denylist" enforceable even when a write is approved.

All paths are canonicalised by :func:`kivi.safety.policy.normalize_windows_path`,
which rejects alternate data streams, device paths, 8.3 short names and parent
traversal before any zone rule runs.
"""

from __future__ import annotations

import fnmatch
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from kivi.safety.policy import (
    DEFAULT_DENY_PREFIXES,
    Access,
    normalize_windows_path,
)

# Files that must never be read by a tool, even inside the workspace.
SENSITIVE_NAMES: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "*.kdbx",
    "id_rsa*",
    "id_ed25519*",
    "secrets.*",
    "credentials.*",
)

# Directories that are off limits by name at any depth.
SENSITIVE_DIRS: frozenset[str] = frozenset({"private", "voice-dataset", ".git", ".ssh"})

# Workspace-relative locations Kivi may read but never write.
_WORKSPACE_WRITE_DENY: tuple[tuple[str, str], ...] = (
    ("src/kivi/safety", "safety core"),
    ("src/kivi/kernel", "audit and storage core"),
    ("config", "configuration and denylist"),
    ("data/kivi.db", "audit database"),
    ("data/killswitch.json", "kill switch flag"),
    ("data/logs", "append-only logs"),
    ("data/backups", "backups"),
)


class ZoneError(ValueError):
    """Raised when a path cannot be checked safely."""


@dataclass(frozen=True, slots=True)
class ZoneRule:
    """A single deny rule; ``prefix`` compares canonical path prefixes."""

    id: str
    reason: str
    access: frozenset[Access]
    prefix: str = ""
    name: str = ""
    directory: str = ""

    def matches(self, normalized: str, path: Path) -> bool:
        if self.prefix:
            prefix = self.prefix.rstrip("\\")
            return normalized == prefix or normalized.startswith(prefix + "\\")
        if self.name:
            return fnmatch.fnmatchcase(path.name.lower(), self.name)
        if self.directory:
            parts = {part.lower() for part in path.parts}
            return self.directory in parts
        return False


@dataclass(frozen=True, slots=True)
class ZoneDecision:
    """Result of a zone check; ``resolved`` is safe to hand to the OS."""

    allowed: bool
    reason: str = ""
    rule: str | None = None
    resolved: Path | None = None

    @classmethod
    def allow(cls, resolved: Path) -> ZoneDecision:
        return cls(True, resolved=resolved)

    @classmethod
    def deny(cls, reason: str, rule: str) -> ZoneDecision:
        return cls(False, reason, rule)

    def require(self) -> Path:
        if not self.allowed or self.resolved is None:
            raise ZoneError(f"{self.rule}: {self.reason}")
        return self.resolved


def _is_reparse_point(path: Path) -> bool:
    """True for symlinks and NTFS junctions (fail closed on any reparse tag)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if flag and attributes & flag:
        return True
    return path.is_symlink()


def _real(path: Path) -> Path:
    # realpath resolves existing symlink/junction prefixes; a missing tail is
    # appended unchanged, which is what write-to-a-new-file needs.
    return Path(os.path.realpath(str(path)))


class ProtectedZones:
    """Canonical path checks with a workspace boundary and built-in deny rules."""

    def __init__(
        self,
        workspace: Path,
        *,
        deny_prefixes: tuple[str, ...] = DEFAULT_DENY_PREFIXES,
        extra_rules: tuple[ZoneRule, ...] = (),
    ) -> None:
        self.workspace = _real(Path(workspace))
        self._workspace_norm = normalize_windows_path(str(self.workspace))
        rules: list[ZoneRule] = []
        for index, prefix in enumerate(deny_prefixes):
            rules.append(
                ZoneRule(
                    id=f"system-{index}",
                    reason=f"protected system location: {prefix}",
                    access=frozenset({"write", "exec"}),
                    prefix=normalize_windows_path(prefix),
                )
            )
        for relative, label in _WORKSPACE_WRITE_DENY:
            rules.append(
                ZoneRule(
                    id=f"core-{relative}",
                    reason=f"{label} is not writable by tools",
                    access=frozenset({"write"}),
                    prefix=normalize_windows_path(str(self.workspace / relative)),
                )
            )
        for name in SENSITIVE_NAMES:
            rules.append(
                ZoneRule(
                    id=f"secret-{name}",
                    reason="secret or key material",
                    access=frozenset({"read", "write"}),
                    name=name.lower(),
                )
            )
        for directory in sorted(SENSITIVE_DIRS):
            rules.append(
                ZoneRule(
                    id=f"dir-{directory}",
                    reason="private or repository-control directory",
                    access=frozenset({"read", "write"}),
                    directory=directory,
                )
            )
        self._rules = tuple(rules) + tuple(extra_rules)

    @property
    def rules(self) -> tuple[ZoneRule, ...]:
        return self._rules

    def _within_workspace(self, normalized: str) -> bool:
        root = self._workspace_norm
        return normalized == root or normalized.startswith(root + "\\")

    def check(self, raw: str | os.PathLike[str], access: Access = "read") -> ZoneDecision:
        """Canonicalise ``raw`` and apply the workspace and deny rules."""
        if access not in ("read", "write", "exec"):
            raise ValueError(f"unknown access level: {access}")
        try:
            normalized = normalize_windows_path(raw)
        except ValueError as exc:
            return ZoneDecision.deny(str(exc), "zone-invalid")

        if normalized.startswith("\\\\"):
            return ZoneDecision.deny("UNC paths are disabled", "zone-unc")

        path = Path(str(raw))
        if not path.is_absolute():
            path = self.workspace / path
        resolved = _real(path)
        try:
            resolved_norm = normalize_windows_path(str(resolved))
        except ValueError as exc:
            return ZoneDecision.deny(str(exc), "zone-invalid")

        if not self._within_workspace(resolved_norm):
            return ZoneDecision.deny(
                "path is outside the workspace",
                "zone-scope",
            )

        if _is_reparse_point(path):
            return ZoneDecision.deny(
                "symlinks and junctions are not allowed",
                "zone-reparse",
            )

        for rule in self._rules:
            if access in rule.access and rule.matches(resolved_norm, resolved):
                return ZoneDecision.deny(rule.reason, rule.id)

        return ZoneDecision.allow(resolved)


__all__ = [
    "SENSITIVE_DIRS",
    "SENSITIVE_NAMES",
    "ProtectedZones",
    "ZoneDecision",
    "ZoneError",
    "ZoneRule",
]
