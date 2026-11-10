"""Tool contract: typed arguments, previews and structured results.

A tool is deliberately boring: a name, a risk level, a capability set, a
Pydantic argument model and two methods. ``dry_run`` must be side-effect free
and ``run`` must be safe to call once the executor has checked permissions.

Tools receive a :class:`ToolContext` and never reach for global state, so the
same tool can be exercised with a temporary workspace in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from kivi.safety.permissions import Grant, Risk
from kivi.safety.policy import PolicyEngine
from kivi.safety.zones import ProtectedZones

RiskLevel = Risk


class ToolError(RuntimeError):
    """A structured, expected failure while preparing or running a tool."""


@dataclass(frozen=True, slots=True)
class Limits:
    """Hard caps that keep a tool from returning or consuming too much."""

    max_read_bytes: int = 64 * 1024
    max_write_bytes: int = 256 * 1024
    max_output_chars: int = 8000
    shell_timeout_s: float = 30.0


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Everything a tool may use; no ambient authority beyond this object."""

    workspace: Path
    zones: ProtectedZones
    policy: PolicyEngine
    grant: Grant
    limits: Limits = field(default_factory=Limits)

    @property
    def task_id(self) -> str:
        return self.grant.task_id


@dataclass(frozen=True, slots=True)
class Preview:
    """A side-effect-free description of what ``run`` would do."""

    action: str
    details: dict[str, Any] = field(default_factory=dict)
    reversible: bool = True
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "details": self.details,
            "reversible": self.reversible,
            "warnings": list(self.warnings),
        }


@dataclass(slots=True)
class ToolResult:
    """Structured result; errors are values, not exceptions, at this boundary."""

    ok: bool
    output: str = ""
    error: str = ""
    artifacts: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    trust: str = "untrusted:tool"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "artifacts": list(self.artifacts),
            "meta": dict(self.meta),
            "truncated": self.truncated,
            "trust": self.trust,
        }


class Tool(Protocol):
    """The contract every tool implements."""

    name: str
    description: str
    risk: RiskLevel
    capabilities: tuple[str, ...]
    args_model: type[BaseModel]

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview: ...

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult: ...


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Description published to a model or the CLI."""

    name: str
    description: str
    risk: RiskLevel
    capabilities: tuple[str, ...]
    schema: dict[str, Any]


def describe(tool: Tool) -> ToolSpec:
    """Build the public spec, including the JSON schema of the arguments."""
    return ToolSpec(
        name=tool.name,
        description=tool.description,
        risk=tool.risk,
        capabilities=tuple(tool.capabilities),
        schema=tool.args_model.model_json_schema(),
    )


__all__ = [
    "Limits",
    "Preview",
    "RiskLevel",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolResult",
    "ToolSpec",
    "describe",
]
