"""Exact-text patch tool: replace a counted occurrence inside one UTF-8 file.

``code.patch`` is deliberately stricter than a general diff engine. The caller
supplies the exact old text, the new text and how many times the old text must
occur. If the file does not contain exactly that many occurrences the patch is
refused before anything is written, so a stale model of the file cannot silently
corrupt it.

The preview is a unified diff. The real run writes a temporary file in the same
directory and replaces the target atomically, exactly like ``fs.write``. Paths
go through Protected Zones, so secrets, private directories and the safety core
stay unreachable even though the capability is ``fs.write``.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from kivi.safety.zones import ZoneError
from kivi.tools.base import Preview, ToolContext, ToolError, ToolResult


class PatchArgs(BaseModel):
    """Arguments for :class:`CodePatchTool`."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        min_length=1,
        max_length=4096,
        description="File path, relative to the workspace.",
    )
    old: str = Field(description="Exact text to replace; must not be empty.")
    new: str = Field(description="Replacement text; may be empty.")
    count: int = Field(default=1, ge=1, le=1000)


def _resolve(ctx: ToolContext, raw: str, access: str) -> Path:
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = ctx.workspace / candidate
    decision = ctx.zones.check(candidate, access)  # type: ignore[arg-type]
    if not decision.allowed:
        raise ZoneError(f"{decision.rule}: {decision.reason}")
    if decision.resolved is None:
        raise ZoneError("path could not be resolved")
    return decision.resolved


@dataclass(frozen=True, slots=True)
class _PatchPlan:
    """Everything validated before a patch is previewed or written."""

    target: Path
    relative: str
    before: str
    after: str
    diff: str
    occurrences: int
    bytes_before: int
    bytes_after: int
    added: int
    removed: int


def _relative(ctx: ToolContext, target: Path) -> str:
    try:
        return target.relative_to(ctx.zones.workspace).as_posix()
    except ValueError as exc:
        raise ZoneError("path is outside the workspace") from exc


def _plan(args: PatchArgs, ctx: ToolContext) -> _PatchPlan:
    """Validate the request and compute the patched text without writing."""
    if not args.old:
        raise ToolError("old text must not be empty")
    if args.old == args.new:
        raise ToolError("old and new text are identical")

    target = _resolve(ctx, args.path, "write")
    _resolve(ctx, args.path, "read")
    if not target.is_file():
        raise ToolError(f"not a file: {target}")

    limit = ctx.limits.max_write_bytes
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise ToolError(f"cannot read file: {exc}") from exc
    if len(data) > limit:
        raise ToolError(f"file is too large to patch (limit {limit} bytes)")
    try:
        before = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError("file is not valid UTF-8 text") from exc

    occurrences = before.count(args.old)
    if occurrences != args.count:
        raise ToolError(
            f"old text occurs {occurrences} times, expected {args.count}"
        )

    after = before.replace(args.old, args.new, args.count)
    after_bytes = after.encode("utf-8")
    if len(after_bytes) > limit:
        raise ToolError(f"patched file would exceed {limit} bytes")

    relative = _relative(ctx, target)
    diff_lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{relative}",
        tofile=f"b/{relative}",
        n=3,
    )
    diff = "".join(diff_lines)
    added = 0
    removed = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1

    return _PatchPlan(
        target=target,
        relative=relative,
        before=before,
        after=after,
        diff=diff,
        occurrences=occurrences,
        bytes_before=len(data),
        bytes_after=len(after_bytes),
        added=added,
        removed=removed,
    )


def _details(plan: _PatchPlan) -> dict[str, object]:
    return {
        "path": plan.relative,
        "occurrences": plan.occurrences,
        "bytes_before": plan.bytes_before,
        "bytes_after": plan.bytes_after,
        "added": plan.added,
        "removed": plan.removed,
        "diff": plan.diff,
    }


class CodePatchTool:
    """Replace an exact, counted text occurrence in a workspace file."""

    name = "code.patch"
    description = "Replace exact text in one UTF-8 file after a unified-diff preview."
    risk = "R1"
    capabilities = ("fs.write",)
    args_model = PatchArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, PatchArgs)
        plan = _plan(args, ctx)
        return Preview(
            "patch",
            _details(plan),
            reversible=False,
            warnings=(),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, PatchArgs)
        plan = _plan(args, ctx)
        data = plan.after.encode("utf-8")
        temporary = plan.target.with_name(f".{plan.target.name}.kivi-{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, plan.target)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)
        return ToolResult(
            ok=True,
            output=(
                f"patched {plan.relative}: {plan.occurrences} occurrence(s) "
                f"(+{plan.added}/-{plan.removed})"
            ),
            meta={
                "path": str(plan.target),
                "relative_path": plan.relative,
                "occurrences": plan.occurrences,
                "bytes_before": plan.bytes_before,
                "bytes_after": plan.bytes_after,
                "added": plan.added,
                "removed": plan.removed,
                "diff": plan.diff,
            },
            trust="untrusted:tool",
        )


__all__ = ["CodePatchTool", "PatchArgs"]
