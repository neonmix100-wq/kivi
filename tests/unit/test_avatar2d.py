from __future__ import annotations

import struct
from pathlib import Path

import pytest

from kivi.interfaces.avatar2d import (
    AvatarController,
    AvatarError,
    AvatarRenderer,
    AvatarState,
    LipSync,
    TkAvatarRenderer,
)


def pcm16(*values: int) -> bytes:
    return struct.pack(f"<{len(values)}h", *values)


def test_avatar_states_include_the_five_required_values() -> None:
    assert {state.value for state in AvatarState} == {
        "idle",
        "listening",
        "thinking",
        "speaking",
        "error",
    }


def test_controller_allows_any_state_and_requires_explicit_error() -> None:
    controller = AvatarController()
    assert controller.state is AvatarState.IDLE
    for state in AvatarState:
        assert controller.set_state(state) is state
    with pytest.raises(AvatarError, match="unknown avatar state"):
        controller.set_state("sleep")


def test_lip_sync_silence_loud_and_decay() -> None:
    lip_sync = LipSync()
    assert lip_sync.update(pcm16(*([0] * 480))) == 0.0

    loud = lip_sync.update(pcm16(*([30000] * 480)))
    assert 0.5 < loud <= 1.0

    after_one_window = lip_sync.update(pcm16(*([0] * 48)))
    assert 0.0 < after_one_window < loud
    for _ in range(20):
        after_one_window = lip_sync.update(pcm16(*([0] * 48)))
    assert after_one_window < 0.02

    assert lip_sync.openness == pytest.approx(after_one_window)


def test_lip_sync_accepts_list_of_ints_and_clamps() -> None:
    lip_sync = LipSync()
    openness = lip_sync.update([40000, -40000, 0, 0] * 100)
    assert 0.0 <= openness <= 1.0


def test_lip_sync_rejects_odd_pcm_and_bad_tuning() -> None:
    lip_sync = LipSync()
    with pytest.raises(AvatarError, match="even number"):
        lip_sync.update(b"\x00\x01\x02")
    with pytest.raises(AvatarError, match="attack"):
        LipSync(attack=0.0)
    with pytest.raises(AvatarError, match="sample_rate"):
        LipSync(sample_rate=0)


def test_controller_speaking_state_snapshot_and_events() -> None:
    events: list[dict[str, object]] = []
    controller = AvatarController(on_event=events.append)

    assert controller.snapshot() == {"state": AvatarState.IDLE, "openness": 0.0}

    controller.on_speech_start()
    assert controller.state is AvatarState.SPEAKING
    openness = controller.update_pcm(pcm16(*([28000] * 480)))
    assert openness > 0.5
    snapshot = controller.snapshot()
    assert snapshot["state"] == AvatarState.SPEAKING
    assert snapshot["openness"] == openness

    controller.on_speech_end()
    assert controller.state is AvatarState.IDLE
    assert controller.openness == 0.0
    assert [event["type"] for event in events] == [
        "speech_start",
        "lip_sync",
        "speech_end",
    ]


def test_controller_error_is_explicit_and_recoverable() -> None:
    controller = AvatarController()
    controller.set_state("error")
    assert controller.state is AvatarState.ERROR
    controller.on_speech_start()
    assert controller.state is AvatarState.SPEAKING


def test_controller_state_change_emits_event_only_when_changed() -> None:
    events: list[dict[str, object]] = []
    controller = AvatarController(on_event=events.append)
    controller.set_state(AvatarState.IDLE)
    assert events == []
    controller.set_state(AvatarState.LISTENING)
    assert len(events) == 1
    assert events[0]["state"] == AvatarState.LISTENING
    assert events[0]["previous"] == AvatarState.IDLE


class FakeRenderer:
    def __init__(self) -> None:
        self.frames: list[tuple[AvatarState, float]] = []
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def render(self, state: AvatarState, openness: float) -> None:
        self.frames.append((state, openness))

    def stop(self) -> None:
        self.stopped = True


def test_fake_renderer_satisfies_protocol() -> None:
    renderer = FakeRenderer()
    assert isinstance(renderer, AvatarRenderer)
    renderer.start()
    renderer.render(AvatarState.SPEAKING, 0.75)
    renderer.stop()
    assert renderer.started is True
    assert renderer.frames == [(AvatarState.SPEAKING, 0.75)]
    assert renderer.stopped is True


def test_tk_constructor_does_not_create_a_window(tmp_path: Path) -> None:
    renderer = TkAvatarRenderer(tmp_path / "missing.png", show=False)
    assert renderer.is_running is False
    renderer.render(AvatarState.THINKING, 0.4)
    assert renderer.is_running is False
    renderer.stop()
    with pytest.raises(AvatarError, match="unknown avatar state"):
        renderer.render("sleep", 0.0)  # type: ignore[arg-type]


def test_tk_default_portrait_is_the_concept_art() -> None:
    renderer = TkAvatarRenderer()
    assert renderer.portrait.name == "kivi_portrait.png"
    assert renderer.portrait.parent.name == "concept"
    assert renderer.is_running is False
