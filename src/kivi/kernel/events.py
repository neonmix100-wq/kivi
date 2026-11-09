"""In-process event bus.

Publishers never depend on subscribers: every event is persisted in SQLite and
mirrored to JSONL before handlers run. A broken handler is logged, not allowed
to break the publisher.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kivi.kernel.db import Database, utc_now
from kivi.kernel.logs import JsonlLogger

Handler = Callable[["Event"], None]
log = logging.getLogger(__name__)


@dataclass(slots=True)
class Event:
    type: str
    task_id: str | None = None
    step_id: str | None = None
    source: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=utc_now)

    def as_record(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "type": self.type,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "source": self.source,
            "payload": self.payload,
        }


class EventBus:
    def __init__(self, db: Database, logger: JsonlLogger | None = None) -> None:
        self.db = db
        self.logger = logger
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._any: list[Handler] = []

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """Subscribe to an exact type, or to ``*`` for every event."""
        if event_type == "*":
            self._any.append(handler)
        else:
            self._handlers[event_type].append(handler)

    def publish(self, event: Event) -> int:
        event_id = self.db.append_event(
            event.type,
            task_id=event.task_id,
            step_id=event.step_id,
            source=event.source,
            payload=event.payload,
        )
        if self.logger is not None:
            self.logger.write(event.as_record())
        for handler in (*self._handlers.get(event.type, ()), *self._any):
            try:
                handler(event)
            except Exception:  # noqa: BLE001 - a subscriber must not break the bus
                log.exception("event handler failed for %s", event.type)
        return event_id

    def emit(self, event_type: str, **kwargs: Any) -> int:
        return self.publish(Event(type=event_type, **kwargs))
