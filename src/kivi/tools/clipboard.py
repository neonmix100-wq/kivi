"""Clipboard tools: explicit, confirmed access to the Windows clipboard.

The clipboard often contains passwords, tokens and personal text, so both
directions are R2 and need confirmation. Reads pass through the sanitizer and
redact secret-looking tokens before the model sees them. The native Windows
backend is isolated behind :class:`ClipboardBackend`, so tests and future
platforms can substitute a fake.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from kivi.safety.sanitize import sanitize_text
from kivi.tools.base import Preview, ToolContext, ToolError, ToolResult

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


class ClipboardUnavailable(RuntimeError):
    """Raised when the platform clipboard cannot be opened."""


class ClipboardBackend(Protocol):
    def read(self) -> str: ...

    def write(self, text: str) -> None: ...


class WindowsClipboard:
    """Minimal ``CF_UNICODETEXT`` reader/writer via user32/kernel32."""

    def _handles(self) -> tuple[Any, Any, Any]:
        if sys.platform != "win32":
            raise ClipboardUnavailable("the clipboard backend is Windows-only")
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL
        return ctypes, wintypes, (user32, kernel32)

    def read(self) -> str:
        ctypes_module, _wintypes, handles = self._handles()
        user32, kernel32 = handles
        if not user32.OpenClipboard(None):
            raise ClipboardUnavailable("cannot open the clipboard")
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                raise ClipboardUnavailable("cannot lock clipboard data")
            try:
                return ctypes_module.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def write(self, text: str) -> None:
        ctypes_module, _wintypes, handles = self._handles()
        user32, kernel32 = handles
        payload = text.encode("utf-16-le") + b"\x00\x00"
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
        if not handle:
            raise ClipboardUnavailable("cannot allocate clipboard memory")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            raise ClipboardUnavailable("cannot lock clipboard memory")
        try:
            ctypes_module.memmove(pointer, payload, len(payload))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.OpenClipboard(None):
            kernel32.GlobalFree(handle)
            raise ClipboardUnavailable("cannot open the clipboard")
        try:
            if not user32.EmptyClipboard():
                raise ClipboardUnavailable("cannot clear the clipboard")
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                raise ClipboardUnavailable("cannot set clipboard data")
            handle = None  # ownership passed to the system
        finally:
            user32.CloseClipboard()
            if handle:
                kernel32.GlobalFree(handle)


class ClipboardReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_chars: int = Field(default=20_000, ge=1, le=65_536)


class ClipboardWriteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(max_length=262_144, description="Text to put on the clipboard.")


class ClipboardReadTool:
    name = "clipboard.read"
    description = "Read text from the clipboard; requires confirmation (R2)."
    risk = "R2"
    capabilities = ("clipboard.read",)
    args_model = ClipboardReadArgs

    def __init__(self, backend: ClipboardBackend | None = None) -> None:
        self._backend = backend or WindowsClipboard()

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, ClipboardReadArgs)
        return Preview(
            "clipboard-read",
            {"max_chars": args.max_chars},
            reversible=False,
            warnings=("clipboard may contain secrets",),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, ClipboardReadArgs)
        try:
            raw = self._backend.read()
        except ClipboardUnavailable as exc:
            raise ToolError(str(exc)) from exc
        sanitized = sanitize_text(
            raw,
            html=False,
            max_chars=min(args.max_chars, ctx.limits.max_output_chars),
        )
        return ToolResult(
            ok=True,
            output=sanitized.text,
            meta={"removed": sanitized.removed, "suspicious": list(sanitized.suspicious)},
            truncated=sanitized.truncated,
            trust="untrusted:tool",
        )


class ClipboardWriteTool:
    name = "clipboard.write"
    description = "Replace the clipboard text; requires confirmation (R2)."
    risk = "R2"
    capabilities = ("clipboard.write",)
    args_model = ClipboardWriteArgs

    def __init__(self, backend: ClipboardBackend | None = None) -> None:
        self._backend = backend or WindowsClipboard()

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, ClipboardWriteArgs)
        size = len(args.content.encode("utf-8"))
        return Preview(
            "clipboard-write",
            {"bytes": size},
            reversible=False,
            warnings=("this replaces the current clipboard",),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, ClipboardWriteArgs)
        try:
            self._backend.write(args.content)
        except ClipboardUnavailable as exc:
            raise ToolError(str(exc)) from exc
        return ToolResult(
            ok=True,
            output=f"wrote {len(args.content)} characters to the clipboard",
            meta={"chars": len(args.content)},
            trust="untrusted:tool",
        )


__all__ = [
    "ClipboardBackend",
    "ClipboardReadArgs",
    "ClipboardReadTool",
    "ClipboardUnavailable",
    "ClipboardWriteArgs",
    "ClipboardWriteTool",
    "WindowsClipboard",
]
