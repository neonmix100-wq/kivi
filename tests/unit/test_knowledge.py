from __future__ import annotations

from pathlib import Path

import pytest

from scripts import kivi_context
from scripts.build_knowledge_index import build, collect_docs, summarize


def test_index_covers_all_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "A.md").write_text("# Alpha\n\nFirst paragraph.\n\n## One\n", encoding="utf-8")
    (docs / "B.md").write_text("# Beta\n\n## Two\n", encoding="utf-8")
    monkeypatch.setattr("scripts.build_knowledge_index.DOCS", docs)
    monkeypatch.setattr("scripts.build_knowledge_index.SRC", tmp_path / "src")
    index_path, code_path = build()
    text = index_path.read_text(encoding="utf-8")
    assert "[[A]]" in text
    assert "[[A#One|One]]" in text
    assert "[[B#Two|Two]]" in text
    assert "Карта кода" in code_path.read_text(encoding="utf-8")


def test_summary_skips_frontmatter_and_code() -> None:
    lines = ["---", "tags: [x]", "---", "", "# Title", "", "Summary line.", "", "## Next"]
    assert summarize(lines) == "Summary line."


def test_collect_docs_ignores_generated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "_INDEX.md").write_text("# generated", encoding="utf-8")
    (docs / "REAL.md").write_text("# Real\n", encoding="utf-8")
    monkeypatch.setattr("scripts.build_knowledge_index.DOCS", docs)
    assert [doc.path.name for doc in collect_docs()] == ["REAL.md"]


def test_search_finds_term(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    note = docs / "ARCH.md"
    note.write_text("# Arch\n\n## Model Gateway\n\nlease manager lives here\n", encoding="utf-8")
    monkeypatch.setattr(kivi_context, "ROOT", tmp_path)
    monkeypatch.setattr(kivi_context, "DOCS", docs)
    monkeypatch.setattr(kivi_context, "_sources", lambda: [note])
    code = kivi_context.main(["search", "lease"])
    assert code == 0
    out = capsys.readouterr().out
    assert "ARCH.md:5" in out
    assert "[Model Gateway]" in out


def test_section_stops_at_next_heading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    note = tmp_path / "doc.md"
    note.write_text("# T\n\n## A\n\nalpha\n\n### A1\n\ninner\n\n## B\n\nbeta\n", encoding="utf-8")
    monkeypatch.setattr(kivi_context, "ROOT", tmp_path)
    code = kivi_context.main(["section", "doc.md", "A"])
    assert code == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "inner" in out
    assert "beta" not in out


def test_section_rejects_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kivi_context, "ROOT", tmp_path / "root")
    monkeypatch.setattr(kivi_context, "DOCS", tmp_path / "root" / "docs")
    assert kivi_context.main(["section", "../outside.md", "x"]) == 2


def test_stage_returns_first_open_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ROADMAP.md").write_text(
        "## Этап 1\n\n- [x] done\n\n## Этап 2\n\n- [x] a\n- [ ] b\n\n"
        "**Приёмка:** ok\n\n## Этап 3\n\n- [ ] c\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(kivi_context, "ROOT", tmp_path)
    code = kivi_context.main(["stage"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Этап 2" in out and "- [ ] b" in out
    assert "Этап 3" not in out
