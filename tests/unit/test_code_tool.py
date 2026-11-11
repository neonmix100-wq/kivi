"""Unit tests for ``code.run`` and its best-effort sandbox.

The tests never touch the network and only run tiny snippets. Timeout coverage
uses a short ``time.sleep`` that the parent kills after one second.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kivi.kernel.db import Database
from kivi.safety.permissions import PermissionEngine
from kivi.safety.policy import PolicyEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools.base import Limits, ToolContext, ToolResult
from kivi.tools.code import CodeRunArgs, CodeRunTool
from kivi.tools.executor import ToolExecutor
from kivi.tools.registry import ToolRegistry


def make_context(workspace: Path, limits: Limits | None = None) -> ToolContext:
    """Build a context with a fresh R3 grant for ``code.run``."""
    zones = ProtectedZones(workspace)
    engine = PermissionEngine(workspace=str(workspace))
    grant = engine.issue("code-task", ["code.run"])
    return ToolContext(
        workspace=workspace,
        zones=zones,
        policy=PolicyEngine(),
        grant=grant,
        limits=limits or Limits(),
    )


@pytest.fixture()
def context(tmp_path: Path) -> ToolContext:
    workspace = tmp_path / "workspace"
    (workspace / "data").mkdir(parents=True)
    return make_context(workspace)


def run_tool(context: ToolContext, code: str, **kwargs: object) -> ToolResult:
    return CodeRunTool().run(CodeRunArgs(code=code, **kwargs), context)


def test_tool_contract() -> None:
    tool = CodeRunTool()
    assert tool.name == "code.run"
    assert tool.risk == "R3"
    assert tool.capabilities == ("code.run",)
    assert tool.args_model is CodeRunArgs


def test_successful_print(context: ToolContext) -> None:
    code = "print('hello sandbox')"
    result = run_tool(context, code)
    assert result.ok
    assert "hello sandbox" in result.output
    assert result.error == ""
    assert result.trust == "untrusted:code"
    assert result.meta["returncode"] == 0
    assert result.meta["timed_out"] is False
    assert result.meta["sandboxed"] is True
    assert isinstance(result.meta["job_object"], bool)
    assert result.meta["bytes"] == len(code.encode("utf-8"))


def test_nonzero_exit_code(context: ToolContext) -> None:
    result = run_tool(context, "raise SystemExit(3)")
    assert not result.ok
    assert result.meta["returncode"] == 3
    assert "exit code 3" in result.error


def test_timeout_kills_the_child(context: ToolContext) -> None:
    result = run_tool(context, "import time\ntime.sleep(5)", timeout_s=1)
    assert not result.ok
    assert result.meta["timed_out"] is True
    assert "timed out" in result.error


def test_write_inside_workspace_is_allowed(context: ToolContext) -> None:
    result = run_tool(context, "open('inside.txt', 'w').write('ok')")
    assert result.ok
    assert (context.workspace / "inside.txt").read_text(encoding="utf-8") == "ok"


def test_write_outside_workspace_is_denied(context: ToolContext, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    code = f"open({str(outside)!r}, 'w').write('nope')"
    result = run_tool(context, code)
    assert not result.ok
    assert "sandbox" in result.error
    assert "outside the workspace" in result.error
    assert outside.exists() is False


def test_socket_is_denied(context: ToolContext) -> None:
    result = run_tool(context, "import socket\nsocket.socket()")
    assert not result.ok
    assert "network" in result.error


def test_subprocess_is_denied(context: ToolContext) -> None:
    code = "import subprocess, sys\nsubprocess.run([sys.executable, '-c', 'print(1)'])"
    result = run_tool(context, code)
    assert not result.ok
    assert "process creation" in result.error


@pytest.mark.parametrize(
    "relative",
    [
        ".env",
        "private/secret.txt",
        ".git/config",
        "certs/server.pem",
        "keys/id_rsa.key",
    ],
)
def test_sensitive_read_is_denied(context: ToolContext, relative: str) -> None:
    target = context.workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("CANARY=1", encoding="utf-8")
    result = run_tool(context, f"open({relative!r}).read()")
    assert not result.ok
    assert "sensitive" in result.error
    assert "CANARY" not in result.output
    assert "CANARY" not in result.error


def test_sensitive_listing_is_denied(context: ToolContext) -> None:
    (context.workspace / ".ssh").mkdir()
    result = run_tool(context, "import os\nos.listdir('.ssh')")
    assert not result.ok
    assert "sensitive" in result.error


def test_syntax_error_is_reported(context: ToolContext) -> None:
    result = run_tool(context, "def broken(:")
    assert not result.ok
    assert "SyntaxError" in result.error


def test_output_is_truncated(context: ToolContext) -> None:
    small = make_context(context.workspace, Limits(max_output_chars=100))
    result = run_tool(small, "print('x' * 1000)")
    assert result.ok
    assert result.truncated is True
    assert len(result.output) == 100


def test_dry_run_does_not_execute(context: ToolContext) -> None:
    tool = CodeRunTool()
    args = CodeRunArgs(code="open('dry.txt', 'w').write('x')")
    preview = tool.dry_run(args, context)
    assert preview.action == "code-run"
    assert preview.reversible is False
    assert preview.details["bytes"] == len(args.code.encode("utf-8"))
    assert preview.details["timeout_s"] == 10.0
    assert preview.details["memory_mb"] == 512
    assert any("execute" in warning for warning in preview.warnings)
    assert not (context.workspace / "dry.txt").exists()
    assert not (context.workspace / "data" / "sandbox").exists()


def test_args_are_passed_to_the_script(context: ToolContext) -> None:
    code = "import sys\nprint('|'.join(sys.argv[1:]))"
    result = run_tool(context, code, args=["one", "two"])
    assert result.ok
    assert result.output.strip() == "one|two"


def test_ctypes_and_native_modules_are_denied(context: ToolContext) -> None:
    result = run_tool(context, "import ctypes\nctypes.CDLL('kernel32')")
    assert not result.ok
    assert "sandbox" in result.error
    assert "ctypes" in result.error


def test_script_lives_under_the_sandbox_directory(context: ToolContext) -> None:
    code = "print('location')"
    result = run_tool(context, code)
    assert result.ok
    sandbox = Path(result.meta["sandbox_dir"])
    assert sandbox.is_relative_to(context.workspace / "data" / "sandbox")
    assert (sandbox / "main.py").read_text(encoding="utf-8") == code
    assert (sandbox / "bootstrap.py").is_file()


def test_project_src_is_not_on_sys_path(context: ToolContext) -> None:
    (context.workspace / "src").mkdir()
    result = run_tool(context, "import sys\nprint('\\n'.join(sys.path))")
    assert result.ok
    assert str(context.workspace / "src") not in result.output.splitlines()


def test_protected_repo_write_is_denied(context: ToolContext) -> None:
    target = context.workspace / "src" / "kivi" / "safety" / "escape.py"
    target.parent.mkdir(parents=True)
    result = run_tool(context, f"open({str(target)!r}, 'w').write('x')")
    assert not result.ok
    assert "protected path" in result.output
    assert not target.exists()


def test_executor_requires_approval_for_r3(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "data").mkdir(parents=True)
    db = Database(tmp_path / "kivi.db")
    engine = PermissionEngine(db, workspace=str(workspace))
    grant = engine.issue("t1", ["code.run"])
    registry = ToolRegistry()
    registry.register(CodeRunTool())
    executor = ToolExecutor(
        registry,
        db,
        engine,
        ProtectedZones(workspace),
        workspace=workspace,
    )
    try:
        denied = executor.run("code.run", {"code": "print(1)"}, grant=grant)
        preview = executor.run("code.run", {"code": "print(1)"}, grant=grant, dry_run=True)
        allowed = executor.run(
            "code.run",
            {"code": "print(1)"},
            grant=grant,
            approved=True,
        )
    finally:
        db.close()
    assert not denied.ok
    assert "confirmation required" in denied.error
    assert preview.ok
    assert '"action": "code-run"' in preview.output
    assert allowed.ok
    assert "1" in allowed.output
