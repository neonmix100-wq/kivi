"""Windows desktop control: top-level windows and application processes.

M3 Operator PC. The module has three layers:

* :class:`WindowInfo` — an immutable snapshot of one top-level window;
* :class:`WindowBackend` — a pure protocol, so tests never touch a desktop;
* :class:`Win32Backend` — ctypes ``EnumWindows``/``ShowWindow``/``PostMessageW``
  plus ``subprocess.Popen`` for launching (never a shell).

The tools above them validate typed arguments, resolve a case-insensitive title
substring to exactly one handle, refuse protected processes and rely on the
executor for R2 confirmation and audit. Window titles are OS-provided text:
they are labelled ``untrusted:os`` and are never treated as instructions.
``pywinauto`` is optional and never imported on the default path.
"""

from __future__ import annotations

import importlib.util
import ntpath
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import psutil
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kivi.tools.base import Preview, ToolContext, ToolError, ToolResult
from kivi.tools.proc import PROTECTED_PROCESSES
from kivi.tools.shell import _minimal_env

# Window titles come from arbitrary applications; they are data, not orders.
TRUST_OS = "untrusted:os"

SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9
WM_CLOSE = 0x0010
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Console and terminal hosts are intentionally invisible to window control.
_CONSOLE_CLASSES: frozenset[str] = frozenset(
    {
        "consolewindowclass",
        "pseudoconsolewindow",
        "cascadia_hosting_window_class",
        "virtualconsoleclass",
    }
)

# Only real executables; scripts, installers and shortcuts need a different path.
ALLOWED_SUFFIXES: frozenset[str] = frozenset({".exe"})

# Interpreters would turn app.launch into an unrestricted shell.
DENY_LAUNCH_NAMES: frozenset[str] = frozenset(
    {
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "wscript.exe",
        "cscript.exe",
        "mshta.exe",
        "rundll32.exe",
        "regsvr32.exe",
        "certutil.exe",
        "msiexec.exe",
        "installutil.exe",
        "wsl.exe",
        "bash.exe",
    }
)

# Closing these would destabilise the session, the shell or Kivi itself.
CLOSE_PROTECTED_PROCESSES: frozenset[str] = PROTECTED_PROCESSES | frozenset(
    {
        "python.exe",
        "pythonw.exe",
        "kivi.exe",
        "registry",
        "memcompression",
        "lsm.exe",
        "searchhost.exe",
        "startmenuexperiencehost.exe",
        "shellexperiencehost.exe",
        "textinputhost.exe",
    }
)

# Interpreter hosts are protected for termination as well.
TERMINATE_PROTECTED_PROCESSES: frozenset[str] = CLOSE_PROTECTED_PROCESSES | frozenset(
    {"cmd.exe", "powershell.exe", "pwsh.exe"}
)

_SHELL_CHARS: frozenset[str] = frozenset("&|;<>^`\n\r\x00")
_URL_PREFIXES = ("http:", "https:", "ftp:", "file:", "shell:", "mailto:")

# Bidi overrides and zero-width characters can spoof a title in the operator UI.
_INVISIBLE_CHARS: frozenset[str] = frozenset(
    chr(code) for code in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))
) | frozenset({"\u200b", "\u200c", "\u200d", "\ufeff"})


class WindowError(RuntimeError):
    """A window or application operation failed at the backend boundary."""


class WindowsUnavailable(WindowError):
    """The backend needs Windows or an optional dependency that is missing."""


Bounds = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class WindowInfo:
    """One top-level window; ``bounds`` is ``(left, top, right, bottom)``."""

    handle: int
    title: str
    process: str
    pid: int | None = None
    visible: bool = True
    minimized: bool = False
    bounds: Bounds = (0, 0, 0, 0)


class WindowBackend(Protocol):
    """Pure window backend: no policy, no approval, no audit.

    ``list`` returns visible windows by default; ``visible_only=False`` also
    returns hidden windows so the caller can show or filter them.
    """

    def list(self, *, visible_only: bool = False) -> list[WindowInfo]: ...

    def focus(self, handle: int) -> None: ...

    def minimize(self, handle: int) -> None: ...

    def maximize(self, handle: int) -> None: ...

    def close(self, handle: int) -> None: ...

    def launch(self, executable: str, args: Sequence[str], *, cwd: str | None = None) -> int: ...

    def terminate(self, pid: int) -> None: ...


class _Win32Api:
    """Configured ``user32``/``kernel32`` entry points, built once on Windows."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise WindowsUnavailable("window control requires Windows")
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        user32 = self.user32
        user32.EnumWindows.argtypes = [self.enum_proc, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.IsZoomed.argtypes = [wintypes.HWND]
        user32.IsZoomed.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.PostMessageW.restype = wintypes.BOOL

        kernel32 = self.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL


@lru_cache(maxsize=1)
def _win32_api() -> _Win32Api:
    return _Win32Api()


class Win32Backend:
    """Win32 implementation: ctypes user32/kernel32 and ``Popen`` only.

    The class is cheap to construct on any platform; the DLLs are loaded on the
    first call, so importing this module never fails on a non-Windows host.
    """

    def __init__(self) -> None:
        self._api_instance: _Win32Api | None = None
        self._name_cache: dict[int, str] = {}

    @property
    def _api(self) -> _Win32Api:
        if self._api_instance is None:
            self._api_instance = _win32_api()
        return self._api_instance

    # -- enumeration -----------------------------------------------------
    def list(self, *, visible_only: bool = False) -> list[WindowInfo]:
        """Enumerate top-level windows, skipping own, console and untitled ones."""
        api = self._api
        own_pid = os.getpid()
        found: list[WindowInfo] = []

        def _callback(hwnd: int, _lparam: int) -> bool:
            try:
                window = self._inspect(api, hwnd, own_pid)
            except (OSError, ValueError):
                return True
            if window is None:
                return True
            if window.visible or not visible_only:
                found.append(window)
            return True

        callback = api.enum_proc(_callback)
        if not api.user32.EnumWindows(callback, 0):
            raise WindowError(f"EnumWindows failed (error {api.ctypes.get_last_error()})")
        return found

    def _inspect(self, api: _Win32Api, hwnd: int, own_pid: int) -> WindowInfo | None:
        if not hwnd:
            return None
        if self._class_name(api, hwnd).casefold() in _CONSOLE_CLASSES:
            return None
        title = self._title(api, hwnd)
        if not title:
            return None
        pid = self._pid(api, hwnd)
        if pid <= 0 or pid == own_pid:
            return None
        rect = api.wintypes.RECT()
        if api.user32.GetWindowRect(hwnd, api.ctypes.byref(rect)):
            bounds: Bounds = (rect.left, rect.top, rect.right, rect.bottom)
        else:
            bounds = (0, 0, 0, 0)
        return WindowInfo(
            handle=int(hwnd),
            title=title,
            process=self._process_name(api, pid),
            pid=pid,
            visible=bool(api.user32.IsWindowVisible(hwnd)),
            minimized=bool(api.user32.IsIconic(hwnd)),
            bounds=bounds,
        )

    @staticmethod
    def _title(api: _Win32Api, hwnd: int) -> str:
        length = api.user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = api.ctypes.create_unicode_buffer(length + 1)
        if api.user32.GetWindowTextW(hwnd, buffer, length + 1) <= 0:
            return ""
        return buffer.value

    @staticmethod
    def _class_name(api: _Win32Api, hwnd: int) -> str:
        buffer = api.ctypes.create_unicode_buffer(256)
        if api.user32.GetClassNameW(hwnd, buffer, len(buffer)) <= 0:
            return ""
        return buffer.value

    @staticmethod
    def _pid(api: _Win32Api, hwnd: int) -> int:
        pid = api.wintypes.DWORD(0)
        api.user32.GetWindowThreadProcessId(hwnd, api.ctypes.byref(pid))
        return int(pid.value)

    def _process_name(self, api: _Win32Api, pid: int) -> str:
        cached = self._name_cache.get(pid)
        if cached is not None:
            return cached
        wintypes = api.wintypes
        handle = api.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        name = ""
        if handle:
            try:
                size = wintypes.DWORD(32768)
                buffer = api.ctypes.create_unicode_buffer(size.value)
                if api.kernel32.QueryFullProcessImageNameW(
                    handle, 0, buffer, api.ctypes.byref(size)
                ):
                    name = ntpath.basename(buffer.value)
            finally:
                api.kernel32.CloseHandle(handle)
        self._name_cache[pid] = name
        return name

    # -- control ---------------------------------------------------------
    def _hwnd(self, handle: int) -> int:
        api = self._api
        if handle <= 0 or not api.user32.IsWindow(handle):
            raise WindowError(f"window handle is not valid: {handle}")
        return handle

    def focus(self, handle: int) -> None:
        """Restore if minimised and bring the window to the foreground."""
        api = self._api
        hwnd = self._hwnd(handle)
        user32 = api.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        if not user32.SetForegroundWindow(hwnd):
            # Windows may refuse the first call while another app owns focus.
            user32.ShowWindow(hwnd, SW_RESTORE)
            if not user32.SetForegroundWindow(hwnd):
                raise WindowError(
                    f"Windows refused to focus window {handle}; "
                    "click the window once or retry"
                )

    def minimize(self, handle: int) -> None:
        api = self._api
        hwnd = self._hwnd(handle)
        api.user32.ShowWindow(hwnd, SW_MINIMIZE)
        if not api.user32.IsIconic(hwnd):
            raise WindowError(f"window {handle} did not minimize")

    def maximize(self, handle: int) -> None:
        api = self._api
        hwnd = self._hwnd(handle)
        api.user32.ShowWindow(hwnd, SW_MAXIMIZE)
        if not api.user32.IsZoomed(hwnd):
            raise WindowError(f"window {handle} did not maximize")

    def close(self, handle: int) -> None:
        """Post ``WM_CLOSE``; the application decides whether to exit."""
        api = self._api
        hwnd = self._hwnd(handle)
        if not api.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
            raise WindowError(f"cannot post WM_CLOSE to window {handle}")

    def launch(self, executable: str, args: Sequence[str], *, cwd: str | None = None) -> int:
        """Start ``executable`` with an argv list, no shell, and return its pid."""
        argv = [executable, *args]
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=_minimal_env(),
                shell=False,
                close_fds=True,
            )
        except OSError as exc:
            raise WindowError(f"cannot launch {executable}: {exc}") from exc
        return int(process.pid)

    def terminate(self, pid: int) -> None:
        """Force-terminate ``pid``; the tools verify the target before calling."""
        api = self._api
        handle = api.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not handle:
            error = api.ctypes.get_last_error()
            raise WindowError(f"cannot open process {pid} for termination (error {error})")
        try:
            if not api.kernel32.TerminateProcess(handle, 1):
                error = api.ctypes.get_last_error()
                raise WindowError(f"cannot terminate process {pid} (error {error})")
        finally:
            api.kernel32.CloseHandle(handle)


def _load_pywinauto() -> Any:
    try:
        import pywinauto
    except ImportError as exc:  # pragma: no cover - depends on the environment
        message = "pywinauto is not installed; the Win32 backend is primary"
        raise WindowsUnavailable(message) from exc
    return pywinauto


def pywinauto_available() -> bool:
    """True when the optional UIA backend can be imported."""
    return importlib.util.find_spec("pywinauto") is not None


class PywinautoBackend:
    """Optional UIA fallback for focus/close; everything else stays Win32.

    The primary Win32 backend is always tried first. Only when it fails (for
    example an elevated window that ignores ``SetForegroundWindow``) does the
    wrapper attempt the pywinauto UIA wrapper. The class is never instantiated
    by default, so a missing pywinauto cannot break the regular tools.
    """

    def __init__(
        self,
        primary: WindowBackend | None = None,
        *,
        uia_factory: Callable[[int], Any] | None = None,
    ) -> None:
        self._primary: WindowBackend = primary or Win32Backend()
        self._uia_factory = uia_factory

    def _window(self, handle: int) -> Any:
        if self._uia_factory is not None:
            return self._uia_factory(handle)
        return _load_pywinauto().Desktop(backend="uia").window(handle=handle)

    def list(self, *, visible_only: bool = False) -> list[WindowInfo]:
        return self._primary.list(visible_only=visible_only)

    def focus(self, handle: int) -> None:
        try:
            self._primary.focus(handle)
        except (WindowError, OSError) as exc:
            try:
                self._window(handle).set_focus()
            except Exception as uia_exc:  # noqa: BLE001 - report both failures
                raise WindowError(f"focus failed: {exc}; UIA fallback: {uia_exc}") from uia_exc

    def minimize(self, handle: int) -> None:
        self._primary.minimize(handle)

    def maximize(self, handle: int) -> None:
        self._primary.maximize(handle)

    def close(self, handle: int) -> None:
        try:
            self._primary.close(handle)
        except (WindowError, OSError) as exc:
            try:
                self._window(handle).close()
            except Exception as uia_exc:  # noqa: BLE001 - report both failures
                raise WindowError(f"close failed: {exc}; UIA fallback: {uia_exc}") from uia_exc

    def launch(self, executable: str, args: Sequence[str], *, cwd: str | None = None) -> int:
        return self._primary.launch(executable, args, cwd=cwd)

    def terminate(self, pid: int) -> None:
        self._primary.terminate(pid)


# -- argument models -----------------------------------------------------
class WindowListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visible_only: bool = True
    limit: int = Field(default=50, ge=1, le=200)


class WindowTargetArgs(BaseModel):
    """Exactly one selector: a handle from ``window.list`` or a title substring."""

    model_config = ConfigDict(extra="forbid")

    handle: int | None = Field(default=None, ge=1, description="Window handle from window.list.")
    title: str | None = Field(
        default=None,
        max_length=256,
        description="Case-insensitive substring of the window title.",
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> WindowTargetArgs:
        if (self.handle is None) == (self.title is None):
            raise ValueError("provide exactly one of handle or title")
        if self.title is not None and not self.title.strip():
            raise ValueError("title must not be empty")
        return self


class AppLaunchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        min_length=1,
        max_length=4096,
        description="Path to an .exe inside the workspace.",
    )
    args: list[str] = Field(
        default_factory=list,
        max_length=16,
        description="Arguments passed as an argv list; no shell string.",
    )
    cwd: str = Field(
        default="",
        max_length=4096,
        description="Working directory inside the workspace; empty means the workspace.",
    )

    @field_validator("args")
    @classmethod
    def _validate_args(cls, value: list[str]) -> list[str]:
        for item in value:
            if len(item) > 1024:
                raise ValueError("argument is too long")
            if _SHELL_CHARS.intersection(item):
                raise ValueError("shell metacharacters are not allowed in arguments")
        return value


class AppTerminateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pid: int = Field(gt=0, description="Process id to terminate.")


# -- helpers -------------------------------------------------------------
def _clean_title(title: str, limit: int = 80) -> str:
    """Flatten a window title to one safe display line."""
    visible = "".join(
        " " if char < " " or char == "\x7f" else char
        for char in title
        if char not in _INVISIBLE_CHARS
    )
    cleaned = " ".join(visible.split())
    if len(cleaned) > limit:
        return cleaned[: limit - 1] + "…"
    return cleaned


def _title_matches(title: str, needle: str) -> bool:
    return needle.strip().casefold() in title.casefold()


def _resolve_window(backend: WindowBackend, args: WindowTargetArgs) -> WindowInfo:
    """Resolve exactly one window; ambiguity is an error, never a guess."""
    if args.handle is not None:
        for window in backend.list(visible_only=False):
            if window.handle == args.handle:
                return window
        raise ToolError(f"window not found: handle {args.handle}")

    needle = (args.title or "").strip()
    matches = [
        window
        for window in backend.list(visible_only=True)
        if window.title and _title_matches(window.title, needle)
    ]
    if not matches:
        raise ToolError(f"no window matches title {needle!r}")
    if len(matches) > 1:
        listing = ", ".join(
            f"{window.handle} ({_clean_title(window.title, 40)!r})" for window in matches[:8]
        )
        suffix = " ..." if len(matches) > 8 else ""
        raise ToolError(
            f"ambiguous title {needle!r}: {len(matches)} windows match; "
            f"use a handle: {listing}{suffix}"
        )
    return matches[0]


def _ensure_closable(window: WindowInfo) -> None:
    """Fail closed for system, shell and Kivi-owned windows."""
    if window.pid is None:
        raise ToolError(f"window {window.handle} has no owner process; refusing to close it")
    if window.pid <= 4:
        raise ToolError(f"window {window.handle} belongs to a system process")
    if window.pid == os.getpid():
        raise ToolError("refusing to close a Kivi window")
    name = window.process.casefold()
    if not name:
        raise ToolError(f"owner process of window {window.handle} is unknown; refusing to close")
    if name in CLOSE_PROTECTED_PROCESSES or name.startswith("kivi"):
        raise ToolError(f"protected process: {window.process}")


def _looks_like_url(raw: str) -> bool:
    lowered = raw.casefold()
    return "://" in lowered or lowered.startswith(_URL_PREFIXES)


def _ensure_no_shell_path(raw: str) -> None:
    if _SHELL_CHARS.intersection(raw) or '"' in raw or "'" in raw:
        raise ToolError("shell metacharacters and quotes are not allowed in the path")


class _WindowTool:
    """Shared plumbing for handle/title window actions."""

    def __init__(self, backend: WindowBackend | None = None) -> None:
        self._backend: WindowBackend = backend if backend is not None else Win32Backend()

    def _resolve(self, args: WindowTargetArgs) -> WindowInfo:
        try:
            return _resolve_window(self._backend, args)
        except (WindowError, OSError) as exc:
            raise ToolError(str(exc)) from exc

    def _act(self, action: str, handle: int) -> None:
        try:
            getattr(self._backend, action)(handle)
        except WindowError as exc:
            raise ToolError(str(exc)) from exc


class WindowListTool:
    name = "window.list"
    description = "List visible top-level windows with handle, pid, process and title."
    risk = "R0"
    capabilities = ("window.read",)
    args_model = WindowListArgs

    def __init__(self, backend: WindowBackend | None = None) -> None:
        self._backend: WindowBackend = backend if backend is not None else Win32Backend()

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, WindowListArgs)
        return Preview(
            "window-list",
            {"visible_only": args.visible_only, "limit": args.limit},
            reversible=True,
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, WindowListArgs)
        try:
            windows = self._backend.list(visible_only=args.visible_only)
        except (WindowError, OSError) as exc:
            raise ToolError(f"cannot list windows: {exc}") from exc
        rows = windows[: args.limit]
        lines = [f"{'handle':>10}  {'pid':>7}  {'state':<9}  {'process':<24}  title"]
        for window in rows:
            state = "minimized" if window.minimized else ("visible" if window.visible else "hidden")
            pid = "-" if window.pid is None else str(window.pid)
            process = window.process or "-"
            lines.append(
                f"{window.handle:>10}  {pid:>7}  {state:<9}  "
                f"{process[:24]:<24}  {_clean_title(window.title)}"
            )
        output = "\n".join(lines)
        output_limit = ctx.limits.max_output_chars
        truncated = len(output) > output_limit
        if truncated:
            output = output[:output_limit] + "\n... (truncated)"
        return ToolResult(
            ok=True,
            output=output,
            meta={
                "count": len(rows),
                "total": len(windows),
                "visible_only": args.visible_only,
            },
            truncated=truncated or len(windows) > len(rows),
            trust=TRUST_OS,
        )


class WindowFocusTool(_WindowTool):
    name = "window.focus"
    description = "Focus one window by handle or a unique title substring (R1)."
    risk = "R1"
    capabilities = ("window.control",)
    args_model = WindowTargetArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, WindowTargetArgs)
        window = self._resolve(args)
        return Preview(
            "window-focus",
            {
                "handle": window.handle,
                "title": _clean_title(window.title),
                "process": window.process,
            },
            reversible=True,
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, WindowTargetArgs)
        window = self._resolve(args)
        self._act("focus", window.handle)
        return ToolResult(
            ok=True,
            output=f"focused {_clean_title(window.title)!r} (handle {window.handle})",
            meta={
                "handle": window.handle,
                "pid": window.pid,
                "process": window.process,
            },
            trust=TRUST_OS,
        )


class WindowMinimizeTool(_WindowTool):
    name = "window.minimize"
    description = "Minimize one window by handle or a unique title substring (R1)."
    risk = "R1"
    capabilities = ("window.control",)
    args_model = WindowTargetArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, WindowTargetArgs)
        window = self._resolve(args)
        return Preview(
            "window-minimize",
            {"handle": window.handle, "title": _clean_title(window.title)},
            reversible=True,
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, WindowTargetArgs)
        window = self._resolve(args)
        self._act("minimize", window.handle)
        return ToolResult(
            ok=True,
            output=f"minimized {_clean_title(window.title)!r} (handle {window.handle})",
            meta={"handle": window.handle, "pid": window.pid, "process": window.process},
            trust=TRUST_OS,
        )


class WindowMaximizeTool(_WindowTool):
    name = "window.maximize"
    description = "Maximize one window by handle or a unique title substring (R1)."
    risk = "R1"
    capabilities = ("window.control",)
    args_model = WindowTargetArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, WindowTargetArgs)
        window = self._resolve(args)
        return Preview(
            "window-maximize",
            {"handle": window.handle, "title": _clean_title(window.title)},
            reversible=True,
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, WindowTargetArgs)
        window = self._resolve(args)
        self._act("maximize", window.handle)
        return ToolResult(
            ok=True,
            output=f"maximized {_clean_title(window.title)!r} (handle {window.handle})",
            meta={"handle": window.handle, "pid": window.pid, "process": window.process},
            trust=TRUST_OS,
        )


class WindowCloseTool(_WindowTool):
    name = "window.close"
    description = "Ask one window to close with WM_CLOSE after user approval (R2)."
    risk = "R2"
    capabilities = ("window.control",)
    args_model = WindowTargetArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, WindowTargetArgs)
        window = self._resolve(args)
        _ensure_closable(window)
        return Preview(
            "window-close",
            {
                "handle": window.handle,
                "title": _clean_title(window.title),
                "process": window.process,
                "pid": window.pid,
            },
            reversible=False,
            warnings=("the application may lose unsaved work",),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, WindowTargetArgs)
        window = self._resolve(args)
        _ensure_closable(window)
        self._act("close", window.handle)
        return ToolResult(
            ok=True,
            output=(
                f"sent WM_CLOSE to {_clean_title(window.title)!r} "
                f"(handle {window.handle}, {window.process})"
            ),
            meta={
                "handle": window.handle,
                "pid": window.pid,
                "process": window.process,
            },
            trust=TRUST_OS,
        )


class AppLaunchTool:
    name = "app.launch"
    description = "Launch one .exe from the workspace with an argv list; no shell (R2)."
    risk = "R2"
    capabilities = ("app.launch",)
    args_model = AppLaunchArgs

    def __init__(self, backend: WindowBackend | None = None) -> None:
        self._backend: WindowBackend = backend if backend is not None else Win32Backend()

    def _resolve(self, args: AppLaunchArgs, ctx: ToolContext) -> tuple[Path, Path]:
        raw = args.path.strip()
        if _looks_like_url(raw):
            raise ToolError("URLs are not executable paths")
        if raw.startswith("\\\\"):
            raise ToolError("UNC paths are not allowed")
        _ensure_no_shell_path(raw)

        suffix = Path(raw).suffix.casefold()
        if suffix not in ALLOWED_SUFFIXES:
            raise ToolError(
                f"only .exe files may be launched (got {suffix or 'no extension'}); "
                "scripts and installers are not allowed"
            )
        basename = ntpath.basename(raw).casefold()
        if basename in DENY_LAUNCH_NAMES:
            raise ToolError(f"script hosts and interpreters are not launchable: {basename}")

        candidate = Path(raw) if Path(raw).is_absolute() else ctx.workspace / raw
        for access in ("read", "exec"):
            decision = ctx.zones.check(candidate, access)
            if not decision.allowed:
                raise ToolError(f"{decision.rule}: {decision.reason}")
        if decision.resolved is None:
            raise ToolError("executable path could not be resolved")
        executable = decision.resolved
        ctx.policy.check_path(executable, "exec").require()
        if not executable.is_file():
            raise ToolError(f"executable not found: {executable}")

        raw_cwd = args.cwd.strip() or "."
        cwd_candidate = Path(raw_cwd)
        if not cwd_candidate.is_absolute():
            cwd_candidate = ctx.workspace / cwd_candidate
        cwd_decision = ctx.zones.check(cwd_candidate, "exec")
        if not cwd_decision.allowed:
            raise ToolError(f"{cwd_decision.rule}: {cwd_decision.reason}")
        if cwd_decision.resolved is None:
            raise ToolError("working directory could not be resolved")
        cwd = cwd_decision.resolved
        if not cwd.is_dir():
            raise ToolError(f"working directory does not exist: {cwd}")
        return executable, cwd

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, AppLaunchArgs)
        executable, cwd = self._resolve(args, ctx)
        decision = ctx.policy.check_command([str(executable), *args.args])
        warnings = ["the executable will start a new process"]
        if not decision.allowed:
            warnings.append(f"policy would deny: {decision.reason}")
        return Preview(
            "app-launch",
            {"path": str(executable), "args": list(args.args), "cwd": str(cwd)},
            reversible=False,
            warnings=tuple(warnings),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, AppLaunchArgs)
        executable, cwd = self._resolve(args, ctx)
        ctx.policy.check_command([str(executable), *args.args]).require()
        try:
            pid = self._backend.launch(str(executable), list(args.args), cwd=str(cwd))
        except (WindowError, OSError) as exc:
            raise ToolError(str(exc)) from exc
        if pid <= 0:
            raise ToolError("the backend returned an invalid pid")
        return ToolResult(
            ok=True,
            output=f"launched {executable.name} (pid {pid})",
            meta={
                "pid": pid,
                "path": str(executable),
                "cwd": str(cwd),
                "args": list(args.args),
            },
            trust="untrusted:tool",
        )


class AppTerminateTool:
    name = "app.terminate"
    description = "Terminate one verified application process after approval (R2)."
    risk = "R2"
    capabilities = ("app.terminate",)
    args_model = AppTerminateArgs

    def __init__(
        self,
        backend: WindowBackend | None = None,
        process_factory: Any = None,
    ) -> None:
        self._backend: WindowBackend = backend if backend is not None else Win32Backend()
        self._process_factory = process_factory or psutil.Process

    def _target(self, args: AppTerminateArgs) -> tuple[Any, str]:
        if args.pid <= 4:
            raise ToolError("system pids are not terminable")
        if args.pid == os.getpid():
            raise ToolError("refusing to terminate the Kivi process")
        try:
            process = self._process_factory(args.pid)
            name = str(process.name())
        except psutil.NoSuchProcess as exc:
            raise ToolError(f"process not found: {args.pid}") from exc
        except psutil.AccessDenied as exc:
            raise ToolError(f"access denied: {args.pid}") from exc
        lowered = name.casefold()
        if lowered in TERMINATE_PROTECTED_PROCESSES or lowered.startswith("kivi"):
            raise ToolError(f"protected process: {name}")
        return process, name

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, AppTerminateArgs)
        _, name = self._target(args)
        return Preview(
            "app-terminate",
            {"pid": args.pid, "process": name},
            reversible=False,
            warnings=("the process will be terminated and may lose unsaved work",),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, AppTerminateArgs)
        process, name = self._target(args)
        try:
            self._backend.terminate(args.pid)
        except (WindowError, OSError) as exc:
            raise ToolError(str(exc)) from exc
        try:
            process.wait(timeout=5)
        except psutil.TimeoutExpired as exc:
            raise ToolError(
                f"process {args.pid} did not exit in time; it may be elevated or hung"
            ) from exc
        except psutil.AccessDenied as exc:
            raise ToolError(f"access denied while waiting for process {args.pid}") from exc
        except psutil.NoSuchProcess:
            return ToolResult(
                ok=True,
                output=f"process {args.pid} ({name}) already exited",
                meta={"pid": args.pid, "process": name, "already_exited": True},
                trust="untrusted:tool",
            )
        return ToolResult(
            ok=True,
            output=f"terminated {name} (pid {args.pid})",
            meta={"pid": args.pid, "process": name},
            trust="untrusted:tool",
        )


__all__ = [
    "ALLOWED_SUFFIXES",
    "AppLaunchArgs",
    "AppLaunchTool",
    "AppTerminateArgs",
    "AppTerminateTool",
    "CLOSE_PROTECTED_PROCESSES",
    "DENY_LAUNCH_NAMES",
    "TERMINATE_PROTECTED_PROCESSES",
    "TRUST_OS",
    "PywinautoBackend",
    "Win32Backend",
    "WindowBackend",
    "WindowCloseTool",
    "WindowError",
    "WindowFocusTool",
    "WindowInfo",
    "WindowListArgs",
    "WindowListTool",
    "WindowMaximizeTool",
    "WindowMinimizeTool",
    "WindowTargetArgs",
    "WindowsUnavailable",
    "pywinauto_available",
]
