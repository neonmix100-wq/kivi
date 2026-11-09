from __future__ import annotations

from pathlib import Path

from kivi.kernel.db import SCHEMA_VERSION, Database


def make_db(tmp_path: Path) -> Database:
    return Database(tmp_path / "kivi.db")


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    assert db.schema_version() == SCHEMA_VERSION
    db.close()

    reopened = make_db(tmp_path)
    assert reopened.schema_version() == SCHEMA_VERSION
    reopened.close()


def test_task_lifecycle(tmp_path: Path) -> None:
    with make_db(tmp_path) as db:
        db.create_task("tsk_1", "chat", {"prompt": "hi"})
        task = db.get_task("tsk_1")
        assert task is not None
        assert task["status"] == "pending"
        assert task["payload"] == '{"prompt":"hi"}'

        db.update_task("tsk_1", "done")
        assert db.get_task("tsk_1")["status"] == "done"


def test_update_task_replaces_payload(tmp_path: Path) -> None:
    with make_db(tmp_path) as db:
        db.create_task("tsk_1", "chat", {"prompt": "hi"})
        db.update_task("tsk_1", "done", {"answer": "hello"})
        assert db.get_task("tsk_1")["payload"] == '{"answer":"hello"}'


def test_missing_task_returns_none(tmp_path: Path) -> None:
    with make_db(tmp_path) as db:
        assert db.get_task("nope") is None


def test_steps_are_ordered_and_finished(tmp_path: Path) -> None:
    with make_db(tmp_path) as db:
        db.create_task("tsk_1", "chat")
        db.add_step("stp_2", "tsk_1", 1, "fs.write", {"path": "b.txt"})
        db.add_step("stp_1", "tsk_1", 0, "fs.read", {"path": "a.txt"})
        db.finish_step("stp_1", "done", {"bytes": 12})

        steps = db.steps_for("tsk_1")
        assert [step["id"] for step in steps] == ["stp_1", "stp_2"]
        assert steps[0]["status"] == "done"
        assert steps[0]["output"] == '{"bytes":12}'
        assert steps[0]["finished_at"] is not None
        assert steps[1]["status"] == "running"


def test_events_are_persisted_newest_first(tmp_path: Path) -> None:
    with make_db(tmp_path) as db:
        db.append_event("chat.request", task_id="tsk_1", source="cli", payload={"n": 1})
        db.append_event("chat.done", task_id="tsk_1", payload={"n": 2})

        rows = db.recent_events()
        assert [row["type"] for row in rows] == ["chat.done", "chat.request"]
        assert rows[1]["payload"] == '{"n":1}'
        assert rows[1]["source"] == "cli"


def test_audit_entries(tmp_path: Path) -> None:
    with make_db(tmp_path) as db:
        db.append_audit(
            actor="kivi",
            action="fs.write",
            target=r"C:\Users\minea\notes.txt",
            decision="allow",
            reason="inside workspace",
            details={"size": 3},
        )
        rows = db.recent_audit()
        assert len(rows) == 1
        assert rows[0]["actor"] == "kivi"
        assert rows[0]["decision"] == "allow"
        assert rows[0]["details"] == '{"size":3}'
