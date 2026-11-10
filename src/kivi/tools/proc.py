"""Process list tool: names and memory only, never command lines.

Command lines often contain tokens, paths and customer data, so this tool
deliberately exposes only pid, name, RSS and start time. It is a read-only R0
action and still goes through the executor and audit.
"""

from __future__ import annotations

import os
from typing import Any

import psutil
from pydantic import BaseModel, ConfigDict, Field

from kivi.tools.base import Preview, ToolContext, ToolError, ToolResult

# Closing these would destabilise Windows or the user's session.
PROTECTED_PROCESSES: frozenset[str] = frozenset(
    {
        "system",
        "system idle process",
        "smss.exe",
        "csrss.exe",
        "wininit.exe",
        "winlogon.exe",
        "services.exe",
        "lsass.exe",
        "lsaiso.exe",
        "svchost.exe",
        "explorer.exe",
        "dwm.exe",
        "fontdrvhost.exe",
        "sihost.exe",
        "ctfmon.exe",
        "audiodg.exe",
    }
)


class ProcListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=50, ge=1, le=500)
    name_filter: str = Field(default="", max_length=128)


class ProcListTool:
    name = "proc.list"
    description = "List processes with pid, name and RSS; no command lines."
    risk = "R0"
    capabilities = ("proc.list",)
    args_model = ProcListArgs

    def __init__(self, process_iter: Any = None) -> None:
        self._process_iter = process_iter or psutil.process_iter

    def _collect(self, args: ProcListArgs) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        needle = args.name_filter.lower()
        for process in self._process_iter(["pid", "name", "memory_info", "create_time"]):
            try:
                info = process.info
                name = str(info.get("name") or "")
                if needle and needle not in name.lower():
                    continue
                memory = info.get("memory_info")
                rss = int(getattr(memory, "rss", 0) or 0)
                rows.append(
                    {
                        "pid": int(info.get("pid") or 0),
                        "name": name,
                        "rss_mb": round(rss / (1024 * 1024), 1),
                        "created": float(info.get("create_time") or 0.0),
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError, ValueError):
                continue
        rows.sort(key=lambda row: (-row["rss_mb"], row["name"].lower(), row["pid"]))
        return rows[: args.limit]

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, ProcListArgs)
        return Preview(
            "process-list",
            {"limit": args.limit, "name_filter": args.name_filter or None},
            reversible=True,
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, ProcListArgs)
        try:
            rows = self._collect(args)
        except OSError as exc:
            raise ToolError(f"cannot enumerate processes: {exc}") from exc
        lines = [f"{row['pid']:>7}  {row['rss_mb']:>8.1f} MB  {row['name']}" for row in rows]
        return ToolResult(
            ok=True,
            output="\n".join(lines),
            meta={"count": len(rows), "filtered": bool(args.name_filter)},
            trust="untrusted:tool",
        )


class ProcKillArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pid: int = Field(gt=4, description="Process id; 0..4 are system processes.")
    name: str = Field(
        min_length=1,
        max_length=260,
        description="Expected process name; must match the live process.",
    )
    force: bool = False


class ProcKillTool:
    name = "proc.kill"
    description = "Terminate one process after checking its name and system role (R3)."
    risk = "R3"
    capabilities = ("proc.kill",)
    args_model = ProcKillArgs

    def __init__(self, process_factory: Any = None) -> None:
        self._process_factory = process_factory or psutil.Process

    def _target(self, args: ProcKillArgs) -> Any:
        if args.pid <= 4:
            raise ToolError("system pids are not killable")
        if args.pid == os.getpid():
            raise ToolError("refusing to kill the Kivi process")
        expected = args.name.strip().casefold()
        if expected in PROTECTED_PROCESSES or expected.startswith("kivi"):
            raise ToolError(f"protected process: {args.name}")
        try:
            process = self._process_factory(args.pid)
            actual = process.name()
        except psutil.NoSuchProcess as exc:
            raise ToolError(f"process not found: {args.pid}") from exc
        except psutil.AccessDenied as exc:
            raise ToolError(f"access denied: {args.pid}") from exc
        if actual.casefold() != expected:
            raise ToolError(f"name mismatch: pid {args.pid} is {actual}, not {args.name}")
        return process

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, ProcKillArgs)
        self._target(args)
        return Preview(
            "process-kill",
            {"pid": args.pid, "name": args.name, "force": args.force},
            reversible=False,
            warnings=("the process will be terminated",),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, ProcKillArgs)
        process = self._target(args)
        try:
            if args.force:
                process.kill()
            else:
                process.terminate()
            process.wait(timeout=5)
        except psutil.TimeoutExpired as exc:
            raise ToolError(
                f"process {args.pid} did not exit in time; retry with force=true"
            ) from exc
        except psutil.NoSuchProcess:
            return ToolResult(
                ok=True,
                output=f"process {args.pid} already exited",
                meta={"pid": args.pid, "already_exited": True},
                trust="untrusted:tool",
            )
        except psutil.AccessDenied as exc:
            raise ToolError(f"access denied: {exc}") from exc
        return ToolResult(
            ok=True,
            output=f"terminated {args.name} (pid {args.pid})",
            meta={"pid": args.pid, "force": args.force},
            trust="untrusted:tool",
        )


__all__ = [
    "PROTECTED_PROCESSES",
    "ProcKillArgs",
    "ProcKillTool",
    "ProcListArgs",
    "ProcListTool",
]
