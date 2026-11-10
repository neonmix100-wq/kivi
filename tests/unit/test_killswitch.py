from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from kivi.cloud.broker import CloudBroker, CloudDisabled
from kivi.cloud.providers import CloudProvider, CloudRegistry
from kivi.config import CloudSettings
from kivi.interfaces import cli as cli_module
from kivi.interfaces.cli import main
from kivi.kernel.db import Database
from kivi.paths import Paths
from kivi.safety.killswitch import KillSwitch
from kivi.safety.permissions import PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools import ToolExecutor, build_registry


class FakeEvents:
    def __init__(self) -> None:
        self.events: list[str] = []

    def emit(self, event_type: str, **_: object) -> int:
        self.events.append(event_type)
        return len(self.events)


def make_switch(tmp_path: Path, **kwargs) -> tuple[KillSwitch, Database, FakeEvents]:
    db = Database(tmp_path / "kivi.db")
    events = FakeEvents()
    switch = KillSwitch(tmp_path / "killswitch.json", db=db, events=events, **kwargs)
    return switch, db, events


def test_inactive_by_default(tmp_path: Path) -> None:
    switch, db, _ = make_switch(tmp_path)
    try:
        assert switch.is_engaged() is False
        assert switch.state().engaged is False
    finally:
        db.close()


def test_engage_writes_flag_and_audits(tmp_path: Path) -> None:
    switch, db, events = make_switch(tmp_path)
    try:
        state = switch.engage("stop the task")
        audit = db.recent_audit()[0]
        assert state.engaged is True
        assert state.reason == "stop the task"
        assert state.engaged_at
        assert switch.path.is_file()
        assert switch.is_engaged() is True
        assert audit["decision"] == "engaged"
        assert audit["actor"] == "safety"
    finally:
        db.close()
    assert "safety.killswitch_engaged" in events.events


def test_repeat_engage_preserves_the_first_timestamp(tmp_path: Path) -> None:
    now = [datetime(2026, 11, 10, 12, 0, tzinfo=UTC)]

    def clock() -> datetime:
        return now[0]

    switch, db, _ = make_switch(tmp_path, clock=clock)
    try:
        original = switch.engage("first")
        now[0] = now[0] + timedelta(minutes=5)
        repeated = switch.engage("second")
    finally:
        db.close()
    assert repeated.engaged_at == original.engaged_at
    assert repeated.reason == "first"


def test_corrupt_flag_fails_closed(tmp_path: Path) -> None:
    switch, db, _ = make_switch(tmp_path)
    try:
        switch.path.write_text("{not json", encoding="utf-8")
        state = switch.state()
    finally:
        db.close()
    assert state.engaged is True
    assert "unreadable" in state.reason


def test_disengage_removes_flag_and_audits(tmp_path: Path) -> None:
    switch, db, events = make_switch(tmp_path)
    try:
        switch.engage("stop")
        switch.disengage()
        audit = db.recent_audit()[0]
        assert switch.is_engaged() is False
        assert not switch.path.exists()
        assert audit["decision"] == "disengaged"
    finally:
        db.close()
    assert "safety.killswitch_disengaged" in events.events


def make_executor(
    tmp_path: Path, switch: KillSwitch
) -> tuple[ToolExecutor, Database]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "note.txt").write_text("hello", encoding="utf-8")
    db = Database(tmp_path / "executor.db")
    engine = PermissionEngine(db, workspace=str(workspace))
    grant = engine.issue("t1", ["fs.read", "fs.write"])
    executor = ToolExecutor(
        build_registry(),
        db,
        engine,
        ProtectedZones(workspace),
        workspace=workspace,
        killswitch=switch,
    )
    executor.grant = grant  # type: ignore[attr-defined]
    return executor, db


def test_executor_blocks_every_tool_when_engaged(tmp_path: Path) -> None:
    switch, switch_db, _ = make_switch(tmp_path)
    executor, db = make_executor(tmp_path, switch)
    try:
        switch.engage("hostile task")
        result = executor.run(
            "fs.write",
            {"path": "note.txt", "content": "overwrite", "overwrite": True},
            grant=executor.grant,  # type: ignore[attr-defined]
        )
        audit = executor.db.recent_audit()[0]
    finally:
        db.close()
        switch_db.close()
    assert not result.ok
    assert "kill switch" in result.error
    assert (tmp_path / "workspace" / "note.txt").read_text(encoding="utf-8") == "hello"
    assert audit["decision"] == "deny"


def make_broker(tmp_path: Path, switch: KillSwitch, handler):
    db = Database(tmp_path / "broker.db")
    provider = CloudProvider(
        id="groq",
        kind="openai",
        base_url="https://example.test/v1",
        api_key_env="TEST_CLOUD_KEY",
        default_model="test-model",
        free_requests_per_day=10,
        allows_private_data=False,
    )
    broker = CloudBroker(
        db,
        CloudSettings(mode="auto_free"),
        CloudRegistry({"groq": provider}),
        transport=httpx.MockTransport(handler),
        today=lambda: "2026-11-10",
        killswitch=switch,
    )
    return broker, db


def test_broker_refuses_to_call_when_engaged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_CLOUD_KEY", "secret")
    switch, switch_db, _ = make_switch(tmp_path)
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    broker, db = make_broker(tmp_path, switch, handler)
    try:
        switch.engage("stop cloud")
        with pytest.raises(CloudDisabled, match="kill switch"):
            broker.complete(
                "groq",
                [{"role": "user", "content": "hi"}],
                data_class="public",
            )
        audit = db.recent_audit()[0]
    finally:
        broker.close()
        db.close()
        switch_db.close()
    assert called is False
    assert audit["decision"] == "deny"
    assert "kill switch" in audit["reason"]


def make_cli_profile(tmp_path: Path) -> Paths:
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "config" / "kivi.yaml").write_text("version: 1\n", encoding="utf-8")
    (root / "note.txt").write_text("hello", encoding="utf-8")
    return Paths(root=root, home=tmp_path / "profile")


def test_engage_calls_unload_hook_once(tmp_path: Path) -> None:
    calls: list[int] = []
    db = Database(tmp_path / "kivi.db")
    switch = KillSwitch(tmp_path / "killswitch.json", db=db, on_engage=lambda: calls.append(1))
    try:
        switch.engage("stop")
        switch.engage("stop again")
    finally:
        db.close()
    assert calls == [1]


def test_unload_failure_does_not_prevent_the_stop(tmp_path: Path) -> None:
    def broken() -> None:
        raise RuntimeError("ollama is down")

    switch, db, _ = make_switch(tmp_path, on_engage=broken)
    try:
        state = switch.engage("stop")
        audit = db.recent_audit()[0]
    finally:
        db.close()
    assert state.engaged is True
    assert switch.is_engaged() is True
    assert audit["action"] == "safety.killswitch_unload"
    assert audit["decision"] == "error"


def test_cli_killswitch_blocks_tools(tmp_path: Path, monkeypatch) -> None:
    paths = make_cli_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)
    monkeypatch.setattr(cli_module, "_unload_local_models", lambda _paths: 0)

    assert main(["killswitch", "status"]) == 0
    assert main(["killswitch", "on", "--reason", "test stop"]) == 0
    assert main(["killswitch", "status"]) == 0
    assert main(["tools", "run", "fs.read", "--args", '{"path": "note.txt"}']) == 1
    assert main(["killswitch", "off"]) == 0
    assert main(["tools", "run", "fs.read", "--args", '{"path": "note.txt"}']) == 0
