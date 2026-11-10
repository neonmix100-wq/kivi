"""Elevated helper: execute exactly one allowlisted operation.

This module is the only process that should ever run with administrator rights.
:class:`kivi.safety.elevated.ElevatedBroker` starts it through Windows UAC; it
can also be invoked manually::

    python -m kivi.elevated_helper --request <path>

The helper never trusts the request file. It reads only the operation and its
parameters, re-validates both against the allowlist in
:mod:`kivi.safety.elevated`, obtains the fixed argv from there and runs it with
``shell=False``. A response is written next to the request
(``request-<id>.result.json``) with truncated stdout/stderr.

Do not add a command string, an extra executable or ``shell=True`` here. New
admin work belongs in the broker's allowlist, not in this helper.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kivi.safety.elevated import (
    MAX_REQUEST_BYTES,
    MAX_STREAM_CHARS,
    REQUEST_VERSION,
    RESULT_VERSION,
    ElevatedError,
    is_valid_request_id,
    resolve_operation,
    result_file_path,
    write_json_file,
)

_ENV_KEYS = (
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
)


@dataclass(frozen=True, slots=True)
class Execution:
    """Result of one fixed command; small enough to serialise safely."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


Runner = Callable[[Sequence[str], float], Execution]


def _minimal_env() -> dict[str, str]:
    env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
    root = env.get("SYSTEMROOT") or env.get("WINDIR") or r"C:\Windows"
    # Fixed commands must resolve from the system directory only. The caller's
    # PATH is user-writable and must not influence an elevated process.
    env["PATH"] = os.pathsep.join((os.path.join(root, "System32"), root))
    env["NoDefaultCurrentDirectoryInExePath"] = "1"
    return env


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "backslashreplace")
    return value


def run_fixed_command(argv: Sequence[str], timeout_s: float) -> Execution:
    """Run one fixed argv with ``shell=False``, a minimal env and a timeout."""
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            list(argv),
            env=_minimal_env(),
            capture_output=True,
            text=True,
            errors="backslashreplace",
            timeout=timeout_s,
            shell=False,
            stdin=subprocess.DEVNULL,
            creationflags=creation_flags,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return Execution(
            returncode=124,
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
            timed_out=True,
        )
    return Execution(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def _load_request(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        if size > MAX_REQUEST_BYTES:
            raise ElevatedError("request file is too large")
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ElevatedError(f"cannot read request: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ElevatedError("request is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ElevatedError("request must be a JSON object")
    if payload.get("version") != REQUEST_VERSION:
        raise ElevatedError("unsupported request version")
    if not is_valid_request_id(payload.get("request_id")):
        raise ElevatedError("request_id is missing or invalid")
    if "operation" not in payload:
        raise ElevatedError("operation is missing")
    return payload


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_STREAM_CHARS:
        return text, False
    return text[:MAX_STREAM_CHARS], True


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _error_result(request_id: str, operation: str, message: str) -> dict[str, Any]:
    return {
        "version": RESULT_VERSION,
        "request_id": request_id,
        "operation": operation[:200],
        "ok": False,
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "truncated": False,
        "error": message[:500],
        "finished_at": _now(),
    }


def _execution_result(
    request_id: str,
    operation: str,
    execution: Execution,
) -> dict[str, Any]:
    stdout, stdout_cut = _truncate(execution.stdout)
    stderr, stderr_cut = _truncate(execution.stderr)
    ok = execution.returncode == 0 and not execution.timed_out
    if execution.timed_out:
        error = "operation timed out"
    elif execution.returncode is None:
        error = "operation did not return an exit code"
    elif not ok:
        error = f"exit code {execution.returncode}"
    else:
        error = ""
    return {
        "version": RESULT_VERSION,
        "request_id": request_id,
        "operation": operation,
        "ok": ok,
        "returncode": execution.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": stdout_cut or stderr_cut,
        "error": error,
        "finished_at": _now(),
    }


def execute_request(
    request_path: Path | str,
    *,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Validate one request file, run its fixed argv and write the result file.

    ``runner`` is injectable so tests never execute real admin commands. The
    returned mapping is exactly what was written to ``*.result.json``.
    """
    path = Path(request_path)
    try:
        payload = _load_request(path)
    except ElevatedError as exc:
        result = _error_result("", "", str(exc))
        write_json_file(result_file_path(path), result)
        return result

    request_id = payload["request_id"]
    operation = payload.get("operation")
    params = payload.get("params", {})
    display_operation = operation if isinstance(operation, str) else ""
    try:
        resolved = resolve_operation(operation, params)
    except ElevatedError as exc:
        result = _error_result(request_id, display_operation, str(exc))
        write_json_file(result_file_path(path), result)
        return result

    execute = runner or run_fixed_command
    try:
        execution = execute(resolved.argv, resolved.spec.timeout_s)
    except (OSError, subprocess.SubprocessError) as exc:
        result = _error_result(
            request_id,
            resolved.spec.name,
            f"cannot run operation: {exc}",
        )
        write_json_file(result_file_path(path), result)
        return result

    result = _execution_result(request_id, resolved.spec.name, execution)
    write_json_file(result_file_path(path), result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Run one request file and return 0 on success, 1 on any failure."""
    parser = argparse.ArgumentParser(
        prog="python -m kivi.elevated_helper",
        description="Execute one allowlisted Kivi admin operation (elevated).",
    )
    parser.add_argument("--request", required=True, help="path to the broker request JSON")
    args = parser.parse_args(argv)
    try:
        result = execute_request(Path(args.request))
    except Exception as exc:  # noqa: BLE001 - process boundary must return 1
        print(f"kivi elevated helper: {exc}", file=sys.stderr)
        return 1
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
