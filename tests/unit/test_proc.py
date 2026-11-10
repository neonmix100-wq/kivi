from __future__ import annotations

from pathlib import Path

import psutil

from kivi.kernel.db import Database
from kivi.safety.permissions import PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools import ToolExecutor, ToolRegistry, build_registry
from kivi.tools.proc import ProcListTool


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
