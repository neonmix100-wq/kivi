"""Permission Engine: task grants, risk levels and allow/confirm/deny.

The engine is the only place where a risk level becomes an action. A task gets
a short-lived grant with an explicit capability set; a tool call must present
that grant, its capability must be listed, and the risk policy of the current
autonomy mode decides whether the call runs, waits for confirmation or is
denied. Grants never widen: a tool cannot mint capabilities for itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from kivi.kernel.db import Database

Risk = Literal["R0", "R1", "R2", "R3", "R4"]
Action = Literal["allow", "confirm", "deny"]

RISKS: tuple[Risk, ...] = ("R0", "R1", "R2", "R3", "R4")

# Autonomy mode -> R0..R4 action. A4-Bounded is the configured default.
_AUTONOMY_RULES: dict[str, tuple[Action, ...]] = {
    "A4-Bounded": ("allow", "allow", "confirm", "confirm", "deny"),
    "A4": ("allow", "allow", "confirm", "confirm", "deny"),
    "A3": ("allow", "allow", "confirm", "deny", "deny"),
    "A2": ("allow", "confirm", "deny", "deny", "deny"),
    "A1": ("allow", "deny", "deny", "deny", "deny"),
    "A0": ("deny", "deny", "deny", "deny", "deny"),
}

MAX_TTL_S = 3600


@dataclass(frozen=True, slots=True)
class Grant:
    """A short-lived, task-scoped permission token."""

    token: str
    task_id: str
    capabilities: frozenset[str]
    workspace: str
    expires_at: datetime

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """One proposed tool call, before it reaches the operating system."""

    tool: str
    capability: str
    risk: Risk
    task_id: str = ""
    path: str = ""
    approved: bool = False


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    reason: str = ""
    rule: str | None = None

    @property
    def allowed(self) -> bool:
        return self.action == "allow"

    def require(self) -> None:
        if self.action != "allow":
            raise PermissionError(f"{self.rule}: {self.reason}")


class PermissionEngine:
    """Issues grants and turns risk levels into decisions."""

    def __init__(
        self,
        db: Database | None = None,
        *,
        autonomy: str = "A4-Bounded",
        workspace: str = "",
        events: Any = None,
        clock: Any = None,
        default_ttl_s: int = 300,
    ) -> None:
        if autonomy not in _AUTONOMY_RULES:
            raise ValueError(f"unknown autonomy mode: {autonomy}")
        if not 1 <= default_ttl_s <= MAX_TTL_S:
            raise ValueError(f"default_ttl_s must be between 1 and {MAX_TTL_S}")
        self.db = db
        self.autonomy = autonomy
        self.workspace = workspace
        self.events = events
        self._clock = clock or (lambda: datetime.now(UTC))
        self._default_ttl_s = default_ttl_s
        self._grants: dict[str, Grant] = {}

    # -- grants ----------------------------------------------------------
    def issue(
        self,
        task_id: str,
        capabilities: list[str] | tuple[str, ...] | set[str],
        *,
        ttl_s: int | None = None,
    ) -> Grant:
        """Create a grant for exactly the listed capabilities."""
        if not task_id:
            raise ValueError("task_id is required")
        wanted = frozenset(item for item in capabilities if item)
        if not wanted:
            raise ValueError("a grant needs at least one capability")
        ttl = self._default_ttl_s if ttl_s is None else ttl_s
        if not 1 <= ttl <= MAX_TTL_S:
            raise ValueError(f"ttl_s must be between 1 and {MAX_TTL_S}")
        grant = Grant(
            token=uuid.uuid4().hex,
            task_id=task_id,
            capabilities=wanted,
            workspace=self.workspace,
            expires_at=self._clock() + timedelta(seconds=ttl),
        )
        self._grants[task_id] = grant
        return grant

    def revoke(self, task_id: str) -> bool:
        """Drop a grant; further calls with it are denied."""
        return self._grants.pop(task_id, None) is not None

    def current(self, task_id: str) -> Grant | None:
        return self._grants.get(task_id)

    # -- decisions -------------------------------------------------------
    def _rule(self, risk: str) -> Action:
        index = RISKS.index(risk)  # raises ValueError for an unknown risk
        return _AUTONOMY_RULES[self.autonomy][index]

    def check(self, request: PermissionRequest, grant: Grant | None) -> Decision:
        """Decide whether ``request`` may run under ``grant``."""
        if grant is None:
            return self._deny(request, "no grant for this task", "grant-missing")
        current = self._grants.get(grant.task_id)
        if current is None or current.token != grant.token:
            return self._deny(request, "grant was revoked or replaced", "grant-revoked")
        if request.task_id and request.task_id != grant.task_id:
            return self._deny(request, "grant belongs to another task", "grant-task")
        now = self._clock()
        if grant.expired(now):
            return self._deny(request, "grant expired", "grant-expired")
        if request.capability not in grant.capabilities:
            return self._deny(
                request,
                f"capability {request.capability} is not granted",
                "grant-capability",
            )
        try:
            action = self._rule(request.risk)
        except ValueError:
            return self._deny(request, f"unknown risk level: {request.risk}", "risk-invalid")

        if action == "confirm" and request.approved:
            action = "allow"
        if action == "deny":
            return self._deny(
                request,
                f"risk {request.risk} is denied in {self.autonomy}",
                "risk-deny",
            )
        if action == "confirm":
            self._audit(request, "confirm", "user confirmation required", "risk-confirm")
            self._emit("confirm", request)
            return Decision("confirm", "user confirmation required", "risk-confirm")
        return Decision("allow")

    def _deny(self, request: PermissionRequest, reason: str, rule: str) -> Decision:
        self._audit(request, "deny", reason, rule)
        self._emit("deny", request)
        return Decision("deny", reason, rule)

    def _audit(self, request: PermissionRequest, decision: str, reason: str, rule: str) -> None:
        if self.db is None:
            return
        self.db.append_audit(
            actor="permission",
            action=f"policy.{request.tool}",
            target=request.path or request.tool,
            decision=decision,
            reason=reason,
            details={
                "capability": request.capability,
                "risk": request.risk,
                "task_id": request.task_id,
                "rule": rule,
            },
        )

    def _emit(self, name: str, request: PermissionRequest) -> None:
        if self.events is None:
            return
        self.events.emit(
            f"safety.{name}",
            payload={
                "tool": request.tool,
                "capability": request.capability,
                "risk": request.risk,
                "task_id": request.task_id,
            },
        )


__all__ = [
    "MAX_TTL_S",
    "RISKS",
    "Action",
    "Decision",
    "Grant",
    "PermissionEngine",
    "PermissionRequest",
    "Risk",
]
