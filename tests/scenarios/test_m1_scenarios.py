from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kivi.brain import (
    FastPath,
    KiviBrain,
    Plan,
    PlanError,
    PlanExecutor,
    Planner,
    Step,
    Verifier,
    parse_plan,
)
from kivi.kernel.db import Database
from kivi.safety.killswitch import KillSwitch
from kivi.safety.permissions import PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools import ToolExecutor, build_registry


@pytest.fixture()
def runtime(tmp_path: Path) -> SimpleNamespace:
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "note.txt").write_text("hello world", encoding="utf-8")
    db = Database(tmp_path / "kivi.db")
    registry = build_registry()
    permissions = PermissionEngine(db, workspace=str(workspace))
    zones = ProtectedZones(workspace)
    executor = ToolExecutor(registry, db, permissions, zones, workspace=workspace)
    plan_executor = PlanExecutor(registry, executor, permissions)
    runtime = SimpleNamespace(
        tmp_path=tmp_path,
        workspace=workspace,
        db=db,
        registry=registry,
        permissions=permissions,
        executor=executor,
        plan_executor=plan_executor,
    )
    yield runtime
    db.close()


def make_plan(*steps: Step, criteria: tuple[str, ...] = ()) -> Plan:
    return Plan(goal="scenario", steps=steps, criteria=criteria)


def test_parse_fenced_plan() -> None:
    text = '```json\n{"steps": [{"id": "s1", "tool": "fs.read", "args": {"path": "a"}}]}\n```'
    plan = parse_plan(text, goal="read a")
    assert plan.goal == "read a"
    assert plan.steps[0].tool == "fs.read"


def test_plan_rejects_unknown_tool(runtime: SimpleNamespace) -> None:
    text = '{"steps": [{"id": "s1", "tool": "fs.delete", "args": {}}]}'
    with pytest.raises(PlanError, match="unknown tool"):
        parse_plan(text, runtime.registry)


def test_plan_rejects_cycle(runtime: SimpleNamespace) -> None:
    text = (
        '{"steps": ['
        '{"id": "a", "tool": "fs.read", "args": {"path": "x"}, "depends_on": ["b"]},'
        '{"id": "b", "tool": "fs.read", "args": {"path": "y"}, "depends_on": ["a"]}'
        "]}"
    )
    with pytest.raises(PlanError, match="cycle"):
        parse_plan(text, runtime.registry)


def test_plan_rejects_missing_dependency(runtime: SimpleNamespace) -> None:
    text = '{"steps": [{"id": "a", "tool": "fs.read", "args": {"path": "x"}, "depends_on": ["z"]}]}'
    with pytest.raises(PlanError, match="depends on unknown"):
        parse_plan(text, runtime.registry)


def test_fs_read_step(runtime: SimpleNamespace) -> None:
    plan = make_plan(Step("s1", "fs.read", {"path": "docs/note.txt"}))
    result = runtime.plan_executor.execute(plan, "t1")
    assert result.ok
    assert "hello world" in result.output


def test_fs_list_step(runtime: SimpleNamespace) -> None:
    plan = make_plan(Step("s1", "fs.list", {"path": "docs"}))
    result = runtime.plan_executor.execute(plan, "t2")
    assert result.ok
    assert "note.txt" in result.output


def test_fs_write_creates_file(runtime: SimpleNamespace) -> None:
    plan = make_plan(Step("s1", "fs.write", {"path": "docs/new.txt", "content": "new"}))
    result = runtime.plan_executor.execute(plan, "t3")
    assert result.ok
    assert (runtime.workspace / "docs" / "new.txt").read_text(encoding="utf-8") == "new"


def test_fs_write_overwrite(runtime: SimpleNamespace) -> None:
    plan = make_plan(
        Step("s1", "fs.write", {"path": "docs/note.txt", "content": "changed", "overwrite": True})
    )
    result = runtime.plan_executor.execute(plan, "t4")
    assert result.ok
    assert "changed" in (runtime.workspace / "docs" / "note.txt").read_text(encoding="utf-8")


def test_substitution_between_steps(runtime: SimpleNamespace) -> None:
    plan = make_plan(
        Step("s1", "fs.write", {"path": "docs/a.txt", "content": "hello"}),
        Step(
            "s2",
            "fs.write",
            {"path": "docs/b.txt", "content": "copy: {{ s1 }}"},
            depends_on=("s1",),
        ),
    )
    result = runtime.plan_executor.execute(plan, "t5")
    assert result.ok
    assert "copy:" in (runtime.workspace / "docs" / "b.txt").read_text(encoding="utf-8")


def test_dependency_order(runtime: SimpleNamespace) -> None:
    plan = make_plan(
        Step("first", "fs.write", {"path": "docs/order.txt", "content": "x"}),
        Step("second", "fs.read", {"path": "docs/order.txt"}, depends_on=("first",)),
    )
    result = runtime.plan_executor.execute(plan, "t6")
    assert [item.step_id for item in result.step_results] == ["first", "second"]
    assert "x" in result.step_results[1].output


def test_failed_step_stops_execution(runtime: SimpleNamespace) -> None:
    plan = make_plan(
        Step("s1", "fs.read", {"path": "docs/missing.txt"}),
        Step("s2", "fs.write", {"path": "docs/after.txt", "content": "x"}),
    )
    result = runtime.plan_executor.execute(plan, "t7")
    assert not result.ok
    assert not (runtime.workspace / "docs" / "after.txt").exists()


def test_replan_after_failure(runtime: SimpleNamespace) -> None:
    initial = make_plan(Step("s1", "fs.read", {"path": "docs/missing.txt"}))

    def replanner(plan: Plan, step: Step, error: str) -> Plan:
        return make_plan(
            Step("s1", "fs.write", {"path": "docs/recovered.txt", "content": "ok"})
        )

    executor = PlanExecutor(
        runtime.registry,
        runtime.executor,
        runtime.permissions,
        replanner=replanner,
    )
    result = executor.execute(initial, "t8")
    assert result.status == "replanned"
    assert (runtime.workspace / "docs" / "recovered.txt").is_file()


def test_shell_requires_approval(runtime: SimpleNamespace) -> None:
    plan = make_plan(
        Step("s1", "shell.exec", {"argv": [sys.executable, "-c", "print(1)"]})
    )
    result = runtime.plan_executor.execute(plan, "t9")
    assert not result.ok
    assert "confirm" in result.error.lower()


def test_shell_runs_with_approval(runtime: SimpleNamespace) -> None:
    plan = make_plan(
        Step("s1", "shell.exec", {"argv": [sys.executable, "-c", "print(21 * 2)"]})
    )
    result = runtime.plan_executor.execute(plan, "t10", approve=lambda step: True)
    assert result.ok
    assert "42" in result.output


def test_fastpath_read(runtime: SimpleNamespace) -> None:
    result = FastPath(
        runtime.registry, runtime.executor, runtime.permissions
    ).try_run("прочитай docs/note.txt", "t11")
    assert result is not None
    assert result.ok
    assert "hello world" in result.output


def test_fastpath_list(runtime: SimpleNamespace) -> None:
    result = FastPath(
        runtime.registry, runtime.executor, runtime.permissions
    ).try_run("список docs", "t12")
    assert result is not None
    assert result.ok
    assert "note.txt" in result.output


def test_fastpath_processes(runtime: SimpleNamespace) -> None:
    result = FastPath(
        runtime.registry, runtime.executor, runtime.permissions
    ).try_run("процессы", "t13")
    assert result is not None
    assert result.ok


def test_brain_planned_route(runtime: SimpleNamespace) -> None:
    class FakeLLM:
        def __call__(self, messages: list[dict[str, str]]) -> str:
            return (
                '{"steps": [{"id": "s1", "tool": "fs.read", '
                '"args": {"path": "docs/note.txt"}}]}'
            )

    brain = KiviBrain(FakeLLM(), runtime.registry, runtime.executor, runtime.permissions)
    result = brain.ask("ответь на вопрос из заметки", task_id="t14")
    assert result.ok
    assert result.fastpath_used is False
    assert "hello world" in result.answer


def test_brain_fastpath_skips_llm(runtime: SimpleNamespace) -> None:
    class ExplodingLLM:
        def __call__(self, messages: list[dict[str, str]]) -> str:
            raise AssertionError("fast path must not call the LLM")

    brain = KiviBrain(
        ExplodingLLM(), runtime.registry, runtime.executor, runtime.permissions
    )
    result = brain.ask("прочитай docs/note.txt", task_id="t15")
    assert result.ok
    assert result.fastpath_used is True
    assert "hello world" in result.answer


def test_killswitch_blocks_plan(runtime: SimpleNamespace) -> None:
    switch = KillSwitch(runtime.tmp_path / "killswitch.json")
    switch.engage("scenario test")
    executor = ToolExecutor(
        runtime.registry,
        runtime.db,
        runtime.permissions,
        ProtectedZones(runtime.workspace),
        workspace=runtime.workspace,
        killswitch=switch,
    )
    plan_executor = PlanExecutor(runtime.registry, executor, runtime.permissions)
    result = plan_executor.execute(
        make_plan(Step("s1", "fs.read", {"path": "docs/note.txt"})),
        "t16",
    )
    assert not result.ok
    assert "kill switch" in result.error.lower()


def test_verifier_fails_failed_execution() -> None:
    from kivi.brain.executor import ExecutionResult

    plan = make_plan(Step("s1", "fs.read", {"path": "x"}))
    assert Verifier().verify(plan, ExecutionResult(status="failed", error="boom")).ok is False


def test_planner_returns_valid_plan(runtime: SimpleNamespace) -> None:
    class FakeLLM:
        def __call__(self, messages: list[dict[str, str]]) -> str:
            return (
                '{"goal": "x", "steps": ['
                '{"id": "s1", "tool": "fs.list", "args": {"path": "docs"}}]}'
            )

    planner = Planner(FakeLLM(), runtime.registry)
    plan = planner.plan("покажи docs", context="", task_id="t17")
    assert plan.steps[0].tool == "fs.list"
    assert plan.goal == "покажи docs"
