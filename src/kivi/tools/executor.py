"""Capability Broker: the only path from a proposed tool call to the OS.

The executor validates typed arguments, asks the Permission Engine for a
decision on every capability the tool needs, runs ``dry_run`` or ``run``, and
writes one audit row per attempt. Tools never call each other and never see the
database, so they cannot hide an action from the audit trail.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from kivi.kernel.db import Database
from kivi.safety.permissions import Grant, PermissionEngine, PermissionRequest
from kivi.safety.policy import PolicyEngine, PolicyError
from kivi.safety.zones import ProtectedZones, ZoneError
from kivi.tools.base import Limits, ToolContext, ToolError, ToolResult
from kivi.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


def _digest(payload: Mapping[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return sha256(text.encode("utf-8")).hexdigest()[:16]


class ToolExecutor:
    """Validates, authorises, audits and executes tool calls."""

    def __init__(
        self,
        registry: ToolRegistry,
        db: Database,
        permissions: PermissionEngine,
        zones: ProtectedZones,
        *,
        workspace: Path,
        policy: PolicyEngine | None = None,
        events: Any = None,
        limits: Limits | None = None,
        killswitch: Any = None,
    ) -> None:
        self.registry = registry
        self.db = db
        self.permissions = permissions
        self.zones = zones
        self.workspace = Path(workspace)
        self.policy = policy or PolicyEngine()
        self.events = events
        self.limits = limits or Limits()
        self.killswitch = killswitch

    # -- helpers ---------------------------------------------------------
    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        if self.events is not None:
            self.events.emit(f"tool.{name}", payload=payload)

    def _audit(
        self,
        tool: str,
        decision: str,
        reason: str,
        *,
        target: str,
        digest: str,
        details: dict[str, Any],
    ) -> None:
        self.db.append_audit(
            actor="tool",
            action=f"tool.{tool}",
            target=target or tool,
            decision=decision,
            reason=reason,
            details={"args_digest": digest, **details},
        )

    def _fail(
        self,
        tool: str,
        message: str,
        *,
        target: str = "",
        digest: str = "",
        decision: str = "deny",
        reason: str = "",
        details: dict[str, Any] | None = None,
    ) -> ToolResult:
        self._audit(
            tool,
            decision,
            reason or message,
            target=target,
            digest=digest,
            details=details or {},
        )
        self._emit("denied", {"tool": tool, "reason": message})
        return ToolResult(ok=False, error=message)

    @staticmethod
    def _exception_decision(exc: Exception) -> str:
        """A policy or zone refusal is a deny; an operational failure is an error."""
        if isinstance(exc, (PolicyError, ZoneError)):
            return "deny"
        return "error"

    # -- public API ------------------------------------------------------
    def run(
        self,
        name: str,
        raw_args: Mapping[str, Any] | None = None,
        *,
        grant: Grant,
        approved: bool = False,
        dry_run: bool = False,
    ) -> ToolResult:
        """Run one tool call under ``grant``; errors are returned, not raised."""
        raw = dict(raw_args or {})
        digest = _digest(raw)
        target = str(raw.get("path") or raw.get("cwd") or name)

        if self.killswitch is not None and self.killswitch.is_engaged():
            state = self.killswitch.state()
            return self._fail(
                name,
                f"kill switch engaged: {state.reason or 'stopped by user'}",
                target=target,
                digest=digest,
                reason="kill switch engaged",
                details={"killswitch": True},
            )

        tool = self.registry.get(name)
        if tool is None:
            return self._fail(
                name,
                f"unknown tool: {name}",
                target=target,
                digest=digest,
                decision="error",
            )

        try:
            args = tool.args_model.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - pydantic ValidationError is a value
            return self._fail(
                name,
                f"invalid arguments: {exc}",
                target=target,
                digest=digest,
                decision="error",
                details={"risk": tool.risk},
            )

        context = ToolContext(
            workspace=self.workspace,
            zones=self.zones,
            policy=self.policy,
            grant=grant,
            limits=self.limits,
        )

        for capability in tool.capabilities:
            request = PermissionRequest(
                tool=name,
                capability=capability,
                risk=tool.risk,  # type: ignore[arg-type]
                task_id=grant.task_id,
                path=target,
                approved=approved or dry_run,
            )
            decision = self.permissions.check(request, grant)
            if decision.action == "deny":
                return self._fail(
                    name,
                    f"denied: {decision.reason}",
                    target=target,
                    digest=digest,
                    reason=decision.reason,
                    details={
                        "capability": capability,
                        "risk": tool.risk,
                        "rule": decision.rule,
                        "task_id": grant.task_id,
                    },
                )
            if decision.action == "confirm":
                return self._fail(
                    name,
                    f"confirmation required for {tool.risk}: {decision.reason}",
                    target=target,
                    digest=digest,
                    decision="confirm",
                    reason=decision.reason,
                    details={
                        "capability": capability,
                        "risk": tool.risk,
                        "rule": decision.rule,
                        "task_id": grant.task_id,
                    },
                )

        details = {
            "capability": tool.capabilities[0],
            "risk": tool.risk,
            "task_id": grant.task_id,
            "dry_run": dry_run,
            "approved": approved,
        }

        if dry_run:
            try:
                preview = tool.dry_run(args, context)
            except (ToolError, ZoneError, PolicyError, OSError, ValueError) as exc:
                return self._fail(
                    name,
                    str(exc),
                    target=target,
                    digest=digest,
                    decision=self._exception_decision(exc),
                    reason=type(exc).__name__,
                    details=details,
                )
            self._audit(
                name,
                "allow",
                "dry run",
                target=target,
                digest=digest,
                details={**details, "preview": preview.as_dict()},
            )
            self._emit("preview", {"tool": name, "task_id": grant.task_id})
            return ToolResult(
                ok=True,
                output=json.dumps(preview.as_dict(), ensure_ascii=False, indent=2),
                meta={"dry_run": True, "action": preview.action},
            )

        try:
            result = tool.run(args, context)
        except (ToolError, ZoneError, PolicyError, OSError, ValueError) as exc:
            return self._fail(
                name,
                str(exc),
                target=target,
                digest=digest,
                decision=self._exception_decision(exc),
                reason=type(exc).__name__,
                details=details,
            )
        except Exception as exc:  # noqa: BLE001 - never crash the caller on a tool bug
            log.exception("tool %s crashed", name)
            return self._fail(
                name,
                f"tool crashed: {type(exc).__name__}",
                target=target,
                digest=digest,
                decision="error",
                reason="unexpected-error",
                details=details,
            )

        self._audit(
            name,
            "allow",
            "tool result",
            target=target,
            digest=digest,
            details={
                **details,
                "ok": result.ok,
                "truncated": result.truncated,
                "trust": result.trust,
            },
        )
        self._emit(
            "done",
            {"tool": name, "task_id": grant.task_id, "ok": result.ok},
        )
        return result


__all__ = ["ToolExecutor"]
