"""Model Gateway: budget checks, leases and eviction.

The gateway is the only component allowed to load or unload a model. It keeps
one heavy model resident at a time, counts VRAM from real observations, and
records every load, eviction and failure as an event.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from kivi.models.backends.base import BackendError, ModelBackend
from kivi.models.registry import ModelRegistry, ModelSpec, RegistryError
from kivi.models.vram import VramSnapshot, query_vram

log = logging.getLogger(__name__)

VramSource = Callable[[], VramSnapshot | None]


class GatewayError(RuntimeError):
    """Base class for gateway failures."""


class InsufficientVram(GatewayError):
    """The model does not fit even after evicting every evictable lease."""


class ModelBusy(GatewayError):
    """A pinned or higher-priority model prevents the requested layout."""


@dataclass(slots=True)
class Lease:
    model_id: str
    task_id: str
    priority: int
    exclusive: bool
    ttl_s: int
    vram_mb: int
    created_at: float
    last_used: float
    pinned: bool = False

    def touch(self, now: float) -> None:
        self.last_used = now

    def expired(self, now: float) -> bool:
        return self.ttl_s > 0 and now - self.last_used >= self.ttl_s

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "task_id": self.task_id,
            "priority": self.priority,
            "exclusive": self.exclusive,
            "ttl_s": self.ttl_s,
            "vram_mb": self.vram_mb,
            "created_at": self.created_at,
            "last_used": self.last_used,
            "pinned": self.pinned,
        }


class ModelGateway:
    def __init__(
        self,
        registry: ModelRegistry,
        backend: ModelBackend,
        *,
        vram_total_mb: int = 8192,
        safety_margin_mb: int = 512,
        reserve_mb: int = 0,
        vram_source: VramSource = query_vram,
        clock: Callable[[], float] = time.monotonic,
        events: Any = None,
    ) -> None:
        if vram_total_mb <= 0:
            raise ValueError("vram_total_mb must be positive")
        if safety_margin_mb < 0 or reserve_mb < 0:
            raise ValueError("budget margins must be non-negative")
        self.registry = registry
        self.backend = backend
        self.vram_total_mb = vram_total_mb
        self.safety_margin_mb = safety_margin_mb
        self.reserve_mb = reserve_mb
        self._vram_source = vram_source
        self._clock = clock
        self._events = events
        self._leases: dict[str, Lease] = {}
        self._managed: set[str] = set()
        self._lock = threading.RLock()
        self._metrics: dict[str, int | float] = {
            "loads": 0,
            "unloads": 0,
            "evictions": 0,
            "failures": 0,
            "lost": 0,
            "orphan_unloads": 0,
            "last_load_ms": 0.0,
        }

    # -- events ----------------------------------------------------------
    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._events is None:
            return
        try:
            self._events.emit(event_type, source="model_gateway", payload=payload)
        except Exception:  # noqa: BLE001 - telemetry must not break the gateway
            log.exception("failed to emit %s", event_type)

    # -- inspection ------------------------------------------------------
    def vram(self) -> VramSnapshot | None:
        return self._vram_source()

    def leases(self) -> tuple[Lease, ...]:
        with self._lock:
            return tuple(self._leases.values())

    def loaded(self) -> dict[str, int]:
        with self._lock:
            return {model_id: lease.vram_mb for model_id, lease in self._leases.items()}

    def metrics(self) -> dict[str, int | float]:
        with self._lock:
            return dict(self._metrics)

    def status(self) -> dict[str, Any]:
        """Serialisable snapshot used by the watchdog and CLI."""
        with self._lock:
            snapshot = self.vram()
            return {
                "leases": [lease.as_dict() for lease in self._leases.values()],
                "managed": sorted(self._managed),
                "metrics": dict(self._metrics),
                "vram": snapshot.as_dict() if snapshot is not None else None,
            }

    def _required_mb(self, spec: ModelSpec) -> int:
        return spec.vram_mb + self.safety_margin_mb + self.reserve_mb

    def _eviction_order(self) -> list[Lease]:
        """Background first, then least recently used, then the largest."""
        leases = [lease for lease in self._leases.values() if not lease.pinned]
        return sorted(
            leases,
            key=lambda lease: (-lease.priority, lease.last_used, -lease.vram_mb),
        )

    # -- loading ---------------------------------------------------------
    def acquire(
        self,
        model_id: str,
        *,
        task_id: str = "",
        priority: int = 5,
        ttl_s: int = 300,
    ) -> Lease:
        """Ensure ``model_id`` is loaded and return its lease."""
        spec = self.registry.require(model_id)
        now = self._clock()

        with self._lock:
            existing = self._leases.get(model_id)
            if existing is not None:
                existing.touch(now)
                return existing

            victims: list[Lease] = []
            if spec.exclusive:
                victims = [lease for lease in self._leases.values() if not lease.pinned]
                blocked = [lease for lease in self._leases.values() if lease.pinned]
                if blocked:
                    raise ModelBusy(
                        "exclusive model is blocked by pinned: "
                        + ", ".join(lease.model_id for lease in blocked)
                    )

            snapshot = self.vram()
            if snapshot is not None:
                if snapshot.overcommitted and spec.vram_mb >= 4000:
                    self._metrics["failures"] += 1
                    raise InsufficientVram(
                        "shared GPU memory is active; heavy loads are refused until "
                        f"VRAM is free (used {snapshot.used_mb} MB, free {snapshot.free_mb} MB)"
                    )
                required = self._required_mb(spec)
                available = snapshot.free_mb + sum(lease.vram_mb for lease in victims)
                if available < required:
                    for lease in self._eviction_order():
                        if lease in victims:
                            continue
                        victims.append(lease)
                        available += lease.vram_mb
                        if available >= required:
                            break
                if available < required:
                    self._metrics["failures"] += 1
                    raise InsufficientVram(
                        f"{model_id} needs {required} MB, "
                        f"only {available} MB can be freed "
                        f"(free {snapshot.free_mb} MB, total {snapshot.total_mb} MB)"
                    )

            for victim in victims:
                self._evict(victim, reason="budget")

            started = self._clock()
            try:
                self.backend.load(model_id)
            except BackendError:
                self._metrics["failures"] += 1
                raise
            elapsed_ms = (self._clock() - started) * 1000
            lease = Lease(
                model_id=model_id,
                task_id=task_id,
                priority=priority,
                exclusive=spec.exclusive,
                ttl_s=ttl_s,
                vram_mb=spec.vram_mb,
                created_at=now,
                last_used=now,
                pinned=spec.pinned,
            )
            self._leases[model_id] = lease
            self._managed.add(model_id)
            self._metrics["loads"] += 1
            self._metrics["last_load_ms"] = round(elapsed_ms, 1)
            self._emit(
                "model.loaded",
                {
                    "model_id": model_id,
                    "task_id": task_id,
                    "vram_mb": spec.vram_mb,
                    "load_ms": round(elapsed_ms, 1),
                    "evicted": [victim.model_id for victim in victims],
                },
            )
            return lease

    def _evict(self, lease: Lease, *, reason: str) -> None:
        try:
            self.backend.unload(lease.model_id)
        except BackendError as exc:
            self._emit(
                "model.unload_failed",
                {"model_id": lease.model_id, "reason": reason, "error": str(exc)},
            )
        self._leases.pop(lease.model_id, None)
        self._metrics["unloads"] += 1
        self._metrics["evictions"] += 1
        self._emit(
            "model.unloaded",
            {"model_id": lease.model_id, "reason": reason, "task_id": lease.task_id},
        )

    def release(self, model_id: str) -> None:
        with self._lock:
            lease = self._leases.get(model_id)
            if lease is None:
                return
            self._evict(lease, reason="release")

    def forget(self, model_id: str, *, reason: str = "backend_missing") -> bool:
        """Drop a lease whose model is no longer loaded by the backend.

        Unlike :meth:`release` this does not call the backend: the model is
        already gone, so an unload request would only produce a spurious error.
        """
        with self._lock:
            lease = self._leases.pop(model_id, None)
            if lease is None:
                return False
            self._metrics["lost"] += 1
            self._emit(
                "model.lost",
                {"model_id": model_id, "reason": reason, "task_id": lease.task_id},
            )
            return True

    def reconcile(self, running: dict[str, int]) -> tuple[list[str], list[str]]:
        """Align leases with what the backend reports as loaded.

        Returns ``(missing, orphans)``: leased models the backend lost, and
        models Kivi loaded earlier that now have no lease.
        """
        with self._lock:
            missing = [model_id for model_id in self._leases if model_id not in running]
            for model_id in missing:
                self.forget(model_id)

            orphans = [
                model_id
                for model_id in running
                if model_id not in self._leases and model_id in self._managed
            ]
            for model_id in orphans:
                self._unload_orphan(model_id)
            return missing, orphans

    def _unload_orphan(self, model_id: str) -> None:
        try:
            self.backend.unload(model_id)
        except BackendError as exc:
            self._metrics["failures"] += 1
            self._emit(
                "model.unload_failed",
                {"model_id": model_id, "reason": "orphan", "error": str(exc)},
            )
            return
        self._metrics["orphan_unloads"] += 1
        self._emit("model.orphan_unloaded", {"model_id": model_id})

    def expire(self) -> list[str]:
        """Unload leases whose TTL has elapsed; return the evicted ids."""
        now = self._clock()
        expired = [lease.model_id for lease in self._leases.values() if lease.expired(now)]
        for model_id in expired:
            self.release(model_id)
        return expired

    # -- inference -------------------------------------------------------
    def chat(
        self,
        model_id: str,
        messages: list[dict[str, str]],
        *,
        task_id: str = "",
        priority: int = 5,
        keep_alive: str = "5m",
        options: dict[str, Any] | None = None,
    ) -> str:
        lease = self.acquire(
            model_id,
            task_id=task_id,
            priority=priority,
            ttl_s=max(1, int(self._ttl_from_keep_alive(keep_alive))),
        )
        try:
            answer = self.backend.chat(
                model_id,
                messages,
                keep_alive=keep_alive,
                options=options,
            )
        except BackendError:
            self._metrics["failures"] += 1
            raise
        with self._lock:
            lease.touch(self._clock())
        return answer

    def chat_stream(
        self,
        model_id: str,
        messages: list[dict[str, str]],
        *,
        task_id: str = "",
        priority: int = 5,
        keep_alive: str = "5m",
        options: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        """Stream answer chunks, keeping the lease warm until the last chunk."""
        lease = self.acquire(
            model_id,
            task_id=task_id,
            priority=priority,
            ttl_s=max(1, int(self._ttl_from_keep_alive(keep_alive))),
        )
        try:
            yield from self.backend.chat_stream(
                model_id,
                messages,
                keep_alive=keep_alive,
                options=options,
            )
        except BackendError:
            self._metrics["failures"] += 1
            raise
        finally:
            with self._lock:
                lease.touch(self._clock())

    @staticmethod
    def _ttl_from_keep_alive(keep_alive: str) -> float:
        text = keep_alive.strip().lower()
        if text in ("0", "0s"):
            return 0
        units = {"s": 1, "m": 60, "h": 3600}
        if text and text[-1] in units:
            try:
                return float(text[:-1]) * units[text[-1]]
            except ValueError:
                pass
        return 300.0

    def close(self) -> None:
        with self._lock:
            for lease in list(self._leases.values()):
                self._evict(lease, reason="shutdown")


def gateway_from_config(config: Any, backend: ModelBackend, **kwargs: Any) -> ModelGateway:
    """Build a gateway from ``KiviConfig.model_gateway`` defaults."""
    settings = config.model_gateway
    return ModelGateway(
        kwargs.pop("registry", ModelRegistry()),
        backend,
        vram_total_mb=settings.vram_total_mb,
        safety_margin_mb=settings.safety_margin_mb,
        **kwargs,
    )


__all__ = [
    "GatewayError",
    "InsufficientVram",
    "Lease",
    "ModelBusy",
    "ModelGateway",
    "RegistryError",
    "gateway_from_config",
]
