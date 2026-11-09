"""A4-Bounded policy checks.

Text is never an authority. Every filesystem or process action passes through
this module before it reaches the operating system. The checks are deliberately
small and deterministic so they can be unit-tested exhaustively.

Hard denies are the outer wall: even an approved task cannot cross them.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Literal

Access = Literal["read", "write", "exec"]

# System locations that are never writable by Kivi, regardless of approval.
DEFAULT_DENY_PREFIXES: tuple[str, ...] = (
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData\Microsoft",
    r"C:\ProgramData\Package Cache",
    r"C:\$Recycle.Bin",
    r"C:\System Volume Information",
    r"C:\Recovery",
    r"C:\Boot",
    r"C:\PerfLogs",
)

# Executables with system-wide destructive potential.
DENY_EXECUTABLES: frozenset[str] = frozenset(
    {
        "diskpart.exe",
        "format.com",
        "bcdedit.exe",
        "bootrec.exe",
        "vssadmin.exe",
        "wbadmin.exe",
        "cipher.exe",
        "takeown.exe",
        "icacls.exe",
        "reg.exe",  # narrowed below: only HKLM/SAM/SECURITY/SYSTEM are denied
        "sc.exe",
        "net.exe",
        "net1.exe",
    }
)

REG_DENY_KEYS = ("hklm", "hkey_local_machine", r"\sam", r"\security", r"\system")

# PowerShell can re-implement anything above; block the obvious escapes.
POWERSHELL_DENY_PATTERNS = (
    re.compile(r"-e(nc|ncodedcommand)?\s", re.IGNORECASE),
    re.compile(r"\bremove-item\b.*-recurse", re.IGNORECASE),
    re.compile(r"\bset-mppreference\b", re.IGNORECASE),
    re.compile(r"\bstop-service\b.*(winsock|windefend|kivi)", re.IGNORECASE),
)

_ADS_PATTERN = re.compile(r"^[A-Za-z]:[\\/][^:]*:")  # C:\file.txt:stream
_SHORT_NAME_PATTERN = re.compile(r"~\d")
_DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "\\GLOBAL??\\")


class PolicyError(PermissionError):
    """Raised when an action is denied by policy."""


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: str = ""
    rule: str | None = None

    @classmethod
    def allow(cls) -> Decision:
        return cls(True)

    @classmethod
    def deny(cls, reason: str, rule: str) -> Decision:
        return cls(False, reason, rule)

    def require(self) -> None:
        if not self.allowed:
            raise PolicyError(f"{self.rule}: {self.reason}")


def _expand_short_names(path: str) -> str:
    """Expand 8.3 names for an existing path, when running on Windows."""
    if sys.platform != "win32":
        return path
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel32.GetLongPathNameW(path, buffer, len(buffer))
        if 0 < length < len(buffer):
            return buffer.value
    except Exception:  # noqa: BLE001 - best effort; the pattern check still runs
        pass
    return path


def normalize_windows_path(raw: str | os.PathLike[str]) -> str:
    """Return a canonical, case-folded Windows path without a trailing slash.

    Rejects alternate data streams, device paths, parent traversal, and 8.3
    short names that could dodge the protected-prefix checks. Relative paths
    are accepted; callers must resolve them against a trusted root first.

    Raises ``ValueError`` for inputs that cannot be judged safely.
    """
    text = str(raw).strip().strip('"')
    if not text:
        raise ValueError("empty path")

    if _ADS_PATTERN.match(text):
        raise ValueError("alternate data streams are not allowed")
    if any(text.upper().startswith(prefix) for prefix in _DEVICE_PREFIXES):
        raise ValueError("device paths are not allowed")

    if any(part == ".." for part in PureWindowsPath(text).parts):
        raise ValueError("parent traversal is not allowed")

    normalized = ntpath.normpath(text)

    drive, rest = ntpath.splitdrive(normalized)
    is_absolute = bool(drive) or normalized.startswith("\\")

    if is_absolute and os.path.exists(normalized):
        normalized = os.path.realpath(_expand_short_names(normalized))
        drive, rest = ntpath.splitdrive(normalized)

    if drive:
        components = [part.rstrip(" .") for part in rest.split("\\") if part]
        if any(not component for component in components):
            raise ValueError("invalid path component")
        for component in components:
            if _SHORT_NAME_PATTERN.search(component):
                raise ValueError("8.3 short names are not allowed")
        normalized = drive + "\\" + "\\".join(components) if components else drive
    else:
        components = [part.rstrip(" .") for part in normalized.split("\\") if part]
        if any(not component for component in components):
            raise ValueError("invalid path component")
        for component in components:
            if _SHORT_NAME_PATTERN.search(component):
                raise ValueError("8.3 short names are not allowed")
        normalized = "\\".join(components)

    return ntpath.normcase(normalized).rstrip("\\/")


class PolicyEngine:
    def __init__(
        self,
        *,
        deny_prefixes: tuple[str, ...] = DEFAULT_DENY_PREFIXES,
        allow_unc: bool = False,
    ) -> None:
        self.deny_prefixes = tuple(normalize_windows_path(p) for p in deny_prefixes)
        self.allow_unc = allow_unc

    def check_path(self, raw: str | os.PathLike[str], access: Access = "read") -> Decision:
        if access not in ("read", "write", "exec"):
            raise ValueError(f"unknown access level: {access}")
        try:
            path = normalize_windows_path(raw)
        except ValueError as exc:
            return Decision.deny(str(exc), "path-invalid")

        if path.startswith("\\\\") and not self.allow_unc:
            return Decision.deny("UNC paths are disabled", "path-unc")

        for prefix in self.deny_prefixes:
            if path == prefix or path.startswith(prefix + "\\"):
                return Decision.deny(f"protected location: {prefix}", "path-protected")

        return Decision.allow()

    def check_command(self, argv: list[str] | str) -> Decision:
        parts = argv.strip().split() if isinstance(argv, str) else list(argv)

        if not parts:
            return Decision.deny("empty command", "cmd-empty")

        exe_name = ntpath.basename(parts[0]).lower()
        exe = exe_name if exe_name in DENY_EXECUTABLES else f"{exe_name}.exe"
        args = [part.lower() for part in parts[1:]]
        rest = " ".join(args)

        if exe in DENY_EXECUTABLES:
            verb = args[0] if args else ""
            if exe == "reg.exe" and not any(key in rest for key in REG_DENY_KEYS):
                return Decision.allow()
            if exe in ("net.exe", "net1.exe") and verb not in (
                "user",
                "group",
                "localgroup",
                "accounts",
            ):
                return Decision.allow()
            if exe == "sc.exe" and verb not in ("create", "delete", "config", "stop"):
                return Decision.allow()
            return Decision.deny(f"system-level command: {exe}", "cmd-system")

        if exe_name in ("powershell.exe", "pwsh.exe", "powershell", "pwsh"):
            joined = " ".join(parts[1:])
            for pattern in POWERSHELL_DENY_PATTERNS:
                if pattern.search(joined):
                    return Decision.deny(f"blocked PowerShell pattern: {pattern.pattern}", "cmd-ps")

        return Decision.allow()


def is_within(root: Path, candidate: Path) -> bool:
    """True when ``candidate`` is ``root`` or lives underneath it."""
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False
