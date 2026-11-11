"""Tests for the M3 system tools: no real audio, lock, sleep or shutdown."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kivi.kernel.db import Database
from kivi.safety.permissions import PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools import ToolExecutor, ToolRegistry
from kivi.tools.system import (
    SystemUnavailable,
    WindowsSystemBackend,
    build_system_tools,
)

CAPABILITIES = ("system.notify", "system.volume", "system.power")


class FakeSystemBackend:
    """Records every call; raises injected errors instead of touching Windows."""

    def __init__(self, volume: int = 40, muted: bool = False) -> None:
        self.volume = volume
        self.muted = muted
        self.notifications: list[tuple[str, str, int]] = []
        self.volumes: list[int] = []
        self.mutes: list[bool] = []
        self.locks = 0
        self.sleeps = 0
        self.shutdowns = 0
        self.restarts = 0
        self.notify_error: Exception | None = None
        self.get_volume_error: Exception | None = None
        self.set_volume_error: Exception | None = None
        self.lock_error: Exception | None = None

    def notify(self, title: str, message: str, timeout_s: int = 5) -> None:
        if self.notify_error is not None:
            raise self.notify_error
        self.notifications.append((title, message, timeout_s))

    def get_volume(self) -> int:
        if self.get_volume_error is not None:
            raise self.get_volume_error
        return self.volume

    def set_volume(self, level: int) -> None:
        if self.set_volume_error is not None:
            raise self.set_volume_error
        self.volumes.append(level)
        self.volume = level

    def is_muted(self) -> bool:
        return self.muted

    def set_muted(self, muted: bool) -> None:
        self.mutes.append(muted)
        self.muted = muted

    def lock(self) -> None:
        if self.lock_error is not None:
            raise self.lock_error
        self.locks += 1

    def sleep(self) -> None:
        self.sleeps += 1

    def shutdown(self) -> None:
        self.shutdowns += 1

    def restart(self) -> None:
        self.restarts += 1


class NoMuteBackend(FakeSystemBackend):
    """A backend without the optional is_muted/set_muted extensions."""

    is_muted = None  # type: ignore[assignment]
    set_muted = None  # type: ignore[assignment]


class FakeRunner:
    """Subprocess stand-in for the PowerShell and shutdown.exe calls."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stderr: str = "",
        error: Exception | None = None,
    ) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.error = error
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)


@pytest.fixture
def env_factory(tmp_path: Path):
    """Build executors with a temporary DB and close every database at the end."""
    databases: list[Database] = []

    def build(
        backend: FakeSystemBackend | None = None,
        capabilities: tuple[str, ...] = CAPABILITIES,
    ) -> tuple[ToolExecutor, Database, Any, FakeSystemBackend]:
        workspace = tmp_path / "workspace"
        workspace.mkdir(exist_ok=True)
        db = Database(tmp_path / "kivi.db")
        databases.append(db)
        engine = PermissionEngine(db, workspace=str(workspace))
        grant = engine.issue("t1", list(capabilities))
        selected = backend if backend is not None else FakeSystemBackend()
        registry = ToolRegistry()
        for tool in build_system_tools(selected):
            registry.register(tool)
        executor = ToolExecutor(
            registry,
            db,
            engine,
            ProtectedZones(workspace),
            workspace=workspace,
        )
        return executor, db, grant, selected

    yield build
    for db in databases:
        db.close()


def test_all_system_tools_declare_expected_risk_and_capability() -> None:
    registry = ToolRegistry()
    for tool in build_system_tools(FakeSystemBackend()):
        registry.register(tool)
    assert set(registry.names()) == {
        "system.notify",
        "system.volume.get",
        "system.volume.set",
        "system.lock",
        "system.sleep",
        "system.shutdown",
        "system.restart",
    }
    specs = {spec.name: spec for spec in registry.specs()}
    assert specs["system.notify"].risk == "R1"
    assert specs["system.notify"].capabilities == ("system.notify",)
    assert specs["system.volume.get"].risk == "R0"
    assert specs["system.volume.set"].risk == "R1"
    assert specs["system.volume.get"].capabilities == ("system.volume",)
    assert specs["system.lock"].risk == "R2"
    assert specs["system.sleep"].risk == "R3"
    assert specs["system.shutdown"].risk == "R4"
    assert specs["system.restart"].risk == "R4"
    assert specs["system.lock"].capabilities == ("system.power",)


def test_notify_redacts_secrets_before_calling_the_backend(env_factory) -> None:
    executor, db, grant, backend = env_factory()
    secret = "sk-abcdEFGH12345678"
    result = executor.run(
        "system.notify",
        {"title": f"Deploy {secret}", "message": f"done with {secret}", "timeout_s": 9},
        grant=grant,
    )
    assert result.ok
    assert result.trust == "trusted:system"
    assert result.meta["redacted"] == 2
    assert "sk-abcd" not in result.output
    title, message, timeout_s = backend.notifications[0]
    assert secret not in title
    assert secret not in message
    assert "[REDACTED]" in title
    assert "[REDACTED]" in message
    assert timeout_s == 9
    assert db.recent_audit()[0]["decision"] == "allow"
    assert db.recent_audit()[0]["action"] == "tool.system.notify"


def test_notify_rejects_out_of_range_fields(env_factory) -> None:
    executor, _db, grant, backend = env_factory()
    payloads = (
        {"title": "", "message": "body"},
        {"title": "   ", "message": "body"},
        {"title": "x" * 81, "message": "body"},
        {"title": "title", "message": ""},
        {"title": "title", "message": "x" * 501},
    )
    for payload in payloads:
        result = executor.run("system.notify", payload, grant=grant)
        assert not result.ok, payload
        assert "invalid arguments" in result.error
    assert backend.notifications == []


def test_notify_dry_run_does_not_call_the_backend(env_factory) -> None:
    executor, _db, grant, backend = env_factory()
    result = executor.run(
        "system.notify",
        {"title": "Build", "message": "finished"},
        grant=grant,
        dry_run=True,
    )
    assert result.ok
    preview = json.loads(result.output)
    assert preview["action"] == "system-notify"
    assert preview["details"]["message_chars"] == 8
    assert backend.notifications == []


def test_notify_backend_error_becomes_a_tool_error(env_factory) -> None:
    backend = FakeSystemBackend()
    backend.notify_error = SystemUnavailable("notification host unavailable")
    executor, db, grant, _backend = env_factory(backend)
    result = executor.run(
        "system.notify", {"title": "x", "message": "y"}, grant=grant
    )
    assert not result.ok
    assert "notification host unavailable" in result.error
    assert db.recent_audit()[0]["decision"] == "error"


def test_windows_toast_uses_fixed_argv_and_xml_escapes(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    runner = FakeRunner()
    beeps: list[int] = []
    backend = WindowsSystemBackend(runner=runner, beep=lambda: beeps.append(1))
    backend.notify('Kivi & Co <x> "q"', "50% < 100 & 'quoted'", 9)

    argv, kwargs = runner.calls[0]
    assert argv[:4] == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
    ]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 5
    assert kwargs["check"] is False
    script = argv[4]
    assert "Kivi &amp; Co &lt;x&gt; &quot;q&quot;" in script
    assert "50% &lt; 100 &amp; &apos;quoted&apos;" in script
    assert "<x>" not in script
    assert beeps == []


def test_windows_toast_falls_back_to_beep_and_logs(monkeypatch, caplog) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    beeps: list[int] = []

    def beep() -> None:
        beeps.append(1)

    missing = WindowsSystemBackend(
        runner=FakeRunner(error=OSError("powershell missing")),
        beep=beep,
    )
    with caplog.at_level(logging.WARNING, logger="kivi.tools.system"):
        missing.notify("title", "message", 5)
    assert beeps == [1]
    assert "toast" in caplog.text

    failed = WindowsSystemBackend(
        runner=FakeRunner(returncode=1, stderr="AppID is not registered"),
        beep=beep,
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="kivi.tools.system"):
        failed.notify("title", "message", 5)
    assert beeps == [1, 1]
    assert "AppID is not registered" in caplog.text


def test_windows_backend_refuses_other_platforms(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    backend = WindowsSystemBackend(runner=FakeRunner(), beep=lambda: None)
    with pytest.raises(SystemUnavailable):
        backend.lock()
    with pytest.raises(SystemUnavailable):
        backend.get_volume()
    with pytest.raises(SystemUnavailable):
        backend.shutdown()


def test_windows_volume_uses_pycaw_endpoint(monkeypatch) -> None:
    pycaw = pytest.importorskip("pycaw.pycaw")
    monkeypatch.setattr(sys, "platform", "win32")

    class FakeEndpoint:
        def __init__(self) -> None:
            self.scalar = 0.37
            self.muted = False

        def GetMasterVolumeLevelScalar(self) -> float:
            return self.scalar

        def SetMasterVolumeLevelScalar(self, value: float, context: Any) -> None:
            self.scalar = value

        def GetMute(self) -> bool:
            return self.muted

        def SetMute(self, value: bool, context: Any) -> None:
            self.muted = value

    endpoint = FakeEndpoint()

    class FakeSpeakers:
        EndpointVolume = endpoint

    monkeypatch.setattr(
        pycaw.AudioUtilities,
        "GetSpeakers",
        staticmethod(lambda: FakeSpeakers()),
    )
    backend = WindowsSystemBackend(runner=FakeRunner(), beep=lambda: None)
    assert backend.get_volume() == 37
    backend.set_volume(65)
    assert endpoint.scalar == pytest.approx(0.65)
    backend.set_volume(140)
    assert endpoint.scalar == pytest.approx(1.0)
    backend.set_muted(True)
    assert backend.is_muted() is True


def test_volume_get_returns_level_and_mute(env_factory) -> None:
    backend = FakeSystemBackend(volume=42, muted=True)
    executor, _db, grant, _backend = env_factory(backend)
    result = executor.run("system.volume.get", {}, grant=grant)
    assert result.ok
    assert result.output == "volume: 42%"
    assert result.meta == {"level": 42, "muted": True}
    assert result.trust == "trusted:system"


def test_volume_get_clamps_and_handles_missing_mute(env_factory) -> None:
    backend = NoMuteBackend(volume=140)
    executor, _db, grant, _backend = env_factory(backend)
    result = executor.run("system.volume.get", {}, grant=grant)
    assert result.ok
    assert result.meta["level"] == 100
    assert result.meta["muted"] is None


def test_volume_set_level_and_delta_clamp(env_factory) -> None:
    backend = FakeSystemBackend(volume=30)
    executor, _db, grant, _backend = env_factory(backend)

    result = executor.run("system.volume.set", {"level": 45}, grant=grant)
    assert result.ok
    assert backend.volumes == [45]
    assert result.meta["previous"] == 30
    assert result.meta["target"] == 45
    assert result.meta["level"] == 45
    assert result.output == "volume set to 45%"

    backend.volume = 90
    high = executor.run("system.volume.set", {"delta": 50}, grant=grant)
    assert high.ok
    assert backend.volumes[-1] == 100
    assert high.meta["target"] == 100

    backend.volume = 5
    low = executor.run("system.volume.set", {"delta": -20}, grant=grant)
    assert low.ok
    assert backend.volumes[-1] == 0


def test_volume_set_mute_and_mute_only(env_factory) -> None:
    backend = FakeSystemBackend(volume=30, muted=False)
    executor, _db, grant, _backend = env_factory(backend)

    both = executor.run(
        "system.volume.set", {"level": 55, "mute": True}, grant=grant
    )
    assert both.ok
    assert backend.volumes == [55]
    assert backend.mutes == [True]
    assert both.meta["muted"] is True
    assert both.output == "volume set to 55%; muted"

    mute_only = executor.run("system.volume.set", {"mute": False}, grant=grant)
    assert mute_only.ok
    assert backend.volumes == [55]
    assert backend.mutes == [True, False]
    assert mute_only.output == "unmuted"


def test_volume_set_dry_run_reads_but_does_not_change(env_factory) -> None:
    backend = FakeSystemBackend(volume=40)
    executor, _db, grant, _backend = env_factory(backend)
    result = executor.run(
        "system.volume.set", {"delta": 10}, grant=grant, dry_run=True
    )
    assert result.ok
    assert backend.volumes == []
    assert backend.mutes == []
    preview = json.loads(result.output)
    assert preview["action"] == "system-volume-set"
    assert preview["details"]["target"] == 50


def test_volume_set_validates_arguments(env_factory) -> None:
    backend = FakeSystemBackend(volume=20)
    executor, _db, grant, _backend = env_factory(backend)
    for payload in (
        {},
        {"level": 10, "delta": 5},
        {"level": 101},
        {"delta": -101},
    ):
        result = executor.run("system.volume.set", payload, grant=grant)
        assert not result.ok, payload
        assert "invalid arguments" in result.error
    assert backend.volumes == []
    assert backend.mutes == []


def test_volume_set_rejects_unsupported_mute(env_factory) -> None:
    backend = NoMuteBackend(volume=20)
    executor, _db, grant, _backend = env_factory(backend)
    result = executor.run(
        "system.volume.set", {"level": 25, "mute": True}, grant=grant
    )
    assert not result.ok
    assert "does not support mute" in result.error
    assert backend.volumes == []
    assert backend.mutes == []


def test_volume_backend_errors_become_tool_errors(env_factory) -> None:
    backend = FakeSystemBackend(volume=20)
    backend.get_volume_error = SystemUnavailable("no audio endpoint")
    executor, db, grant, _backend = env_factory(backend)
    read = executor.run("system.volume.get", {}, grant=grant)
    assert not read.ok
    assert "no audio endpoint" in read.error
    assert db.recent_audit()[0]["decision"] == "error"

    backend.get_volume_error = None
    backend.set_volume_error = SystemUnavailable("volume is locked")
    write = executor.run("system.volume.set", {"level": 10}, grant=grant)
    assert not write.ok
    assert "volume is locked" in write.error


def test_lock_and_sleep_require_approval(env_factory) -> None:
    executor, db, grant, backend = env_factory()

    lock_denied = executor.run("system.lock", {}, grant=grant)
    assert not lock_denied.ok
    assert "confirmation required" in lock_denied.error
    assert backend.locks == 0
    assert db.recent_audit()[0]["decision"] == "confirm"

    lock_ok = executor.run("system.lock", {}, grant=grant, approved=True)
    assert lock_ok.ok
    assert backend.locks == 1
    assert lock_ok.trust == "trusted:system"
    assert db.recent_audit()[0]["decision"] == "allow"

    sleep_denied = executor.run("system.sleep", {}, grant=grant)
    assert not sleep_denied.ok
    assert "confirmation required" in sleep_denied.error
    assert backend.sleeps == 0

    sleep_ok = executor.run("system.sleep", {}, grant=grant, approved=True)
    assert sleep_ok.ok
    assert backend.sleeps == 1


def test_shutdown_and_restart_are_hard_denied_even_with_approval(env_factory) -> None:
    executor, db, grant, backend = env_factory()
    for name in ("system.shutdown", "system.restart"):
        for dry_run in (False, True):
            result = executor.run(
                name, {}, grant=grant, approved=True, dry_run=dry_run
            )
            assert not result.ok, name
            assert "denied" in result.error
            assert "R4" in result.error
        audit = db.recent_audit()[0]
        assert audit["decision"] == "deny"
        assert json.loads(audit["details"])["rule"] == "risk-deny"
    assert backend.shutdowns == 0
    assert backend.restarts == 0


def test_sleep_dry_run_does_not_suspend(env_factory) -> None:
    executor, _db, grant, backend = env_factory()
    result = executor.run("system.sleep", {}, grant=grant, dry_run=True)
    assert result.ok
    assert backend.sleeps == 0
    preview = json.loads(result.output)
    assert preview["action"] == "system-sleep"
    assert preview["reversible"] is True
    assert preview["warnings"]


def test_lock_backend_error_becomes_a_tool_error(env_factory) -> None:
    backend = FakeSystemBackend()
    backend.lock_error = SystemUnavailable("session policy blocked the lock")
    executor, db, grant, _backend = env_factory(backend)
    result = executor.run("system.lock", {}, grant=grant, approved=True)
    assert not result.ok
    assert "cannot lock the workstation" in result.error
    assert "session policy blocked the lock" in result.error
    assert db.recent_audit()[0]["decision"] == "error"
