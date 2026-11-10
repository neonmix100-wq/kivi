"""Read-only import of text knowledge bases into memory.

The importer walks a user-supplied directory, skips repositories, private
folders, symlinks, binary files and oversized files, sanitizes each text file
and stores paragraph-sized chunks as ``untrusted:file`` messages. Re-importing
the same relative path is skipped, so running it twice does not duplicate data.

Nothing is written back to the source directory.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from kivi.memory.store import MemoryStore
from kivi.safety.sanitize import sanitize_text

TEXT_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst"})
SKIP_DIRS = frozenset({".git", ".obsidian", ".ssh", "private", "voice-dataset", "node_modules"})
DEFAULT_MAX_FILE_BYTES = 1024 * 1024
DEFAULT_CHUNK_CHARS = 4000


@dataclass(slots=True)
class ImportResult:
    files: int = 0
    chunks: int = 0
    skipped: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _is_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(flag and attributes & flag) or path.is_symlink()


def _chunks(text: str, size: int) -> list[str]:
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > size:
            if current:
                chunks.append(current)
                current = ""
            for start in range(0, len(paragraph), size):
                chunks.append(paragraph[start : start + size])
            continue
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= size:
            current = candidate
        else:
            chunks.append(current)
            current = paragraph
    if current:
        chunks.append(current)
    return chunks


def import_directory(
    store: MemoryStore,
    root: Path,
    *,
    limit_files: int = 100,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
) -> ImportResult:
    """Import readable text files under ``root``; source stays untouched."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")
    if limit_files < 1:
        raise ValueError("limit_files must be positive")
    if chunk_chars < 200:
        raise ValueError("chunk_chars must be at least 200")

    result = ImportResult()
    files_seen = 0
    for path in sorted(root.rglob("*")):
        if files_seen >= limit_files:
            break
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            result.skipped += 1
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            result.skipped += 1
            continue
        if _is_reparse(path):
            result.skipped += 1
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            result.errors.append(f"{path}: {exc}")
            continue
        if size > max_file_bytes:
            result.skipped += 1
            continue
        relative = path.relative_to(root).as_posix()
        if store.has_task_prefix(f"import:{relative}#"):
            result.duplicates += 1
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            result.errors.append(f"{relative}: {exc}")
            continue
        sanitized = sanitize_text(raw, html=False, max_chars=max_file_bytes)
        if not sanitized.text:
            result.skipped += 1
            continue
        pieces = _chunks(sanitized.text, chunk_chars)
        for index, piece in enumerate(pieces):
            store.add_message(
                "system",
                piece,
                trust="untrusted:file",
                task_id=f"import:{relative}#{index}",
            )
        files_seen += 1
        result.files += 1
        result.chunks += len(pieces)
    return result


__all__ = [
    "DEFAULT_CHUNK_CHARS",
    "DEFAULT_MAX_FILE_BYTES",
    "SKIP_DIRS",
    "TEXT_SUFFIXES",
    "ImportResult",
    "import_directory",
]
