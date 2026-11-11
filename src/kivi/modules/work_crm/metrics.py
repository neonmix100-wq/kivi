"""Thread-safe automation metrics for the Work CRM module.

The old mode tracked timings in the UI. This headless counter is deliberately
small: one order attempt updates five counters, and ``as_dict`` returns a stable
snapshot for logs, the API and tests. A lock protects every mutation and read so
the metrics can be updated from worker threads.
"""

from __future__ import annotations

import threading
from typing import Any


class CrmMetrics:
    """Counters for orders, automation coverage, errors and elapsed time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._orders_total = 0
        self._prepared = 0
        self._filled = 0
        self._errors = 0
        self._seconds_total = 0.0

    @property
    def orders_total(self) -> int:
        """Number of recorded order attempts."""
        with self._lock:
            return self._orders_total

    @property
    def prepared(self) -> int:
        """Number of orders for which drafts were prepared."""
        with self._lock:
            return self._prepared

    @property
    def filled(self) -> int:
        """Number of orders that were filled automatically end to end."""
        with self._lock:
            return self._filled

    @property
    def errors(self) -> int:
        """Number of order attempts that ended with an error."""
        with self._lock:
            return self._errors

    @property
    def seconds_total(self) -> float:
        """Total processing time across all orders, in seconds."""
        with self._lock:
            return self._seconds_total

    @property
    def automation_rate(self) -> float:
        """Share of orders filled automatically; ``0.0`` when none were recorded."""
        with self._lock:
            if not self._orders_total:
                return 0.0
            return self._filled / self._orders_total

    @property
    def average_seconds(self) -> float:
        """Average processing time per order; ``0.0`` when none were recorded."""
        with self._lock:
            if not self._orders_total:
                return 0.0
            return self._seconds_total / self._orders_total

    def record_order(
        self,
        seconds: float,
        prepared: bool = False,
        filled: bool = False,
        error: bool = False,
    ) -> None:
        """Record one order attempt and its outcome."""
        elapsed = max(float(seconds), 0.0)
        with self._lock:
            self._orders_total += 1
            self._seconds_total += elapsed
            if prepared:
                self._prepared += 1
            if filled:
                self._filled += 1
            if error:
                self._errors += 1

    def reset(self) -> None:
        """Reset all counters to zero."""
        with self._lock:
            self._orders_total = 0
            self._prepared = 0
            self._filled = 0
            self._errors = 0
            self._seconds_total = 0.0

    def as_dict(self) -> dict[str, Any]:
        """Return a consistent snapshot of all counters and derived rates."""
        with self._lock:
            total = self._orders_total
            filled = self._filled
            seconds = self._seconds_total
            return {
                "orders_total": total,
                "prepared": self._prepared,
                "filled": filled,
                "errors": self._errors,
                "seconds_total": seconds,
                "automation_rate": (filled / total) if total else 0.0,
                "average_seconds": (seconds / total) if total else 0.0,
            }
