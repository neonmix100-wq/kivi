"""Filesystem tools: read, list and write inside the workspace.

Every path goes through :class:`~kivi.safety.zones.ProtectedZones`, so secrets,
private directories and the safety core stay unreachable even when a write is
otherwise approved. Writes are atomic: a temporary file in the same directory is
replaced into place, so a crash cannot leave a half-written file.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from kivi.safety.zones import ZoneError
from kivi.tools.base import Preview, ToolContext, ToolError, ToolResult


class _FsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FsReadArgs(_FsArgs):
    path: str = Field(
        min_length=1,
        max_length=4096,
        description="File path, relative to the workspace.",
    )
    max_bytes: int = Field(default=65536, ge=1, le=1_048_576)


class FsListArgs(_FsArgs):
    path: str = Field(default=".", min_length=1, max_length=4096, description="Directory path.")
    limit: int = Field(default=200, ge=1, le=1000)


class FsWriteArgs(_FsArgs):
    path: str = Field(
        min_length=1,
        max_length=4096,
        description="File path, relative to the workspace.",
    )
    content: str = Field(description="UTF-8 text; parent directory must exist.")
    overwrite: bool = False


class FsMkdirArgs(_FsArgs):
    path: str = Field(
        min_length=1,
        max_length=4096,
        description="New directory path, relative to the workspace.",
    )


class FsRenameArgs(_FsArgs):
    source: str = Field(min_length=1, max_length=4096)
    destination: str = Field(min_length=1, max_length=4096)


def _decode_text(data: bytes, *, allow_partial: bool = False) -> str:
    """Decode UTF-8 without inventing broken replacement characters.

    ``allow_partial`` is set when the byte cap may have split a multi-byte
    character: up to three trailing bytes are then dropped before giving up.
    Invalid bytes are escaped, never turned into U+FFFD.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        if allow_partial:
            for cut in range(1, 4):
                try:
                    return data[:-cut].decode("utf-8")
                except UnicodeDecodeError:
                    continue
        return data.decode("utf-8", errors="backslashreplace")


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


class FsReadTool:
    name = "fs.read"
    description = "Read a UTF-8 text file from the workspace."
    risk = "R0"
    capabilities = ("fs.read",)
    args_model = FsReadArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, FsReadArgs)
        target = _resolve(ctx, args.path, "read")
        details: dict[str, object] = {"path": str(target), "max_bytes": args.max_bytes}
        warnings: list[str] = []
        if target.is_file():
            details["size"] = target.stat().st_size
        else:
            warnings.append("file does not exist")
        return Preview("read", details, reversible=True, warnings=tuple(warnings))

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, FsReadArgs)
        target = _resolve(ctx, args.path, "read")
        if not target.is_file():
            raise ToolError(f"not a file: {target}")
        limit = min(args.max_bytes, ctx.limits.max_read_bytes)
        with target.open("rb") as handle:
            data = handle.read(limit + 1)
        truncated = len(data) > limit
        text = _decode_text(data[:limit], allow_partial=truncated)
        if len(text) > ctx.limits.max_output_chars:
            text = text[: ctx.limits.max_output_chars]
            truncated = True
        return ToolResult(
            ok=True,
            output=text,
            meta={"path": str(target), "bytes": len(data[:limit])},
            truncated=truncated,
            trust="untrusted:file",
        )


class FsListTool:
    name = "fs.list"
    description = "List a workspace directory without following symlinks."
    risk = "R0"
    capabilities = ("fs.read",)
    args_model = FsListArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, FsListArgs)
        target = _resolve(ctx, args.path, "read")
        return Preview("list", {"path": str(target), "limit": args.limit}, reversible=True)

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, FsListArgs)
        target = _resolve(ctx, args.path, "read")
        if not target.is_dir():
            raise ToolError(f"not a directory: {target}")
        with os.scandir(target) as iterator:
            entries = sorted(iterator, key=lambda item: item.name.lower())
        lines: list[str] = []
        for entry in entries[: args.limit]:
            try:
                if entry.is_dir(follow_symlinks=False):
                    lines.append(f"d  {entry.name}")
                else:
                    size = entry.stat(follow_symlinks=False).st_size
                    lines.append(f"f  {entry.name}  {size} B")
            except OSError:
                lines.append(f"?  {entry.name}")
        output = "\n".join(lines)
        if len(entries) > args.limit:
            output += f"\n… {len(entries) - args.limit} more entries"
        return ToolResult(
            ok=True,
            output=output,
            meta={"path": str(target), "entries": len(entries)},
            trust="untrusted:file",
        )


class FsWriteTool:
    name = "fs.write"
    description = "Create or overwrite a UTF-8 text file inside the workspace."
    risk = "R1"
    capabilities = ("fs.write",)
    args_model = FsWriteArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, FsWriteArgs)
        target = _resolve(ctx, args.path, "write")
        parent = target.parent
        _resolve(ctx, str(parent), "write")
        size = len(args.content.encode("utf-8"))
        exists = target.exists()
        warnings: list[str] = []
        if target.is_dir():
            warnings.append("target is a directory")
        if exists and not args.overwrite:
            warnings.append("file exists; run would be refused without overwrite")
        return Preview(
            "overwrite" if exists else "create",
            {"path": str(target), "bytes": size, "exists": exists},
            reversible=not exists,
            warnings=tuple(warnings),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, FsWriteArgs)
        target = _resolve(ctx, args.path, "write")
        _resolve(ctx, str(target.parent), "write")
        if not target.parent.is_dir():
            raise ToolError(f"parent directory does not exist: {target.parent}")
        if target.is_dir():
            raise ToolError(f"target is a directory: {target}")
        exists = target.exists()
        if exists and not args.overwrite:
            raise ToolError(f"file exists: {target} (pass overwrite=true)")
        data = args.content.encode("utf-8")
        if len(data) > ctx.limits.max_write_bytes:
            raise ToolError(f"content exceeds {ctx.limits.max_write_bytes} bytes")
        temporary = target.with_name(f".{target.name}.kivi-{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)
        return ToolResult(
            ok=True,
            output=f"wrote {len(data)} bytes to {target}",
            meta={"path": str(target), "bytes": len(data), "created": not exists},
        )


class FsMkdirTool:
    """Create one directory inside the workspace; never recursive."""

    name = "fs.mkdir"
    description = "Create one new directory inside the workspace."
    risk = "R1"
    capabilities = ("fs.write",)
    args_model = FsMkdirArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, FsMkdirArgs)
        target = _resolve(ctx, args.path, "write")
        _resolve(ctx, str(target.parent), "write")
        return Preview(
            "create-directory",
            {"path": str(target), "exists": target.exists()},
            reversible=not target.exists(),
            warnings=("directory already exists",) if target.exists() else (),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, FsMkdirArgs)
        target = _resolve(ctx, args.path, "write")
        _resolve(ctx, str(target.parent), "write")
        if target.exists() or target.is_symlink():
            raise ToolError(f"path already exists: {target}")
        if not target.parent.is_dir():
            raise ToolError(f"parent directory does not exist: {target.parent}")
        try:
            target.mkdir()
        except OSError as exc:
            raise ToolError(f"cannot create directory: {exc}") from exc
        return ToolResult(
            ok=True,
            output=f"created directory {target}",
            meta={"path": str(target), "created": True},
        )


class FsRenameTool:
    """Rename one workspace entry without overwriting an existing path."""

    name = "fs.rename"
    description = "Rename a file/directory inside the workspace; no overwrite."
    risk = "R1"
    capabilities = ("fs.write",)
    args_model = FsRenameArgs

    def _check(self, args: FsRenameArgs, ctx: ToolContext) -> tuple[Path, Path]:
        source = _resolve(ctx, args.source, "read")
        destination = _resolve(ctx, args.destination, "write")
        _resolve(ctx, str(destination.parent), "write")
        if not source.exists():
            raise ToolError(f"source does not exist: {source}")
        if source.is_symlink() or destination.is_symlink():
            raise ToolError("symlink rename is not supported")
        if destination.exists():
            raise ToolError(f"destination already exists: {destination}")
        if not destination.parent.is_dir():
            raise ToolError(f"destination parent does not exist: {destination.parent}")
        return source, destination

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, FsRenameArgs)
        source, destination = self._check(args, ctx)
        return Preview(
            "rename",
            {"source": str(source), "destination": str(destination)},
            reversible=True,
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, FsRenameArgs)
        source, destination = self._check(args, ctx)
        try:
            source.rename(destination)
        except OSError as exc:
            raise ToolError(f"cannot rename: {exc}") from exc
        return ToolResult(
            ok=True,
            output=f"renamed {source.name} -> {destination.name}",
            meta={"source": str(source), "destination": str(destination)},
        )


__all__ = [
    "FsListArgs",
    "FsListTool",
    "FsMkdirArgs",
    "FsMkdirTool",
    "FsReadArgs",
    "FsReadTool",
    "FsRenameArgs",
    "FsRenameTool",
    "FsWriteArgs",
    "FsWriteTool",
]
