"""Shell tool: one approved process, no shell expansion, minimal environment.

``shell.exec`` is R2, so the executor requires explicit user confirmation. The
command is checked by the Policy Engine before it runs, the working directory
must stay inside the workspace, the environment is reduced to the variables a
process needs to start, and output is capped. There is no stdin and no shell
interpolation: argv is passed to the OS as a list.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kivi.safety.zones import ZoneError
from kivi.tools.base import Preview, ToolContext, ToolError, ToolResult

_ENV_KEYS = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "LOCALAPPDATA",
)


def _minimal_env() -> dict[str, str]:
    return {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}


def _cwd(ctx: ToolContext, raw: str) -> Path:
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = ctx.workspace / candidate
    decision = ctx.zones.check(candidate, "exec")
    if not decision.allowed:
        raise ZoneError(f"{decision.rule}: {decision.reason}")
    if decision.resolved is None:
        raise ZoneError("working directory could not be resolved")
    return decision.resolved


class ShellExecArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(
        min_length=1,
        max_length=64,
        description="Command and arguments; no shell string.",
    )
    cwd: str = Field(
        default=".",
        max_length=4096,
        description="Working directory inside the workspace.",
    )
    timeout_s: float = Field(default=30.0, ge=1.0, le=300.0)


class ShellExecTool:
    name = "shell.exec"
    description = "Run one command with an argv list; requires user confirmation (R2)."
    risk = "R2"
    capabilities = ("shell.exec",)
    args_model = ShellExecArgs

    def _command(self, args: ShellExecArgs) -> str:
        if sys.platform == "win32":
            return subprocess.list2cmdline(args.argv)
        return " ".join(args.argv)

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, ShellExecArgs)
        cwd = _cwd(ctx, args.cwd)
        decision = ctx.policy.check_command(args.argv)
        details: dict[str, object] = {
            "argv": list(args.argv),
            "cwd": str(cwd),
            "timeout_s": args.timeout_s,
        }
        warnings: list[str] = []
        if not decision.allowed:
            warnings.append(f"policy would deny: {decision.reason}")
        return Preview("exec", details, reversible=False, warnings=tuple(warnings))

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, ShellExecArgs)
        cwd = _cwd(ctx, args.cwd)
        if not cwd.is_dir():
            raise ToolError(f"working directory does not exist: {cwd}")
        ctx.policy.check_command(args.argv).require()
        timeout = min(args.timeout_s, ctx.limits.shell_timeout_s)
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                args.argv,
                cwd=str(cwd),
                env=_minimal_env(),
                capture_output=True,
                text=True,
                errors="backslashreplace",
                timeout=timeout,
                shell=False,
                creationflags=creation_flags,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, error=f"timed out after {timeout:.0f}s")
        except OSError as exc:
            raise ToolError(f"cannot start process: {exc}") from exc

        output = (completed.stdout or "") + (completed.stderr or "")
        truncated = len(output) > ctx.limits.max_output_chars
        if truncated:
            output = output[: ctx.limits.max_output_chars]
        return ToolResult(
            ok=completed.returncode == 0,
            output=output,
            error="" if completed.returncode == 0 else f"exit code {completed.returncode}",
            meta={"returncode": completed.returncode, "command": self._command(args)},
            truncated=truncated,
        )


__all__ = ["ShellExecArgs", "ShellExecTool", "_minimal_env"]
