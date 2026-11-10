"""Memory lifecycle: summarization, TTL retention and durable approvals.

Stage 5 turns the memory tables into a lifecycle. Old messages can be folded
into compact per-task summaries, a TTL sweep removes aged transcripts together
with their vectors, and the ``approvals`` table from migration 4 gets an
explicit API. Summaries are *derived, untrusted* text: they may describe what
was said but they may never issue instructions.

Nothing is deleted behind the caller's back. Retention is an explicit call;
the defaults only bound how much one pass may do.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from kivi.memory.store import MemoryStore

Clock = Callable[[], datetime]
Summarizer = Callable[[list[dict[str, Any]]], str]

SUMMARY_TASK_PREFIX = "summary:"
SUMMARY_TRUST = "untrusted:tool"
DEFAULT_SUMMARY_CHARS = 1200
# Expired/revoked approvals stay visible for a day before ``cleanup`` removes
# them, so an operator can still see why a call was refused.
APPROVAL_GRACE = timedelta(days=1)


def _utc(moment: datetime) -> datetime:
    """Normalise a datetime to aware UTC; naive values are assumed UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _now(clock: Clock | None) -> datetime:
    return _utc(clock()) if clock is not None else datetime.now(UTC)


def _iso(moment: datetime) -> str:
    """Render a timestamp in the format ``utc_now`` uses for the database."""
    return _utc(moment).isoformat(timespec="seconds")


def extractive_summary(
    messages: Sequence[dict[str, Any]],
    max_chars: int = DEFAULT_SUMMARY_CHARS,
) -> str:
    """Build a deterministic, model-free summary from the oldest messages.

    Every message becomes ``role: content`` with whitespace collapsed. The
    result is cut at ``max_chars``; when a sentence boundary is close to the
    cut it is preferred, and a trailing ellipsis marks the truncation.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    lines: list[str] = []
    for message in messages:
        content = " ".join(str(message.get("content", "")).split())
        if not content:
            continue
        role = str(message.get("role") or "message")
        lines.append(f"{role}: {content}")
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    cut = max_chars - 1
    window = text[:cut]
    boundary = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if boundary >= cut // 2:
        window = window[: boundary + 1]
    return window.rstrip() + "…"


@dataclass(frozen=True, slots=True)
class SummaryResult:
    """What one summarization pass did."""

    groups: int = 0
    summarized: int = 0
    deleted: int = 0
    summary_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class TTLResult:
    """What one TTL pass removed."""

    messages_deleted: int = 0
    embeddings_deleted: int = 0
    approvals_deleted: int = 0
    cutoff: str = ""


def summarize_messages(
    store: MemoryStore,
    *,
    older_than_days: int = 7,
    max_messages: int = 200,
    summarizer: Summarizer | None = None,
    clock: Clock | None = None,
    delete_originals: bool = False,
) -> SummaryResult:
    """Fold messages older than ``older_than_days`` into per-task summaries.

    Messages are grouped by ``task_id`` (a missing task id is one group) and
    summarised oldest-first, at most ``max_messages`` per pass. The default
    summarizer is :func:`extractive_summary`; an injected ``summarizer``
    receives the group's message dicts. Each summary is stored as a ``system``
    message with trust ``untrusted:tool`` and task id ``summary:<group>``.

    Originals are kept unless ``delete_originals`` is true; deleting also
    removes their embeddings through :meth:`MemoryStore.forget_message`.
    """
    if older_than_days < 0:
        raise ValueError("older_than_days must not be negative")
    if max_messages < 1:
        raise ValueError("max_messages must be positive")
    now = _now(clock)
    cutoff = _iso(now - timedelta(days=older_than_days))
    # Summary rows are derived data: never summarize them again. This keeps
    # repeated passes from building summaries of summaries.
    rows = store.db.connection.execute(
        "SELECT * FROM messages WHERE created_at < ? "
        "AND (task_id IS NULL OR task_id NOT LIKE ?) "
        "ORDER BY id LIMIT ?",
        (cutoff, f"{SUMMARY_TASK_PREFIX}%", max_messages),
    ).fetchall()

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        message = dict(row)
        groups.setdefault(str(message.get("task_id") or ""), []).append(message)

    summary_ids: list[int] = []
    original_ids: list[int] = []
    for group, messages in groups.items():
        text = summarizer(messages) if summarizer else extractive_summary(messages)
        text = text.strip()
        if not text:
            raise ValueError(f"summarizer returned an empty summary for group {group!r}")
        # Store every summary before deleting anything: a failure halfway
        # through must not leave originals gone without their replacement.
        summary_ids.append(
            store.add_message(
                "system",
                text,
                trust=SUMMARY_TRUST,
                task_id=f"{SUMMARY_TASK_PREFIX}{group}",
            )
        )
        original_ids.extend(int(message["id"]) for message in messages)

    deleted = 0
    if delete_originals:
        for message_id in original_ids:
            if store.forget_message(message_id):
                deleted += 1

    return SummaryResult(
        groups=len(groups),
        summarized=len(rows),
        deleted=deleted,
        summary_ids=tuple(summary_ids),
    )


def _count_message_embeddings(store: MemoryStore, message_id: int) -> int:
    row = store.db.connection.execute(
        "SELECT COUNT(*) AS n FROM embeddings WHERE owner_type = 'message' AND owner_id = ?",
        (str(message_id),),
    ).fetchone()
    return int(row["n"]) if row else 0


def apply_ttl(
    store: MemoryStore,
    *,
    days: int = 90,
    clock: Clock | None = None,
) -> TTLResult:
    """Delete messages older than ``days`` and their embeddings.

    Facts, preferences and artifacts are curated state and are never touched
    by retention. Expired/revoked approvals are delegated to
    :meth:`ApprovalStore.cleanup`, which keeps a one-day grace for visibility.
    A negative ``days`` is rejected; zero means "everything created before now".
    """
    if days < 0:
        raise ValueError("days must not be negative")
    now = _now(clock)
    cutoff = _iso(now - timedelta(days=days))
    rows = store.db.connection.execute(
        "SELECT id FROM messages WHERE created_at < ? ORDER BY id", (cutoff,)
    ).fetchall()

    messages_deleted = 0
    embeddings_deleted = 0
    for row in rows:
        message_id = int(row["id"])
        embeddings_deleted += _count_message_embeddings(store, message_id)
        if store.forget_message(message_id):
            messages_deleted += 1

    approvals_deleted = ApprovalStore(store).cleanup(now=now)
    return TTLResult(
        messages_deleted=messages_deleted,
        embeddings_deleted=embeddings_deleted,
        approvals_deleted=approvals_deleted,
        cutoff=cutoff,
    )


class ApprovalStore:
    """Durable approvals backed by the ``approvals`` table (migration 4).

    An approval is a ``(task_id, action, scope)`` row with an optional expiry
    and an optional revocation timestamp. Nothing is updated in place except
    revocation: expiry is derived from ``expires_at``, so a stale row simply
    stops matching.

    Scope matching: an empty *requested* scope matches any stored scope; an
    empty *stored* scope covers any request. This makes ``scope=""`` a broad
    grant and a non-empty scope a narrowing one.
    """

    def __init__(self, store: MemoryStore, *, clock: Clock | None = None) -> None:
        self.store = store
        self._clock = clock

    @property
    def _conn(self) -> sqlite3.Connection:
        return self.store.db.connection

    def _now(self) -> datetime:
        return _now(self._clock)

    def grant(
        self,
        task_id: str,
        action: str,
        *,
        scope: str = "",
        ttl_s: int | None = None,
    ) -> dict[str, Any]:
        """Record an approval; ``ttl_s=None`` means it never expires."""
        task_id = task_id.strip()
        action = action.strip()
        if not task_id:
            raise ValueError("task_id must not be empty")
        if not action:
            raise ValueError("action must not be empty")
        if ttl_s is not None and ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        now = self._now()
        expires_at = _iso(now + timedelta(seconds=ttl_s)) if ttl_s is not None else None
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO approvals (task_id, action, scope, granted_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, action, scope.strip(), _iso(now), expires_at),
            )
            approval_id = int(cursor.lastrowid)
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        return dict(row) if row else {}

    def is_approved(self, task_id: str, action: str, *, scope: str = "") -> bool:
        """True when a live approval matches; expired/revoked rows never match."""
        task_id = task_id.strip()
        action = action.strip()
        if not task_id:
            raise ValueError("task_id must not be empty")
        if not action:
            raise ValueError("action must not be empty")
        scope = scope.strip()
        now = _iso(self._now())
        row = self._conn.execute(
            "SELECT 1 FROM approvals WHERE task_id = ? AND action = ? "
            "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?) "
            "AND (? = '' OR scope = '' OR scope = ?) LIMIT 1",
            (task_id, action, now, scope, scope),
        ).fetchone()
        return row is not None

    def revoke(self, approval_id: int) -> bool:
        """Revoke one approval; False when missing or already revoked."""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE approvals SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (_iso(self._now()), approval_id),
            )
        return cursor.rowcount > 0

    def cleanup(self, now: datetime | None = None) -> int:
        """Delete approvals revoked or expired for at least one day.

        The one-day grace keeps just-expired rows observable through ``list``;
        ``is_approved`` already ignores them before cleanup runs. Returns the
        number of deleted rows.
        """
        moment = _utc(now) if now is not None else self._now()
        cutoff = _iso(moment - APPROVAL_GRACE)
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM approvals WHERE "
                "(revoked_at IS NOT NULL AND revoked_at <= ?) OR "
                "(revoked_at IS NULL AND expires_at IS NOT NULL AND expires_at <= ?)",
                (cutoff, cutoff),
            )
        return int(cursor.rowcount)

    def list(self, task_id: str | None = None) -> list[dict[str, Any]]:
        """Return approvals newest-first, with a derived ``active`` flag."""
        if task_id is None:
            rows = self._conn.execute("SELECT * FROM approvals ORDER BY id DESC").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM approvals WHERE task_id = ? ORDER BY id DESC",
                (task_id,),
            ).fetchall()
        now = _iso(self._now())
        result: list[dict[str, Any]] = []
        for row in rows:
            approval = dict(row)
            approval["active"] = approval["revoked_at"] is None and (
                approval["expires_at"] is None or approval["expires_at"] > now
            )
            result.append(approval)
        return result


__all__ = [
    "APPROVAL_GRACE",
    "DEFAULT_SUMMARY_CHARS",
    "SUMMARY_TASK_PREFIX",
    "SUMMARY_TRUST",
    "ApprovalStore",
    "Clock",
    "Summarizer",
    "SummaryResult",
    "TTLResult",
    "apply_ttl",
    "extractive_summary",
    "summarize_messages",
]
