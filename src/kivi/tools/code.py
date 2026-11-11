"""``code.run``: run generated Python in a best-effort process sandbox.

The tool is R3 because generated code is untrusted by definition. It writes the
code to ``<workspace>/data/sandbox/<uuid>/main.py`` and starts a fresh
interpreter with ``-I`` (isolated) and a bootstrap module that installs an audit
hook *before* the user code runs. On Windows the child is also placed in a Job
Object with memory, CPU-time, active-process and kill-on-close limits.

This is best-effort, not an OS boundary: audit hooks live in the same process as
the code they police, so native code can bypass them. See ``docs/SANDBOX.md``
for the threat model and the path to WSL2 / Windows Sandbox.

The sandbox deliberately does not add the project to ``sys.path``: generated
code gets the standard library (and the interpreter's own packages), not Kivi's
imports. ``-I`` implies ``-P``/``-s``/``-E``, and the bootstrap additionally
drops ``<workspace>/src`` from ``sys.path`` when an editable install put it
there.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kivi.safety.zones import ZoneError
from kivi.tools.base import Preview, ToolContext, ToolError, ToolResult
from kivi.tools.shell import _minimal_env

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 10.0
_DEFAULT_MEMORY_MB = 512

# Windows Job Object constants (winnt.h).
_JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_HUNDRED_NS = 10_000_000


class _JobIoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobBasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _JobExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimits),
        ("IoInfo", _JobIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JobObject:
    """A minimal Windows Job Object with resource and lifetime limits.

    Construction raises ``OSError`` when the platform cannot provide a job; the
    caller then falls back to the plain subprocess timeout and records
    ``job_object: false`` in the result metadata.
    """

    def __init__(self, memory_mb: int, timeout_s: float) -> None:
        if sys.platform != "win32":
            raise OSError("Job Objects are only available on Windows")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
        )
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.TerminateJobObject.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        kernel32.TerminateJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        self._kernel32 = kernel32
        self._handle: int | None = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self._configure(memory_mb, timeout_s)
        except OSError:
            self.close()
            raise

    def _configure(self, memory_mb: int, timeout_s: float) -> None:
        info = _JobExtendedLimits()
        limit = info.BasicLimitInformation
        limit.PerProcessUserTimeLimit = max(1, int(timeout_s * _HUNDRED_NS))
        limit.LimitFlags = (
            _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | _JOB_OBJECT_LIMIT_PROCESS_TIME
            | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        limit.ActiveProcessLimit = 1
        info.ProcessMemoryLimit = memory_mb * 1024 * 1024
        ok = self._kernel32.SetInformationJobObject(
            self._handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process: subprocess.Popen[str]) -> None:
        """Put ``process`` in the job; raises ``OSError`` if the handle is unusable."""
        handle = getattr(process, "_handle", None)
        if handle is None:
            raise OSError("process handle is unavailable")
        ok = self._kernel32.AssignProcessToJobObject(self._handle, int(handle))
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self) -> None:
        """Terminate every process in the job (best-effort)."""
        if self._handle is not None:
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        """Close the job handle; ``KILL_ON_JOB_CLOSE`` reaps any survivors."""
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _sandbox_python() -> str:
    """Return a real interpreter, not a venv redirector.

    A Windows venv ``python.exe`` is a launcher that spawns the base interpreter
    as a child. ``ActiveProcessLimit=1`` would block that child, so the sandbox
    runs the base interpreter instead. That also keeps the venv's packages and
    the editable-install ``src`` path out of the sandbox.
    """
    base = getattr(sys, "_base_executable", "") or ""
    if base and Path(base).is_file():
        return base
    return sys.executable


def _cap(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


class CodeRunArgs(BaseModel):
    """Arguments for ``code.run``."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(
        min_length=1,
        max_length=200_000,
        description="Python source to execute.",
    )
    timeout_s: float = Field(
        default=_DEFAULT_TIMEOUT_S,
        ge=1.0,
        le=60.0,
        description="Wall-clock timeout in seconds.",
    )
    memory_mb: int = Field(
        default=_DEFAULT_MEMORY_MB,
        ge=64,
        le=2048,
        description="Per-process memory limit enforced by the Job Object.",
    )
    args: list[str] | None = Field(
        default=None,
        max_length=16,
        description="Optional argv passed to the script.",
    )


class CodeRunTool:
    """Run generated Python with audit hooks, limits and captured output."""

    name = "code.run"
    description = "Run generated Python in a best-effort sandbox; requires approval (R3)."
    risk = "R3"
    capabilities = ("code.run",)
    args_model = CodeRunArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, CodeRunArgs)
        payload = args.code.encode("utf-8")
        details: dict[str, Any] = {
            "bytes": len(payload),
            "timeout_s": args.timeout_s,
            "memory_mb": args.memory_mb,
            "args": list(args.args or []),
            "workspace": str(ctx.workspace),
        }
        return Preview(
            "code-run",
            details,
            reversible=False,
            warnings=("generated Python code will execute in a best-effort sandbox",),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, CodeRunArgs)
        workspace = Path(ctx.workspace)
        payload = args.code.encode("utf-8")
        sandbox_dir = workspace / "data" / "sandbox" / uuid.uuid4().hex
        decision = ctx.zones.check(sandbox_dir, "write")
        if not decision.allowed:
            raise ZoneError(f"{decision.rule}: {decision.reason}")
        if decision.resolved is not None:
            sandbox_dir = decision.resolved
        script = sandbox_dir / "main.py"
        bootstrap = sandbox_dir / "bootstrap.py"
        try:
            sandbox_dir.mkdir(parents=True, exist_ok=True)
            script.write_bytes(payload)
            bootstrap.write_text(_BOOTSTRAP, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"cannot prepare sandbox: {exc}") from exc

        user_args = list(args.args or [])
        command = [
            _sandbox_python(),
            "-I",
            "-X",
            "utf8",
            str(bootstrap),
            str(workspace),
            str(script),
            "--",
            *user_args,
        ]
        timeout = args.timeout_s
        try:
            process = subprocess.Popen(
                command,
                cwd=str(workspace),
                env=_minimal_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="backslashreplace",
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise ToolError(f"cannot start sandbox process: {exc}") from exc

        job: _JobObject | None = None
        job_object = False
        if sys.platform == "win32":
            try:
                job = _JobObject(args.memory_mb, timeout)
                job.assign(process)
                job_object = True
            except OSError as exc:
                log.debug("job object unavailable: %s", exc, exc_info=True)
                if job is not None:
                    job.close()
                    job = None

        timed_out = False
        try:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                if job is not None:
                    job.terminate()
                with contextlib.suppress(OSError):
                    process.kill()
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
        finally:
            if job is not None:
                job.close()

        returncode = process.returncode if process.returncode is not None else -1
        cap = max(1, ctx.limits.max_output_chars)
        stdout_text, stdout_cut = _cap(stdout or "", cap)
        stderr_text, stderr_cut = _cap(stderr or "", cap)
        output = stdout_text + stderr_text
        truncated = stdout_cut or stderr_cut

        if timed_out:
            error = f"timed out after {timeout:g}s"
        elif returncode != 0:
            error = (stderr or "")[-cap:] or f"exit code {returncode}"
        else:
            error = ""

        return ToolResult(
            ok=returncode == 0 and not timed_out,
            output=output,
            error=error,
            meta={
                "returncode": returncode,
                "timed_out": timed_out,
                "sandboxed": True,
                "job_object": job_object,
                "bytes": len(payload),
                "output_bytes": len(stdout or "") + len(stderr or ""),
                "sandbox_dir": str(sandbox_dir),
                "timeout_s": timeout,
            },
            truncated=truncated,
            trust="untrusted:code",
        )


_BOOTSTRAP = r'''"""Sandbox bootstrap: install an audit hook, then run the user script."""
from __future__ import annotations

import fnmatch
import os
import sys
import sysconfig
from typing import Any

_SENSITIVE_NAMES = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "*.kdbx",
    "id_rsa*",
    "id_ed25519*",
    "secrets.*",
    "credentials.*",
)
_SENSITIVE_DIRS = frozenset({"private", "voice-dataset", ".git", ".ssh"})
_PROCESS_EVENTS = frozenset(
    {
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.spawn",
        "os.posix_spawn",
        "os.startfile",
    }
)
_FS_EVENTS = {
    "os.remove": (0,),
    "os.rmdir": (0,),
    "os.mkdir": (0,),
    "os.rename": (0, 1),
    "os.replace": (0, 1),
    "os.link": (0, 1),
    "os.symlink": (0, 1),
    "os.truncate": (0,),
    "os.chmod": (0,),
}
_WRITE_FLAGS = (
    getattr(os, "O_WRONLY", 0)
    | getattr(os, "O_RDWR", 0)
    | getattr(os, "O_CREAT", 0)
    | getattr(os, "O_TRUNC", 0)
    | getattr(os, "O_APPEND", 0)
)


def _deny(message: str) -> None:
    raise PermissionError("sandbox: " + message)


def _inside(path: str, root: str) -> bool:
    path = os.path.normcase(path)
    root = os.path.normcase(root)
    return path == root or path.startswith(root + os.sep)


def _text(raw: object) -> str | None:
    if isinstance(raw, bytes):
        return os.fsdecode(raw)
    if isinstance(raw, str):
        return raw
    return None


def _resolve(raw: object) -> str | None:
    text = _text(raw)
    if text is None:
        return None
    return os.path.realpath(os.path.abspath(text))


def _sensitive(path: str) -> bool:
    parts = [part.lower() for part in path.split(os.sep) if part]
    if any(part in _SENSITIVE_DIRS for part in parts):
        return True
    name = os.path.basename(path).lower()
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in _SENSITIVE_NAMES)


_PROTECTED_WRITES = (
    "src/kivi",
    "tests",
    "docs",
    "config",
    "pyproject.toml",
    "data/kivi.db",
    "data/killswitch.json",
)


def _protected_write(path: str, workspace: str) -> bool:
    try:
        relative = os.path.relpath(path, workspace).replace("\\", "/").lower()
    except ValueError:
        return True
    return any(
        relative == prefix or relative.startswith(prefix + "/")
        for prefix in _PROTECTED_WRITES
    )


def _write_mode(mode: object, flags: object) -> bool:
    if isinstance(mode, str):
        return any(ch in mode for ch in "wax+")
    if isinstance(flags, int):
        return bool(flags & _WRITE_FLAGS)
    return False


def _read_roots(workspace: str) -> tuple[str, ...]:
    roots = {
        workspace,
        os.path.realpath(sys.base_prefix),
        os.path.realpath(sys.prefix),
        os.path.realpath(os.path.dirname(sys.executable)),
    }
    for key, value in sysconfig.get_paths().items():
        if key in {"stdlib", "platstdlib", "purelib", "platlib"} and value:
            roots.add(os.path.realpath(value))
    return tuple(sorted(roots))


def _check_open(
    raw: object,
    mode: object,
    flags: object,
    workspace: str,
    roots: tuple[str, ...],
) -> None:
    path = _resolve(raw)
    if path is None:
        return
    if _sensitive(path):
        _deny(f"sensitive path is not allowed: {path}")
    if _write_mode(mode, flags):
        if not _inside(path, workspace):
            _deny(f"write outside the workspace is not allowed: {path}")
        if _protected_write(path, workspace):
            _deny(f"write to a protected path is not allowed: {path}")
        return
    if not any(_inside(path, root) for root in roots):
        _deny(f"read outside the sandbox roots is not allowed: {path}")


def _check_listing(raw: object, roots: tuple[str, ...]) -> None:
    path = _resolve(raw)
    if path is None:
        return
    if _sensitive(path):
        _deny(f"sensitive path is not allowed: {path}")
    if not any(_inside(path, root) for root in roots):
        _deny(f"listing outside the sandbox roots is not allowed: {path}")


def _check_fs(paths: tuple[Any, ...], indexes: tuple[int, ...], workspace: str) -> None:
    for index in indexes:
        if index >= len(paths):
            continue
        path = _resolve(paths[index])
        if path is None:
            continue
        if _sensitive(path):
            _deny(f"sensitive path is not allowed: {path}")
        if not _inside(path, workspace):
            _deny(f"file change outside the workspace is not allowed: {path}")


def _install_hook(workspace: str) -> None:
    roots = _read_roots(workspace)

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if event == "import":
            module = args[0] if args else None
            if isinstance(module, str) and module.split(".")[0] in {"ctypes", "_ctypes"}:
                _deny(f"native module is not allowed: {module}")
            return
        if event == "open":
            raw = args[0] if args else None
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else None
            _check_open(raw, mode, flags, workspace, roots)
            return
        if event in {"os.listdir", "os.scandir"}:
            _check_listing(args[0] if args else None, roots)
            return
        if event.startswith("socket."):
            _deny("network access is disabled")
            return
        if event in _PROCESS_EVENTS:
            _deny(f"process creation is disabled ({event})")
            return
        if event == "ctypes.dlopen":
            _deny("native library loading is disabled")
            return
        if event in _FS_EVENTS:
            _check_fs(args, _FS_EVENTS[event], workspace)
            return

    sys.addaudithook(hook)


def _drop_project_paths(workspace: str) -> None:
    project_src = os.path.join(workspace, "src")
    kept: list[str] = []
    for entry in sys.path:
        resolved = os.path.realpath(entry) if entry else ""
        if resolved and _inside(resolved, project_src):
            continue
        kept.append(entry)
    sys.path[:] = kept


def _exit_code(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return int(value)
    print(value, file=sys.stderr)
    return 1


def _main() -> int:
    if len(sys.argv) < 3:
        print("sandbox bootstrap: expected <workspace> <script> [-- args]", file=sys.stderr)
        return 2
    workspace = os.path.realpath(sys.argv[1])
    script = os.path.realpath(sys.argv[2])
    rest = sys.argv[3:]
    user_args = rest[1:] if rest[:1] == ["--"] else rest
    if not _inside(script, workspace):
        print("sandbox bootstrap: script is outside the workspace", file=sys.stderr)
        return 2

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass

    sys.dont_write_bytecode = True
    _drop_project_paths(workspace)
    _install_hook(workspace)

    try:
        with open(script, "rb") as handle:
            source = handle.read()
        compiled = compile(source, script, "exec")
        sys.argv = [script, *user_args]
        namespace: dict[str, Any] = {
            "__name__": "__main__",
            "__file__": script,
            "__package__": None,
            "__spec__": None,
        }
        exec(compiled, namespace)
    except SystemExit as exc:
        return _exit_code(exc.code)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
'''


__all__ = ["CodeRunArgs", "CodeRunTool"]
