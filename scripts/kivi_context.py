"""Small, dependency-free context helper for the Kivi knowledge base.

OpenCode skills call this instead of reading 200+ KB of docs. Commands print
compact text to stdout and never mutate the repository.

    python scripts/kivi_context.py index
    python scripts/kivi_context.py search "lease manager"
    python scripts/kivi_context.py section ARCHITECTURE "Model Gateway"
    python scripts/kivi_context.py stage
    python scripts/kivi_context.py verify --path tests/unit/test_gateway.py
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
GENERATED = {"_INDEX.md", "_CODE-MAP.md"}
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _read(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _sources() -> list[Path]:
    paths = [ROOT / "README.md", ROOT / "AGENTS.md"]
    paths.extend(sorted(DOCS.glob("*.md"), key=lambda item: item.name.lower()))
    paths.extend(sorted((ROOT / "config").glob("*.yaml"), key=lambda item: item.name.lower()))
    return [path for path in paths if path.exists() and path.name not in GENERATED]


def _heading_at(lines: list[str]) -> list[str]:
    """Return the nearest heading for every line (1-based index maps to line-1)."""
    current = ""
    result: list[str] = []
    for line in lines:
        match = HEADING_RE.match(line)
        if match:
            current = match.group(2).strip()
        result.append(current)
    return result


def cmd_index(args: argparse.Namespace) -> int:
    path = DOCS / "_INDEX.md"
    if not path.exists():
        print("docs/_INDEX.md is missing; run scripts/build_knowledge_index.py", file=sys.stderr)
        return 1
    print(path.read_text(encoding="utf-8"))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    query = args.query.casefold().strip()
    if not query:
        print("empty query", file=sys.stderr)
        return 2
    hits = 0
    for path in _sources():
        lines = _read(path)
        headings = _heading_at(lines)
        for number, line in enumerate(lines, 1):
            if query not in line.casefold():
                continue
            heading = headings[number - 1]
            prefix = f"{path.relative_to(ROOT).as_posix()}:{number}"
            if heading:
                prefix += f" [{heading}]"
            snippet = line.strip()
            if len(snippet) > 240:
                snippet = snippet[:237] + "…"
            print(f"{prefix} {snippet}")
            hits += 1
            if hits >= args.limit:
                print(f"\n(limit {args.limit} reached; refine the query)")
                return 0
    if hits == 0:
        print(f"no matches for {args.query!r}", file=sys.stderr)
        return 1
    return 0


def _resolve_file(name: str) -> Path | None:
    candidate = Path(name)
    if not candidate.suffix:
        candidate = candidate.with_suffix(".md")
    if candidate.is_absolute():
        path = candidate.resolve()
    else:
        path = (ROOT / candidate).resolve()
        if not path.exists():
            path = (DOCS / candidate).resolve()
    try:
        path.relative_to(ROOT)
    except ValueError:
        return None
    return path if path.is_file() else None


def cmd_section(args: argparse.Namespace) -> int:
    path = _resolve_file(args.file)
    if path is None:
        print(f"file not found inside the project: {args.file}", file=sys.stderr)
        return 2
    lines = _read(path)
    wanted = args.heading.casefold()
    start = level = None
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if match and wanted in match.group(2).casefold():
            start = index
            level = len(match.group(1))
            break
    if start is None:
        print(f"heading {args.heading!r} not found in {path.name}", file=sys.stderr)
        return 1

    end = len(lines)
    in_fence = False
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        match = HEADING_RE.match(line) if not in_fence else None
        if match and len(match.group(1)) <= level:
            end = index
            break

    text = "\n".join(lines[start:end]).rstrip()
    if len(text) > args.max_chars:
        text = (
            text[: args.max_chars].rstrip()
            + "\n\n… (section truncated; read the file for the rest)"
        )
    print(f"# {path.relative_to(ROOT).as_posix()} lines {start + 1}-{end}\n")
    print(text)
    return 0


def cmd_stage(args: argparse.Namespace) -> int:
    path = ROOT / "docs" / "ROADMAP.md"
    if not path.exists():
        print("docs/ROADMAP.md not found", file=sys.stderr)
        return 1
    lines = _read(path)
    starts = [index for index, line in enumerate(lines) if line.startswith("## ")]
    if not starts:
        print("no stage sections found", file=sys.stderr)
        return 1
    starts.append(len(lines))
    for position in range(len(starts) - 1):
        block = lines[starts[position] : starts[position + 1]]
        if any(line.lstrip().startswith("- [ ]") for line in block):
            header = block[0]
            print(header)
            print()
            for line in block[1:]:
                stripped = line.strip()
                if stripped.startswith("- [ ]") or stripped.startswith("**Приёмка"):
                    print(line)
            return 0
    print("all roadmap checkboxes are closed")
    return 0


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )


def cmd_verify(args: argparse.Namespace) -> int:
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = ROOT / ".venv" / "bin" / "python"
    executable = str(python) if python.exists() else sys.executable

    results: list[tuple[str, subprocess.CompletedProcess[str]]] = []
    if not args.no_lint:
        results.append(
            (
                "ruff",
                _run(
                    [
                        executable,
                        "-m",
                        "ruff",
                        "check",
                        "src",
                        "tests",
                        "--output-format",
                        "concise",
                    ],
                    args.timeout,
                ),
            )
        )
    if not args.lint_only:
        target = args.path or "tests"
        results.append(
            ("pytest", _run([executable, "-m", "pytest", "-q", "--tb=short", target], args.timeout))
        )

    logs = ROOT / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    failed = False
    chunks: list[str] = []
    for name, result in results:
        ok = result.returncode == 0
        failed = failed or not ok
        body = (result.stdout + result.stderr).strip()
        chunks.append(f"===== {name}: {'PASS' if ok else 'FAIL'} (exit {result.returncode}) =====")
        chunks.append(body[-4000:] if body else "(no output)")
    report = "\n".join(chunks)
    (logs / f"verify-{stamp}.log").write_text(report, encoding="utf-8", newline="\n")
    print(f"verify: {'FAIL' if failed else 'PASS'} (log data/logs/verify-{stamp}.log)")
    print(report)
    return 1 if failed else 0


def cmd_map(args: argparse.Namespace) -> int:
    path = DOCS / "_CODE-MAP.md"
    if not path.exists():
        print("docs/_CODE-MAP.md is missing; run scripts/build_knowledge_index.py", file=sys.stderr)
        return 1
    print(path.read_text(encoding="utf-8"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kivi knowledge/context helper")
    sub = parser.add_subparsers(dest="command", required=True)

    index = sub.add_parser("index", help="print the docs map of content")
    index.set_defaults(func=cmd_index)

    search = sub.add_parser("search", help="search docs, README, AGENTS and config")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=40)
    search.set_defaults(func=cmd_search)

    section = sub.add_parser("section", help="print one heading section")
    section.add_argument("file")
    section.add_argument("heading")
    section.add_argument("--max-chars", type=int, default=14000)
    section.set_defaults(func=cmd_section)

    stage = sub.add_parser("stage", help="show the current roadmap stage and open items")
    stage.set_defaults(func=cmd_stage)

    verify = sub.add_parser("verify", help="run ruff and pytest, save full log")
    verify.add_argument("--path", help="pytest target (default: tests)")
    verify.add_argument("--timeout", type=int, default=300)
    verify.add_argument("--lint-only", action="store_true")
    verify.add_argument("--no-lint", action="store_true")
    verify.set_defaults(func=cmd_verify)

    code_map = sub.add_parser("map", help="print the generated code map")
    code_map.set_defaults(func=cmd_map)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
