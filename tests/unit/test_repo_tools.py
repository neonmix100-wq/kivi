from __future__ import annotations

from pathlib import Path

import pytest

from kivi.kernel.db import Database
from kivi.safety.permissions import Grant, PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools.base import Limits, ToolResult
from kivi.tools.executor import ToolExecutor
from kivi.tools.registry import ToolRegistry
from kivi.tools.repo import RepoIndex, RepoIndexTool, language_for


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
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "Docs").mkdir()
    (root / "src" / "app.py").write_text(
        "class Greeter:\n"
        "    def hello(self):\n"
        '        return "hello"\n'
        "\n"
        "async def fetch():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (root / "src" / "helper.py").write_text(
        "def helper():\n    return 1\n",
        encoding="utf-8",
    )
    (root / "src" / "ui.ts").write_text(
        "export function render(): void {}\nexport class Panel {}\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("# Demo\n\nText\n", encoding="utf-8")
    (root / "Docs" / "Guide.md").write_text("The FooBar helper.\nUse it.\n", encoding="utf-8")

    # Skipped by name, by suffix, by size or because the bytes are not UTF-8.
    (root / "data").mkdir()
    (root / "data" / "secret.txt").write_text("hidden", encoding="utf-8")
    (root / "private").mkdir()
    (root / "private" / "note.md").write_text("hidden", encoding="utf-8")
    (root / "voice-dataset").mkdir()
    (root / "voice-dataset" / "rec.txt").write_text("hidden", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("hidden", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "lib.py").write_text("hidden", encoding="utf-8")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("hidden", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "cached.py").write_text("hidden", encoding="utf-8")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    (root / "src" / "blob.dat").write_bytes(b"\x00binary")
    (root / "src" / "bad.txt").write_bytes(b"\xff\xfe")
    (root / "big.txt").write_text("x" * 2_100_000, encoding="utf-8")
    return root


@pytest.fixture()
def env(tmp_path: Path, workspace: Path) -> ToolEnv:
    db = Database(tmp_path / "kivi.db")
    zones = ProtectedZones(workspace)
    engine = PermissionEngine(db, workspace=str(workspace))
    grant = engine.issue("repo-task", ["repo.read"])
    registry = ToolRegistry()
    registry.register(RepoIndexTool())
    executor = ToolExecutor(registry, db, engine, zones, workspace=workspace)
    tool_env = ToolEnv(workspace, db, executor, grant)
    yield tool_env
    db.close()


def test_language_for_known_and_unknown_suffixes() -> None:
    assert language_for(Path("x.py")) == "python"
    assert language_for(Path("x.ts")) == "typescript"
    assert language_for(Path("x.unknown")) == "text"


def test_index_scans_only_safe_text_files(workspace: Path) -> None:
    index = RepoIndex(workspace, max_bytes=100)
    files = index.index()
    paths = [item.path for item in files]
    assert paths == [
        "Docs/Guide.md",
        "README.md",
        "src/app.py",
        "src/helper.py",
        "src/ui.ts",
    ]
    assert index.truncated is False
    by_path = {item.path: item for item in files}
    app = by_path["src/app.py"]
    assert app.language == "python"
    assert app.lines == 6
    assert app.bytes > 0
    assert app.mtime > 0
    assert by_path["src/ui.ts"].language == "typescript"
    assert by_path["README.md"].language == "markdown"


def test_index_refreshes_after_a_new_file(workspace: Path) -> None:
    index = RepoIndex(workspace)
    index.index()
    (workspace / "src" / "new.md").write_text("new\n", encoding="utf-8")
    paths = [item.path for item in index.index()]
    assert "src/new.md" in paths


def test_index_reports_the_file_cap(workspace: Path) -> None:
    index = RepoIndex(workspace, max_files=2)
    files = index.index()
    assert len(files) == 2
    assert index.truncated is True


def test_search_matches_paths_and_content_case_insensitively(workspace: Path) -> None:
    index = RepoIndex(workspace)
    content_hits = index.search("foobar")
    assert any(
        hit.kind == "content" and hit.path == "Docs/Guide.md" and hit.line == 1
        for hit in content_hits
    )
    assert any("FooBar" in hit.snippet for hit in content_hits)
    path_hits = index.search("guide")
    assert path_hits[0].kind == "path"
    assert path_hits[0].path == "Docs/Guide.md"
    assert path_hits[0].line is None


def test_search_honours_the_limit_and_rejects_empty_queries(workspace: Path) -> None:
    index = RepoIndex(workspace)
    assert len(index.search("e", limit=2)) == 2
    with pytest.raises(ValueError, match="query"):
        index.search("")


def test_search_snippet_is_trimmed_around_the_match(workspace: Path) -> None:
    (workspace / "src" / "long.txt").write_text(
        "a" * 300 + "Needle" + "b" * 300,
        encoding="utf-8",
    )
    hits = RepoIndex(workspace).search("needle")
    assert len(hits) == 1
    assert "Needle" in hits[0].snippet
    assert len(hits[0].snippet) <= 162


def test_symbols_finds_python_and_typescript_definitions(workspace: Path) -> None:
    symbols = RepoIndex(workspace).symbols()
    rows = {(item.path, item.line, item.kind, item.name) for item in symbols}
    assert ("src/app.py", 1, "class", "Greeter") in rows
    assert ("src/app.py", 2, "def", "hello") in rows
    assert ("src/app.py", 5, "def", "fetch") in rows
    assert ("src/helper.py", 1, "def", "helper") in rows
    assert ("src/ui.ts", 1, "function", "render") in rows
    assert ("src/ui.ts", 2, "class", "Panel") in rows


def test_summary_counts_files_lines_and_languages(workspace: Path) -> None:
    summary = RepoIndex(workspace).summary()
    assert summary.files == 5
    assert summary.lines == 15
    assert summary.bytes > 0
    assert summary.languages == {"markdown": 2, "python": 2, "typescript": 1}


def test_tool_summary(env: ToolEnv) -> None:
    result = env.run("repo.index", {"action": "summary"})
    assert result.ok
    assert "files: 5" in result.output
    assert "python: 2" in result.output
    assert result.meta["action"] == "summary"
    assert result.trust == "untrusted:file"


def test_tool_search(env: ToolEnv) -> None:
    result = env.run("repo.index", {"action": "search", "query": "foobar"})
    assert result.ok
    assert "Docs/Guide.md:1:" in result.output
    assert "FooBar" in result.output
    assert result.meta["count"] == 1


def test_tool_symbols(env: ToolEnv) -> None:
    result = env.run("repo.index", {"action": "symbols"})
    assert result.ok
    assert "class Greeter" in result.output
    assert "function render" in result.output


def test_tool_search_limit(env: ToolEnv) -> None:
    result = env.run("repo.index", {"action": "search", "query": "e", "limit": 1})
    assert result.ok
    assert len(result.output.splitlines()) == 1


def test_tool_dry_run_does_not_scan_or_write(env: ToolEnv) -> None:
    result = env.run(
        "repo.index",
        {"action": "search", "query": "foobar"},
        dry_run=True,
    )
    assert result.ok
    assert '"action": "repo.search"' in result.output
    assert result.meta["dry_run"] is True


def test_tool_search_requires_a_query(env: ToolEnv) -> None:
    result = env.run("repo.index", {"action": "search"})
    assert not result.ok
    assert "query is required" in result.error


def test_tool_rejects_unknown_actions_and_extra_arguments(env: ToolEnv) -> None:
    unknown = env.run("repo.index", {"action": "delete"})
    assert not unknown.ok
    assert "invalid arguments" in unknown.error
    extra = env.run("repo.index", {"action": "summary", "path": "src"})
    assert not extra.ok
    assert "invalid arguments" in extra.error


def test_tool_output_is_truncated(env: ToolEnv) -> None:
    env.executor.limits = Limits(max_output_chars=10)
    result = env.run("repo.index", {"action": "summary"})
    assert result.ok
    assert len(result.output) == 10
    assert result.truncated is True


def test_tool_requires_repo_read_capability(env: ToolEnv) -> None:
    limited = env.engine.issue("limited", ["fs.read"])
    result = env.executor.run("repo.index", {"action": "summary"}, grant=limited)
    assert not result.ok
    assert "not granted" in result.error
