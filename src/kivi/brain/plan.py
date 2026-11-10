"""Plan model, JSON parser and structural validator for the deep path.

The planner model is untrusted: it only proposes a plan. This module is the
first gate. It accepts a JSON object (optionally wrapped in a markdown fence),
builds immutable :class:`Step` objects and rejects anything the executor could
not run deterministically: empty plans, duplicate ids, unknown tools, missing
dependencies and cycles.

A plan is a DAG, but execution is deliberately a deterministic topological
order so the same plan always touches the system in the same sequence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from kivi.tools.registry import ToolRegistry

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_STEP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_MAX_GOAL_CHARS = 4000
_MAX_CRITERIA = 20


class PlanError(ValueError):
    """Raised when a plan payload is malformed or unsafe to execute."""


@dataclass(frozen=True, slots=True)
class Step:
    """One tool call in a plan. ``depends_on`` names earlier steps."""

    id: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "args": dict(self.args),
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True, slots=True)
class Plan:
    """A goal, an ordered set of steps and optional acceptance criteria."""

    goal: str
    steps: tuple[Step, ...]
    criteria: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "steps": [step.as_dict() for step in self.steps],
            "criteria": list(self.criteria),
        }


def _strip_fence(text: str) -> str:
    match = _FENCE.search(text)
    if match is not None:
        return match.group(1).strip()
    return text.strip()


def _load_object(text: str) -> dict[str, Any]:
    """Parse the first JSON object in ``text``; tolerate prose around it."""
    if not isinstance(text, str) or not text.strip():
        raise PlanError("plan is empty")
    cleaned = _strip_fence(text)
    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        candidates.append(cleaned[start : end + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            raise PlanError("plan must be a JSON object")
        return payload
    raise PlanError("plan is not valid JSON")


def _parse_criteria(payload: dict[str, Any]) -> tuple[str, ...]:
    raw = payload.get("criteria", ())
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise PlanError("criteria must be a list of strings")
    if len(raw) > _MAX_CRITERIA:
        raise PlanError(f"too many criteria: {len(raw)} > {_MAX_CRITERIA}")
    criteria: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise PlanError("criteria must be non-empty strings")
        criteria.append(item.strip()[:500])
    return tuple(criteria)


def _parse_step(raw: Any) -> Step:
    if not isinstance(raw, dict):
        raise PlanError("every step must be a JSON object")
    step_id = raw.get("id")
    tool = raw.get("tool")
    args = raw.get("args", {})
    depends_on = raw.get("depends_on", ())
    if not isinstance(step_id, str) or not _STEP_ID.match(step_id):
        raise PlanError(f"invalid step id: {step_id!r}")
    if not isinstance(tool, str) or not tool.strip():
        raise PlanError(f"step {step_id}: tool must be a non-empty string")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise PlanError(f"step {step_id}: args must be an object")
    if not all(isinstance(key, str) for key in args):
        raise PlanError(f"step {step_id}: args keys must be strings")
    if depends_on is None:
        depends_on = ()
    if not isinstance(depends_on, (list, tuple)):
        raise PlanError(f"step {step_id}: depends_on must be a list")
    deps: list[str] = []
    for dep in depends_on:
        if not isinstance(dep, str) or not dep:
            raise PlanError(f"step {step_id}: depends_on entries must be strings")
        if dep not in deps:
            deps.append(dep)
    return Step(id=step_id, tool=tool.strip(), args=dict(args), depends_on=tuple(deps))


def topological_order(steps: tuple[Step, ...] | list[Step]) -> tuple[Step, ...]:
    """Return a stable topological order, or raise on duplicates/missing deps."""
    items = list(steps)
    ids: set[str] = set()
    by_id: dict[str, Step] = {}
    for step in items:
        if step.id in ids:
            raise PlanError(f"duplicate step id: {step.id}")
        ids.add(step.id)
        by_id[step.id] = step
    for step in items:
        for dep in step.depends_on:
            if dep not in ids:
                raise PlanError(f"step {step.id} depends on unknown step {dep}")
            if dep == step.id:
                raise PlanError(f"step {step.id} depends on itself")
    order: list[Step] = []
    done: set[str] = set()
    while len(order) < len(items):
        progressed = False
        for step in items:
            if step.id in done:
                continue
            if all(dep in done for dep in step.depends_on):
                order.append(step)
                done.add(step.id)
                progressed = True
        if not progressed:
            blocked = sorted(step.id for step in items if step.id not in done)
            raise PlanError(f"dependency cycle among steps: {', '.join(blocked)}")
    return tuple(order)


def validate_plan(plan: Plan, registry: ToolRegistry | None = None) -> None:
    """Validate an already constructed plan; raise :class:`PlanError` on error."""
    if not isinstance(plan, Plan):
        raise PlanError("not a Plan")
    if not isinstance(plan.goal, str):
        raise PlanError("goal must be a string")
    if len(plan.goal) > _MAX_GOAL_CHARS:
        raise PlanError(f"goal is too long: {len(plan.goal)} > {_MAX_GOAL_CHARS}")
    if not plan.steps:
        raise PlanError("plan has no steps")
    if not isinstance(plan.criteria, tuple):
        raise PlanError("criteria must be a tuple")
    for criterion in plan.criteria:
        if not isinstance(criterion, str) or not criterion.strip():
            raise PlanError("criteria must be non-empty strings")

    seen: set[str] = set()
    for step in plan.steps:
        if not isinstance(step, Step):
            raise PlanError("every step must be a Step")
        if not _STEP_ID.match(step.id):
            raise PlanError(f"invalid step id: {step.id!r}")
        if step.id in seen:
            raise PlanError(f"duplicate step id: {step.id}")
        seen.add(step.id)
        if not isinstance(step.tool, str) or not step.tool:
            raise PlanError(f"step {step.id}: tool is required")
        if not isinstance(step.args, dict):
            raise PlanError(f"step {step.id}: args must be an object")
        if not isinstance(step.depends_on, (tuple, list)):
            raise PlanError(f"step {step.id}: depends_on must be a sequence")
        if registry is not None and registry.get(step.tool) is None:
            raise PlanError(f"step {step.id}: unknown tool {step.tool!r}")
    topological_order(list(plan.steps))


def parse_plan(
    text: str,
    registry: ToolRegistry | None = None,
    *,
    goal: str = "",
) -> Plan:
    """Parse and validate a plan JSON document.

    ``registry`` enables tool-name validation; without it only structure is
    checked. ``goal`` overrides the goal inside the JSON so the caller's trusted
    wording is kept instead of the model's paraphrase.
    """
    payload = _load_object(text)
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanError("plan must contain a non-empty steps list")
    steps = tuple(_parse_step(raw) for raw in raw_steps)
    payload_goal = payload.get("goal", "")
    if not goal:
        if payload_goal is None:
            payload_goal = ""
        if not isinstance(payload_goal, str):
            raise PlanError("goal must be a string")
        goal = payload_goal.strip()
    plan = Plan(goal=goal, steps=steps, criteria=_parse_criteria(payload))
    validate_plan(plan, registry)
    return plan


__all__ = [
    "Plan",
    "PlanError",
    "Step",
    "parse_plan",
    "topological_order",
    "validate_plan",
]
