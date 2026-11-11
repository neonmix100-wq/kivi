"""Repository index: a read-only map of the text files inside the workspace.

The index is deliberately conservative. It walks the workspace without
following links, skips dependency, cache and private directories, and ignores
binary, oversized and secret-looking files. Nothing in this module writes to
disk; the only consumer is :class:`RepoIndexTool`, which exposes summary,
search and symbol lookup to the model.

Search is a case-insensitive substring match over workspace-relative paths and
file contents, while symbols are found with a small line-oriented regex. That
is enough for orientation and cheap enough to run on every request. A real
parser or database can replace this behind the same API later.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kivi.tools.base import Preview, ToolContext, ToolError, ToolResult

# Directory names that are never indexed, at any depth.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        "data",
        "node_modules",
        "private",
        "voice-dataset",
    }
)

# Suffixes that almost always contain binary data; reading them is pointless.
_BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".7z",
        ".avi",
        ".bin",
        ".bmp",
        ".bz2",
        ".class",
        ".db",
        ".dll",
        ".docx",
        ".dylib",
        ".exe",
        ".flac",
        ".gif",
        ".gz",
        ".ico",
        ".jpeg",
        ".jpg",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".npy",
        ".npz",
        ".ogg",
        ".onnx",
        ".otf",
        ".parquet",
        ".pdf",
        ".pkl",
        ".png",
        ".pptx",
        ".pt",
        ".pyc",
        ".pyd",
        ".rar",
        ".safetensors",
        ".so",
        ".sqlite",
        ".sqlite3",
        ".tar",
        ".ttf",
        ".wav",
        ".webp",
        ".woff",
        ".woff2",
        ".xlsx",
        ".zip",
    }
)

# Files that may carry secrets even though the workspace is trusted.
_SECRET_PATTERNS: tuple[str, ...] = (
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

# Suffix -> language name. Unknown text extensions fall back to "text".
_LANGUAGES: dict[str, str] = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".md": "markdown",
    ".php": "php",
    ".ps1": "shell",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".sh": "shell",
    ".sql": "sql",
    ".swift": "swift",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".txt": "text",
    ".yml": "yaml",
    ".yaml": "yaml",
}

_PYTHON_SYMBOLS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("class", re.compile(r"^\s*class\s+([A-Za-z_]\w*)\b")),
    ("def", re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\b")),
)

_JS_SYMBOLS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "class",
        re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)\b"),
    ),
    (
        "function",
        re.compile(
            r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+"
            r"([A-Za-z_$][\w$]*)\b"
        ),
    ),
)

_MAX_SYMBOLS = 1000
_SNIPPET_WIDTH = 160


@dataclass(frozen=True, slots=True)
class RepoFile:
    """One indexed text file."""

    path: str
    lines: int
    bytes: int
    language: str
    mtime: float


@dataclass(frozen=True, slots=True)
class RepoMatch:
    """A path or content hit from :meth:`RepoIndex.search`."""

    path: str
    kind: str
    line: int | None
    snippet: str


@dataclass(frozen=True, slots=True)
class RepoSymbol:
    """A class or function definition found by :meth:`RepoIndex.symbols`."""

    path: str
    line: int
    name: str
    kind: str


@dataclass(frozen=True, slots=True)
class RepoSummary:
    """Totals and per-language file counts for the current index."""

    files: int
    lines: int
    bytes: int
    languages: dict[str, int]


def language_for(path: Path) -> str:
    """Return a short language name for a file, or ``"text"`` when unknown."""
    return _LANGUAGES.get(path.suffix.casefold(), "text")


def _is_secret_name(name: str) -> bool:
    lowered = name.casefold()
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in _SECRET_PATTERNS)


def _snippet(line: str, needle: str) -> str:
    """Trim a matching line to a readable window around the match."""
    text = line.strip()
    if len(text) <= _SNIPPET_WIDTH:
        return text
    index = text.casefold().find(needle)
    if index < 0:
        return text[:_SNIPPET_WIDTH] + "…"
    start = max(0, index - _SNIPPET_WIDTH // 3)
    end = min(len(text), start + _SNIPPET_WIDTH)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


def _patterns_for(language: str) -> tuple[tuple[str, re.Pattern[str]], ...]:
    if language == "python":
        return _PYTHON_SYMBOLS
    if language in ("javascript", "typescript"):
        return _JS_SYMBOLS
    return ()


class RepoIndex:
    """Scan and query text files under a single workspace root.

    ``max_files`` bounds the number of indexed text files and ``max_bytes``
    bounds one file. Both are safety valves, not tuning knobs: once a cap is
    hit the index is partial and :attr:`truncated` is set.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        max_files: int = 5000,
        max_bytes: int = 2_000_000,
    ) -> None:
        if max_files < 1:
            raise ValueError("max_files must be positive")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.workspace = Path(os.path.realpath(workspace))
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.truncated = False
        self._files: list[RepoFile] | None = None
        self._texts: dict[str, str] = {}

    # -- scanning --------------------------------------------------------
    def _walk(self) -> Iterator[Path]:
        """Yield regular files, skipping links and ignored directories."""
        stack: list[Path] = [self.workspace]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as iterator:
                    entries = sorted(iterator, key=lambda item: item.name.casefold())
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    name = entry.name.casefold()
                    # A worktree or submodule has a ``.git`` file, not a directory.
                    if name == ".git":
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if name in _SKIP_DIRS:
                            continue
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        yield Path(entry.path)
                except OSError:
                    continue

    def _read_text(self, path: Path) -> str | None:
        """Return decoded UTF-8 text, or ``None`` when the file must be skipped."""
        if path.suffix.casefold() in _BINARY_EXTENSIONS:
            return None
        if _is_secret_name(path.name):
            return None
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size > self.max_bytes:
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if b"\x00" in data:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def index(self) -> list[RepoFile]:
        """Rescan the workspace and return the refreshed file rows."""
        files: list[RepoFile] = []
        texts: dict[str, str] = {}
        self.truncated = False
        for path in self._walk():
            text = self._read_text(path)
            if text is None:
                continue
            if len(files) >= self.max_files:
                self.truncated = True
                break
            try:
                relative = path.relative_to(self.workspace).as_posix()
                mtime = path.stat().st_mtime
            except OSError:
                continue
            except ValueError:
                continue
            newline_count = text.count("\n")
            lines = newline_count + (1 if text and not text.endswith("\n") else 0)
            files.append(
                RepoFile(
                    path=relative,
                    lines=lines,
                    bytes=len(text.encode("utf-8")),
                    language=language_for(path),
                    mtime=mtime,
                )
            )
            texts[relative] = text
        files.sort(key=lambda item: item.path.casefold())
        self._files = files
        self._texts = texts
        return list(files)

    def _ensure_index(self) -> list[RepoFile]:
        if self._files is None:
            return self.index()
        return self._files

    # -- queries ---------------------------------------------------------
    def search(self, query: str, limit: int = 20) -> list[RepoMatch]:
        """Case-insensitive path and content search with one-line snippets."""
        needle = query.casefold()
        if not needle:
            raise ValueError("query must not be empty")
        if limit < 1:
            raise ValueError("limit must be positive")
        hits: list[RepoMatch] = []
        for file in self._ensure_index():
            if len(hits) >= limit:
                break
            if needle in file.path.casefold():
                hits.append(RepoMatch(file.path, "path", None, file.path))
                if len(hits) >= limit:
                    break
            text = self._texts.get(file.path, "")
            for number, line in enumerate(text.splitlines(), start=1):
                if needle in line.casefold():
                    hits.append(
                        RepoMatch(file.path, "content", number, _snippet(line, needle))
                    )
                    if len(hits) >= limit:
                        break
        return hits

    def symbols(self) -> list[RepoSymbol]:
        """Find Python and JavaScript/TypeScript class and function definitions."""
        found: list[RepoSymbol] = []
        for file in self._ensure_index():
            patterns = _patterns_for(file.language)
            if not patterns:
                continue
            text = self._texts.get(file.path, "")
            for number, line in enumerate(text.splitlines(), start=1):
                for kind, pattern in patterns:
                    match = pattern.match(line)
                    if match is None:
                        continue
                    found.append(RepoSymbol(file.path, number, match.group(1), kind))
                    break
                if len(found) >= _MAX_SYMBOLS:
                    return found
        return found

    def summary(self) -> RepoSummary:
        """Return totals and file counts grouped by language."""
        files = self._ensure_index()
        languages: dict[str, int] = {}
        for file in files:
            languages[file.language] = languages.get(file.language, 0) + 1
        return RepoSummary(
            files=len(files),
            lines=sum(item.lines for item in files),
            bytes=sum(item.bytes for item in files),
            languages=dict(sorted(languages.items())),
        )


class RepoIndexArgs(BaseModel):
    """Arguments for :class:`RepoIndexTool`."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["summary", "search", "symbols"] = "summary"
    query: str | None = Field(
        default=None,
        max_length=500,
        description="Search text; required for action=search.",
    )
    limit: int = Field(default=20, ge=1, le=100)


class RepoIndexTool:
    """Expose the read-only repository index to the model."""

    name = "repo.index"
    description = "Summarise, search or list symbols in workspace text files."
    risk = "R0"
    capabilities = ("repo.read",)
    args_model = RepoIndexArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, RepoIndexArgs)
        details: dict[str, object] = {"action": args.action, "limit": args.limit}
        if args.query is not None:
            details["query"] = args.query
        return Preview(f"repo.{args.action}", details, reversible=True)

    def _render_summary(self, index: RepoIndex, summary: RepoSummary) -> str:
        lines = [
            f"files: {summary.files}",
            f"lines: {summary.lines}",
            f"bytes: {summary.bytes}",
        ]
        lines.extend(f"{language}: {count}" for language, count in summary.languages.items())
        if index.truncated:
            lines.append("note: file cap reached; index is partial")
        return "\n".join(lines)

    def _render_search(self, index: RepoIndex, query: str, limit: int) -> str:
        hits = index.search(query, limit=limit)
        if not hits:
            return f"no matches for {query!r}"
        lines: list[str] = []
        for hit in hits:
            if hit.kind == "path":
                lines.append(f"{hit.path} (path match)")
            else:
                lines.append(f"{hit.path}:{hit.line}: {hit.snippet}")
        return "\n".join(lines)

    def _render_symbols(self, index: RepoIndex, limit: int) -> str:
        symbols = index.symbols()[:limit]
        if not symbols:
            return "no symbols found"
        return "\n".join(
            f"{item.path}:{item.line}: {item.kind} {item.name}" for item in symbols
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, RepoIndexArgs)
        if not ctx.workspace.is_dir():
            raise ToolError(f"workspace is not a directory: {ctx.workspace}")
        index = RepoIndex(ctx.workspace)
        count = 0
        if args.action == "summary":
            summary = index.summary()
            output = self._render_summary(index, summary)
            count = summary.files
        elif args.action == "search":
            query = (args.query or "").strip()
            if not query:
                raise ToolError("query is required for action=search")
            output = self._render_search(index, query, args.limit)
            count = 0 if output.startswith("no matches") else output.count("\n") + 1
        else:
            output = self._render_symbols(index, args.limit)
            count = 0 if output == "no symbols found" else output.count("\n") + 1

        limit = ctx.limits.max_output_chars
        truncated = len(output) > limit
        if truncated:
            output = output[:limit]
        return ToolResult(
            ok=True,
            output=output,
            meta={
                "action": args.action,
                "count": count,
                "index_truncated": index.truncated,
            },
            truncated=truncated,
            trust="untrusted:file",
        )


__all__ = [
    "RepoFile",
    "RepoIndex",
    "RepoIndexArgs",
    "RepoIndexTool",
    "RepoMatch",
    "RepoSummary",
    "RepoSymbol",
    "language_for",
]
