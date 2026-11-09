from __future__ import annotations

from typing import Any

import pytest

from kivi.models.backends.base import BackendUnavailable
from kivi.models.gateway import ModelGateway
from kivi.models.registry import ModelRegistry, ModelSpec
from kivi.models.vram import VramSnapshot
from kivi.models.watchdog import GatewayWatchdog

SMALL = ModelSpec(id="small", tier=0, role="router", vram_mb=3000)


class FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.loaded: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.running_error: Exception | None = None

    def list_models(self) -> list[str]:
        return ["small"]

    def running(self) -> dict[str, int]:
        if self.running_error is not None:
            raise self.running_error
        return {model_id: 0 for model_id in self.loaded}

    def load(
        self,
        model_id: str,
        keep_alive: str = "5m",
        options: dict | None = None,
    ) -> None:
        self.calls.append(("load", model_id))
        self.loaded.add(model_id)

    def unload(self, model_id: str) -> None:
        self.calls.append(("unload", model_id))
        self.loaded.discard(model_id)

    def chat(
        self,
        model_id: str,
        messages: list[dict[str, str]],
        *,
        keep_alive: str = "5m",
        options: dict[str, Any] | None = None,
    ) -> str:
        return "ok"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeEvents:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **kwargs: Any) -> None:
        self.events.append((event_type, kwargs.get("payload", {})))


def make_gateway() -> tuple[ModelGateway, FakeBackend, FakeClock]:
    backend = FakeBackend()
    clock = FakeClock()
    snapshot = VramSnapshot(total_mb=8192, used_mb=192, free_mb=8000)
    gateway = ModelGateway(
        ModelRegistry([SMALL]),
        backend,
        vram_total_mb=8192,
        safety_margin_mb=512,
        vram_source=lambda: snapshot,
        clock=clock,
    )
    return gateway, backend, clock


def test_tick_expires_idle_lease_and_emits_summary() -> None:
    gateway, backend, clock = make_gateway()
    gateway.acquire("small", ttl_s=10)
    clock.now += 11
    events = FakeEvents()
    watchdog = GatewayWatchdog(gateway, interval_s=1, events=events)

    result = watchdog.tick()

    assert result.expired == ("small",)
    assert result.missing == ()
    assert result.orphans == ()
    assert result.error is None
    assert result.vram == {"total_mb": 8192, "used_mb": 192, "free_mb": 8000}
    assert backend.loaded == set()
    assert [event_type for event_type, _ in events.events][-1] == "model.watchdog"
    assert events.events[-1][1]["expired"] == ["small"]


def test_tick_reports_backend_failure_without_dropping_leases() -> None:
    gateway, backend, _ = make_gateway()
    gateway.acquire("small")
    backend.running_error = BackendUnavailable("ollama is down")
    watchdog = GatewayWatchdog(gateway, interval_s=1)

    result = watchdog.tick()

    assert result.error == "ollama is down"
    assert result.missing == ()
    assert gateway.loaded() == {"small": 3000}


def test_start_is_idempotent_and_stop_joins_thread() -> None:
    gateway, _, _ = make_gateway()
    watchdog = GatewayWatchdog(gateway, interval_s=0.01)
    watchdog.start()
    thread = watchdog._thread
    watchdog.start()
    assert watchdog._thread is thread
    watchdog.stop(timeout=1)
    assert not watchdog.is_running


def test_invalid_interval_is_rejected() -> None:
    gateway, _, _ = make_gateway()
    with pytest.raises(ValueError, match="positive"):
        GatewayWatchdog(gateway, interval_s=0)
