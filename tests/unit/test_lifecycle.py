"""Tests for the memory lifecycle: summaries, TTL retention and approvals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kivi.kernel.db import Database
from kivi.memory.lifecycle import (
    ApprovalStore,
    apply_ttl,
    extractive_summary,
    summarize_messages,
)
from kivi.memory.store import MemoryStore

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
OLD = "2020-01-01T00:00:00+00:00"


def make_store(tmp_path: Path) -> tuple[MemoryStore, Database]:
    db = Database(tmp_path / "kivi.db")
    store = MemoryStore(db, tmp_path / "artifacts")
    return store, db


def age_messages(store: MemoryStore, message_ids: list[int], created_at: str = OLD) -> None:
    """Backdate messages so a fixed test clock sees them as old."""
    with store.db.connection:
        for message_id in message_ids:
            store.db.connection.execute(
                "UPDATE messages SET created_at = ? WHERE id = ?",
                (created_at, message_id),
            )


def test_extractive_summary_is_deterministic_and_truncated() -> None:
    messages = [
        {"role": "user", "content": "alpha " * 50},
        {"role": "assistant", "content": "beta " * 50},
    ]
    full = extractive_summary(messages, max_chars=5000)
    short = extractive_summary(messages, max_chars=40)

    assert full.startswith("user: alpha alpha")
    assert "assistant: beta" in full
    assert full == extractive_summary(messages, max_chars=5000)
    assert len(short) <= 40
    assert short.endswith("…")
    assert short == extractive_summary(messages, max_chars=40)


def test_extractive_summary_truncates_at_a_sentence_boundary() -> None:
    messages = [{"role": "user", "content": "First sentence. Second sentence. Third sentence."}]
    summary = extractive_summary(messages, max_chars=40)
    assert len(summary) <= 40
    assert summary.endswith("…")
    assert summary.startswith("user: First sentence.")
    with pytest.raises(ValueError, match="max_chars"):
        extractive_summary(messages, max_chars=0)


def test_summarize_groups_by_task_and_stores_untrusted_summaries(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    try:
        alpha = store.add_message("user", "alpha", task_id="t1")
        beta = store.add_message("assistant", "beta", task_id="t1")
        gamma = store.add_message("user", "gamma", task_id="t2")
        loose = store.add_message("user", "loose")
        age_messages(store, [alpha, beta, gamma, loose])
        fresh = store.add_message("user", "fresh")
        calls: list[list[str]] = []

        def summarizer(messages: list[dict]) -> str:
            calls.append([str(message["content"]) for message in messages])
            return f"summary of {len(messages)}"

        result = summarize_messages(
            store,
            summarizer=summarizer,
            clock=lambda: NOW,
        )
        summaries = [row for row in store.recent_messages(20) if row["role"] == "system"]
        fresh_row = store.get_message(fresh)
    finally:
        db.close()

    assert calls == [["alpha", "beta"], ["gamma"], ["loose"]]
    assert result.groups == 3
    assert result.summarized == 4
    assert result.deleted == 0
    assert len(result.summary_ids) == 3
    assert fresh_row is not None
    assert len(summaries) == 3
    assert {row["task_id"] for row in summaries} == {"summary:t1", "summary:t2", "summary:"}
    assert all(row["trust"] == "untrusted:tool" for row in summaries)
    assert sorted(row["content"] for row in summaries) == [
        "summary of 1",
        "summary of 1",
        "summary of 2",
    ]


def test_summarize_keeps_originals_by_default(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    try:
        message_id = store.add_message("user", "alpha", task_id="t1")
        age_messages(store, [message_id])
        result = summarize_messages(store, clock=lambda: NOW)
        original = store.get_message(message_id)
        messages = store.stats()["messages"]
    finally:
        db.close()

    assert result.deleted == 0
    assert result.summarized == 1
    assert original is not None
    assert messages == 2


def test_summarize_delete_originals_removes_embeddings(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    try:
        message_id = store.add_message("user", "alpha", task_id="t1")
        store.put_embedding("message", message_id, "m", [1.0, 0.0])
        age_messages(store, [message_id])
        result = summarize_messages(store, clock=lambda: NOW, delete_originals=True)
        original = store.get_message(message_id)
        embedding_stats = store.embedding_stats("m")
    finally:
        db.close()

    assert result.deleted == 1
    assert result.summarized == 1
    assert original is None
    assert embedding_stats["messages"] == 0


def test_summarize_skips_derived_summaries(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    try:
        message_id = store.add_message("user", "alpha", task_id="t1")
        age_messages(store, [message_id])
        first = summarize_messages(store, clock=lambda: NOW, delete_originals=True)
        summary_id = first.summary_ids[0]
        age_messages(store, [summary_id])
        second = summarize_messages(
            store,
            clock=lambda: NOW + timedelta(days=30),
            delete_originals=True,
        )
    finally:
        db.close()

    assert first.summarized == 1
    assert second.groups == 0
    assert second.summarized == 0


def test_summarize_rejects_invalid_inputs(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    try:
        message_id = store.add_message("user", "alpha")
        age_messages(store, [message_id])
        with pytest.raises(ValueError, match="older_than_days"):
            summarize_messages(store, older_than_days=-1)
        with pytest.raises(ValueError, match="max_messages"):
            summarize_messages(store, max_messages=0)
        with pytest.raises(ValueError, match="empty"):
            summarize_messages(
                store,
                summarizer=lambda messages: "   ",
                clock=lambda: NOW,
            )
    finally:
        db.close()


def test_extractive_summary_is_the_default(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    try:
        message_id = store.add_message("user", "alpha beta", task_id="t1")
        age_messages(store, [message_id])
        result = summarize_messages(store, clock=lambda: NOW)
        summary = store.get_message(result.summary_ids[0])
    finally:
        db.close()

    assert summary is not None
    assert summary["content"] == "user: alpha beta"


def test_apply_ttl_removes_old_messages_but_keeps_new(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    try:
        old = store.add_message("user", "old", task_id="t1")
        new = store.add_message("user", "new", task_id="t2")
        store.put_embedding("message", old, "m", [1.0, 0.0])
        store.put_embedding("message", new, "m", [0.0, 1.0])
        age_messages(store, [old])
        store.remember("city", "Novosibirsk")
        store.set_preference("language", "ru")
        store.write_artifact(b"data")

        result = apply_ttl(store, days=30, clock=lambda: NOW)
        old_row = store.get_message(old)
        new_row = store.get_message(new)
        embeddings = store.embedding_stats("m")
        fact = store.get_fact("city")
        preference = store.get_preference("language")
        artifacts = store.stats()["artifacts"]
    finally:
        db.close()

    assert result.messages_deleted == 1
    assert result.embeddings_deleted == 1
    assert result.cutoff == "2026-05-02T12:00:00+00:00"
    assert old_row is None
    assert new_row is not None
    assert embeddings["messages"] == 1
    assert fact is not None
    assert preference == "ru"
    assert artifacts == 1


def test_apply_ttl_rejects_negative_days(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    try:
        with pytest.raises(ValueError, match="negative"):
            apply_ttl(store, days=-1)
        with pytest.raises(ValueError, match="negative"):
            summarize_messages(store, older_than_days=-1)
    finally:
        db.close()


def test_apply_ttl_cleans_expired_approvals(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    clock = [NOW]
    try:
        approvals = ApprovalStore(store, clock=lambda: clock[0])
        approvals.grant("t1", "shell.exec", ttl_s=60)
        approvals.grant("t1", "fs.write")
        clock[0] = NOW + timedelta(days=3)

        result = apply_ttl(store, days=1, clock=lambda: clock[0])
        remaining = [row["action"] for row in approvals.list("t1")]
    finally:
        db.close()

    assert result.approvals_deleted == 1
    assert remaining == ["fs.write"]


def test_approval_grant_and_check(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    try:
        approvals = ApprovalStore(store, clock=lambda: NOW)
        granted = approvals.grant("t1", "shell.exec")
        approved = approvals.is_approved("t1", "shell.exec")
        wrong_action = approvals.is_approved("t1", "fs.write")
        wrong_task = approvals.is_approved("t2", "shell.exec")
    finally:
        db.close()

    assert granted["task_id"] == "t1"
    assert granted["action"] == "shell.exec"
    assert granted["scope"] == ""
    assert granted["expires_at"] is None
    assert granted["revoked_at"] is None
    assert approved is True
    assert wrong_action is False
    assert wrong_task is False


def test_approval_scope_matching(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    try:
        approvals = ApprovalStore(store, clock=lambda: NOW)
        approvals.grant("t1", "fs.write", scope="C:/work")
        narrow_hit = approvals.is_approved("t1", "fs.write", scope="C:/work")
        narrow_miss = approvals.is_approved("t1", "fs.write", scope="C:/other")
        empty_request = approvals.is_approved("t1", "fs.write")
        approvals.grant("t2", "fs.write")
        broad_hit = approvals.is_approved("t2", "fs.write", scope="C:/any")
    finally:
        db.close()

    assert narrow_hit is True
    assert narrow_miss is False
    assert empty_request is True
    assert broad_hit is True


def test_approval_expiry_is_strict(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    clock = [NOW]
    try:
        approvals = ApprovalStore(store, clock=lambda: clock[0])
        approvals.grant("t1", "shell.exec", ttl_s=60)
        before = approvals.is_approved("t1", "shell.exec")
        clock[0] = NOW + timedelta(seconds=59)
        still_valid = approvals.is_approved("t1", "shell.exec")
        clock[0] = NOW + timedelta(seconds=60)
        expired = approvals.is_approved("t1", "shell.exec")
    finally:
        db.close()

    assert before is True
    assert still_valid is True
    assert expired is False


def test_approval_revoke_is_idempotent(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    try:
        approvals = ApprovalStore(store, clock=lambda: NOW)
        granted = approvals.grant("t1", "shell.exec")
        first = approvals.revoke(int(granted["id"]))
        second = approvals.revoke(int(granted["id"]))
        missing = approvals.revoke(9999)
        approved = approvals.is_approved("t1", "shell.exec")
    finally:
        db.close()

    assert first is True
    assert second is False
    assert missing is False
    assert approved is False


def test_approval_list_and_cleanup_policy(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    clock = [NOW]
    try:
        approvals = ApprovalStore(store, clock=lambda: clock[0])
        permanent = approvals.grant("t1", "fs.read")
        expiring = approvals.grant("t2", "shell.exec", ttl_s=60)

        listed = approvals.list()
        filtered = approvals.list("t1")
        clock[0] = NOW + timedelta(seconds=61)
        after_expiry = approvals.list("t2")
        within_grace = approvals.cleanup()
        approvals.revoke(int(permanent["id"]))
        clock[0] = NOW + timedelta(days=2)
        after_grace = approvals.cleanup()
        remaining = approvals.list()
    finally:
        db.close()

    assert [row["id"] for row in listed] == [int(expiring["id"]), int(permanent["id"])]
    assert len(filtered) == 1
    assert filtered[0]["active"] is True
    assert after_expiry[0]["active"] is False
    assert within_grace == 0
    assert after_grace == 2
    assert remaining == []


def test_approval_rejects_invalid_inputs(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    try:
        approvals = ApprovalStore(store, clock=lambda: NOW)
        with pytest.raises(ValueError, match="task_id"):
            approvals.grant("", "shell.exec")
        with pytest.raises(ValueError, match="action"):
            approvals.grant("t1", "   ")
        with pytest.raises(ValueError, match="ttl_s"):
            approvals.grant("t1", "shell.exec", ttl_s=0)
        with pytest.raises(ValueError, match="ttl_s"):
            approvals.grant("t1", "shell.exec", ttl_s=-1)
        with pytest.raises(ValueError, match="task_id"):
            approvals.is_approved("", "shell.exec")
        with pytest.raises(ValueError, match="action"):
            approvals.is_approved("t1", "")
    finally:
        db.close()
