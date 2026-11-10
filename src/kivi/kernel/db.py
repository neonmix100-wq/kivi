"""SQLite storage with forward-only migrations.

The database holds durable state only (tasks, steps, events, audit). Large
artifacts and logs stay on the filesystem and are referenced by id.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

MIGRATIONS: dict[int, list[str]] = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id          TEXT PRIMARY KEY,
            kind        TEXT NOT NULL,
            status      TEXT NOT NULL,
            payload     TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS steps (
            id          TEXT PRIMARY KEY,
            task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            idx         INTEGER NOT NULL,
            tool        TEXT NOT NULL,
            status      TEXT NOT NULL,
            input       TEXT,
            output      TEXT,
            error       TEXT,
            started_at  TEXT,
            finished_at TEXT,
            UNIQUE (task_id, idx)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS events (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT NOT NULL,
            type     TEXT NOT NULL,
            task_id  TEXT,
            step_id  TEXT,
            source   TEXT,
            payload  TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS audit (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT NOT NULL,
            actor    TEXT NOT NULL,
            action   TEXT NOT NULL,
            target   TEXT,
            decision TEXT NOT NULL,
            reason   TEXT,
            details  TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts)",
        "CREATE INDEX IF NOT EXISTS idx_events_task ON events (task_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit (ts)",
    ],
    2: [
        """
        CREATE TABLE IF NOT EXISTS cloud_usage (
            provider TEXT NOT NULL,
            day      TEXT NOT NULL,
            requests INTEGER NOT NULL DEFAULT 0,
            tokens   INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (provider, day)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_cloud_usage_day ON cloud_usage (day)",
    ],
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Database:
    """A thin, explicit wrapper around a SQLite connection."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self.migrate()

    def _configure(self) -> None:
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._conn:
            yield self._conn

    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> int:
        current = self.schema_version()
        for version in sorted(MIGRATIONS):
            if version <= current:
                continue
            with self._conn:
                for statement in MIGRATIONS[version]:
                    self._conn.execute(statement)
                self._conn.execute(f"PRAGMA user_version={version}")
            current = version
        return current

    # -- tasks -----------------------------------------------------------
    def create_task(self, task_id: str, kind: str, payload: Any = None) -> None:
        now = utc_now()
        with self._conn:
            self._conn.execute(
                "INSERT INTO tasks (id, kind, status, payload, created_at, updated_at) "
                "VALUES (?, ?, 'pending', ?, ?, ?)",
                (task_id, kind, _json(payload), now, now),
            )

    def update_task(self, task_id: str, status: str, payload: Any = None) -> None:
        with self._conn:
            if payload is None:
                self._conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (status, utc_now(), task_id),
                )
            else:
                self._conn.execute(
                    "UPDATE tasks SET status = ?, payload = ?, updated_at = ? WHERE id = ?",
                    (status, _json(payload), utc_now(), task_id),
                )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    # -- steps -----------------------------------------------------------
    def add_step(
        self, step_id: str, task_id: str, idx: int, tool: str, payload: Any = None
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO steps (id, task_id, idx, tool, status, input, started_at) "
                "VALUES (?, ?, ?, ?, 'running', ?, ?)",
                (step_id, task_id, idx, tool, _json(payload), utc_now()),
            )

    def finish_step(
        self, step_id: str, status: str, output: Any = None, error: str | None = None
    ) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE steps SET status = ?, output = ?, error = ?, finished_at = ? WHERE id = ?",
                (status, _json(output), error, utc_now(), step_id),
            )

    def steps_for(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM steps WHERE task_id = ? ORDER BY idx", (task_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    # -- events ----------------------------------------------------------
    def append_event(
        self,
        event_type: str,
        *,
        task_id: str | None = None,
        step_id: str | None = None,
        source: str | None = None,
        payload: Any = None,
    ) -> int:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO events (ts, type, task_id, step_id, source, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (utc_now(), event_type, task_id, step_id, source, _json(payload)),
            )
            return int(cursor.lastrowid)

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    # -- audit -----------------------------------------------------------
    def append_audit(
        self,
        *,
        actor: str,
        action: str,
        target: str,
        decision: str,
        reason: str = "",
        details: Any = None,
    ) -> int:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO audit (ts, actor, action, target, decision, reason, details) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (utc_now(), actor, action, target, decision, reason, _json(details)),
            )
            return int(cursor.lastrowid)

    def recent_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    # -- cloud quota -----------------------------------------------------
    def add_cloud_usage(
        self,
        provider: str,
        day: str,
        *,
        requests: int = 1,
        tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO cloud_usage (provider, day, requests, tokens, cost_usd) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(provider, day) DO UPDATE SET "
                "requests = requests + excluded.requests, "
                "tokens = tokens + excluded.tokens, "
                "cost_usd = cost_usd + excluded.cost_usd",
                (provider, day, requests, tokens, cost_usd),
            )

    def cloud_usage(self, provider: str, day: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT provider, day, requests, tokens, cost_usd "
            "FROM cloud_usage WHERE provider = ? AND day = ?",
            (provider, day),
        ).fetchone()
        if row is None:
            return {
                "provider": provider,
                "day": day,
                "requests": 0,
                "tokens": 0,
                "cost_usd": 0.0,
            }
        return dict(row)

    def cloud_usage_all(self, day: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT provider, day, requests, tokens, cost_usd "
            "FROM cloud_usage WHERE day = ? ORDER BY provider",
            (day,),
        ).fetchall()
        return [dict(row) for row in rows]

