"""Elevated broker: a narrow allowlist of admin operations behind UAC.

The core process never runs with administrator rights. When the user explicitly
approves an admin action, the broker writes a fixed-schema request file and asks
Windows to start ``kivi.elevated_helper`` with the ``runas`` verb. The helper
re-validates the operation against the same allowlist defined here, builds a
fixed argv list and writes a result file next to the request.

There is deliberately no caller-supplied executable, argv or shell string.
Adding an operation means adding an :class:`OperationSpec` and a builder that
validates every parameter. The helper must stay minimal; see
``docs/ELEVATED.md`` for the threat model.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from kivi.paths import get_paths

if TYPE_CHECKING:
    from kivi.kernel.db import Database

REQUEST_VERSION = 1
RESULT_VERSION = 1
HELPER_MODULE = "kivi.elevated_helper"
REQUEST_DIRNAME = "elevated"
SW_HIDE = 0
SHELL_EXECUTE_MIN_SUCCESS = 32
MAX_STREAM_CHARS = 8000
MAX_REQUEST_BYTES = 64 * 1024

# Services the broker is allowed to touch. Extend this set deliberately and
# only with a service Kivi actually owns or manages.
ALLOWED_SERVICES: frozenset[str] = frozenset({"KiviAgent"})

_ACTION_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_RESULT_KEYS = frozenset(
    {
        "version",
        "request_id",
        "operation",
        "ok",
        "returncode",
        "stdout",
        "stderr",
        "truncated",
        "error",
        "finished_at",
    }
)


class ElevatedError(Exception):
    """Base error for rejected or malformed elevated requests."""


class UnknownOperation(ElevatedError):
    """The requested operation is not in the allowlist."""


class InvalidParams(ElevatedError):
    """Operation parameters are missing, extra or invalid."""


class LaunchError(ElevatedError):
    """The helper could not be started (UAC cancelled, missing helper, ...)."""


class Launcher(Protocol):
    """Start the elevated helper and return a ShellExecuteW-style code."""

    def __call__(self, helper: Path, request: Path) -> int: ...


class KillSwitchLike(Protocol):
    """Minimal kill-switch surface; the broker stays decoupled from its type."""

    def is_engaged(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """One admin operation: a fixed command plus validated parameters."""

    name: str
    description: str
    param_names: tuple[str, ...]
    timeout_s: float
    build_argv: Callable[[Mapping[str, Any]], tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class ResolvedOperation:
    """A validated operation and the exact argv the helper must run."""

    spec: OperationSpec
    params: dict[str, str]
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ElevatedResult:
    """Outcome of one broker call; errors are values, not exceptions."""

    ok: bool
    status: str
    operation: str
    decision: str
    message: str = ""
    request_id: str = ""
    request_path: Path | None = None
    result_path: Path | None = None
    launcher_code: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation for CLI and event consumers."""
        return {
            "ok": self.ok,
            "status": self.status,
            "operation": self.operation,
            "decision": self.decision,
            "message": self.message,
            "request_id": self.request_id,
            "request_path": str(self.request_path) if self.request_path else None,
            "result_path": str(self.result_path) if self.result_path else None,
            "launcher_code": self.launcher_code,
        }


# -- parameter validation -------------------------------------------------
def _require_params(params: Mapping[str, Any], expected: frozenset[str]) -> None:
    actual = set(params)
    if not all(isinstance(key, str) for key in actual):
        raise InvalidParams("parameter names must be strings")
    if actual == expected:
        return
    parts: list[str] = []
    missing = sorted(expected.difference(actual))
    extra = sorted(actual.difference(expected))
    if missing:
        parts.append("missing: " + ", ".join(missing))
    if extra:
        parts.append("unexpected: " + ", ".join(extra))
    raise InvalidParams("invalid parameters (" + "; ".join(parts) + ")")


def _service_param(params: Mapping[str, Any]) -> str:
    _require_params(params, frozenset({"service"}))
    service = params["service"]
    if not isinstance(service, str):
        raise InvalidParams("service must be a string")
    if service not in ALLOWED_SERVICES:
        allowed = ", ".join(sorted(ALLOWED_SERVICES))
        raise InvalidParams(f"service is not allowlisted (allowed: {allowed})")
    return service


def _build_dns_flush(params: Mapping[str, Any]) -> tuple[str, ...]:
    _require_params(params, frozenset())
    return ("ipconfig", "/flushdns")


def _build_service_query(params: Mapping[str, Any]) -> tuple[str, ...]:
    return ("sc.exe", "query", _service_param(params))


def _build_service_restart(params: Mapping[str, Any]) -> tuple[str, ...]:
    return ("sc.exe", "restart", _service_param(params))


OPERATIONS: Mapping[str, OperationSpec] = MappingProxyType(
    {
        "dns.flush": OperationSpec(
            name="dns.flush",
            description="Flush the Windows DNS resolver cache.",
            param_names=(),
            timeout_s=30.0,
            build_argv=_build_dns_flush,
        ),
        "service.query": OperationSpec(
            name="service.query",
            description="Query one allowlisted service (read-only).",
            param_names=("service",),
            timeout_s=30.0,
            build_argv=_build_service_query,
        ),
        "service.restart": OperationSpec(
            name="service.restart",
            description="Restart one allowlisted service.",
            param_names=("service",),
            timeout_s=60.0,
            build_argv=_build_service_restart,
        ),
    }
)
ALLOWED_OPERATIONS = frozenset(OPERATIONS)


def resolve_operation(
    operation: object,
    params: Mapping[str, Any] | None = None,
) -> ResolvedOperation:
    """Validate an operation request and return its fixed argv.

    Raises:
        UnknownOperation: when ``operation`` is not in the allowlist.
        InvalidParams: when ``params`` do not match the operation spec.
    """
    if not isinstance(operation, str):
        raise UnknownOperation("operation must be a string")
    spec = OPERATIONS.get(operation)
    if spec is None:
        raise UnknownOperation(f"unknown operation: {operation[:64]!r}")
    raw: Mapping[str, Any] = {} if params is None else params
    if not isinstance(raw, Mapping):
        raise InvalidParams("params must be an object")
    argv = spec.build_argv(raw)
    # Builders return only literals and strings already checked against the
    # allowlist. This guard catches a mistake in a future builder.
    if not argv or any(not isinstance(part, str) for part in argv):
        raise InvalidParams("operation produced an invalid argv")
    return ResolvedOperation(spec=spec, params=dict(raw), argv=tuple(argv))


def is_valid_request_id(value: object) -> bool:
    """Return whether ``value`` is a safe request id for a file name."""
    return isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value) is not None


# -- request and result files --------------------------------------------
def request_file_path(directory: Path, request_id: str) -> Path:
    """Return ``request-<id>.json`` inside ``directory``."""
    return Path(directory) / f"request-{request_id}.json"


def result_file_path(request_path: Path) -> Path:
    """Return the helper response path for ``request_path``."""
    return Path(request_path).with_suffix(".result.json")


def _restrict_directory(directory: Path) -> None:
    # POSIX honors the mode. Windows only clears the read-only bit; the real
    # protection there is the per-user ACL on Kivi's data directory.
    with suppress(OSError):
        os.chmod(directory, 0o700)


def write_json_file(path: Path, payload: Mapping[str, Any]) -> None:
    """Write ``payload`` as UTF-8 JSON, replacing the target atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _restrict_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_result(path: Path | str) -> dict[str, Any]:
    """Read and validate one helper result file.

    Raises:
        ElevatedError: when the file is missing, not JSON or the wrong shape.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ElevatedError(f"cannot read result: {exc}") from exc
    if not isinstance(data, dict):
        raise ElevatedError("result must be a JSON object")
    missing = _RESULT_KEYS.difference(data)
    if missing:
        raise ElevatedError("result is missing keys: " + ", ".join(sorted(missing)))
    if data["version"] != RESULT_VERSION:
        raise ElevatedError("unsupported result version")
    for key in ("request_id", "operation", "stdout", "stderr", "error", "finished_at"):
        if not isinstance(data[key], str):
            raise ElevatedError(f"result field {key!r} must be a string")
    if not isinstance(data["ok"], bool):
        raise ElevatedError("result field 'ok' must be a boolean")
    if not isinstance(data["truncated"], bool):
        raise ElevatedError("result field 'truncated' must be a boolean")
    if data["returncode"] is not None and not isinstance(data["returncode"], int):
        raise ElevatedError("result field 'returncode' must be an integer or null")
    return data


# -- launcher -------------------------------------------------------------
def shell_execute_launcher(helper: Path, request: Path) -> int:
    """Launch the helper through UAC; return the raw ShellExecuteW code."""
    if sys.platform != "win32":
        raise LaunchError("the elevated broker is only available on Windows")
    import ctypes

    parameters = subprocess.list2cmdline([str(helper), "--request", str(request)])
    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_int,
    ]
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    code = shell32.ShellExecuteW(
        None,
        "runas",
        sys.executable,
        parameters,
        None,
        SW_HIDE,
    )
    return int(code or 0)


# -- broker ---------------------------------------------------------------
def _audit_action(operation: object) -> str:
    text = operation if isinstance(operation, str) else ""
    if _ACTION_RE.fullmatch(text) is None:
        text = "unknown"
    return f"elevated.{text}"


def _safe_text(value: object, limit: int = 200) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text[:limit]


def _params_preview(params: object) -> str:
    try:
        text = json.dumps(params, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        text = repr(params)
    return text[:200]


def _killswitch_engaged(
    killswitch: KillSwitchLike | Callable[[], bool] | None,
) -> bool:
    if killswitch is None:
        return False
    try:
        if callable(killswitch):
            return bool(killswitch())
        return bool(killswitch.is_engaged())
    except Exception:  # noqa: BLE001 - a broken stop check must stop, not allow
        return True


class ElevatedBroker:
    """Validate, audit and dispatch allowlisted admin operations."""

    def __init__(
        self,
        db: Database,
        *,
        request_dir: Path | None = None,
        launcher: Launcher | None = None,
        helper: Path | None = None,
        killswitch: KillSwitchLike | Callable[[], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = db
        self.request_dir = (
            Path(request_dir) if request_dir is not None else get_paths().data / REQUEST_DIRNAME
        )
        self.launcher: Launcher = launcher or shell_execute_launcher
        self.helper = (
            Path(helper)
            if helper is not None
            else Path(__file__).resolve().parents[1] / "elevated_helper.py"
        )
        self.killswitch = killswitch
        self._clock = clock or (lambda: datetime.now(UTC))

    def _audit(
        self,
        operation: object,
        decision: str,
        reason: str,
        *,
        target: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        self.db.append_audit(
            actor="elevated",
            action=_audit_action(operation),
            target=target or _safe_text(operation),
            decision=decision,
            reason=reason[:500],
            details=details,
        )

    def _deny(
        self,
        operation: object,
        message: str,
        *,
        reason: str,
        approved: bool,
        params: object = None,
    ) -> ElevatedResult:
        self._audit(
            operation,
            "deny",
            reason,
            details={
                "approved": approved,
                "params_preview": _params_preview(params),
            },
        )
        return ElevatedResult(
            ok=False,
            status="denied",
            operation=_safe_text(operation),
            decision="deny",
            message=message,
        )

    def request(
        self,
        operation: str,
        params: Mapping[str, Any] | None = None,
        *,
        approved: bool = False,
    ) -> ElevatedResult:
        """Start one allowlisted operation after explicit user approval.

        Returns ``status="started"`` as soon as the helper is launched; it never
        waits for the helper. The response is written to ``result_path``.
        """
        if _killswitch_engaged(self.killswitch):
            return self._deny(
                operation,
                "kill switch engaged",
                reason="kill switch engaged",
                approved=approved,
                params=params,
            )
        if not approved:
            return self._deny(
                operation,
                "explicit user approval is required",
                reason="approval required",
                approved=approved,
                params=params,
            )
        try:
            resolved = resolve_operation(operation, params)
        except UnknownOperation as exc:
            return self._deny(
                operation,
                str(exc),
                reason="unknown operation",
                approved=approved,
                params=params,
            )
        except InvalidParams as exc:
            return self._deny(
                operation,
                str(exc),
                reason="invalid parameters",
                approved=approved,
                params=params,
            )

        if not self.helper.is_file():
            message = f"elevated helper is missing: {self.helper}"
            self._audit(
                operation,
                "error",
                message,
                target=_safe_text(operation),
                details={"approved": True, "helper": str(self.helper)},
            )
            return ElevatedResult(
                ok=False,
                status="error",
                operation=resolved.spec.name,
                decision="error",
                message=message,
            )

        request_id = uuid.uuid4().hex
        request_path = request_file_path(self.request_dir, request_id)
        result_path = result_file_path(request_path)
        payload = {
            "version": REQUEST_VERSION,
            "request_id": request_id,
            "operation": resolved.spec.name,
            "params": resolved.params,
            "created_at": self._clock().isoformat(timespec="seconds"),
        }
        details: dict[str, Any] = {
            "approved": True,
            "request_id": request_id,
            "request_path": str(request_path),
            "result_path": str(result_path),
            "argv": list(resolved.argv),
            "params": resolved.params,
        }
        try:
            write_json_file(request_path, payload)
        except OSError as exc:
            message = f"cannot write request file: {exc}"
            self._audit(operation, "error", message, target=str(request_path), details=details)
            return ElevatedResult(
                ok=False,
                status="error",
                operation=resolved.spec.name,
                decision="error",
                message=message,
                request_id=request_id,
                request_path=request_path,
                result_path=result_path,
            )

        try:
            code = int(self.launcher(self.helper, request_path))
        except Exception as exc:  # noqa: BLE001 - the core process must survive
            message = f"cannot launch elevated helper: {exc}"
            self._audit(operation, "error", message, target=str(request_path), details=details)
            return ElevatedResult(
                ok=False,
                status="error",
                operation=resolved.spec.name,
                decision="error",
                message=message,
                request_id=request_id,
                request_path=request_path,
                result_path=result_path,
            )
        if code <= SHELL_EXECUTE_MIN_SUCCESS:
            message = f"helper launch failed (ShellExecuteW code {code})"
            self._audit(operation, "error", message, target=str(request_path), details=details)
            return ElevatedResult(
                ok=False,
                status="error",
                operation=resolved.spec.name,
                decision="error",
                message=message,
                request_id=request_id,
                request_path=request_path,
                result_path=result_path,
                launcher_code=code,
            )

        details["launcher_code"] = code
        self._audit(
            operation,
            "allow",
            "approved and launched",
            target=str(request_path),
            details=details,
        )
        return ElevatedResult(
            ok=True,
            status="started",
            operation=resolved.spec.name,
            decision="allow",
            message="elevated helper started; poll the result file",
            request_id=request_id,
            request_path=request_path,
            result_path=result_path,
            launcher_code=code,
        )


__all__ = [
    "ALLOWED_OPERATIONS",
    "ALLOWED_SERVICES",
    "HELPER_MODULE",
    "MAX_REQUEST_BYTES",
    "MAX_STREAM_CHARS",
    "OPERATIONS",
    "REQUEST_DIRNAME",
    "REQUEST_VERSION",
    "RESULT_VERSION",
    "SHELL_EXECUTE_MIN_SUCCESS",
    "SW_HIDE",
    "ElevatedBroker",
    "ElevatedError",
    "ElevatedResult",
    "InvalidParams",
    "KillSwitchLike",
    "Launcher",
    "LaunchError",
    "OperationSpec",
    "ResolvedOperation",
    "UnknownOperation",
    "is_valid_request_id",
    "read_result",
    "request_file_path",
    "resolve_operation",
    "result_file_path",
    "shell_execute_launcher",
    "write_json_file",
]
