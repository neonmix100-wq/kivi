from __future__ import annotations

from pathlib import Path

import pytest

from kivi.safety.policy import (
    PolicyEngine,
    PolicyError,
    is_within,
    normalize_windows_path,
)


@pytest.fixture()
def policy() -> PolicyEngine:
    return PolicyEngine()


def test_protected_prefix_is_denied(policy: PolicyEngine) -> None:
    decision = policy.check_path(r"C:\Windows\System32\drivers\etc\hosts", "write")
    assert not decision.allowed
    assert decision.rule == "path-protected"


def test_protected_prefix_is_case_insensitive(policy: PolicyEngine) -> None:
    assert not policy.check_path(r"c:/WINDOWS/System32", "read").allowed


def test_program_files_is_denied(policy: PolicyEngine) -> None:
    assert not policy.check_path(r"C:\Program Files\Kivi\kivi.exe", "exec").allowed


def test_user_file_is_allowed(policy: PolicyEngine) -> None:
    decision = policy.check_path(r"C:\Users\minea\Documents\notes.txt", "write")
    assert decision.allowed


def test_parent_traversal_is_rejected(policy: PolicyEngine) -> None:
    decision = policy.check_path(r"C:\Windows\..\Users\minea\notes.txt", "read")
    assert not decision.allowed
    assert decision.rule == "path-invalid"


def test_alternate_data_stream_is_rejected(policy: PolicyEngine) -> None:
    decision = policy.check_path(r"C:\Users\minea\notes.txt:hidden", "write")
    assert not decision.allowed
    assert decision.rule == "path-invalid"


def test_device_path_is_rejected(policy: PolicyEngine) -> None:
    decision = policy.check_path(r"\\?\C:\Windows\System32", "read")
    assert not decision.allowed
    assert decision.rule == "path-invalid"


def test_unc_path_is_denied_by_default(policy: PolicyEngine) -> None:
    decision = policy.check_path(r"\\server\share\file.txt", "read")
    assert not decision.allowed
    assert decision.rule == "path-unc"


def test_unc_can_be_enabled() -> None:
    assert PolicyEngine(allow_unc=True).check_path(r"\\server\share\file.txt").allowed


def test_trailing_dot_and_space_are_trimmed() -> None:
    assert normalize_windows_path(r"C:\temp\file.txt. ") == r"c:\temp\file.txt"


def test_empty_path_is_invalid(policy: PolicyEngine) -> None:
    assert policy.check_path("   ").rule == "path-invalid"


@pytest.mark.parametrize(
    "command",
    [
        "diskpart /s script.txt",
        [r"C:\Windows\System32\format.com", "D:"],
        "bcdedit /set safeboot minimal",
        r"reg delete HKLM\SOFTWARE\Microsoft /f",
        "sc.exe create evil binPath= C:\\evil.exe",
        "sc.exe stop WinDefend",
        "net user hacker pass /add",
        "net localgroup administrators hacker /add",
        "powershell -enc ZQBjAGgAbwA=",
        "powershell Remove-Item C:\\data -Recurse -Force",
    ],
)
def test_destructive_commands_are_denied(policy: PolicyEngine, command: str | list[str]) -> None:
    decision = policy.check_command(command)
    assert not decision.allowed, command
    assert decision.rule in {"cmd-system", "cmd-ps"}
    with pytest.raises(PolicyError):
        decision.require()


@pytest.mark.parametrize(
    "command",
    [
        "reg query HKCU\\Software\\Kivi",
        "sc.exe query Kivi",
        "net use Z: \\\\server\\share",
        "notepad.exe C:\\Users\\minea\\notes.txt",
        "python -V",
    ],
)
def test_ordinary_commands_are_allowed(policy: PolicyEngine, command: str) -> None:
    assert policy.check_command(command).allowed


def test_empty_command_is_denied(policy: PolicyEngine) -> None:
    assert policy.check_command([]).rule == "cmd-empty"


def test_unknown_access_level_is_rejected(policy: PolicyEngine) -> None:
    with pytest.raises(ValueError):
        policy.check_path(r"C:\temp", "delete")  # type: ignore[arg-type]


def test_is_within() -> None:
    root = Path(r"C:\Users\minea\Kivi")
    assert is_within(root, root / "notes.txt")
    assert is_within(root, root)
    assert not is_within(root, Path(r"C:\Users\minea\Other\notes.txt"))
