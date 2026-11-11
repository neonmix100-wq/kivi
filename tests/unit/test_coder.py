from __future__ import annotations

import json
import shlex
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from kivi.brain.coder import Coder, CoderResult
from kivi.kernel.db import Database
from kivi.safety.permissions import PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools import ToolExecutor, ToolRegistry, ToolResult, build_registry
from kivi.tools.base import Preview, ToolContext
from kivi.tools.fs import FsWriteTool


class Env:
    def __init__(
        self,
        workspace: Path,
        db: Database,
        registry: ToolRegistry,
        executor: ToolExecutor,
        permissions: PermissionEngine,
    ) -> None:
        self.workspace = workspace
        self.db = db
        self.registry = registry
        self.executor = executor
        self.permissions = permissions


class FakeLLM:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = responses
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        response = self.responses[index]
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def prompt(self) -> str:
        if not self.calls:
            return ""
        return "\n".join(message["content"] for message in self.calls[-1])


class FakeRunner:
    def __init__(self, outcomes: list[tuple[bool, str]]) -> None:
        self.outcomes = outcomes
        self.workspaces: list[Path] = []

    def __call__(self, workspace: Path) -> tuple[bool, str]:
        self.workspaces.append(workspace)
        index = min(len(self.workspaces) - 1, len(self.outcomes) - 1)
        return self.outcomes[index]


class CodeRunArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    timeout_s: float = 30.0


class FakeCodeRunTool:
    name = "code.run"
    description = "Fake sandbox runner used to prove the R3 approval path."
    risk = "R3"
    capabilities = ("code.run",)
    args_model = CodeRunArgs

    def __init__(self) -> None:
        self.calls: list[str] = []

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, CodeRunArgs)
        return Preview("run", {"code": args.code}, reversible=False)

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, CodeRunArgs)
        self.calls.append(args.code)
        return ToolResult(ok=True, output="ran", trust="untrusted:tool")


def generation(
    *,
    files: tuple[tuple[str, str], ...] = (),
    test_files: tuple[tuple[str, str], ...] = (),
    notes: str = "",
) -> str:
    return json.dumps(
        {
            "files": [{"path": path, "content": content} for path, content in files],
            "test_files": [
                {"path": path, "content": content} for path, content in test_files
            ],
            "notes": notes,
        },
        ensure_ascii=False,
    )


def audit_rows(env: Env) -> list[dict]:
    return env.db.recent_audit(50)


def code_env(env: Env) -> tuple[ToolRegistry, ToolExecutor, FakeCodeRunTool]:
    """Build an isolated registry so the test does not depend on build_registry."""
    registry = ToolRegistry()
    registry.register(FsWriteTool())
    tool = FakeCodeRunTool()
    registry.register(tool)
    zones = ProtectedZones(env.workspace)
    executor = ToolExecutor(registry, env.db, env.permissions, zones, workspace=env.workspace)
    return registry, executor, tool


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[Env]:
    root = tmp_path / "workspace"
    root.mkdir()
    db = Database(tmp_path / "kivi.db")
    zones = ProtectedZones(root)
    permissions = PermissionEngine(db, workspace=str(root))
    registry = build_registry()
    executor = ToolExecutor(registry, db, permissions, zones, workspace=root)
    yield Env(root, db, registry, executor, permissions)
    db.close()


# -- successful loop -----------------------------------------------------------


def test_run_writes_files_through_executor_and_passes(env: Env) -> None:
    llm = FakeLLM(
        [
            generation(
                files=[("calculator.py", "def add(a, b):\n    return a + b\n")],
                test_files=[
                    (
                        "test_calculator.py",
                        "from calculator import add\n\n\ndef test_add():\n"
                        "    assert add(1, 2) == 3\n",
                    )
                ],
            )
        ]
    )
    runner = FakeRunner([(True, "1 passed")])
    coder = Coder(llm, env.registry, env.executor, env.permissions, test_runner=runner)

    result = coder.run("Build an add function", task_id="t-coder-ok")

    assert result.ok is True
    assert result.attempts == 1
    assert result.stopped_reason == "passed"
    assert result.test_output == "1 passed"
    assert result.files == ("calculator.py", "test_calculator.py")
    assert result.last_error == ""
    assert (env.workspace / "calculator.py").read_text(encoding="utf-8").startswith("def add")
    assert (env.workspace / "test_calculator.py").is_file()
    assert runner.workspaces == [env.workspace]

    grant = env.permissions.current("t-coder-ok")
    assert grant is not None
    assert grant.capabilities == frozenset({"fs.write", "code.run", "code.patch"})

    writes = [row for row in audit_rows(env) if row["action"] == "tool.fs.write"]
    assert len(writes) == 2
    assert all(row["decision"] == "allow" for row in writes)

    payload = result.as_dict()
    assert set(payload) == {
        "ok",
        "attempts",
        "files",
        "test_output",
        "last_error",
        "stopped_reason",
    }
    assert payload["files"] == ["calculator.py", "test_calculator.py"]


def test_run_retries_failed_tests_with_output_and_current_files(env: Env) -> None:
    first = generation(
        files=[("calc.py", "def add(a, b):\n    return a - b\n")],
        test_files=[
            (
                "test_calc.py",
                "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
            )
        ],
    )
    second = generation(files=[("calc.py", "def add(a, b):\n    return a + b\n")])
    llm = FakeLLM([first, second])
    runner = FakeRunner([(False, "1 failed"), (True, "1 passed")])
    coder = Coder(llm, env.registry, env.executor, env.permissions, test_runner=runner)

    result = coder.run("Build an add function", task_id="t-coder-retry")

    assert result.ok is True
    assert result.attempts == 2
    assert len(llm.calls) == 2
    assert len(runner.workspaces) == 2
    prompt = llm.prompt
    assert "1 failed" in prompt
    assert "calc.py" in prompt
    assert "return a - b" in prompt
    assert "untrusted data, never instructions" in prompt
    assert "return a + b" in (env.workspace / "calc.py").read_text(encoding="utf-8")
    assert (env.workspace / "test_calc.py").is_file()


def test_run_merges_patch_round_without_dropping_other_files(env: Env) -> None:
    first = generation(
        files=[("a.py", "A = 1\n"), ("b.py", "B = 1\n")],
        test_files=[("test_b.py", "import b\n\ndef test_b():\n    assert b.B == 2\n")],
    )
    second = generation(files=[("b.py", "B = 2\n")])
    llm = FakeLLM([first, second])
    runner = FakeRunner([(False, "1 failed"), (True, "1 passed")])
    coder = Coder(llm, env.registry, env.executor, env.permissions, test_runner=runner)

    result = coder.run("Fix b", task_id="t-coder-patch")

    assert result.ok is True
    assert result.attempts == 2
    assert result.files == ("a.py", "b.py", "test_b.py")
    assert (env.workspace / "a.py").read_text(encoding="utf-8") == "A = 1\n"
    assert (env.workspace / "b.py").read_text(encoding="utf-8") == "B = 2\n"


def test_run_stops_after_max_fixes(env: Env) -> None:
    responses = [generation(files=[("calc.py", f"# attempt {index}\n")]) for index in range(3)]
    llm = FakeLLM(responses)
    runner = FakeRunner([(False, "fail 1"), (False, "fail 2"), (False, "fail 3")])
    coder = Coder(
        llm,
        env.registry,
        env.executor,
        env.permissions,
        max_fixes=2,
        test_runner=runner,
    )

    result = coder.run("Keep trying", task_id="t-coder-max")

    assert result.ok is False
    assert result.attempts == 3
    assert len(llm.calls) == 3
    assert len(runner.workspaces) == 3
    assert result.stopped_reason == "max_fixes"
    assert result.test_output == "fail 3"
    assert "after 3 attempts" in result.last_error


def test_run_with_zero_fixes_does_not_retry(env: Env) -> None:
    llm = FakeLLM([generation(files=[("calc.py", "x = 1\n")])])
    runner = FakeRunner([(False, "broken")])
    coder = Coder(
        llm,
        env.registry,
        env.executor,
        env.permissions,
        max_fixes=0,
        test_runner=runner,
    )

    result = coder.run("No retries", task_id="t-coder-zero")

    assert result.ok is False
    assert result.attempts == 1
    assert len(llm.calls) == 1
    assert len(runner.workspaces) == 1
    assert result.stopped_reason == "max_fixes"


# -- path, size and payload validation ----------------------------------------


def test_run_rejects_parent_traversal(env: Env) -> None:
    llm = FakeLLM([generation(files=[("../escape.py", "print('escape')\n")])])
    runner = FakeRunner([(True, "should not run")])
    coder = Coder(llm, env.registry, env.executor, env.permissions, test_runner=runner)

    result = coder.run("Escape", task_id="t-coder-traversal")

    assert result.ok is False
    assert result.attempts == 1
    assert result.stopped_reason == "invalid_generation"
    assert ".." in result.last_error
    assert runner.workspaces == []
    assert not (env.workspace.parent / "escape.py").exists()


@pytest.mark.parametrize(
    "path",
    [
        "C:/evil.py",
        "C:evil.py",
        "/etc/passwd",
        "\\\\server\\share\\evil.py",
    ],
)
def test_run_rejects_absolute_and_drive_paths(env: Env, path: str) -> None:
    llm = FakeLLM([generation(files=[(path, "x = 1\n")])])
    runner = FakeRunner([(True, "should not run")])
    coder = Coder(llm, env.registry, env.executor, env.permissions, test_runner=runner)

    result = coder.run("Absolute path", task_id="t-coder-absolute")

    assert result.ok is False
    assert result.stopped_reason == "invalid_generation"
    assert "workspace-relative" in result.last_error
    assert runner.workspaces == []


def test_run_rejects_too_many_files(env: Env) -> None:
    files = tuple((f"file_{index}.py", "x = 1\n") for index in range(51))
    llm = FakeLLM([generation(files=files)])
    runner = FakeRunner([(True, "should not run")])
    coder = Coder(llm, env.registry, env.executor, env.permissions, test_runner=runner)

    result = coder.run("Too many files", task_id="t-coder-many")

    assert result.ok is False
    assert result.stopped_reason == "invalid_generation"
    assert "51" in result.last_error
    assert runner.workspaces == []
    assert not any(env.workspace.iterdir())


def test_run_rejects_oversized_generation(env: Env) -> None:
    content = "x" * (200 * 1024 + 1)
    llm = FakeLLM([generation(files=[("big.py", content)])])
    runner = FakeRunner([(True, "should not run")])
    coder = Coder(llm, env.registry, env.executor, env.permissions, test_runner=runner)

    result = coder.run("Too big", task_id="t-coder-big")

    assert result.ok is False
    assert result.stopped_reason == "invalid_generation"
    assert "exceed" in result.last_error
    assert runner.workspaces == []


def test_run_rejects_binary_content(env: Env) -> None:
    llm = FakeLLM([generation(files=[("binary.py", "x = 1\x00\n")])])
    runner = FakeRunner([(True, "should not run")])
    coder = Coder(llm, env.registry, env.executor, env.permissions, test_runner=runner)

    result = coder.run("Binary", task_id="t-coder-binary")

    assert result.ok is False
    assert result.stopped_reason == "invalid_generation"
    assert "binary" in result.last_error


def test_run_rejects_duplicate_paths(env: Env) -> None:
    llm = FakeLLM(
        [
            generation(
                files=[("same.py", "x = 1\n")],
                test_files=[("same.py", "x = 2\n")],
            )
        ]
    )
    runner = FakeRunner([(True, "should not run")])
    coder = Coder(llm, env.registry, env.executor, env.permissions, test_runner=runner)

    result = coder.run("Duplicate", task_id="t-coder-dup")

    assert result.ok is False
    assert result.stopped_reason == "invalid_generation"
    assert "duplicate" in result.last_error


def test_run_rejects_case_insensitive_duplicate_paths(env: Env) -> None:
    llm = FakeLLM([generation(files=[("A.py", "x = 1\n"), ("a.py", "x = 2\n")])])
    runner = FakeRunner([(True, "should not run")])
    coder = Coder(llm, env.registry, env.executor, env.permissions, test_runner=runner)

    result = coder.run("Case duplicate", task_id="t-coder-case")

    assert result.ok is False
    assert result.stopped_reason == "invalid_generation"
    assert "duplicate" in result.last_error
    assert runner.workspaces == []


def test_run_rejects_empty_generation(env: Env) -> None:
    llm = FakeLLM([generation()])
    runner = FakeRunner([(True, "should not run")])
    coder = Coder(llm, env.registry, env.executor, env.permissions, test_runner=runner)

    result = coder.run("Nothing", task_id="t-coder-empty")

    assert result.ok is False
    assert result.stopped_reason == "invalid_generation"
    assert "no files" in result.last_error


def test_run_rejects_invalid_json(env: Env) -> None:
    llm = FakeLLM(["definitely not json"])
    runner = FakeRunner([(True, "should not run")])
    coder = Coder(llm, env.registry, env.executor, env.permissions, test_runner=runner)

    result = coder.run("Bad answer", task_id="t-coder-json")

    assert result.ok is False
    assert result.attempts == 1
    assert result.stopped_reason == "invalid_json"
    assert "not valid JSON" in result.last_error
    assert runner.workspaces == []
    assert not any(env.workspace.iterdir())


def test_run_accepts_markdown_fence(env: Env) -> None:
    payload = generation(files=[("ok.py", "x = 1\n")])
    llm = FakeLLM([f"```json\n{payload}\n```"])
    runner = FakeRunner([(True, "1 passed")])
    coder = Coder(llm, env.registry, env.executor, env.permissions, test_runner=runner)

    result = coder.run("Fenced", task_id="t-coder-fence")

    assert result.ok is True
    assert (env.workspace / "ok.py").is_file()


# -- failures from dependencies ------------------------------------------------


def test_run_reports_llm_exception(env: Env) -> None:
    llm = FakeLLM([RuntimeError("model offline")])
    runner = FakeRunner([(True, "should not run")])
    coder = Coder(llm, env.registry, env.executor, env.permissions, test_runner=runner)

    result = coder.run("Model fails", task_id="t-coder-llm")

    assert result.ok is False
    assert result.attempts == 1
    assert result.stopped_reason == "llm_error"
    assert "RuntimeError" in result.last_error
    assert runner.workspaces == []


def test_run_reports_write_denied_by_zone_and_audits(env: Env) -> None:
    llm = FakeLLM([generation(files=[(".env", "SECRET=1\n")])])
    runner = FakeRunner([(True, "should not run")])
    coder = Coder(llm, env.registry, env.executor, env.permissions, test_runner=runner)

    result = coder.run("Write secrets", task_id="t-coder-zone")

    assert result.ok is False
    assert result.attempts == 1
    assert result.stopped_reason == "write_failed"
    assert "secret" in result.last_error.lower()
    assert runner.workspaces == []
    assert not (env.workspace / ".env").exists()
    denied = [
        row
        for row in audit_rows(env)
        if row["action"] == "tool.fs.write" and row["decision"] == "deny"
    ]
    assert len(denied) == 1


def test_run_reports_test_runner_crash(env: Env) -> None:
    def broken_runner(workspace: Path) -> tuple[bool, str]:
        raise RuntimeError("runner down")

    llm = FakeLLM([generation(files=[("calc.py", "x = 1\n")])])
    coder = Coder(llm, env.registry, env.executor, env.permissions, test_runner=broken_runner)

    result = coder.run("Runner crashes", task_id="t-coder-runner")

    assert result.ok is False
    assert result.attempts == 1
    assert result.stopped_reason == "test_runner_error"
    assert "RuntimeError" in result.last_error
    assert result.files == ("calc.py",)


def test_run_empty_goal_does_not_call_llm(env: Env) -> None:
    llm = FakeLLM([])
    coder = Coder(llm, env.registry, env.executor, env.permissions)

    result = coder.run("   ", task_id="t-coder-goal")

    assert result.ok is False
    assert result.attempts == 0
    assert result.stopped_reason == "invalid_goal"
    assert llm.calls == []


def test_run_redacts_secrets_from_prompt(env: Env) -> None:
    goal = "Build a token parser for sk-" + "A" * 20
    llm = FakeLLM([generation(files=[("tokens.py", "x = 1\n")])])
    runner = FakeRunner([(True, "1 passed")])
    coder = Coder(llm, env.registry, env.executor, env.permissions, test_runner=runner)

    result = coder.run(goal, task_id="t-coder-redact")

    assert result.ok is True
    prompt = llm.prompt
    assert "sk-" not in prompt
    assert "[REDACTED]" in prompt


def test_coder_requires_fs_write_tool(env: Env) -> None:
    with pytest.raises(ValueError, match="fs.write"):
        Coder(FakeLLM([]), ToolRegistry(), env.executor, env.permissions)


def test_coder_rejects_negative_max_fixes(env: Env) -> None:
    with pytest.raises(ValueError, match="max_fixes"):
        Coder(FakeLLM([]), env.registry, env.executor, env.permissions, max_fixes=-1)


# -- default subprocess runner -------------------------------------------------


def test_default_test_runner_runs_command(env: Env) -> None:
    command = shlex.join([sys.executable, "-c", "raise SystemExit(0)"])
    llm = FakeLLM([generation(files=[("ok.py", "x = 1\n")])])
    coder = Coder(
        llm,
        env.registry,
        env.executor,
        env.permissions,
        test_command=command,
    )

    result = coder.run("Default runner", task_id="t-coder-default")

    assert result.ok is True
    assert result.test_output == "tests passed"


def test_default_test_runner_reports_nonzero_exit(env: Env) -> None:
    command = shlex.join([sys.executable, "-c", "raise SystemExit(3)"])
    llm = FakeLLM([generation(files=[("ok.py", "x = 1\n")])])
    coder = Coder(
        llm,
        env.registry,
        env.executor,
        env.permissions,
        max_fixes=0,
        test_command=command,
    )

    result = coder.run("Default runner fails", task_id="t-coder-default-fail")

    assert result.ok is False
    assert result.stopped_reason == "max_fixes"
    assert "code 3" in result.test_output


# -- R3 code.run approval ------------------------------------------------------


def test_run_code_denied_without_approval(env: Env) -> None:
    registry, executor, tool = code_env(env)
    coder = Coder(FakeLLM([]), registry, executor, env.permissions)

    missing = coder.run_code("print(1)", task_id="t-code-missing")
    refused = coder.run_code(
        "print(1)",
        task_id="t-code-refused",
        approve=lambda name, args: False,
    )

    assert missing.ok is False
    assert "approval" in missing.error
    assert refused.ok is False
    assert "not approved" in refused.error
    assert tool.calls == []


def test_run_code_uses_executor_after_approval(env: Env) -> None:
    registry, executor, tool = code_env(env)
    coder = Coder(FakeLLM([]), registry, executor, env.permissions)
    seen: list[tuple[str, dict]] = []

    def approve(name: str, args: dict) -> bool:
        seen.append((name, args))
        return True

    result = coder.run_code("print(1)", task_id="t-code-ok", approve=approve)

    assert result.ok is True
    assert result.output == "ran"
    assert tool.calls == ["print(1)"]
    assert seen[0][0] == "code.run"
    assert seen[0][1]["code"] == "print(1)"
    allowed = [
        row
        for row in audit_rows(env)
        if row["action"] == "tool.code.run" and row["decision"] == "allow"
    ]
    assert len(allowed) == 1


def test_run_code_rejects_empty_code(env: Env) -> None:
    coder = Coder(FakeLLM([]), env.registry, env.executor, env.permissions)

    result = coder.run_code("  ", task_id="t-code-empty", approve=lambda name, args: True)

    assert result.ok is False
    assert "empty" in result.error


def test_coder_result_as_dict_roundtrip() -> None:
    result = CoderResult(
        ok=False,
        attempts=2,
        files=("a.py",),
        test_output="boom",
        last_error="failed",
        stopped_reason="max_fixes",
    )

    assert result.as_dict() == {
        "ok": False,
        "attempts": 2,
        "files": ["a.py"],
        "test_output": "boom",
        "last_error": "failed",
        "stopped_reason": "max_fixes",
    }
