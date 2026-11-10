"""KiviBrain: fast path first, then plan -> execute -> verify.

This is the single entry point the CLI/chat should call. The brain does not
touch the OS: the fast path and the plan executor both go through the
ToolExecutor, and every action is audited there. The brain only decides which
route to take and what answer to show.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from kivi.brain.executor import Approver, ExecutionResult, PlanExecutor
from kivi.brain.fastpath import FastPath
from kivi.brain.plan import Plan
from kivi.brain.planner import LLMCallable, Planner
from kivi.brain.verifier import Verification, Verifier
from kivi.safety.permissions import PermissionEngine
from kivi.tools import ToolExecutor, ToolRegistry

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BrainResult:
    """Answer plus the route and artefacts that produced it."""

    answer: str
    plan: Plan | None = None
    execution: ExecutionResult | None = None
    fastpath_used: bool = False
    verification: Verification | None = None
    ok: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "ok": self.ok,
            "fastpath_used": self.fastpath_used,
            "plan": self.plan.as_dict() if self.plan is not None else None,
            "execution": self.execution.as_dict() if self.execution is not None else None,
            "verification": (
                self.verification.as_dict() if self.verification is not None else None
            ),
        }


def _last_output(execution: ExecutionResult) -> str:
    for item in reversed(execution.step_results):
        if item.ok and item.output.strip():
            return item.output
    return ""


def _answer(execution: ExecutionResult, verification: Verification, ok: bool) -> str:
    last = _last_output(execution)
    if ok:
        return last or "Готово."
    return execution.error or verification.reason or last or "Не удалось выполнить задачу."


class KiviBrain:
    """Coordinates the fast path and the planned deep path."""

    def __init__(
        self,
        llm: LLMCallable,
        registry: ToolRegistry,
        executor: PlanExecutor | ToolExecutor,
        permissions: PermissionEngine,
        *,
        fastpath: FastPath | None = None,
        planner: Planner | None = None,
        verifier: Verifier | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.permissions = permissions
        # The CLI may pass either the already-built PlanExecutor or the raw
        # ToolExecutor; both are supported so integration does not need a
        # particular wiring order.
        if isinstance(executor, PlanExecutor):
            self.executor = executor
        else:
            self.executor = PlanExecutor(registry, executor, permissions)
        self.tool_executor = self.executor.executor
        self.fastpath = fastpath if fastpath is not None else FastPath(
            registry, self.tool_executor, permissions
        )
        self.planner = planner if planner is not None else Planner(llm, registry)
        self.verifier = verifier if verifier is not None else Verifier(llm)

    def _verify(self, plan: Plan, execution: ExecutionResult) -> Verification:
        try:
            return self.verifier.verify(plan, execution)
        except Exception as exc:  # noqa: BLE001 - verifier must not crash the CLI
            log.exception("verifier crashed")
            return Verification(False, f"verifier crashed: {type(exc).__name__}")

    def ask(
        self,
        goal: str,
        *,
        context: str = "",
        task_id: str = "",
        approve: Approver | None = None,
    ) -> BrainResult:
        """Answer ``goal`` using the fast path or a planned, verified execution."""
        text = goal.strip()
        actual_task = task_id or f"brain-{uuid4().hex[:12]}"
        if not text:
            return BrainResult(answer="Пустая цель.", ok=False)

        try:
            fast = self.fastpath.try_run(text, actual_task)
        except Exception:  # noqa: BLE001 - fast path is only an optimisation
            log.exception("fast path crashed; falling back to planning")
            fast = None
        if fast is not None:
            return BrainResult(
                answer=fast.output or fast.error or "Готово.",
                fastpath_used=True,
                ok=fast.ok,
            )

        try:
            plan = self.planner.plan(text, context=context, task_id=actual_task)
        except Exception as exc:  # noqa: BLE001 - model/parse failure is reported
            log.exception("planning failed")
            return BrainResult(answer=f"Planning failed: {exc}", ok=False)

        try:
            execution = self.executor.execute(
                plan,
                actual_task,
                approve=approve,
                goal=text,
            )
        except Exception as exc:  # noqa: BLE001 - executor failure is reported
            log.exception("plan execution failed")
            return BrainResult(
                answer=f"Execution failed: {exc}",
                plan=plan,
                ok=False,
            )
        verification = self._verify(plan, execution)

        max_replans = getattr(self.executor, "max_replans", 0)
        if not verification.ok and execution.replans < max_replans:
            feedback = f"Previous plan failed verification: {verification.reason}"
            retry_context = f"{context}\n\n{feedback}" if context else feedback
            try:
                retry_plan = self.planner.plan(
                    text,
                    context=retry_context,
                    task_id=actual_task,
                )
                retry_execution = self.executor.execute(
                    retry_plan,
                    actual_task,
                    approve=approve,
                    goal=text,
                )
            except Exception:  # noqa: BLE001 - keep the first result on retry failure
                log.exception("verification retry failed")
            else:
                plan = retry_plan
                execution = retry_execution
                verification = self._verify(plan, execution)

        ok = execution.ok and verification.ok
        return BrainResult(
            answer=_answer(execution, verification, ok),
            plan=plan,
            execution=execution,
            fastpath_used=False,
            verification=verification,
            ok=ok,
        )


__all__ = ["BrainResult", "KiviBrain"]
