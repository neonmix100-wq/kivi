"""Background watchdog for the Model Gateway.

A long-running Kivi process must eventually return VRAM to the desktop. The
watchdog periodically expires idle leases, reconciles the gateway's view with
the backend's ``/api/ps`` and records a compact status event.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kivi.models.backends.base import BackendError
from kivi.models.gateway import ModelGateway

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WatchdogResult:
    expired: tuple[str, ...]
    missing: tuple[str, ...]
    orphans: tuple[str, ...]
    vram: dict[str, int] | None
    metrics: dict[str, int | float]
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "expired": list(self.expired),
            "missing": list(self.missing),
            "orphans": list(self.orphans),
            "vram": self.vram,
            "metrics": self.metrics,
            "error": self.error,
        }


class GatewayWatchdog:
    """Periodic lease expiry and backend reconciliation.

    The thread is a convenience for a long-running service; tests and the
    benchmark call :meth:`tick` directly.
    """

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        interval_s: float = 30.0,
        events: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self.gateway = gateway
        self.interval_s = interval_s
        self._events = events
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _emit(self, result: WatchdogResult) -> None:
        if self._events is None:
            return
        try:
            self._events.emit(
                "model.watchdog",
                source="model_watchdog",
                payload={
                    **result.as_dict(),
                    "at": self._clock(),
                },
            )
        except Exception:  # noqa: BLE001 - telemetry must not stop the watchdog
            log.exception("failed to emit model.watchdog")

    def tick(self) -> WatchdogResult:
        """Run one reconciliation pass."""
        expired = tuple(self.gateway.expire())

        error: str | None = None
        missing: tuple[str, ...] = ()
        orphans: tuple[str, ...] = ()
        try:
            running = self.gateway.backend.running()
        except (BackendError, OSError) as exc:
            error = str(exc)
            running = None

        if running is not None:
            missing_list, orphan_list = self.gateway.reconcile(running)
            missing = tuple(missing_list)
            orphans = tuple(orphan_list)

        result = WatchdogResult(
            expired=expired,
            missing=missing,
            orphans=orphans,
            vram=(snapshot.as_dict() if (snapshot := self.gateway.vram()) else None),
            metrics=self.gateway.metrics(),
            error=error,
        )
        self._emit(result)
        return result

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="kivi-model-watchdog",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a watchdog must survive a bad pass
                log.exception("watchdog tick failed")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None


__all__ = ["GatewayWatchdog", "WatchdogResult"]
