"""Fixed git operations: status, diff, branch and commit.

There is no generic "run git with these arguments" tool. Every operation builds
a fixed argv list from a small typed model, validates branch names and
workspace-relative paths, and runs ``git`` with ``shell=False``, a minimal
environment and a timeout. Destructive or remote commands (push, reset, amend,
force, hooks bypass) have no representation here.

Risk levels follow the rest of Kivi: status and diff are R0, creating a branch
is R2 and committing is R3, so the executor requires confirmation for the last
two. Paths are checked by Protected Zones, which also rejects ``.git`` and
anything outside the workspace.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from kivi.safety.policy import Access
from kivi.safety.zones import ZoneError
from kivi.tools.base import Preview, ToolContext, ToolError, ToolResult
from kivi.tools.shell import _minimal_env

_GIT_TIMEOUT_S = 30.0
_MAX_MESSAGE_CHARS = 200
_MAX_PATHS = 100
_MAX_BRANCH_CHARS = 100

# A conservative subset of git check-ref-format: alphanumeric first character,
# then letters, digits, dot, underscore, dash or slash.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")


def _git_command(ctx: ToolContext, argv: list[str]) -> list[str]:
    return ["git", "-C", str(ctx.workspace), *argv]


def _run_git(
    ctx: ToolContext,
    argv: list[str],
    *,
    literal_paths: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one fixed git argv list inside the workspace.

    ``literal_paths`` adds ``--literal-pathspecs`` so a path such as
    ``notes[1].md`` or ``**`` is matched literally instead of as pathspec magic.
    """
    global_options = ["--literal-pathspecs"] if literal_paths else []
    ctx.policy.check_command(["git", *global_options, *argv]).require()
    command = _git_command(ctx, [*global_options, *argv])
    timeout = min(_GIT_TIMEOUT_S, ctx.limits.shell_timeout_s)
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(
            command,
            cwd=str(ctx.workspace),
            env=_minimal_env(),
            capture_output=True,
            text=True,
            errors="backslashreplace",
            timeout=timeout,
            shell=False,
            creationflags=creation_flags,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"git timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise ToolError(f"cannot run git: {exc}") from exc


def _text(completed: subprocess.CompletedProcess[str]) -> str:
    return (completed.stdout or "") + (completed.stderr or "")


def _finish(
    ctx: ToolContext,
    completed: subprocess.CompletedProcess[str],
    *,
    meta: dict[str, object],
) -> ToolResult:
    output = _text(completed)
    limit = ctx.limits.max_output_chars
    truncated = len(output) > limit
    if truncated:
        output = output[:limit]
    ok = completed.returncode == 0
    return ToolResult(
        ok=ok,
        output=output,
        error="" if ok else f"git exited with code {completed.returncode}",
        meta={**meta, "returncode": completed.returncode},
        truncated=truncated,
    )


def _validate_path(ctx: ToolContext, raw: str, access: Access = "write") -> str:
    """Return a normalised workspace-relative git path or raise ``ToolError``."""
    text = raw.strip()
    if not text:
        raise ToolError("empty path")
    if "\x00" in text:
        raise ToolError("path contains a NUL byte")
    candidate = Path(text)
    if candidate.is_absolute() or candidate.drive:
        raise ToolError(f"path must be workspace-relative: {text}")
    if candidate == Path("."):
        raise ToolError("path must point inside the workspace, not at its root")
    if any(part == ".." for part in candidate.parts):
        raise ToolError(f"path traversal is not allowed: {text}")
    if any(part.casefold() == ".git" for part in candidate.parts):
        raise ToolError(f"repository control paths are not allowed: {text}")
    decision = ctx.zones.check(candidate, access)
    if not decision.allowed:
        raise ZoneError(f"{decision.rule}: {decision.reason}")
    if decision.resolved is None:
        raise ZoneError("path could not be resolved")
    try:
        relative = decision.resolved.relative_to(ctx.zones.workspace)
    except ValueError as exc:
        raise ZoneError("path is outside the workspace") from exc
    return relative.as_posix()


def _validate_branch(name: str) -> str:
    """Validate a branch name before it can become part of an argv list."""
    text = name.strip()
    if not text:
        raise ToolError("branch name must not be empty")
    if len(text) > _MAX_BRANCH_CHARS:
        raise ToolError(f"branch name exceeds {_MAX_BRANCH_CHARS} characters")
    if _BRANCH_RE.fullmatch(text) is None:
        raise ToolError(f"invalid branch name: {text!r}")
    if (
        ".." in text
        or "//" in text
        or "@{" in text
        or text.endswith(("/", ".", ".lock"))
        or any(part.startswith(".") for part in text.split("/"))
    ):
        raise ToolError(f"invalid branch name: {text!r}")
    return text


def _validate_message(message: str) -> str:
    text = message.strip()
    if not text:
        raise ToolError("commit message must not be empty")
    if len(text) > _MAX_MESSAGE_CHARS:
        raise ToolError(f"commit message exceeds {_MAX_MESSAGE_CHARS} characters")
    if "\x00" in text:
        raise ToolError("commit message contains a NUL byte")
    return text


class _GitArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GitStatusArgs(_GitArgs):
    """``git.status`` takes no arguments."""


class GitDiffArgs(_GitArgs):
    path: str | None = Field(
        default=None,
        max_length=4096,
        description="Optional workspace-relative path for a focused diff.",
    )


class GitBranchArgs(_GitArgs):
    action: Literal["current", "create"] = "current"
    name: str | None = Field(
        default=None,
        max_length=_MAX_BRANCH_CHARS,
        description="New branch name; required when action=create.",
    )


class GitCommitArgs(_GitArgs):
    message: str = Field(
        min_length=1,
        max_length=_MAX_MESSAGE_CHARS,
        description="Commit message, one line is recommended.",
    )
    paths: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(
        min_length=1,
        max_length=_MAX_PATHS,
        description="Workspace-relative files or directories to stage.",
    )


class GitStatusTool:
    """Read porcelain status plus the current branch."""

    name = "git.status"
    description = "Show git status (porcelain v1) and the current branch."
    risk = "R0"
    capabilities = ("git.read",)
    args_model = GitStatusArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, GitStatusArgs)
        return Preview(
            "git.status",
            {"command": _git_command(ctx, ["status", "--porcelain=v1", "--branch"])},
            reversible=True,
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, GitStatusArgs)
        completed = _run_git(ctx, ["status", "--porcelain=v1", "--branch"])
        return _finish(ctx, completed, meta={"command": "git status"})


class GitDiffTool:
    """Show a stat summary and, optionally, the diff for one path."""

    name = "git.diff"
    description = "Show git diff --stat and optionally the diff for one path."
    risk = "R0"
    capabilities = ("git.read",)
    args_model = GitDiffArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, GitDiffArgs)
        details: dict[str, object] = {"stat": True}
        if args.path:
            details["path"] = _validate_path(ctx, args.path, "read")
        return Preview("git.diff", details, reversible=True)

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, GitDiffArgs)
        path = _validate_path(ctx, args.path, "read") if args.path else None
        stat = _run_git(ctx, ["diff", "--stat"])
        chunks = ["--- git diff --stat ---", _text(stat).rstrip()]
        returncode = stat.returncode
        if path is not None:
            detail = _run_git(ctx, ["diff", "--", path], literal_paths=True)
            chunks.extend([f"--- git diff -- {path} ---", _text(detail).rstrip()])
            if returncode == 0:
                returncode = detail.returncode
        output = "\n".join(chunks)
        limit = ctx.limits.max_output_chars
        truncated = len(output) > limit
        if truncated:
            output = output[:limit]
        return ToolResult(
            ok=returncode == 0,
            output=output,
            error="" if returncode == 0 else f"git exited with code {returncode}",
            meta={"path": path, "returncode": returncode},
            truncated=truncated,
        )


class GitBranchTool:
    """Show the current branch or create a new one."""

    name = "git.branch"
    description = "Show the current branch or create a new branch (R2)."
    risk = "R2"
    capabilities = ("git.branch",)
    args_model = GitBranchArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, GitBranchArgs)
        if args.action == "create":
            name = _validate_branch(args.name or "")
            return Preview(
                "git.branch.create",
                {"name": name, "command": _git_command(ctx, ["switch", "-c", name])},
                reversible=False,
                warnings=("creates and switches to a new branch",),
            )
        return Preview(
            "git.branch.current",
            {"command": _git_command(ctx, ["branch", "--show-current"])},
            reversible=True,
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, GitBranchArgs)
        if args.action == "current":
            completed = _run_git(ctx, ["branch", "--show-current"])
            return _finish(ctx, completed, meta={"command": "git branch --show-current"})
        name = _validate_branch(args.name or "")
        completed = _run_git(ctx, ["switch", "-c", name])
        return _finish(ctx, completed, meta={"branch": name, "created": completed.returncode == 0})


class GitCommitTool:
    """Stage explicit workspace paths and create one commit."""

    name = "git.commit"
    description = "Stage explicit paths and create a commit (R3)."
    risk = "R3"
    capabilities = ("git.commit",)
    args_model = GitCommitArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, GitCommitArgs)
        message = _validate_message(args.message)
        paths = [_validate_path(ctx, item, "write") for item in args.paths]
        return Preview(
            "git.commit",
            {"message": message, "paths": paths},
            reversible=False,
            warnings=("stages the listed paths and creates a commit",),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, GitCommitArgs)
        message = _validate_message(args.message)
        paths = [_validate_path(ctx, item, "write") for item in args.paths]
        add = _run_git(ctx, ["add", "--", *paths], literal_paths=True)
        if add.returncode != 0:
            return _finish(ctx, add, meta={"message": message, "paths": paths, "stage": "add"})
        commit = _run_git(ctx, ["commit", "-m", message])
        return _finish(
            ctx,
            commit,
            meta={
                "message": message,
                "paths": paths,
                "stage": "commit",
                "add_returncode": add.returncode,
            },
        )


__all__ = [
    "GitBranchArgs",
    "GitBranchTool",
    "GitCommitArgs",
    "GitCommitTool",
    "GitDiffArgs",
    "GitDiffTool",
    "GitStatusArgs",
    "GitStatusTool",
]
