from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil

from kivi.kernel.db import Database
from kivi.safety.permissions import PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools import ToolExecutor, ToolRegistry, build_registry
from kivi.tools.proc import ProcKillTool, ProcListTool, ProcStartTool


class FakeMemory:
    def __init__(self, rss: int) -> None:
        self.rss = rss


class FakeProcess:
    def __init__(self, info: dict | None = None, error: Exception | None = None) -> None:
        self._info = info or {}
        self._error = error

    @property
    def info(self) -> dict:
        if self._error is not None:
            raise self._error
        return self._info


def make_list_tool(processes):
    return ProcListTool(process_iter=lambda attrs: processes)


def proc(pid: int, name: str, rss: int) -> FakeProcess:
    return FakeProcess(
        {
            "pid": pid,
            "name": name,
            "memory_info": FakeMemory(rss),
            "create_time": 0,
        }
    )


def make_executor(tmp_path: Path, tool: ProcListTool) -> tuple[ToolExecutor, Database]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = Database(tmp_path / "kivi.db")
    engine = PermissionEngine(db, workspace=str(workspace))
    grant = engine.issue("t1", ["proc.list"])
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(
        registry,
        db,
        engine,
        ProtectedZones(workspace),
        workspace=workspace,
    )
    executor.grant = grant  # type: ignore[attr-defined]
    return executor, db


def test_processes_are_sorted_by_memory() -> None:
    tool = make_list_tool(
        [
            proc(1, "small", 1024),
            proc(2, "big", 10 * 1024 * 1024),
        ]
    )
    rows = tool._collect(tool.args_model())  # noqa: SLF001 - focused unit test
    assert [row["name"] for row in rows] == ["big", "small"]
    assert rows[0]["rss_mb"] == 10.0


def test_name_filter_is_case_insensitive() -> None:
    tool = make_list_tool(
        [
            FakeProcess({"pid": 1, "name": "python.exe", "memory_info": FakeMemory(1)}),
            FakeProcess({"pid": 2, "name": "notepad.exe", "memory_info": FakeMemory(1)}),
        ]
    )
    rows = tool._collect(tool.args_model(name_filter="PYTHON"))  # noqa: SLF001
    assert [row["name"] for row in rows] == ["python.exe"]


def test_limit_and_broken_processes_are_skipped() -> None:
    tool = make_list_tool(
        [
            FakeProcess({"pid": 1, "name": "a", "memory_info": FakeMemory(3)}),
            FakeProcess(error=psutil.AccessDenied(2)),
            FakeProcess({"pid": 3, "name": "b", "memory_info": FakeMemory(2)}),
            FakeProcess({"pid": 4, "name": "c", "memory_info": FakeMemory(1)}),
        ]
    )
    rows = tool._collect(tool.args_model(limit=2))  # noqa: SLF001
    assert [row["name"] for row in rows] == ["a", "b"]


def test_tool_registry_includes_proc_list() -> None:
    registry = build_registry()
    assert "proc.list" in registry.names()
    spec = next(spec for spec in registry.specs() if spec.name == "proc.list")
    assert spec.risk == "R0"
    assert spec.capabilities == ("proc.list",)
    assert "properties" in spec.schema


def test_dry_run_describes_the_action() -> None:
    tool = make_list_tool([])
    ctx = type("Ctx", (), {})()
    preview = tool.dry_run(tool.args_model(limit=5), ctx)  # type: ignore[arg-type]
    assert preview.action == "process-list"
    assert preview.details["limit"] == 5


def test_executor_runs_proc_list_and_audits(tmp_path: Path) -> None:
    tool = make_list_tool(
        [FakeProcess({"pid": 10, "name": "python.exe", "memory_info": FakeMemory(1024)})]
    )
    executor, db = make_executor(tmp_path, tool)
    try:
        result = executor.run("proc.list", {}, grant=executor.grant)  # type: ignore[attr-defined]
        audit = db.recent_audit()[0]
    finally:
        db.close()
    assert result.ok
    assert "python.exe" in result.output
    assert result.trust == "untrusted:tool"
    assert audit["action"] == "tool.proc.list"
    assert audit["decision"] == "allow"


def test_executor_respects_killswitch(tmp_path: Path) -> None:
    from kivi.safety.killswitch import KillSwitch

    switch = KillSwitch(tmp_path / "killswitch.json")
    switch.engage("test")
    tool = make_list_tool([])
    executor, db = make_executor(tmp_path, tool)
    executor.killswitch = switch
    try:
        result = executor.run("proc.list", {}, grant=executor.grant)  # type: ignore[attr-defined]
    finally:
        db.close()
    assert not result.ok
    assert "kill switch" in result.error


class FakeTarget:
    def __init__(self, name: str, *, exit_on_wait: bool = True) -> None:
        self._name = name
        self.terminated = False
        self.killed = False
        self.exit_on_wait = exit_on_wait

    def name(self) -> str:
        return self._name

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> None:
        if not self.exit_on_wait:
            raise psutil.TimeoutExpired(seconds=timeout or 0)


def test_kill_rejects_system_and_protected_names() -> None:
    tool = ProcKillTool(process_factory=lambda pid: FakeTarget("explorer.exe"))
    try:
        tool.dry_run(tool.args_model(pid=100, name="explorer.exe"), None)  # type: ignore[arg-type]
    except Exception as exc:
        assert "protected" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("explorer.exe must be protected")


def test_kill_rejects_name_mismatch() -> None:
    tool = ProcKillTool(process_factory=lambda pid: FakeTarget("chrome.exe"))
    try:
        tool.dry_run(tool.args_model(pid=100, name="notepad.exe"), None)  # type: ignore[arg-type]
    except Exception as exc:
        assert "name mismatch" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a mismatched name must be refused")


def test_kill_rejects_own_pid() -> None:
    tool = ProcKillTool(process_factory=lambda pid: FakeTarget("python.exe"))
    try:
        tool.dry_run(tool.args_model(pid=os.getpid(), name="python.exe"), None)  # type: ignore[arg-type]
    except Exception as exc:
        assert "Kivi process" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Kivi must not kill itself")


def test_kill_dry_run_and_force() -> None:
    target = FakeTarget("notepad.exe")
    tool = ProcKillTool(process_factory=lambda pid: target)
    preview = tool.dry_run(tool.args_model(pid=100, name="notepad.exe"), None)  # type: ignore[arg-type]
    assert preview.action == "process-kill"
    assert preview.reversible is False

    result = tool.run(tool.args_model(pid=100, name="notepad.exe", force=True), None)  # type: ignore[arg-type]
    assert result.ok
    assert target.killed is True
    assert target.terminated is False


def test_kill_timeout_asks_for_force() -> None:
    target = FakeTarget("notepad.exe", exit_on_wait=False)
    tool = ProcKillTool(process_factory=lambda pid: target)
    try:
        tool.run(tool.args_model(pid=100, name="notepad.exe"), None)  # type: ignore[arg-type]
    except Exception as exc:
        assert "force=true" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a timeout must be reported")


def test_executor_kills_a_real_spawned_process(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        name = psutil.Process(process.pid).name()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        db = Database(tmp_path / "kivi.db")
        engine = PermissionEngine(db, workspace=str(workspace))
        grant = engine.issue("t1", ["proc.kill"])
        registry = ToolRegistry()
        registry.register(ProcKillTool())
        executor = ToolExecutor(
            registry,
            db,
            engine,
            ProtectedZones(workspace),
            workspace=workspace,
        )
        try:
            denied = executor.run("proc.kill", {"pid": process.pid, "name": name}, grant=grant)
            assert not denied.ok
            assert "confirmation required" in denied.error

            result = executor.run(
                "proc.kill",
                {"pid": process.pid, "name": name},
                grant=grant,
                approved=True,
            )
            assert result.ok
            process.wait(timeout=5)
        finally:
            db.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        time.sleep(0)


def test_registry_contains_proc_kill() -> None:
    registry = build_registry()
    assert "proc.kill" in registry.names()
    spec = next(spec for spec in registry.specs() if spec.name == "proc.kill")
    assert spec.risk == "R3"
    assert spec.capabilities == ("proc.kill",)


class FakePopen:
    def __init__(self, argv: list[str], **kwargs: object) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242


def make_executor_for(tmp_path: Path, tool: Any, capability: str) -> tuple[ToolExecutor, Database]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    db = Database(tmp_path / "kivi.db")
    engine = PermissionEngine(db, workspace=str(workspace))
    grant = engine.issue("t1", [capability])
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(
        registry,
        db,
        engine,
        ProtectedZones(workspace),
        workspace=workspace,
    )
    executor.grant = grant  # type: ignore[attr-defined]
    return executor, db


def test_proc_start_dry_run(tmp_path: Path) -> None:
    executor, db = make_executor_for(tmp_path, ProcStartTool(popen=FakePopen), "proc.start")
    try:
        result = executor.run(
            "proc.start",
            {"argv": [sys.executable, "-c", "print(1)"]},
            grant=executor.grant,  # type: ignore[attr-defined]
            dry_run=True,
        )
    finally:
        db.close()
    assert result.ok
    assert '"action": "process-start"' in result.output


def test_proc_start_real_process(tmp_path: Path) -> None:
    executor, db = make_executor_for(tmp_path, ProcStartTool(), "proc.start")
    process = None
    try:
        denied = executor.run(
            "proc.start",
            {"argv": [sys.executable, "-c", "import time; time.sleep(30)"]},
            grant=executor.grant,  # type: ignore[attr-defined]
        )
        assert not denied.ok
        assert "confirmation required" in denied.error

        result = executor.run(
            "proc.start",
            {"argv": [sys.executable, "-c", "import time; time.sleep(30)"]},
            grant=executor.grant,  # type: ignore[attr-defined]
            approved=True,
        )
        assert result.ok
        process = psutil.Process(result.meta["pid"])
        assert process.is_running()
    finally:
        if process is not None and process.is_running():
            process.kill()
            process.wait(timeout=5)
        db.close()


def test_proc_start_denies_system_command(tmp_path: Path) -> None:
    executor, db = make_executor_for(tmp_path, ProcStartTool(), "proc.start")
    try:
        result = executor.run(
            "proc.start",
            {"argv": ["diskpart"]},
            grant=executor.grant,  # type: ignore[attr-defined]
            approved=True,
        )
    finally:
        db.close()
    assert not result.ok
    assert "cmd-system" in result.error


def test_proc_start_rejects_outside_cwd(tmp_path: Path) -> None:
    executor, db = make_executor_for(tmp_path, ProcStartTool(), "proc.start")
    try:
        result = executor.run(
            "proc.start",
            {"argv": [sys.executable, "-c", "print(1)"], "cwd": str(tmp_path)},
            grant=executor.grant,  # type: ignore[attr-defined]
            approved=True,
        )
    finally:
        db.close()
    assert not result.ok
    assert "zone-scope" in result.error


def test_registry_contains_proc_start() -> None:
    registry = build_registry()
    assert "proc.start" in registry.names()
    spec = next(spec for spec in registry.specs() if spec.name == "proc.start")
    assert spec.risk == "R3"
