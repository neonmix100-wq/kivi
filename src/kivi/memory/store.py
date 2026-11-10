"""Durable memory: messages, facts, preferences and content-addressed artifacts.

SQLite is the source of truth; FTS5 provides keyword search without a vector
database. Artifacts are stored by SHA-256 under the Kivi home, so writing the
same bytes twice does not duplicate them. Every fact and message carries a trust
label; search results keep it so the context builder can wrap them properly.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from kivi.kernel.db import Database, utc_now
from kivi.safety.trust import TRUST_LEVELS, TrustError

ROLES = frozenset({"user", "assistant", "system", "tool"})


class MemoryError(ValueError):
    """Raised for invalid memory input."""


def _fts_phrase(query: str) -> str:
    cleaned = " ".join(query.split())
    if not cleaned:
        return ""
    # Quote the whole phrase and double embedded quotes; FTS5 syntax stays safe.
    return '"' + cleaned.replace('"', '""') + '"'


def _check_trust(trust: str) -> None:
    if trust not in TRUST_LEVELS:
        raise TrustError(f"unknown trust level: {trust}")


class MemoryStore:
    """Thin, explicit API over the memory tables."""

    def __init__(self, db: Database, artifacts_root: Path) -> None:
        self.db = db
        self.artifacts_root = Path(artifacts_root)

    @property
    def _conn(self):
        return self.db.connection

    # -- messages --------------------------------------------------------
    def add_message(
        self,
        role: str,
        content: str,
        *,
        trust: str = "trusted:user",
        task_id: str = "",
    ) -> int:
        if role not in ROLES:
            raise MemoryError(f"unknown role: {role}")
        if not content.strip():
            raise MemoryError("message content must not be empty")
        _check_trust(trust)
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO messages (task_id, role, content, trust, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id or None, role, content, trust, utc_now()),
            )
            return int(cursor.lastrowid)

    def recent_messages(self, limit: int = 20, *, task_id: str = "") -> list[dict[str, Any]]:
        if task_id:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def search_messages(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        phrase = _fts_phrase(query)
        if not phrase:
            return []
        rows = self._conn.execute(
            "SELECT m.*, bm25(messages_fts) AS rank, "
            "snippet(messages_fts, 0, '[', ']', '…', 12) AS snippet "
            "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            "WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?",
            (phrase, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- facts -----------------------------------------------------------
    def remember(
        self,
        key: str,
        value: str,
        *,
        source: str = "",
        trust: str = "trusted:user",
        confidence: float = 1.0,
    ) -> int:
        key = key.strip()
        if not key:
            raise MemoryError("fact key must not be empty")
        if not value.strip():
            raise MemoryError("fact value must not be empty")
        if not 0.0 <= confidence <= 1.0:
            raise MemoryError("confidence must be between 0 and 1")
        _check_trust(trust)
        now = utc_now()
        with self._conn:
            self._conn.execute(
                "INSERT INTO facts (key, value, source, trust, confidence, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "source = excluded.source, trust = excluded.trust, "
                "confidence = excluded.confidence, updated_at = excluded.updated_at",
                (key, value, source, trust, confidence, now, now),
            )
            row = self._conn.execute("SELECT id FROM facts WHERE key = ?", (key,)).fetchone()
        return int(row["id"])

    def get_fact(self, key: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM facts WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def facts(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM facts ORDER BY updated_at DESC, key LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def search_facts(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        phrase = _fts_phrase(query)
        if not phrase:
            return []
        rows = self._conn.execute(
            "SELECT f.*, bm25(facts_fts) AS rank "
            "FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
            "WHERE facts_fts MATCH ? ORDER BY rank LIMIT ?",
            (phrase, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def forget(self, key: str) -> bool:
        with self._conn:
            cursor = self._conn.execute("DELETE FROM facts WHERE key = ?", (key,))
        return cursor.rowcount > 0

    # -- preferences -----------------------------------------------------
    def set_preference(self, key: str, value: str) -> None:
        key = key.strip()
        if not key:
            raise MemoryError("preference key must not be empty")
        with self._conn:
            self._conn.execute(
                "INSERT INTO preferences (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, utc_now()),
            )

    def get_preference(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM preferences WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def preferences(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT key, value FROM preferences ORDER BY key").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    # -- artifacts -------------------------------------------------------
    def write_artifact(self, data: bytes, *, mime: str = "") -> dict[str, Any]:
        digest = hashlib.sha256(data).hexdigest()
        existing = self.get_artifact(digest)
        if existing is not None:
            return existing
        target = self.artifacts_root / digest[:2] / digest
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO artifacts (sha256, path, mime, bytes, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (digest, str(target), mime, len(data), utc_now()),
            )
        return self.get_artifact(digest) or {"sha256": digest, "path": str(target)}

    def get_artifact(self, digest: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE sha256 = ?", (digest,)
        ).fetchone()
        return dict(row) if row else None

    def artifacts(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM artifacts ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    # -- export ----------------------------------------------------------
    def export(self) -> dict[str, Any]:
        return {
            "messages": self.recent_messages(100),
            "facts": self.facts(1000),
            "preferences": self.preferences(),
            "artifacts": self.artifacts(1000),
        }

    def stats(self) -> dict[str, int]:
        def count(table: str) -> int:
            row = self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            return int(row["n"])

        return {
            "messages": count("messages"),
            "facts": count("facts"),
            "preferences": count("preferences"),
            "artifacts": count("artifacts"),
        }

    def export_json(self) -> str:
        return json.dumps(self.export(), ensure_ascii=False, indent=2)


__all__ = ["MemoryError", "MemoryStore", "ROLES"]
