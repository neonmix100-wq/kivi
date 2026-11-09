from __future__ import annotations

from pathlib import Path

from kivi.kernel.db import Database
from kivi.kernel.events import Event, EventBus
from kivi.kernel.logs import JsonlLogger


def make_bus(tmp_path: Path) -> tuple[Database, EventBus, JsonlLogger]:
    db = Database(tmp_path / "kivi.db")
    logger = JsonlLogger(tmp_path / "logs")
    return db, EventBus(db, logger), logger


def test_publish_persists_to_db_and_jsonl(tmp_path: Path) -> None:
    db, bus, logger = make_bus(tmp_path)
    with db:
        event_id = bus.publish(
            Event(type="task.created", task_id="tsk_1", source="test", payload={"n": 1})
        )
        assert event_id == 1

        rows = db.recent_events()
        assert rows[0]["type"] == "task.created"

        records = logger.read()
        assert records[-1]["type"] == "task.created"
        assert records[-1]["payload"] == {"n": 1}


def test_exact_and_wildcard_subscribers_get_events(tmp_path: Path) -> None:
    db, bus, _ = make_bus(tmp_path)
    seen: list[str] = []
    with db:
        bus.subscribe("chat.request", lambda event: seen.append("exact"))
        bus.subscribe("*", lambda event: seen.append("any"))
        bus.emit("chat.request")
        bus.emit("chat.done")
    assert seen == ["exact", "any", "any"]


def test_broken_handler_does_not_break_publisher(tmp_path: Path) -> None:
    db, bus, _ = make_bus(tmp_path)
    seen: list[str] = []

    def broken(_event: Event) -> None:
        raise RuntimeError("boom")

    with db:
        bus.subscribe("chat.request", broken)
        bus.subscribe("chat.request", lambda event: seen.append(event.type))
        bus.emit("chat.request")

        assert seen == ["chat.request"]
        assert len(db.recent_events()) == 1


def test_emit_sets_defaults(tmp_path: Path) -> None:
    db, bus, _ = make_bus(tmp_path)
    with db:
        bus.emit("ping", payload={"ok": True})
        row = db.recent_events()[0]
        assert row["ts"]
        assert row["task_id"] is None
