"""Plan executor: run a validated DAG through the ToolExecutor.

The executor is an authority boundary, not a convenience wrapper. Every step
goes through :class:`~kivi.tools.executor.ToolExecutor` with a task-scoped
grant, so the ToolExecutor still validates arguments, checks capabilities,
enforces risk levels and writes the audit row. The plan executor only decides
*order*, injects earlier outputs and asks for approval on R2/R3.

If a step fails and a replanner is configured, the replanner gets the failed
plan, step and error and may return a new plan. Successful step results are
kept, so a replanned plan can reuse earlier outputs and does not repeat side
effects. Failed attempts are marked ``superseded`` so the verifier can ignore
them once a later plan succeeds.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal
from uuid import uuid4

from kivi.brain.plan import (
    Plan,
    PlanError,
    Step,
    topological_order,
    validate_plan,
)
from kivi.kernel.db import Database
from kivi.safety.permissions import Grant, PermissionEngine
from kivi.tools.base import ToolResult
from kivi.tools.executor import ToolExecutor
from kivi.tools.registry import ToolRegistry

ExecutionStatus = Literal["done", "failed", "replanned"]
Approver = Callable[[Step], bool]
Replanner = Callable[[Plan, Step, str], Plan]

_TEMPLATE = re.compile(r"\{\{\s*([A-Za-z0-9][A-Za-z0-9_-]*)\s*\}\}")
_APPROVAL_RISKS = frozenset({"R2", "R3"})


@dataclass(frozen=True, slots=True)
class StepResult:
    """Outcome of one step attempt, including the raw tool result."""

    step_id: str
    tool: str
    ok: bool
    output: str = ""
    error: str = ""
    result: ToolResult | None = None
    superseded: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Final state of a plan execution."""

    status: ExecutionStatus
    output: str = ""
    step_results: tuple[StepResult, ...] = ()
    error: str = ""
    replans: int = 0
    plan: Plan | None = None

    @property
    def ok(self) -> bool:
        """True when execution completed, with or without a replan."""
        return self.status in ("done", "replanned")

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "replans": self.replans,
            "steps": [
                {
                    "step_id": item.step_id,
                    "tool": item.tool,
                    "ok": item.ok,
                    "output": item.output,
                    "error": item.error,
                    "superseded": item.superseded,
                }
                for item in self.step_results
            ],
        }


def _substitute(value: Any, outputs: dict[str, str]) -> Any:
    """Replace ``{{step_id}}`` in string args recursively."""
    if isinstance(value, str):
        return _TEMPLATE.sub(lambda match: outputs.get(match.group(1), match.group(0)), value)
    if isinstance(value, dict):
        return {key: _substitute(item, outputs) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_substitute(item, outputs) for item in value]
    return value


def _first_unresolved(value: Any, outputs: dict[str, str]) -> str | None:
    """Return the first template id that has no successful output yet."""
    if isinstance(value, str):
        for match in _TEMPLATE.finditer(value):
            if match.group(1) not in outputs:
                return match.group(1)
        return None
    if isinstance(value, dict):
        for item in value.values():
            found = _first_unresolved(item, outputs)
            if found is not None:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _first_unresolved(item, outputs)
            if found is not None:
                return found
    return None


class PlanExecutor:
    """Executes plans in DAG order with grants, approvals, replans and optional DB."""

    def __init__(
        self,
        registry: ToolRegistry,
        executor: ToolExecutor,
        permissions: PermissionEngine,
        *,
        max_replans: int = 2,
        replanner: Replanner | None = None,
        db: Database | None = None,
    ) -> None:
        if max_replans < 0:
            raise ValueError("max_replans must be zero or positive")
        self.registry = registry
        self.executor = executor
        self.permissions = permissions
        self.max_replans = max_replans
        self.replanner = replanner
        self.db = db
        self._db_indices: dict[str, int] = {}

    # -- grants ----------------------------------------------------------
    def _grant_for(self, task_id: str, plan: Plan) -> Grant:
        capabilities: set[str] = set()
        for step in plan.steps:
            tool = self.registry.get(step.tool)
            if tool is None:
                raise PlanError(f"unknown tool: {step.tool}")
            capabilities.update(tool.capabilities)
        return self.permissions.issue(task_id, sorted(capabilities))

    # -- optional SQLite state ------------------------------------------
    def _db_start(self, task_id: str, goal: str) -> None:
        if self.db is None:
            return
        if self.db.get_task(task_id) is None:
            self.db.create_task(task_id, "plan", {"goal": goal})
        else:
            self.db.update_task(task_id, "running")

    def _db_record(
        self,
        task_id: str,
        step: Step,
        args: dict[str, Any],
        result: ToolResult | None,
        ok: bool,
        error: str,
    ) -> None:
        if self.db is None:
            return
        index = self._db_indices.get(task_id, 0)
        db_step_id = f"{task_id}:{uuid4().hex[:10]}"
        self.db.add_step(
            db_step_id,
            task_id,
            index,
            step.tool,
            payload={"step_id": step.id, "args": args},
        )
        if ok:
            self.db.finish_step(
                db_step_id,
                "done",
                output=result.as_dict() if result is not None else {"ok": True},
            )
        else:
            self.db.finish_step(db_step_id, "failed", error=error)
        self._db_indices[task_id] = index + 1

    def _db_finish(self, task_id: str, status: str, goal: str, replans: int, error: str) -> None:
        if self.db is None:
            return
        payload: dict[str, Any] = {"goal": goal, "replans": replans, "status": status}
        if error:
            payload["error"] = error
        self.db.update_task(task_id, status, payload)

    # -- execution -------------------------------------------------------
    def execute(
        self,
        plan: Plan,
        task_id: str,
        approve: Approver | None = None,
        goal: str = "",
    ) -> ExecutionResult:
        """Run ``plan`` to completion or stop at the first unrecoverable failure."""
        if not task_id:
            raise ValueError("task_id is required")
        validate_plan(plan, self.registry)
        effective_goal = goal.strip() if goal else plan.goal
        self._db_start(task_id, effective_goal)
        grant = self._grant_for(task_id, plan)

        outputs: dict[str, str] = {}
        completed: dict[str, Step] = {}
        results: list[StepResult] = []
        replans = 0
        current = plan
        status: ExecutionStatus = "done"
        final_error = ""

        def record(
            step: Step,
            args: dict[str, Any],
            result: ToolResult | None,
            ok: bool,
            error: str,
        ) -> StepResult:
            item = StepResult(
                step_id=step.id,
                tool=step.tool,
                ok=ok,
                output=result.output if result is not None else "",
                error=error,
                result=result,
            )
            results.append(item)
            self._db_record(task_id, step, args, result, ok, error)
            return item

        while True:
            failure: tuple[Step, str] | None = None
            for step in topological_order(list(current.steps)):
                tool = self.registry.get(step.tool)
                if tool is None:
                    message = f"unknown tool: {step.tool}"
                    record(step, {}, None, False, message)
                    failure = (step, message)
                    break
                previous = completed.get(step.id)
                if previous is not None:
                    if previous.tool != step.tool or previous.args != step.args:
                        message = f"replan reuses step id {step.id!r} with different tool or args"
                        record(step, {}, None, False, message)
                        failure = (step, message)
                        break
                    continue

                args = _substitute(step.args, outputs)
                unresolved = _first_unresolved(args, outputs)
                if unresolved is not None:
                    message = f"unresolved output reference: {{{{{unresolved}}}}}"
                    record(step, args, None, False, message)
                    failure = (step, message)
                    break

                approved = False
                if tool.risk in _APPROVAL_RISKS and approve is not None:
                    try:
                        # Approve what will actually run, not the raw template.
                        approved = bool(approve(replace(step, args=args)))
                    except Exception as exc:  # noqa: BLE001 - callback is a boundary
                        message = f"approval callback failed: {type(exc).__name__}"
                        record(step, args, None, False, message)
                        failure = (step, message)
                        break

                result = self.executor.run(
                    step.tool,
                    args,
                    grant=grant,
                    approved=approved,
                )
                record(step, args, result, result.ok, result.error)
                if result.ok:
                    completed[step.id] = step
                    outputs[step.id] = result.output
                else:
                    failure = (step, result.error or "tool failed")
                    break

            if failure is None:
                status = "replanned" if replans else "done"
                break

            failed_step, error = failure
            if self.replanner is None or replans >= self.max_replans:
                status = "failed"
                final_error = error
                break

            replans += 1
            try:
                candidate = self.replanner(current, failed_step, error)
            except Exception as exc:  # noqa: BLE001 - replanner is a boundary
                status = "failed"
                final_error = f"replanner failed: {type(exc).__name__}: {exc}"
                break
            if not isinstance(candidate, Plan):
                status = "failed"
                final_error = "replanner did not return a Plan"
                break
            try:
                validate_plan(candidate, self.registry)
                next_grant = self._grant_for(task_id, candidate)
            except (PlanError, ValueError) as exc:
                status = "failed"
                final_error = f"invalid replanned plan: {exc}"
                break

            for index in range(len(results) - 1, -1, -1):
                item = results[index]
                if item.step_id == failed_step.id and not item.ok:
                    results[index] = replace(item, superseded=True)
                    break
            grant = next_grant
            current = candidate

        successful = [item for item in results if item.ok]
        output = successful[-1].output if successful else ""
        execution = ExecutionResult(
            status=status,
            output=output,
            step_results=tuple(results),
            error=final_error,
            replans=replans,
            plan=current,
        )
        self._db_finish(task_id, status, effective_goal, replans, final_error)
        return execution


__all__ = [
    "Approver",
    "ExecutionResult",
    "ExecutionStatus",
    "PlanExecutor",
    "Replanner",
    "StepResult",
]
