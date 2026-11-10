from __future__ import annotations

from pathlib import Path

from kivi.kernel.db import Database
from kivi.safety.permissions import PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools import ToolExecutor, ToolRegistry, build_registry
from kivi.tools.clipboard import (
    ClipboardReadTool,
    ClipboardUnavailable,
    ClipboardWriteTool,
    WindowsClipboard,
)


class FakeClipboard:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.writes: list[str] = []
        self.reads = 0

    def read(self) -> str:
        self.reads += 1
        return self.text

    def write(self, text: str) -> None:
        self.writes.append(text)
        self.text = text


class BrokenClipboard:
    def read(self) -> str:
        raise ClipboardUnavailable("clipboard is busy")

    def write(self, text: str) -> None:
        raise ClipboardUnavailable("clipboard is busy")


def make_executor(tmp_path: Path) -> tuple[ToolExecutor, Database, FakeClipboard]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = Database(tmp_path / "kivi.db")
    engine = PermissionEngine(db, workspace=str(workspace))
    grant = engine.issue("t1", ["clipboard.read", "clipboard.write"])
    backend = FakeClipboard("hello")
    registry = ToolRegistry()
    registry.register(ClipboardReadTool(backend=backend))
    registry.register(ClipboardWriteTool(backend=backend))
    executor = ToolExecutor(
        registry,
        db,
        engine,
        ProtectedZones(workspace),
        workspace=workspace,
    )
    executor.grant = grant  # type: ignore[attr-defined]
    return executor, db, backend


def test_registry_contains_clipboard_tools() -> None:
    registry = build_registry()
    assert "clipboard.read" in registry.names()
    assert "clipboard.write" in registry.names()
    specs = {spec.name: spec for spec in registry.specs()}
    assert specs["clipboard.read"].risk == "R2"
    assert specs["clipboard.write"].risk == "R2"


def test_read_requires_confirmation(tmp_path: Path) -> None:
    executor, db, backend = make_executor(tmp_path)
    try:
        result = executor.run("clipboard.read", {}, grant=executor.grant)  # type: ignore[attr-defined]
    finally:
        db.close()
    assert not result.ok
    assert "confirmation required" in result.error
    assert backend.reads == 0


def test_read_returns_sanitized_text(tmp_path: Path) -> None:
    executor, db, backend = make_executor(tmp_path)
    backend.text = "token sk-canary12345678901234567890"
    try:
        result = executor.run(
            "clipboard.read", {}, grant=executor.grant, approved=True  # type: ignore[attr-defined]
        )
    finally:
        db.close()
    assert result.ok
    assert "sk-canary" not in result.output
    assert "[REDACTED]" in result.output
    assert result.trust == "untrusted:tool"


def test_read_is_truncated(tmp_path: Path) -> None:
    executor, db, backend = make_executor(tmp_path)
    backend.text = "x" * 100
    try:
        result = executor.run(
            "clipboard.read",
            {"max_chars": 10},
            grant=executor.grant,  # type: ignore[attr-defined]
            approved=True,
        )
    finally:
        db.close()
    assert result.ok
    assert len(result.output) == 10
    assert result.truncated is True


def test_write_requires_confirmation(tmp_path: Path) -> None:
    executor, db, backend = make_executor(tmp_path)
    try:
        result = executor.run(
            "clipboard.write",
            {"content": "new"},
            grant=executor.grant,  # type: ignore[attr-defined]
        )
    finally:
        db.close()
    assert not result.ok
    assert backend.writes == []


def test_write_replaces_clipboard_when_approved(tmp_path: Path) -> None:
    executor, db, backend = make_executor(tmp_path)
    try:
        result = executor.run(
            "clipboard.write",
            {"content": "new value"},
            grant=executor.grant,  # type: ignore[attr-defined]
            approved=True,
        )
    finally:
        db.close()
    assert result.ok
    assert backend.writes == ["new value"]


def test_dry_run_does_not_touch_the_clipboard(tmp_path: Path) -> None:
    executor, db, backend = make_executor(tmp_path)
    try:
        read = executor.run(
            "clipboard.read",
            {},
            grant=executor.grant,  # type: ignore[attr-defined]
            dry_run=True,
        )
        write = executor.run(
            "clipboard.write",
            {"content": "x"},
            grant=executor.grant,  # type: ignore[attr-defined]
            dry_run=True,
        )
    finally:
        db.close()
    assert read.ok and write.ok
    assert backend.reads == 0
    assert backend.writes == []


def test_backend_failure_becomes_a_tool_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = Database(tmp_path / "kivi.db")
    engine = PermissionEngine(db, workspace=str(workspace))
    grant = engine.issue("t1", ["clipboard.read"])
    registry = ToolRegistry()
    registry.register(ClipboardReadTool(backend=BrokenClipboard()))  # type: ignore[arg-type]
    executor = ToolExecutor(
        registry,
        db,
        engine,
        ProtectedZones(workspace),
        workspace=workspace,
    )
    try:
        result = executor.run("clipboard.read", {}, grant=grant, approved=True)
    finally:
        db.close()
    assert not result.ok
    assert "clipboard is busy" in result.error


def test_windows_backend_refuses_other_platforms(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    backend = WindowsClipboard()
    try:
        backend.read()
    except ClipboardUnavailable as exc:
        assert "Windows-only" in str(exc)
    else:  # pragma: no cover - only if the platform check is broken
        raise AssertionError("WindowsClipboard must refuse non-Windows platforms")
