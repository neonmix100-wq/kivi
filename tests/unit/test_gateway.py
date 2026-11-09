from __future__ import annotations

import threading
import time

import pytest

from kivi.models.gateway import (
    InsufficientVram,
    ModelBusy,
    ModelGateway,
)
from kivi.models.registry import ModelRegistry, ModelSpec
from kivi.models.vram import VramSnapshot

SMALL = ModelSpec(id="small", tier=0, role="router", vram_mb=3000, exclusive=False)
HEAVY = ModelSpec(id="heavy", tier=1, role="chat", vram_mb=6000, exclusive=True)
PINNED = ModelSpec(id="pinned", tier=3, role="asr", vram_mb=500, pinned=True)


class FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.loaded: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.load_options: dict[str, dict | None] = {}
        self.last_think: bool | None = None

    def list_models(self) -> list[str]:
        return ["small", "heavy", "pinned"]

    def running(self) -> dict[str, int]:
        return {model_id: 0 for model_id in self.loaded}

    def load(
        self,
        model_id: str,
        keep_alive: str = "5m",
        options: dict | None = None,
        embedding: bool = False,
    ) -> None:
        self.calls.append(("load", model_id))
        self.load_options[model_id] = options
        self.loaded.add(model_id)

    def embed(self, model_id, inputs, *, keep_alive="5m", options=None):
        self.calls.append(("embed", model_id))
        return [[float(len(text))] for text in inputs]

    def unload(self, model_id: str) -> None:
        self.calls.append(("unload", model_id))
        self.loaded.discard(model_id)

    def chat(self, model_id, messages, *, keep_alive="5m", options=None, think=None) -> str:
        self.calls.append(("chat", model_id))
        self.last_think = think
        return f"echo:{messages[-1]['content']}"

    def chat_stream(self, model_id, messages, *, keep_alive="5m", options=None, think=None):
        self.calls.append(("stream", model_id))
        self.last_think = think
        yield "echo:"
        yield messages[-1]["content"]


class FakeVram:
    def __init__(
        self, free_mb: int = 8000, total_mb: int = 8192, used_mb: int | None = None
    ) -> None:
        self.free_mb = free_mb
        self.total_mb = total_mb
        self.used_mb = total_mb - free_mb if used_mb is None else used_mb

    def __call__(self) -> VramSnapshot:
        return VramSnapshot(
            total_mb=self.total_mb,
            used_mb=self.used_mb,
            free_mb=self.free_mb,
        )


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeEvents:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **kwargs) -> None:
        self.events.append((event_type, kwargs.get("payload", {})))


def make_gateway(
    *,
    free_mb: int = 8000,
    used_mb: int | None = None,
    models: tuple[ModelSpec, ...] = (SMALL, HEAVY, PINNED),
    events: FakeEvents | None = None,
    allow_cpu_fallback: bool = False,
) -> tuple[ModelGateway, FakeBackend, FakeVram, FakeClock]:
    backend = FakeBackend()
    vram = FakeVram(free_mb, used_mb=used_mb)
    clock = FakeClock()
    gateway = ModelGateway(
        ModelRegistry(list(models)),
        backend,
        vram_total_mb=8192,
        safety_margin_mb=512,
        vram_source=vram,
        clock=clock,
        events=events,
        allow_cpu_fallback=allow_cpu_fallback,
    )
    return gateway, backend, vram, clock


def test_acquire_loads_and_release_unloads() -> None:
    gateway, backend, _, _ = make_gateway()
    lease = gateway.acquire("small", task_id="tsk_1", priority=0)
    assert lease.model_id == "small"
    assert backend.calls == [("load", "small")]
    assert gateway.loaded() == {"small": 3000}

    gateway.release("small")
    assert backend.calls[-1] == ("unload", "small")
    assert gateway.loaded() == {}


def test_repeat_acquire_is_idempotent() -> None:
    gateway, backend, _, _ = make_gateway()
    first = gateway.acquire("small")
    second = gateway.acquire("small", task_id="other")
    assert first is second
    assert backend.calls == [("load", "small")]
    assert second.task_id == ""


def test_insufficient_vram_refuses_before_loading() -> None:
    gateway, backend, _, _ = make_gateway(free_mb=4000)
    with pytest.raises(InsufficientVram, match="needs 6512"):
        gateway.acquire("heavy")
    assert backend.calls == []
    assert gateway.metrics()["failures"] == 1


def test_cpu_fallback_replaces_insufficient_vram() -> None:
    events = FakeEvents()
    gateway, backend, _, _ = make_gateway(
        free_mb=1000,
        events=events,
        allow_cpu_fallback=True,
    )
    lease = gateway.acquire("heavy")
    assert lease.cpu is True
    assert backend.load_options["heavy"] == {"num_ctx": 4096, "num_gpu": 0}
    assert gateway.metrics()["cpu_fallbacks"] == 1
    assert any(name == "model.fallback_cpu" for name, _ in events.events)


def test_cpu_fallback_on_shared_memory() -> None:
    gateway, backend, _, _ = make_gateway(
        free_mb=500,
        used_mb=7300,
        allow_cpu_fallback=True,
    )
    lease = gateway.acquire("heavy")
    assert lease.cpu is True
    assert backend.load_options["heavy"]["num_gpu"] == 0


def test_shared_memory_refuses_heavy_load() -> None:
    gateway, backend, _, _ = make_gateway(free_mb=500, used_mb=7300)
    with pytest.raises(InsufficientVram, match="shared GPU memory"):
        gateway.acquire("heavy")
    assert backend.calls == []


def test_budget_evicts_small_model_for_heavy() -> None:
    gateway, backend, _, _ = make_gateway(free_mb=4000)
    gateway.acquire("small")
    gateway.acquire("heavy")

    assert backend.calls == [
        ("load", "small"),
        ("unload", "small"),
        ("load", "heavy"),
    ]
    assert gateway.loaded() == {"heavy": 6000}
    assert gateway.metrics()["evictions"] == 1


def test_exclusive_model_evicts_even_when_vram_is_free() -> None:
    gateway, backend, _, _ = make_gateway(free_mb=8000)
    gateway.acquire("small")
    gateway.acquire("heavy")
    assert backend.calls == [("load", "small"), ("unload", "small"), ("load", "heavy")]


def test_pinned_model_blocks_exclusive_load() -> None:
    gateway, backend, _, _ = make_gateway(free_mb=8000)
    gateway.acquire("pinned")
    with pytest.raises(ModelBusy, match="pinned"):
        gateway.acquire("heavy")
    assert backend.loaded == {"pinned"}


def test_exclusive_waits_for_pinned_release() -> None:
    gateway, backend, _, _ = make_gateway()
    gateway.acquire("pinned")

    def release_later() -> None:
        time.sleep(0.05)
        gateway.release("pinned")

    worker = threading.Thread(target=release_later)
    worker.start()
    try:
        lease = gateway.acquire("heavy", wait_s=2)
    finally:
        worker.join(timeout=2)

    assert lease.model_id == "heavy"
    assert "heavy" in backend.loaded
    assert gateway.metrics()["waits"] == 1


def test_expired_lease_is_unloaded() -> None:
    gateway, backend, _, clock = make_gateway()
    gateway.acquire("small", ttl_s=10)
    clock.now += 11
    assert gateway.expire() == ["small"]
    assert backend.loaded == set()


def test_chat_touches_lease_and_returns_answer() -> None:
    gateway, backend, _, clock = make_gateway()
    answer = gateway.chat("small", [{"role": "user", "content": "hi"}], keep_alive="10m")
    assert answer == "echo:hi"
    lease = gateway.leases()[0]
    assert lease.ttl_s == 600
    clock.now += 5
    assert gateway.expire() == []


def test_keep_alive_parsing() -> None:
    assert ModelGateway._ttl_from_keep_alive("0") == 0
    assert ModelGateway._ttl_from_keep_alive("30s") == 30
    assert ModelGateway._ttl_from_keep_alive("5m") == 300
    assert ModelGateway._ttl_from_keep_alive("2h") == 7200
    assert ModelGateway._ttl_from_keep_alive("garbage") == 300


def test_embed_returns_vectors_and_keeps_lease() -> None:
    gateway, backend, _, _ = make_gateway()
    vectors = gateway.embed("small", ["a", "bb"])
    assert vectors == [[1.0], [2.0]]
    assert backend.calls == [("load", "small"), ("embed", "small")]
    assert "small" in gateway.loaded()


def test_load_pins_registry_context() -> None:
    gateway, backend, _, _ = make_gateway()
    gateway.acquire("small")
    assert backend.load_options["small"] == {"num_ctx": 4096}


def test_chat_disables_thinking_by_default() -> None:
    gateway, backend, _, _ = make_gateway()
    gateway.chat("small", [{"role": "user", "content": "hi"}])
    assert backend.last_think is False


def test_stream_keeps_lease_and_returns_chunks() -> None:
    gateway, backend, _, _ = make_gateway()
    chunks = list(gateway.chat_stream("small", [{"role": "user", "content": "hi"}]))
    assert chunks == ["echo:", "hi"]
    assert backend.calls == [("load", "small"), ("stream", "small")]
    assert "small" in gateway.loaded()


def test_events_and_metrics_are_recorded() -> None:
    events = FakeEvents()
    gateway, _, _, _ = make_gateway(events=events)
    gateway.acquire("small", task_id="tsk_1")
    gateway.release("small")

    types = [event_type for event_type, _ in events.events]
    assert types == ["model.loaded", "model.unloaded"]
    assert events.events[0][1]["model_id"] == "small"
    assert events.events[1][1]["reason"] == "release"

    metrics = gateway.metrics()
    assert metrics["loads"] == 1
    assert metrics["unloads"] == 1


def test_reconcile_forgets_lease_lost_by_backend() -> None:
    gateway, backend, _, _ = make_gateway()
    gateway.acquire("small")
    backend.loaded.clear()

    missing, orphans = gateway.reconcile({})

    assert missing == ["small"]
    assert orphans == []
    assert gateway.loaded() == {}
    assert gateway.metrics()["lost"] == 1


def test_reconcile_unloads_managed_orphan() -> None:
    gateway, backend, _, _ = make_gateway()
    gateway.acquire("small")
    gateway.forget("small")
    assert backend.loaded == {"small"}

    missing, orphans = gateway.reconcile({"small": 3000})

    assert missing == []
    assert orphans == ["small"]
    assert backend.loaded == set()
    assert gateway.metrics()["orphan_unloads"] == 1


def test_status_reports_leases_and_vram() -> None:
    gateway, _, _, _ = make_gateway()
    gateway.acquire("small", task_id="tsk_1")
    status = gateway.status()
    assert status["leases"][0]["model_id"] == "small"
    assert status["managed"] == ["small"]
    assert status["vram"]["total_mb"] == 8192


def test_close_unloads_everything() -> None:
    gateway, backend, _, _ = make_gateway()
    gateway.acquire("small")
    gateway.close()
    assert backend.loaded == set()
    assert gateway.loaded() == {}


def test_negative_budget_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        ModelGateway(ModelRegistry([SMALL]), FakeBackend(), vram_total_mb=0)
    with pytest.raises(ValueError, match="non-negative"):
        ModelGateway(ModelRegistry([SMALL]), FakeBackend(), safety_margin_mb=-1)
