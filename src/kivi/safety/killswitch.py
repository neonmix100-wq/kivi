"""Kill switch: one durable flag that stops tools and cloud calls.

The flag is a small JSON file under the Kivi home. It is checked before every
tool execution and every cloud request, so a stuck or hostile task cannot keep
acting while the user tries to stop it. The flag is deliberately fail-closed: an
unreadable or corrupt file counts as engaged.

The global hotkey belongs to the shell/tray stage; this module provides the
state, audit and CLI that the hotkey will call.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kivi.kernel.db import Database

FLAG_NAME = "killswitch.json"


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    engaged: bool
    reason: str = ""
    source: str = ""
    engaged_at: str = ""


class KillSwitch:
    """Durable emergency stop with audit for both transitions."""

    def __init__(
        self,
        path: Path,
        *,
        db: Database | None = None,
        events: Any = None,
        clock: Any = None,
        on_engage: Callable[[], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.db = db
        self.events = events
        self._clock = clock or (lambda: datetime.now(UTC))
        self._on_engage = on_engage

    def state(self) -> KillSwitchState:
        if not self.path.is_file():
            return KillSwitchState(engaged=False)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Fail closed: a damaged flag is still a stop signal.
            return KillSwitchState(
                engaged=True,
                reason="kill switch flag is unreadable; treated as engaged",
                source="unknown",
            )
        if not isinstance(data, dict):
            return KillSwitchState(
                engaged=True,
                reason="kill switch flag is invalid; treated as engaged",
                source="unknown",
            )
        return KillSwitchState(
            engaged=True,
            reason=str(data.get("reason", "")),
            source=str(data.get("source", "")),
            engaged_at=str(data.get("engaged_at", "")),
        )

    def is_engaged(self) -> bool:
        return self.state().engaged

    def engage(
        self,
        reason: str = "user requested stop",
        *,
        source: str = "cli",
    ) -> KillSwitchState:
        """Write the flag; repeating the call preserves the original timestamp."""
        current = self.state()
        if current.engaged and current.source != "unknown":
            return current
        state = KillSwitchState(
            engaged=True,
            reason=reason.strip() or "user requested stop",
            source=source,
            engaged_at=self._clock().isoformat(timespec="seconds"),
        )
        payload = {
            "reason": state.reason,
            "source": state.source,
            "engaged_at": state.engaged_at,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        self._record("engaged", state.reason)
        self._emit("engaged", state)
        if self._on_engage is not None:
            try:
                self._on_engage()
            except Exception as exc:  # noqa: BLE001 - the stop flag must still stand
                self._emit("unload_failed", state)
                if self.db is not None:
                    self.db.append_audit(
                        actor="safety",
                        action="safety.killswitch_unload",
                        target=str(self.path),
                        decision="error",
                        reason=str(exc)[:200],
                    )
        return state

    def disengage(self, *, source: str = "cli") -> KillSwitchState:
        """Remove the flag; an absent flag is already disengaged."""
        existed = self.path.exists()
        if existed:
            self.path.unlink()
        state = KillSwitchState(engaged=False)
        if existed:
            self._record("disengaged", f"by {source}")
            self._emit("disengaged", state)
        return state

    def _record(self, decision: str, reason: str) -> None:
        if self.db is None:
            return
        self.db.append_audit(
            actor="safety",
            action="safety.killswitch",
            target=str(self.path),
            decision=decision,
            reason=reason,
        )

    def _emit(self, name: str, state: KillSwitchState) -> None:
        if self.events is None:
            return
        self.events.emit(
            f"safety.killswitch_{name}",
            payload={
                "reason": state.reason,
                "source": state.source,
                "engaged_at": state.engaged_at,
            },
        )


__all__ = ["FLAG_NAME", "KillSwitch", "KillSwitchState"]
