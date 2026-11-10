"""Process list tool: names and memory only, never command lines.

Command lines often contain tokens, paths and customer data, so this tool
deliberately exposes only pid, name, RSS and start time. It is a read-only R0
action and still goes through the executor and audit.
"""

from __future__ import annotations

from typing import Any

import psutil
from pydantic import BaseModel, ConfigDict, Field

from kivi.tools.base import Preview, ToolContext, ToolError, ToolResult


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


__all__ = ["ProcListArgs", "ProcListTool"]
