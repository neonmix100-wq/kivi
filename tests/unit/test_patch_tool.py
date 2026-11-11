from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from kivi.kernel.db import Database
from kivi.safety.permissions import Grant, PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools.base import Limits, ToolResult
from kivi.tools.executor import ToolExecutor
from kivi.tools.patch import CodePatchTool, PatchArgs
from kivi.tools.registry import ToolRegistry


class ToolEnv:
    def __init__(self, workspace: Path, db: Database, executor: ToolExecutor, grant: Grant) -> None:
        self.workspace = workspace
        self.db = db
        self.executor = executor
        self.grant = grant
        self.engine = executor.permissions

    def run(self, name: str, args: dict | None = None, **kwargs) -> ToolResult:
        return self.executor.run(name, args or {}, grant=self.grant, **kwargs)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "src" / "kivi" / "safety").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "src" / "app.py").write_text(
        "TARGET = 1\nprint(TARGET)\n",
        encoding="utf-8",
    )
    (root / "src" / "kivi" / "safety" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "docs" / "note.txt").write_bytes(b"hello\n")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    (root / "crlf.txt").write_bytes(b"alpha\r\nbeta\r\n")
    (root / "unicode.txt").write_text("привет мир\n", encoding="utf-8")
    return root


@pytest.fixture()
def env(tmp_path: Path, workspace: Path) -> ToolEnv:
    db = Database(tmp_path / "kivi.db")
    zones = ProtectedZones(workspace)
    engine = PermissionEngine(db, workspace=str(workspace))
    grant = engine.issue("patch-task", ["fs.write"])
    registry = ToolRegistry()
    registry.register(CodePatchTool())
    executor = ToolExecutor(registry, db, engine, zones, workspace=workspace)
    tool_env = ToolEnv(workspace, db, executor, grant)
    yield tool_env
    db.close()


def test_dry_run_returns_a_diff_and_does_not_write(env: ToolEnv) -> None:
    original = (env.workspace / "src" / "app.py").read_text(encoding="utf-8")
    result = env.run(
        "code.patch",
        {"path": "src/app.py", "old": "TARGET = 1", "new": "VALUE = 1"},
        dry_run=True,
    )
    assert result.ok
    assert '"action": "patch"' in result.output
    assert "@@" in result.output
    assert "VALUE = 1" in result.output
    assert (env.workspace / "src" / "app.py").read_text(encoding="utf-8") == original
    assert result.meta["dry_run"] is True


def test_patch_replaces_one_occurrence_by_default(env: ToolEnv) -> None:
    result = env.run(
        "code.patch",
        {"path": "src/app.py", "old": "TARGET = 1", "new": "VALUE = 1"},
    )
    assert result.ok
    assert (env.workspace / "src" / "app.py").read_text(encoding="utf-8") == (
        "VALUE = 1\nprint(TARGET)\n"
    )
    assert result.meta["occurrences"] == 1
    assert result.meta["added"] == 1
    assert result.meta["removed"] == 1
    assert result.trust == "untrusted:tool"


def test_patch_requires_an_exact_occurrence_count(env: ToolEnv) -> None:
    result = env.run(
        "code.patch",
        {"path": "src/app.py", "old": "TARGET", "new": "ITEM", "count": 2},
    )
    assert result.ok
    assert (env.workspace / "src" / "app.py").read_text(encoding="utf-8") == (
        "ITEM = 1\nprint(ITEM)\n"
    )
    assert result.meta["occurrences"] == 2


def test_patch_refuses_a_count_mismatch(env: ToolEnv) -> None:
    result = env.run(
        "code.patch",
        {"path": "src/app.py", "old": "TARGET", "new": "ITEM"},
    )
    assert not result.ok
    assert "occurs 2 times, expected 1" in result.error
    assert (env.workspace / "src" / "app.py").read_text(encoding="utf-8") == (
        "TARGET = 1\nprint(TARGET)\n"
    )


def test_patch_refuses_identical_and_empty_old_text(env: ToolEnv) -> None:
    identical = env.run(
        "code.patch",
        {"path": "src/app.py", "old": "TARGET", "new": "TARGET"},
    )
    assert not identical.ok
    assert "identical" in identical.error
    empty = env.run("code.patch", {"path": "src/app.py", "old": "", "new": "x"})
    assert not empty.ok
    assert "must not be empty" in empty.error


def test_patch_refuses_missing_files_and_directories(env: ToolEnv) -> None:
    missing = env.run(
        "code.patch",
        {"path": "src/missing.py", "old": "a", "new": "b"},
    )
    assert not missing.ok
    assert "not a file" in missing.error
    directory = env.run("code.patch", {"path": "src", "old": "a", "new": "b"})
    assert not directory.ok
    assert "not a file" in directory.error


def test_patch_refuses_oversized_input_and_output(env: ToolEnv) -> None:
    original_limits = env.executor.limits
    try:
        env.executor.limits = Limits(max_write_bytes=10)
        too_large = env.run(
            "code.patch",
            {"path": "src/app.py", "old": "TARGET", "new": "ITEM", "count": 2},
        )
    finally:
        env.executor.limits = original_limits
    assert not too_large.ok
    assert "too large to patch" in too_large.error

    try:
        env.executor.limits = Limits(max_write_bytes=40)
        grows = env.run(
            "code.patch",
            {"path": "src/app.py", "old": "TARGET = 1", "new": "X" * 50},
        )
    finally:
        env.executor.limits = original_limits
    assert not grows.ok
    assert "would exceed" in grows.error


def test_patch_is_confined_by_protected_zones(env: ToolEnv, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    escaped = env.run(
        "code.patch",
        {"path": str(outside), "old": "outside", "new": "x"},
    )
    assert not escaped.ok
    assert "zone-scope" in escaped.error

    secret = env.run("code.patch", {"path": ".env", "old": "SECRET", "new": "x"})
    assert not secret.ok
    assert "secret-.env" in secret.error

    core = env.run(
        "code.patch",
        {"path": "src/kivi/safety/core.py", "old": "VALUE", "new": "x"},
    )
    assert not core.ok
    assert "safety core" in core.error


def test_patch_preserves_crlf_and_unicode(env: ToolEnv) -> None:
    crlf = env.run(
        "code.patch",
        {"path": "crlf.txt", "old": "alpha", "new": "ALPHA"},
    )
    assert crlf.ok
    assert (env.workspace / "crlf.txt").read_bytes() == b"ALPHA\r\nbeta\r\n"

    unicode = env.run(
        "code.patch",
        {"path": "unicode.txt", "old": "мир", "new": "свет"},
    )
    assert unicode.ok
    assert (env.workspace / "unicode.txt").read_text(encoding="utf-8") == "привет свет\n"


def test_patch_can_delete_matched_text(env: ToolEnv) -> None:
    result = env.run(
        "code.patch",
        {"path": "docs/note.txt", "old": "hello\n", "new": ""},
    )
    assert result.ok
    assert (env.workspace / "docs" / "note.txt").read_text(encoding="utf-8") == ""
    assert result.meta["bytes_after"] == 0


def test_patch_includes_diff_metadata_and_leaves_no_temp_files(env: ToolEnv) -> None:
    result = env.run(
        "code.patch",
        {"path": "src/app.py", "old": "TARGET = 1", "new": "VALUE = 1"},
    )
    assert result.ok
    diff = result.meta["diff"]
    assert diff.startswith("--- a/src/app.py")
    assert "+++ b/src/app.py" in diff
    assert "-TARGET = 1" in diff
    assert "+VALUE = 1" in diff
    assert not list(env.workspace.rglob("*.tmp"))


def test_patch_reports_capability_denial_and_invalid_arguments(env: ToolEnv) -> None:
    limited = env.engine.issue("limited", ["fs.read"])
    denied = env.executor.run(
        "code.patch",
        {"path": "src/app.py", "old": "TARGET = 1", "new": "VALUE = 1"},
        grant=limited,
    )
    assert not denied.ok
    assert "not granted" in denied.error

    extra = env.run(
        "code.patch",
        {"path": "src/app.py", "old": "TARGET", "new": "x", "overwrite": True},
    )
    assert not extra.ok
    assert "invalid arguments" in extra.error
    zero = env.run(
        "code.patch",
        {"path": "src/app.py", "old": "TARGET", "new": "x", "count": 0},
    )
    assert not zero.ok
    assert "invalid arguments" in zero.error


def test_patch_args_have_no_overwrite_field() -> None:
    assert "overwrite" not in PatchArgs.model_fields
    with pytest.raises(ValidationError):
        PatchArgs.model_validate(
            {"path": "x", "old": "a", "new": "b", "overwrite": True}
        )
