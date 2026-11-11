from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kivi.kernel.db import Database
from kivi.safety.permissions import PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools import Limits, ToolExecutor, ToolRegistry, build_registry
from kivi.tools.base import ToolResult
from kivi.tools.fs import FsReadTool
from kivi.tools.registry import RegistryError


class ToolEnv:
    def __init__(self, workspace: Path, db: Database, executor: ToolExecutor, grant) -> None:
        self.workspace = workspace
        self.db = db
        self.executor = executor
        self.grant = grant
        self.engine = executor.permissions

    def run(self, name: str, args: dict | None = None, **kwargs) -> ToolResult:
        return self.executor.run(name, args or {}, grant=self.grant, **kwargs)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    for relative in ("docs", "data/artifacts", "src/kivi/safety"):
        (root / relative).mkdir(parents=True)
    (root / "docs" / "note.txt").write_text("hello world", encoding="utf-8")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    return root


@pytest.fixture()
def env(tmp_path: Path, workspace: Path) -> ToolEnv:
    db = Database(tmp_path / "kivi.db")
    zones = ProtectedZones(workspace)
    engine = PermissionEngine(db, workspace=str(workspace))
    grant = engine.issue("t1", ["fs.read", "fs.write", "shell.exec"])
    executor = ToolExecutor(
        build_registry(),
        db,
        engine,
        zones,
        workspace=workspace,
    )
    tool_env = ToolEnv(workspace, db, executor, grant)
    yield tool_env
    db.close()


def test_registry_exposes_specs() -> None:
    registry = build_registry()
    assert registry.names() == (
        "app.launch",
        "app.terminate",
        "browser.click",
        "browser.close",
        "browser.download",
        "browser.fill",
        "browser.open",
        "browser.read",
        "clipboard.read",
        "clipboard.write",
        "code.patch",
        "code.run",
        "fs.list",
        "fs.read",
        "fs.write",
        "git.branch",
        "git.commit",
        "git.diff",
        "git.status",
        "http.get",
        "mail.attachment",
        "mail.draft",
        "mail.list",
        "mail.read",
        "mail.search",
        "proc.kill",
        "proc.list",
        "proc.start",
        "repo.index",
        "screen.capture",
        "shell.exec",
        "system.lock",
        "system.notify",
        "system.restart",
        "system.shutdown",
        "system.sleep",
        "system.volume.get",
        "system.volume.set",
        "telegram.send",
        "telegram.status",
        "telegram.updates",
        "ui.click",
        "ui.find",
        "ui.hotkey",
        "ui.type",
        "window.close",
        "window.focus",
        "window.list",
        "window.maximize",
        "window.minimize",
    )
    read = next(spec for spec in registry.specs() if spec.name == "fs.read")
    assert read.risk == "R0"
    assert read.capabilities == ("fs.read",)
    assert "properties" in read.schema
    assert read.schema["properties"]["path"]["type"] == "string"


def test_registry_rejects_duplicates() -> None:
    registry = ToolRegistry()
    registry.register(FsReadTool())
    with pytest.raises(RegistryError, match="duplicate"):
        registry.register(FsReadTool())


def test_registry_rejects_invalid_risk() -> None:
    class BadTool(FsReadTool):
        name = "bad.tool"
        risk = "R9"

    registry = ToolRegistry()
    with pytest.raises(RegistryError, match="risk"):
        registry.register(BadTool())


def test_read_returns_file(env: ToolEnv) -> None:
    result = env.run("fs.read", {"path": "docs/note.txt"})
    assert result.ok
    assert result.output == "hello world"
    assert result.trust == "untrusted:file"


def test_read_truncates_at_the_limit(env: ToolEnv) -> None:
    result = env.run("fs.read", {"path": "docs/note.txt", "max_bytes": 5})
    assert result.ok
    assert result.output == "hello"
    assert result.truncated is True


def test_read_does_not_split_a_utf8_character(env: ToolEnv) -> None:
    (env.workspace / "docs" / "utf8.txt").write_text("привет", encoding="utf-8")
    result = env.run("fs.read", {"path": "docs/utf8.txt", "max_bytes": 5})
    assert result.ok
    assert "\ufffd" not in result.output
    assert result.output == "пр"


def test_read_escapes_invalid_bytes_instead_of_replacement(env: ToolEnv) -> None:
    (env.workspace / "docs" / "bad.bin").write_bytes(b"\xff\xfe")
    result = env.run("fs.read", {"path": "docs/bad.bin"})
    assert result.ok
    assert "\ufffd" not in result.output
    assert "\\xff" in result.output


def test_read_missing_file_fails(env: ToolEnv) -> None:
    result = env.run("fs.read", {"path": "docs/missing.txt"})
    assert not result.ok
    assert "not a file" in result.error


def test_read_outside_workspace_is_denied(env: ToolEnv, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    result = env.run("fs.read", {"path": str(outside)})
    assert not result.ok
    assert "zone-scope" in result.error
    assert env.db.recent_audit()[0]["decision"] == "deny"


def test_read_secret_file_is_denied(env: ToolEnv) -> None:
    result = env.run("fs.read", {"path": ".env"})
    assert not result.ok
    assert "secret-.env" in result.error


def test_list_shows_entries(env: ToolEnv) -> None:
    result = env.run("fs.list", {"path": "docs"})
    assert result.ok
    assert "note.txt" in result.output
    assert result.output.startswith("f")


def test_write_creates_a_file(env: ToolEnv) -> None:
    result = env.run("fs.write", {"path": "docs/new.txt", "content": "new"})
    assert result.ok
    target = env.workspace / "docs" / "new.txt"
    assert target.read_text(encoding="utf-8") == "new"
    assert not list(target.parent.glob("*.tmp"))


def test_write_refuses_to_overwrite_by_default(env: ToolEnv) -> None:
    result = env.run("fs.write", {"path": "docs/note.txt", "content": "x"})
    assert not result.ok
    assert "file exists" in result.error
    assert (env.workspace / "docs" / "note.txt").read_text(encoding="utf-8") == "hello world"


def test_write_overwrites_when_asked(env: ToolEnv) -> None:
    result = env.run(
        "fs.write",
        {"path": "docs/note.txt", "content": "replaced", "overwrite": True},
    )
    assert result.ok
    assert (env.workspace / "docs" / "note.txt").read_text(encoding="utf-8") == "replaced"


def test_write_outside_workspace_is_denied(env: ToolEnv, tmp_path: Path) -> None:
    result = env.run(
        "fs.write",
        {"path": str(tmp_path / "outside.txt"), "content": "x"},
    )
    assert not result.ok
    assert "zone-scope" in result.error


def test_write_to_safety_core_is_denied(env: ToolEnv) -> None:
    result = env.run(
        "fs.write",
        {"path": "src/kivi/safety/evil.py", "content": "x"},
    )
    assert not result.ok
    assert "safety core" in result.error


def test_write_requires_an_existing_parent(env: ToolEnv) -> None:
    result = env.run("fs.write", {"path": "docs/nested/new.txt", "content": "x"})
    assert not result.ok
    assert "parent directory" in result.error


def test_dry_run_does_not_write(env: ToolEnv) -> None:
    result = env.run(
        "fs.write",
        {"path": "docs/preview.txt", "content": "x"},
        dry_run=True,
    )
    assert result.ok
    assert '"action": "create"' in result.output
    assert not (env.workspace / "docs" / "preview.txt").exists()


def test_invalid_arguments_are_rejected(env: ToolEnv) -> None:
    result = env.run("fs.read", {"path": ""})
    assert not result.ok
    assert "invalid arguments" in result.error


def test_unknown_tool_is_rejected(env: ToolEnv) -> None:
    result = env.run("fs.delete", {"path": "docs/note.txt"})
    assert not result.ok
    assert "unknown tool" in result.error


def test_shell_dry_run_needs_no_approval(env: ToolEnv) -> None:
    result = env.run(
        "shell.exec",
        {"argv": [sys.executable, "-c", "print(42)"]},
        dry_run=True,
    )
    assert result.ok
    assert '"action": "exec"' in result.output
    assert '"returncode"' not in result.output


def test_shell_requires_confirmation(env: ToolEnv) -> None:
    result = env.run("shell.exec", {"argv": [sys.executable, "-c", "print(42)"]})
    assert not result.ok
    assert "confirmation required" in result.error
    actions = [row["decision"] for row in env.db.recent_audit()]
    assert "confirm" in actions


def test_shell_runs_when_approved(env: ToolEnv) -> None:
    result = env.run(
        "shell.exec",
        {"argv": [sys.executable, "-c", "print(42)"]},
        approved=True,
    )
    assert result.ok
    assert result.output.strip() == "42"
    assert result.meta["returncode"] == 0


def test_shell_denies_system_command(env: ToolEnv) -> None:
    result = env.run("shell.exec", {"argv": ["diskpart"]}, approved=True)
    assert not result.ok
    assert "cmd-system" in result.error


def test_shell_returns_nonzero_as_failure(env: ToolEnv) -> None:
    result = env.run(
        "shell.exec",
        {"argv": [sys.executable, "-c", "raise SystemExit(3)"]},
        approved=True,
    )
    assert not result.ok
    assert result.meta["returncode"] == 3


def test_shell_timeout(env: ToolEnv) -> None:
    original = env.executor.limits
    env.executor.limits = Limits(shell_timeout_s=1)
    try:
        result = env.run(
            "shell.exec",
            {"argv": [sys.executable, "-c", "import time; time.sleep(5)"], "timeout_s": 1},
            approved=True,
        )
    finally:
        env.executor.limits = original
    assert not result.ok
    assert "timed out" in result.error


def test_shell_output_is_truncated(env: ToolEnv) -> None:
    original = env.executor.limits
    env.executor.limits = Limits(max_output_chars=10)
    try:
        result = env.run(
            "shell.exec",
            {"argv": [sys.executable, "-c", "print('x' * 100)"]},
            approved=True,
        )
    finally:
        env.executor.limits = original
    assert result.ok
    assert len(result.output) == 10
    assert result.truncated is True


def test_capability_must_be_granted(env: ToolEnv) -> None:
    limited = env.engine.issue("t2", ["fs.read"])
    result = env.executor.run(
        "fs.write",
        {"path": "docs/x.txt", "content": "x"},
        grant=limited,
    )
    assert not result.ok
    assert "not granted" in result.error


def test_audit_records_every_attempt(env: ToolEnv) -> None:
    env.run("fs.read", {"path": "docs/note.txt"})
    row = env.db.recent_audit()[0]
    assert row["actor"] == "tool"
    assert row["action"] == "tool.fs.read"
    assert row["decision"] == "allow"
    assert '"trust":"untrusted:file"' in row["details"]
