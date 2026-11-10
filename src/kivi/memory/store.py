"""Durable memory: messages, facts, preferences and content-addressed artifacts.

SQLite is the source of truth; FTS5 provides keyword search without a vector
database. Artifacts are stored by SHA-256 under the Kivi home, so writing the
same bytes twice does not duplicate them. Every fact and message carries a trust
label; search results keep it so the context builder can wrap them properly.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
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


def _pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"<{dim}f", blob))


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = math.fsum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(math.fsum(a * a for a in left))
    right_norm = math.sqrt(math.fsum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


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

    def get_message(self, message_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return dict(row) if row else None

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

    def has_task_prefix(self, prefix: str) -> bool:
        """True when any message task id starts with ``prefix``."""
        row = self._conn.execute(
            "SELECT 1 FROM messages WHERE substr(task_id, 1, ?) = ? LIMIT 1",
            (len(prefix), prefix),
        ).fetchone()
        return row is not None

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

    def get_fact_by_id(self, fact_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
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
        row = self._conn.execute("SELECT id FROM facts WHERE key = ?", (key,)).fetchone()
        if row is None:
            return False
        with self._conn:
            self._conn.execute("DELETE FROM facts WHERE id = ?", (row["id"],))
            self._conn.execute(
                "DELETE FROM embeddings WHERE owner_type = 'fact' AND owner_id = ?",
                (str(row["id"]),),
            )
        return True

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

    # -- embeddings ------------------------------------------------------
    def put_embedding(
        self,
        owner_type: str,
        owner_id: int | str,
        model: str,
        vector: list[float] | tuple[float, ...],
    ) -> None:
        if owner_type not in {"message", "fact"}:
            raise MemoryError(f"unknown embedding owner: {owner_type}")
        if not model.strip():
            raise MemoryError("embedding model must not be empty")
        values = [float(item) for item in vector]
        if not values:
            raise MemoryError("embedding vector must not be empty")
        if not all(math.isfinite(item) for item in values):
            raise MemoryError("embedding vector must contain finite numbers")
        with self._conn:
            self._conn.execute(
                "INSERT INTO embeddings (owner_type, owner_id, model, dim, vector, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(owner_type, owner_id, model) DO UPDATE SET "
                "dim = excluded.dim, vector = excluded.vector, created_at = excluded.created_at",
                (owner_type, str(owner_id), model, len(values), _pack_vector(values), utc_now()),
            )

    def get_embedding(
        self, owner_type: str, owner_id: int | str, model: str
    ) -> list[float] | None:
        row = self._conn.execute(
            "SELECT dim, vector FROM embeddings "
            "WHERE owner_type = ? AND owner_id = ? AND model = ?",
            (owner_type, str(owner_id), model),
        ).fetchone()
        return _unpack_vector(row["vector"], int(row["dim"])) if row else None

    def embeddings(self, model: str, limit: int = 100_000) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM embeddings WHERE model = ? ORDER BY id LIMIT ?",
            (model, limit),
        ).fetchall()
        return [
            {
                **dict(row),
                "vector": _unpack_vector(row["vector"], int(row["dim"])),
            }
            for row in rows
        ]

    def unembedded(self, owner_type: str, model: str, limit: int = 1000) -> list[dict[str, Any]]:
        if owner_type == "message":
            rows = self._conn.execute(
                "SELECT m.* FROM messages m LEFT JOIN embeddings e ON "
                "e.owner_type = 'message' AND e.owner_id = CAST(m.id AS TEXT) AND e.model = ? "
                "WHERE e.id IS NULL ORDER BY m.id LIMIT ?",
                (model, limit),
            ).fetchall()
        elif owner_type == "fact":
            rows = self._conn.execute(
                "SELECT f.* FROM facts f LEFT JOIN embeddings e ON "
                "e.owner_type = 'fact' AND e.owner_id = CAST(f.id AS TEXT) AND e.model = ? "
                "WHERE e.id IS NULL ORDER BY f.id LIMIT ?",
                (model, limit),
            ).fetchall()
        else:
            raise MemoryError(f"unknown embedding owner: {owner_type}")
        return [dict(row) for row in rows]

    def search_embeddings(
        self, query_vector: list[float], model: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        if not query_vector:
            return []
        scored: list[dict[str, Any]] = []
        for row in self.embeddings(model):
            score = _cosine(query_vector, row["vector"])
            scored.append({**row, "score": score})
        scored.sort(key=lambda item: (-item["score"], item["owner_type"], item["owner_id"]))
        return scored[:limit]

    def embedding_stats(self, model: str) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT owner_type, COUNT(*) AS n FROM embeddings WHERE model = ? "
            "GROUP BY owner_type",
            (model,),
        ).fetchall()
        counts = {str(row["owner_type"]): int(row["n"]) for row in rows}
        return {
            "messages": counts.get("message", 0),
            "facts": counts.get("fact", 0),
            "total": sum(counts.values()),
        }

    def forget_message(self, message_id: int) -> bool:
        with self._conn:
            cursor = self._conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            self._conn.execute(
                "DELETE FROM embeddings WHERE owner_type = 'message' AND owner_id = ?",
                (str(message_id),),
            )
        return cursor.rowcount > 0

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
