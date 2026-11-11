from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from kivi.kernel.db import Database
from kivi.safety.permissions import Grant, PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools.base import ToolResult
from kivi.tools.executor import ToolExecutor
from kivi.tools.git import (
    GitBranchArgs,
    GitBranchTool,
    GitCommitArgs,
    GitCommitTool,
    GitDiffArgs,
    GitDiffTool,
    GitStatusArgs,
    GitStatusTool,
)
from kivi.tools.registry import ToolRegistry

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _git_ok(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    completed = _git(repo, *args)
    assert completed.returncode == 0, completed.stderr
    return completed


class ToolEnv:
    def __init__(self, workspace: Path, db: Database, executor: ToolExecutor, grant: Grant) -> None:
        self.workspace = workspace
        self.db = db
        self.executor = executor
        self.grant = grant
        self.engine = executor.permissions

    def run(self, name: str, args: dict | None = None, **kwargs) -> ToolResult:
        return self.executor.run(name, args or {}, grant=self.grant, **kwargs)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git_ok(root, "init", "-b", "main")
    for key, value in (
        ("user.name", "Kivi Test"),
        ("user.email", "kivi@example.test"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
        ("core.autocrlf", "false"),
        ("core.hooksPath", str(root / ".git" / "hooks")),
    ):
        _git_ok(root, "config", key, value)
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('one')\n", encoding="utf-8")
    _git_ok(root, "add", "--", "README.md", "src/app.py")
    _git_ok(root, "commit", "-m", "initial")
    return root


@pytest.fixture()
def env(tmp_path: Path, repo: Path) -> ToolEnv:
    db = Database(tmp_path / "kivi.db")
    zones = ProtectedZones(repo)
    engine = PermissionEngine(db, workspace=str(repo))
    grant = engine.issue("git-task", ["git.read", "git.branch", "git.commit"])
    registry = ToolRegistry()
    for tool in (GitStatusTool(), GitDiffTool(), GitBranchTool(), GitCommitTool()):
        registry.register(tool)
    executor = ToolExecutor(registry, db, engine, zones, workspace=repo)
    tool_env = ToolEnv(repo, db, executor, grant)
    yield tool_env
    db.close()


def test_tool_contract() -> None:
    expected = (
        (GitStatusTool, "git.status", "R0", ("git.read",)),
        (GitDiffTool, "git.diff", "R0", ("git.read",)),
        (GitBranchTool, "git.branch", "R2", ("git.branch",)),
        (GitCommitTool, "git.commit", "R3", ("git.commit",)),
    )
    for tool_class, name, risk, capabilities in expected:
        assert tool_class.name == name
        assert tool_class.risk == risk
        assert tool_class.capabilities == capabilities


def test_status_is_clean_and_then_shows_a_modification(env: ToolEnv) -> None:
    clean = env.run("git.status")
    assert clean.ok
    assert clean.output.startswith("## ")
    assert "main" in clean.output

    (env.workspace / "src" / "app.py").write_text("print('two')\n", encoding="utf-8")
    changed = env.run("git.status")
    assert changed.ok
    assert "M src/app.py" in changed.output


def test_diff_stat_and_focused_path(env: ToolEnv) -> None:
    (env.workspace / "src" / "app.py").write_text("print('two')\n", encoding="utf-8")
    stat = env.run("git.diff")
    assert stat.ok
    assert "src/app.py" in stat.output
    assert "1 file changed" in stat.output
    assert "--- git diff -- src/app.py ---" not in stat.output

    focused = env.run("git.diff", {"path": "src/app.py"})
    assert focused.ok
    assert "--- git diff -- src/app.py ---" in focused.output
    assert "print('two')" in focused.output
    assert focused.meta["path"] == "src/app.py"


def test_diff_rejects_paths_outside_or_control_directories(env: ToolEnv, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    cases = (
        (str(outside), "workspace-relative"),
        ("../outside.txt", "traversal"),
        (".git/config", "control"),
        (".env", "secret-.env"),
        (".", "root"),
    )
    for path, fragment in cases:
        result = env.run("git.diff", {"path": path})
        assert not result.ok
        assert fragment in result.error


def test_branch_current_branch(env: ToolEnv) -> None:
    result = env.run("git.branch", approved=True)
    assert result.ok
    assert "main" in result.output


def test_branch_create_requires_approval_then_switches(env: ToolEnv) -> None:
    denied = env.run("git.branch", {"action": "create", "name": "feature/x"})
    assert not denied.ok
    assert "confirmation required" in denied.error
    assert _git_ok(env.workspace, "branch", "--show-current").stdout.strip() == "main"

    created = env.run(
        "git.branch",
        {"action": "create", "name": "feature/x"},
        approved=True,
    )
    assert created.ok
    assert _git_ok(env.workspace, "branch", "--show-current").stdout.strip() == "feature/x"
    current = env.run("git.branch", approved=True)
    assert "feature/x" in current.output


def test_branch_dry_run_does_not_create(env: ToolEnv) -> None:
    result = env.run(
        "git.branch",
        {"action": "create", "name": "preview"},
        dry_run=True,
    )
    assert result.ok
    assert '"action": "git.branch.create"' in result.output
    assert _git(env.workspace, "branch", "--list", "preview").stdout.strip() == ""


@pytest.mark.parametrize(
    "name",
    ["-evil", "a..b", "a b", "x.lock", "feature/../x", "a/b/", "", ".hidden", "a@{b}", "a\\b"],
)
def test_branch_rejects_unsafe_names(env: ToolEnv, name: str) -> None:
    result = env.run(
        "git.branch",
        {"action": "create", "name": name},
        approved=True,
    )
    assert not result.ok


def test_branch_create_requires_a_name(env: ToolEnv) -> None:
    result = env.run("git.branch", {"action": "create"}, approved=True)
    assert not result.ok
    assert "must not be empty" in result.error


def test_commit_requires_approval_then_commits(env: ToolEnv) -> None:
    (env.workspace / "src" / "app.py").write_text("print('two')\n", encoding="utf-8")
    denied = env.run(
        "git.commit",
        {"message": "feat: update app", "paths": ["src/app.py"]},
    )
    assert not denied.ok
    assert "confirmation required" in denied.error
    assert "M src/app.py" in _git_ok(env.workspace, "status", "--porcelain").stdout

    committed = env.run(
        "git.commit",
        {"message": "feat: update app", "paths": ["src/app.py"]},
        approved=True,
    )
    assert committed.ok
    assert _git_ok(env.workspace, "log", "-1", "--format=%s").stdout.strip() == (
        "feat: update app"
    )
    assert _git_ok(env.workspace, "status", "--porcelain").stdout.strip() == ""


def test_commit_stages_only_the_listed_paths(env: ToolEnv) -> None:
    (env.workspace / "README.md").write_text("# changed\n", encoding="utf-8")
    (env.workspace / "src" / "app.py").write_text("print('two')\n", encoding="utf-8")
    (env.workspace / "src" / "other.py").write_text("x = 1\n", encoding="utf-8")

    result = env.run(
        "git.commit",
        {"message": "feat: app only", "paths": ["src/app.py"]},
        approved=True,
    )
    assert result.ok
    status = _git_ok(env.workspace, "status", "--porcelain").stdout
    assert "README.md" in status
    assert "src/other.py" in status
    assert "src/app.py" not in status


def test_commit_rejects_unsafe_paths(env: ToolEnv, tmp_path: Path) -> None:
    before = _git_ok(env.workspace, "rev-parse", "HEAD").stdout.strip()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    cases = (
        (str(outside), "workspace-relative"),
        ("../outside.txt", "traversal"),
        (".git/config", "control"),
        (".env", "secret-.env"),
        (".", "root"),
    )
    for path, fragment in cases:
        result = env.run(
            "git.commit",
            {"message": "feat: x", "paths": [path]},
            approved=True,
        )
        assert not result.ok
        assert fragment in result.error
    assert _git_ok(env.workspace, "rev-parse", "HEAD").stdout.strip() == before


def test_commit_treats_paths_as_literal(env: ToolEnv) -> None:
    (env.workspace / "notes[1].md").write_text("hello\n", encoding="utf-8")
    (env.workspace / ".env").write_text("SECRET=1", encoding="utf-8")
    result = env.run(
        "git.commit",
        {"message": "feat: literal path", "paths": ["notes[1].md"]},
        approved=True,
    )
    assert result.ok
    committed = _git_ok(
        env.workspace,
        "show",
        "--name-only",
        "--format=",
        "HEAD",
    ).stdout
    assert "notes[1].md" in committed
    assert ".env" not in committed
    assert "?? .env" in _git_ok(env.workspace, "status", "--porcelain").stdout


def test_commit_rejects_invalid_messages(env: ToolEnv) -> None:
    cases = (
        {"message": "", "paths": ["README.md"]},
        {"message": "   ", "paths": ["README.md"]},
        {"message": "x" * 201, "paths": ["README.md"]},
    )
    for args in cases:
        result = env.run("git.commit", args, approved=True)
        assert not result.ok


def test_commit_reports_a_missing_path(env: ToolEnv) -> None:
    result = env.run(
        "git.commit",
        {"message": "feat: x", "paths": ["missing.txt"]},
        approved=True,
    )
    assert not result.ok
    assert "git exited" in result.error


def test_commit_dry_run_does_not_stage_or_commit(env: ToolEnv) -> None:
    (env.workspace / "src" / "app.py").write_text("print('two')\n", encoding="utf-8")
    result = env.run(
        "git.commit",
        {"message": "feat: preview", "paths": ["src/app.py"]},
        dry_run=True,
    )
    assert result.ok
    assert '"action": "git.commit"' in result.output
    assert _git_ok(env.workspace, "log", "-1", "--format=%s").stdout.strip() == "initial"


def test_args_models_reject_arbitrary_fields() -> None:
    with pytest.raises(ValidationError):
        GitStatusArgs.model_validate({"argv": ["push"]})
    with pytest.raises(ValidationError):
        GitDiffArgs.model_validate({"path": "a", "argv": ["push"]})
    with pytest.raises(ValidationError):
        GitBranchArgs.model_validate({"action": "create", "name": "x", "force": True})
    with pytest.raises(ValidationError):
        GitCommitArgs.model_validate(
            {"message": "x", "paths": ["a"], "amend": True}
        )
    assert "argv" not in GitStatusArgs.model_fields
    assert "amend" not in GitCommitArgs.model_fields


def test_commit_denial_is_audited(env: ToolEnv) -> None:
    (env.workspace / "src" / "app.py").write_text("print('two')\n", encoding="utf-8")
    result = env.run(
        "git.commit",
        {"message": "feat: audit", "paths": ["src/app.py"]},
    )
    assert not result.ok
    decisions = [row["decision"] for row in env.db.recent_audit()]
    assert "confirm" in decisions
