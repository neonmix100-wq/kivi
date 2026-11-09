"""Build the Obsidian knowledge base for Kivi.

The vault is the ``docs/`` directory itself: no copies, no drift. This script
regenerates two navigation notes:

* ``docs/_INDEX.md``    - map of content: every doc, summary and section link.
* ``docs/_CODE-MAP.md`` - package/module map generated from docstrings.

Run it after changing docs or module docstrings:

    .venv\\Scripts\\python.exe scripts\\build_knowledge_index.py
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SRC = ROOT / "src" / "kivi"
TESTS = ROOT / "tests"

GENERATED = {"_INDEX.md", "_CODE-MAP.md"}
TITLE_RE = re.compile(r"^#\s+(.+?)\s*$")
H2_RE = re.compile(r"^##\s+(.+?)\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


@dataclass(slots=True)
class DocInfo:
    path: Path
    title: str
    summary: str
    headings: list[str] = field(default_factory=list)
    size_kb: float = 0.0


def _strip_markdown(text: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_>#]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def summarize(lines: list[str]) -> str:
    """Return the first real paragraph after the H1, capped for the index."""
    seen_title = False
    paragraph: list[str] = []
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped.startswith("# "):
            seen_title = True
            continue
        if not seen_title:
            continue
        if not stripped:
            if paragraph:
                break
            continue
        if stripped.startswith(("#", "-", "|", ">", "!")):
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    text = _strip_markdown(" ".join(paragraph))
    return text[:220] + ("…" if len(text) > 220 else "")


def read_doc(path: Path) -> DocInfo:
    lines = path.read_text(encoding="utf-8").splitlines()
    title = ""
    headings: list[str] = []
    in_fence = False
    for line in lines:
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not title and (match := TITLE_RE.match(line)):
            title = match.group(1)
        elif match := H2_RE.match(line):
            heading = _strip_markdown(match.group(1))
            if heading and not heading.startswith("_"):
                headings.append(heading)
    return DocInfo(
        path=path,
        title=title or path.stem,
        summary=summarize(lines),
        headings=headings,
        size_kb=path.stat().st_size / 1024,
    )


def collect_docs() -> list[DocInfo]:
    docs = []
    for path in sorted(DOCS.glob("*.md"), key=lambda item: item.name.lower()):
        if path.name in GENERATED or path.name.startswith("."):
            continue
        docs.append(read_doc(path))
    return docs


def render_index(docs: list[DocInfo]) -> str:
    lines = [
        "---",
        "tags: [kivi, moc, index]",
        f"updated: {date.today().isoformat()}",
        "generated: true",
        "---",
        "",
        "# База знаний Kivi",
        "",
        "> Карта содержимого. Сгенерирована `scripts/build_knowledge_index.py`;",
        "> не редактируй вручную. Агент читает этот файл вместо всей документации.",
        "",
        "## Как пользоваться",
        "",
        "- **Человеку:** открой `D:\\AI-assistent\\docs` как vault в Obsidian.",
        "- **Агенту:** `python scripts/kivi_context.py search <термин>` или",
        '  `python scripts/kivi_context.py section ARCHITECTURE "Model Gateway"`.',
        "- Граф Obsidian показывает связи между документами и разделами.",
        "",
        "## Документы",
        "",
        "| Документ | Размер | Разделов | О чём |",
        "| --- | ---: | ---: | --- |",
    ]
    for doc in docs:
        lines.append(
            f"| [[{doc.path.stem}]] | {doc.size_kb:.1f} KB | "
            f"{len(doc.headings)} | {doc.summary or '—'} |"
        )

    for doc in docs:
        lines.extend(["", f"## [[{doc.path.stem}|{doc.title}]]", ""])
        if doc.summary:
            lines.append(f"> {doc.summary}")
            lines.append("")
        if not doc.headings:
            lines.append("_Нет разделов второго уровня._")
            continue
        for heading in doc.headings:
            lines.append(f"- [[{doc.path.stem}#{heading}|{heading}]]")
    lines.append("")
    return "\n".join(lines)


def _module_summary(path: Path) -> tuple[str, int]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return "", 0
    doc = ast.get_docstring(tree) or ""
    first = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    return first, len(source.splitlines())


def render_code_map() -> str:
    lines = [
        "---",
        "tags: [kivi, code, moc]",
        f"updated: {date.today().isoformat()}",
        "generated: true",
        "---",
        "",
        "# Карта кода Kivi",
        "",
        "> Сгенерирована из docstring'ов модулей. Не редактируй вручную.",
        "",
    ]
    if not SRC.exists():
        lines.append("_Пакет `src/kivi` ещё не создан._")
        return "\n".join(lines) + "\n"

    modules = sorted(SRC.rglob("*.py"), key=lambda item: item.as_posix().lower())
    by_package: dict[str, list[Path]] = {}
    for module in modules:
        if "__pycache__" in module.parts:
            continue
        relative = module.relative_to(SRC)
        package = relative.parts[0] if len(relative.parts) > 1 else "(root)"
        by_package.setdefault(package, []).append(module)

    for package in sorted(by_package):
        lines.extend(
            [f"## {package}", "", "| Модуль | Строк | Ответственность |", "| --- | ---: | --- |"]
        )
        for module in by_package[package]:
            relative = module.relative_to(ROOT).with_suffix("").as_posix().replace("/", ".")
            summary, count = _module_summary(module)
            lines.append(f"| `{relative}` | {count} | {summary or '—'} |")
        lines.append("")

    if TESTS.exists():
        test_files = sorted(TESTS.rglob("test_*.py"))
        lines.extend(["## Тесты", "", f"Всего файлов: **{len(test_files)}**.", ""])
        for test in test_files:
            relative = test.relative_to(ROOT).as_posix()
            lines.append(f"- `{relative}`")
        lines.append("")
    return "\n".join(lines)


def build(write: bool = True) -> tuple[Path, Path]:
    index_path = DOCS / "_INDEX.md"
    code_path = DOCS / "_CODE-MAP.md"
    index = render_index(collect_docs())
    code = render_code_map()
    if write:
        DOCS.mkdir(parents=True, exist_ok=True)
        index_path.write_text(index, encoding="utf-8", newline="\n")
        code_path.write_text(code, encoding="utf-8", newline="\n")
    return index_path, code_path


def is_stale() -> bool:
    index_path, code_path = build(write=False)
    for path, content in (
        (index_path, render_index(collect_docs())),
        (code_path, render_code_map()),
    ):
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Kivi Obsidian index notes.")
    parser.add_argument("--check", action="store_true", help="exit 1 if the index is stale")
    parser.add_argument("--no-code", action="store_true", help="only build docs/_INDEX.md")
    args = parser.parse_args(argv)

    if args.no_code:
        index = render_index(collect_docs())
        path = DOCS / "_INDEX.md"
        path.write_text(index, encoding="utf-8", newline="\n")
        print(path)
        return 0

    if args.check:
        stale = is_stale()
        print("stale" if stale else "up-to-date")
        return 1 if stale else 0

    index_path, code_path = build()
    print(index_path)
    print(code_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
