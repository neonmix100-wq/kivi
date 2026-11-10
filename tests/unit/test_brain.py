from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from kivi.brain.core import KiviBrain
from kivi.brain.executor import ExecutionResult, PlanExecutor, StepResult
from kivi.brain.fastpath import FastPath
from kivi.brain.plan import Plan, PlanError, Step, parse_plan
from kivi.brain.planner import Planner
from kivi.brain.verifier import Verification, Verifier
from kivi.kernel.db import Database
from kivi.safety.permissions import PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools import ToolExecutor, ToolRegistry, ToolResult, build_registry
from kivi.tools.base import Preview, ToolContext, ToolError


class EchoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


class NestedArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[str]


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EchoTool:
    name = "test.echo"
    description = "Echo text; used to prove order and substitution."
    risk = "R0"
    capabilities = ("test.echo",)
    args_model = EchoArgs

    def __init__(self) -> None:
        self.calls: list[str] = []

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, EchoArgs)
        return Preview("echo", {"text": args.text})

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, EchoArgs)
        self.calls.append(args.text)
        return ToolResult(ok=True, output=f"echo:{args.text}", trust="untrusted:tool")


class NestedEchoTool:
    name = "test.nested"
    description = "Join a list of strings; used to prove nested substitution."
    risk = "R0"
    capabilities = ("test.nested",)
    args_model = NestedArgs

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, NestedArgs)
        return Preview("nested", {"items": list(args.items)})

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, NestedArgs)
        self.calls.append(list(args.items))
        return ToolResult(ok=True, output="|".join(args.items), trust="untrusted:tool")


class FailTool:
    name = "test.fail"
    description = "Always fails; used to prove stop and replan behaviour."
    risk = "R1"
    capabilities = ("test.fail",)
    args_model = EmptyArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        return Preview("fail", {})

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        raise ToolError("boom")


class ApproveTool:
    name = "test.approve"
    description = "R2 tool; used to prove the approval callback."
    risk = "R2"
    capabilities = ("test.approve",)
    args_model = EmptyArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        return Preview("approve", {}, reversible=False)

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, output="approved-run", trust="untrusted:tool")


class Env:
    def __init__(
        self,
        workspace: Path,
        db: Database,
        registry: ToolRegistry,
        executor: ToolExecutor,
        permissions: PermissionEngine,
        echo: EchoTool,
        nested: NestedEchoTool,
    ) -> None:
        self.workspace = workspace
        self.db = db
        self.registry = registry
        self.executor = executor
        self.permissions = permissions
        self.echo = echo
        self.nested = nested


class FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]

    @property
    def prompt(self) -> str:
        if not self.calls:
            return ""
        return "\n".join(message["content"] for message in self.calls[-1])


def step_json(
    step_id: str,
    tool: str,
    args: dict | None = None,
    depends_on: tuple[str, ...] = (),
) -> dict:
    return {
        "id": step_id,
        "tool": tool,
        "args": args or {},
        "depends_on": list(depends_on),
    }


def plan_json(
    steps: list[dict],
    *,
    goal: str = "goal",
    criteria: list[str] | None = None,
) -> str:
    payload: dict = {"goal": goal, "steps": steps}
    if criteria is not None:
        payload["criteria"] = criteria
    return json.dumps(payload, ensure_ascii=False)


def manual_plan(*steps: Step, criteria: tuple[str, ...] = ()) -> Plan:
    return Plan(goal="goal", steps=tuple(steps), criteria=criteria)


def manual_execution(
    *results: StepResult,
    status: str = "done",
    plan: Plan | None = None,
    error: str = "",
) -> ExecutionResult:
    output = next((item.output for item in reversed(results) if item.ok), "")
    return ExecutionResult(
        status=status,  # type: ignore[arg-type]
        output=output,
        step_results=tuple(results),
        error=error,
        plan=plan,
    )


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[Env]:
    root = tmp_path / "workspace"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "note.txt").write_text("hello brain", encoding="utf-8")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    db = Database(tmp_path / "kivi.db")
    zones = ProtectedZones(root)
    permissions = PermissionEngine(db, workspace=str(root))
    registry = build_registry()
    echo = EchoTool()
    nested = NestedEchoTool()
    for tool in (echo, nested, FailTool(), ApproveTool()):
        registry.register(tool)
    executor = ToolExecutor(registry, db, permissions, zones, workspace=root)
    yield Env(root, db, registry, executor, permissions, echo, nested)
    db.close()


# -- plan parsing and validation ------------------------------------------------


def test_parse_plan_tolerates_markdown_fence(env: Env) -> None:
    text = "Here is the plan:\n```json\n" + plan_json(
        [step_json("s1", "fs.read", {"path": "docs/note.txt"})]
    ) + "\n```\nThanks"
    plan = parse_plan(text, env.registry)
    assert plan.steps[0].tool == "fs.read"
    assert plan.steps[0].args == {"path": "docs/note.txt"}


def test_parse_plan_keeps_trusted_goal(env: Env) -> None:
    text = plan_json([step_json("s1", "fs.read", {"path": "docs/note.txt"})], goal="model goal")
    plan = parse_plan(text, env.registry, goal="user goal")
    assert plan.goal == "user goal"


def test_parse_plan_rejects_empty_plan(env: Env) -> None:
    with pytest.raises(PlanError, match="non-empty"):
        parse_plan(plan_json([]), env.registry)


def test_parse_plan_rejects_duplicate_ids(env: Env) -> None:
    text = plan_json(
        [
            step_json("s1", "fs.read", {"path": "docs/note.txt"}),
            step_json("s1", "fs.list", {"path": "docs"}),
        ]
    )
    with pytest.raises(PlanError, match="duplicate"):
        parse_plan(text, env.registry)


def test_parse_plan_rejects_unknown_tool(env: Env) -> None:
    text = plan_json([step_json("s1", "fs.delete", {"path": "docs/note.txt"})])
    with pytest.raises(PlanError, match="unknown tool"):
        parse_plan(text, env.registry)


def test_parse_plan_rejects_missing_dependency(env: Env) -> None:
    text = plan_json([step_json("s1", "fs.read", {"path": "docs/note.txt"}, ("s2",))])
    with pytest.raises(PlanError, match="unknown step"):
        parse_plan(text, env.registry)


def test_parse_plan_rejects_cycle(env: Env) -> None:
    text = plan_json(
        [
            step_json("s1", "fs.read", {"path": "docs/note.txt"}, ("s2",)),
            step_json("s2", "fs.list", {"path": "docs"}, ("s1",)),
        ]
    )
    with pytest.raises(PlanError, match="cycle"):
        parse_plan(text, env.registry)


def test_parse_plan_rejects_invalid_json(env: Env) -> None:
    with pytest.raises(PlanError, match="not valid JSON"):
        parse_plan("definitely not a plan", env.registry)


# -- planner --------------------------------------------------------------------


def test_planner_prompt_contains_specs_and_redacts_secrets(env: Env) -> None:
    llm = FakeLLM([plan_json([step_json("s1", "fs.read", {"path": "docs/note.txt"})])])
    planner = Planner(llm, env.registry)
    plan = planner.plan("read docs/note.txt", context="token sk-" + "A" * 20, task_id="t-plan")
    prompt = llm.prompt
    assert "fs.read" in prompt
    assert "risk=R0" in prompt
    assert '"path"' in prompt
    assert "sk-" not in prompt
    assert "[REDACTED]" in prompt
    assert plan.steps[0].id == "s1"


def test_planner_enforces_max_steps(env: Env) -> None:
    llm = FakeLLM(
        [
            plan_json(
                [
                    step_json("s1", "fs.read", {"path": "docs/note.txt"}),
                    step_json("s2", "fs.list", {"path": "docs"}),
                ]
            )
        ]
    )
    planner = Planner(llm, env.registry, max_steps=1)
    with pytest.raises(PlanError, match="maximum"):
        planner.plan("do two things")


def test_planner_rejects_garbage(env: Env) -> None:
    planner = Planner(FakeLLM(["not a plan"]), env.registry)
    with pytest.raises(PlanError, match="not valid JSON"):
        planner.plan("do something")


# -- plan executor --------------------------------------------------------------


def test_executor_runs_in_dependency_order(env: Env) -> None:
    plan = Plan(
        goal="order",
        steps=(
            Step("s2", "test.echo", {"text": "{{s1}}"}, ("s1",)),
            Step("s1", "test.echo", {"text": "first"}),
        ),
    )
    result = PlanExecutor(env.registry, env.executor, env.permissions).execute(
        plan, "t-order"
    )
    assert result.ok
    assert result.status == "done"
    assert env.echo.calls == ["first", "echo:first"]
    assert [item.step_id for item in result.step_results] == ["s1", "s2"]
    assert result.output == "echo:echo:first"


def test_executor_substitutes_nested_arguments(env: Env) -> None:
    plan = Plan(
        goal="nested",
        steps=(
            Step("s1", "test.echo", {"text": "seed"}),
            Step("s2", "test.nested", {"items": ["a", "{{s1}}"]}, ("s1",)),
        ),
    )
    result = PlanExecutor(env.registry, env.executor, env.permissions).execute(
        plan, "t-nested"
    )
    assert result.ok
    assert env.nested.calls == [["a", "echo:seed"]]
    assert result.step_results[-1].output == "a|echo:seed"


def test_executor_failed_step_stops(env: Env) -> None:
    plan = Plan(
        goal="fail",
        steps=(
            Step("f", "test.fail"),
            Step("r", "fs.read", {"path": "docs/note.txt"}),
        ),
    )
    result = PlanExecutor(env.registry, env.executor, env.permissions).execute(
        plan, "t-fail"
    )
    assert result.status == "failed"
    assert not result.ok
    assert "boom" in result.error
    assert len(result.step_results) == 1


def test_executor_replans_after_failure(env: Env) -> None:
    calls: list[tuple[str, str, str]] = []

    def replanner(plan: Plan, failed: Step, error: str) -> Plan:
        calls.append((plan.goal, failed.id, error))
        return Plan(
            goal=plan.goal,
            steps=(Step("r", "fs.read", {"path": "docs/note.txt"}),),
        )

    plan = Plan(goal="replan", steps=(Step("f", "test.fail"),))
    executor = PlanExecutor(
        env.registry,
        env.executor,
        env.permissions,
        max_replans=2,
        replanner=replanner,
    )
    result = executor.execute(plan, "t-replan")
    assert result.status == "replanned"
    assert result.ok
    assert result.replans == 1
    assert result.output == "hello brain"
    assert result.step_results[0].superseded is True
    assert calls == [("replan", "f", "boom")]
    assert Verifier().verify(plan, result).ok


def test_executor_requires_approval_for_r2(env: Env) -> None:
    plan = Plan(goal="approve", steps=(Step("a", "test.approve"),))
    result = PlanExecutor(env.registry, env.executor, env.permissions).execute(
        plan, "t-approve-no"
    )
    assert not result.ok
    assert "confirmation required" in result.error


def test_executor_approval_allows_r2(env: Env) -> None:
    seen: list[str] = []

    def approve(step: Step) -> bool:
        seen.append(step.id)
        return True

    plan = Plan(goal="approve", steps=(Step("a", "test.approve"),))
    result = PlanExecutor(env.registry, env.executor, env.permissions).execute(
        plan, "t-approve-yes", approve=approve
    )
    assert result.ok
    assert result.output == "approved-run"
    assert seen == ["a"]


def test_executor_records_task_and_steps_in_db(env: Env) -> None:
    plan = Plan(goal="db", steps=(Step("s1", "test.echo", {"text": "db"}),))
    executor = PlanExecutor(
        env.registry,
        env.executor,
        env.permissions,
        db=env.db,
    )
    result = executor.execute(plan, "t-db")
    assert result.ok
    task = env.db.get_task("t-db")
    assert task is not None
    assert task["status"] == "done"
    steps = env.db.steps_for("t-db")
    assert len(steps) == 1
    assert steps[0]["status"] == "done"
    assert steps[0]["tool"] == "test.echo"
    assert "echo:db" in steps[0]["output"]


# -- verifier -------------------------------------------------------------------


def test_verifier_accepts_success() -> None:
    plan = manual_plan(Step("s1", "fs.read", {"path": "docs/note.txt"}))
    execution = manual_execution(
        StepResult("s1", "fs.read", True, output="hello"),
        plan=plan,
    )
    verification = Verifier().verify(plan, execution)
    assert verification.ok
    assert verification.reason == "all steps completed"


def test_verifier_rejects_failed_step() -> None:
    plan = manual_plan(Step("s1", "fs.read", {"path": "docs/note.txt"}))
    execution = manual_execution(
        StepResult("s1", "fs.read", False, error="bad"),
        plan=plan,
    )
    verification = Verifier().verify(plan, execution)
    assert not verification.ok
    assert "bad" in verification.reason


def test_verifier_rejects_empty_output() -> None:
    plan = manual_plan(Step("s1", "fs.read", {"path": "docs/note.txt"}))
    execution = manual_execution(
        StepResult("s1", "fs.read", True, output="  "),
        plan=plan,
    )
    verification = Verifier().verify(plan, execution)
    assert not verification.ok
    assert "no output" in verification.reason


def test_verifier_uses_llm_for_criteria() -> None:
    plan = manual_plan(
        Step("s1", "fs.read", {"path": "docs/note.txt"}),
        criteria=("mentions hello",),
    )
    execution = manual_execution(
        StepResult("s1", "fs.read", True, output="hello"),
        plan=plan,
    )
    llm = FakeLLM(['{"ok": false, "reason": "missing evidence"}'])
    verification = Verifier(llm).verify(plan, execution)
    assert not verification.ok
    assert verification.reason == "missing evidence"
    assert len(llm.calls) == 1


def test_verifier_deterministic_failure_beats_llm() -> None:
    plan = manual_plan(Step("s1", "fs.read", {"path": "docs/note.txt"}))
    execution = manual_execution(
        StepResult("s1", "fs.read", False, error="failed"),
        plan=plan,
    )
    llm = FakeLLM(['{"ok": true, "reason": "looks fine"}'])
    verification = Verifier(llm).verify(plan, execution)
    assert not verification.ok
    assert llm.calls == []


def test_verifier_without_llm_leaves_criteria_unchecked() -> None:
    plan = manual_plan(
        Step("s1", "fs.read", {"path": "docs/note.txt"}),
        criteria=("mentions hello",),
    )
    execution = manual_execution(
        StepResult("s1", "fs.read", True, output="hello"),
        plan=plan,
    )
    verification = Verifier().verify(plan, execution)
    assert verification.ok
    assert "not evaluated" in verification.reason


def test_verifier_rejects_invalid_llm_verdict() -> None:
    plan = manual_plan(
        Step("s1", "fs.read", {"path": "docs/note.txt"}),
        criteria=("mentions hello",),
    )
    execution = manual_execution(
        StepResult("s1", "fs.read", True, output="hello"),
        plan=plan,
    )
    verification = Verifier(FakeLLM(["not json"])).verify(plan, execution)
    assert not verification.ok
    assert "invalid JSON" in verification.reason


# -- fast path ------------------------------------------------------------------


def test_fastpath_reads_quoted_path(env: Env) -> None:
    fast = FastPath(env.registry, env.executor, env.permissions)
    result = fast.try_run('прочитай "docs/note.txt"', "fp-read")
    assert result is not None
    assert result.ok
    assert result.output == "hello brain"


def test_fastpath_reads_unquoted_path(env: Env) -> None:
    fast = FastPath(env.registry, env.executor, env.permissions)
    result = fast.try_run("read docs/note.txt", "fp-read-plain")
    assert result is not None
    assert result.ok
    assert result.output == "hello brain"


def test_fastpath_lists_directory(env: Env) -> None:
    fast = FastPath(env.registry, env.executor, env.permissions)
    for index, goal in enumerate(("список docs", "list docs")):
        result = fast.try_run(goal, f"fp-list-{index}")
        assert result is not None
        assert result.ok
        assert "note.txt" in result.output


def test_fastpath_lists_processes(env: Env) -> None:
    fast = FastPath(env.registry, env.executor, env.permissions)
    for index, goal in enumerate(("processes", "процессы")):
        result = fast.try_run(goal, f"fp-proc-{index}")
        assert result is not None
        assert result.ok
        assert "count" in result.meta


def test_fastpath_returns_none_for_everything_else(env: Env) -> None:
    fast = FastPath(env.registry, env.executor, env.permissions)
    for goal in ("напиши стих", "shell.exec whoami", "read", "list"):
        assert fast.try_run(goal, "fp-none") is None


def test_fastpath_denies_traversal_and_secrets(env: Env) -> None:
    fast = FastPath(env.registry, env.executor, env.permissions)
    traversal = fast.try_run('read "../outside.txt"', "fp-traversal")
    assert traversal is not None
    assert not traversal.ok
    assert "zone" in traversal.error.lower()
    secret = fast.try_run("read .env", "fp-secret")
    assert secret is not None
    assert not secret.ok
    assert "secret" in secret.error.lower()


def test_fastpath_generates_task_id(env: Env) -> None:
    fast = FastPath(env.registry, env.executor, env.permissions)
    result = fast.try_run("read docs/note.txt", "")
    assert result is not None
    assert result.ok


# -- core -----------------------------------------------------------------------


def test_core_fastpath_does_not_call_llm(env: Env) -> None:
    llm = FakeLLM([])
    brain = KiviBrain(llm, env.registry, env.executor, env.permissions)
    result = brain.ask("read docs/note.txt", task_id="t-core-fast")
    assert result.fastpath_used is True
    assert result.ok
    assert result.answer == "hello brain"
    assert result.plan is None
    assert llm.calls == []


def test_core_planned_path(env: Env) -> None:
    plan = plan_json([step_json("s1", "fs.read", {"path": "docs/note.txt"})])
    llm = FakeLLM([plan])
    brain = KiviBrain(llm, env.registry, env.executor, env.permissions)
    result = brain.ask("summarize docs/note.txt", task_id="t-core-plan")
    assert result.fastpath_used is False
    assert result.ok
    assert result.answer == "hello brain"
    assert result.plan is not None
    assert result.execution is not None
    assert result.execution.status == "done"
    assert len(llm.calls) == 1


def test_core_accepts_plan_executor(env: Env) -> None:
    plan = plan_json([step_json("s1", "fs.read", {"path": "docs/note.txt"})])
    llm = FakeLLM([plan])
    plan_executor = PlanExecutor(env.registry, env.executor, env.permissions)
    brain = KiviBrain(llm, env.registry, plan_executor, env.permissions)
    result = brain.ask("explain docs/note.txt", task_id="t-core-plan-executor")
    assert result.ok
    assert result.answer == "hello brain"


def test_core_retries_after_verification_failure(env: Env) -> None:
    bad = plan_json([step_json("s1", "fs.read", {"path": "docs/missing.txt"})])
    good = plan_json([step_json("s1", "fs.read", {"path": "docs/note.txt"})])
    llm = FakeLLM([bad, good])
    brain = KiviBrain(llm, env.registry, env.executor, env.permissions)
    result = brain.ask("analyze docs/note.txt", task_id="t-core-retry")
    assert result.ok
    assert result.answer == "hello brain"
    assert len(llm.calls) == 2
    assert "Previous plan failed verification" in llm.prompt


def test_core_reports_failure(env: Env) -> None:
    bad = plan_json([step_json("s1", "fs.read", {"path": "docs/missing.txt"})])
    llm = FakeLLM([bad])
    brain = KiviBrain(llm, env.registry, env.executor, env.permissions)
    result = brain.ask("inspect docs/missing.txt", task_id="t-core-fail")
    assert result.fastpath_used is False
    assert result.ok is False
    assert "not a file" in result.answer
    assert len(llm.calls) == 2


def test_core_reports_planning_error(env: Env) -> None:
    llm = FakeLLM(["not a plan"])
    brain = KiviBrain(llm, env.registry, env.executor, env.permissions)
    result = brain.ask("do something impossible", task_id="t-core-bad-plan")
    assert result.ok is False
    assert result.answer.startswith("Planning failed")
    assert len(llm.calls) == 1


def test_core_fastpath_failure_does_not_plan(env: Env) -> None:
    llm = FakeLLM([plan_json([step_json("s1", "fs.read", {"path": "docs/note.txt"})])])
    brain = KiviBrain(llm, env.registry, env.executor, env.permissions)
    result = brain.ask("read .env", task_id="t-core-deny")
    assert result.fastpath_used is True
    assert result.ok is False
    assert "secret" in result.answer.lower()
    assert llm.calls == []


def test_verification_dataclass_roundtrip() -> None:
    verification = Verification(True, "fine")
    assert verification.as_dict() == {"ok": True, "reason": "fine"}
