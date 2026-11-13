"""Unit tests for voice session orchestration, barge-in and confirmations.

No microphone, audio device, model or background thread is used here: the
source, VAD segmenter and transcriber are small fakes with the documented
duck-typed interfaces.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest

from kivi.senses.voice import (
    BargeInController,
    VoiceConfirmation,
    VoiceMetrics,
    VoiceSession,
    format_metrics,
    has_wake_word,
    is_stop_phrase,
    strip_wake_word,
)


@dataclass(frozen=True)
class FakeSegment:
    """Minimal VAD segment: exactly the fields the session reads."""

    pcm: bytes
    duration_s: float = 0.25


@dataclass(frozen=True)
class FakeTranscript:
    """Minimal transcript object with the expected ``text`` attribute."""

    text: str


class FakeSource:
    """Stateful frame source: each call resumes the same iterator."""

    def __init__(self, frames: list[bytes]) -> None:
        self._iterator = iter(frames)

    def frames(self) -> Any:
        return self._iterator


class FakeSegmenter:
    """Return one queued segment per ``feed`` call, then ``None``."""

    def __init__(self, segments: list[Any] | None = None) -> None:
        self._segments = list(segments or [])
        self.fed: list[Any] = []

    def feed(self, frame: bytes) -> Any | None:
        self.fed.append(frame)
        if not self._segments:
            return None
        return self._segments.pop(0)

    @property
    def remaining(self) -> list[Any]:
        return list(self._segments)


class FakeTranscriber:
    """Configurable transcriber with call recording and failure injection."""

    def __init__(
        self,
        text: str = "Киви, привет",
        *,
        error: Exception | None = None,
        on_call: Callable[[bytes], None] | None = None,
    ) -> None:
        self.text = text
        self.error = error
        self.on_call = on_call
        self.calls: list[bytes] = []

    def transcribe(self, pcm: bytes) -> FakeTranscript:
        self.calls.append(pcm)
        if self.on_call is not None:
            self.on_call(pcm)
        if self.error is not None:
            raise self.error
        return FakeTranscript(self.text)


def make_session(
    *,
    frames: list[bytes] | None = None,
    segments: list[Any] | None = None,
    text: str = "Киви, привет",
    answer: Callable[[str], str] | None = None,
    speak: Callable[[str], object] | None = None,
    wake: Callable[[str], bool] | None = None,
    frame_wake: Callable[[Any], bool] | None = None,
    barge_in: BargeInController | None = None,
    max_turns: int = 1,
) -> tuple[VoiceSession, FakeSource, FakeSegmenter, FakeTranscriber]:
    """Build a session with fake ports and sensible one-segment defaults."""
    source = FakeSource(frames if frames is not None else [b"f1"])
    segmenter = FakeSegmenter(
        segments if segments is not None else [FakeSegment(b"pcm")]
    )
    transcriber = FakeTranscriber(text)
    session = VoiceSession(
        source,
        segmenter,
        transcriber,
        answer=answer if answer is not None else lambda value: "ответ",
        speak=speak,
        wake=wake,
        frame_wake=frame_wake,
        barge_in=barge_in,
        max_turns=max_turns,
    )
    return session, source, segmenter, transcriber


@pytest.mark.parametrize(
    "text",
    [
        "стоп",
        "СТОП!",
        "  Киви,   стоп  ",
        "киви стоп",
        "kivi stop",
        "KIVI, STOP!",
        "стоп, пожалуйста",
        "kivi stop now",
        "Киви — стоп",
    ],
)
def test_stop_phrase_variants(text: str) -> None:
    assert is_stop_phrase(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "привет",
        "киви",
        "остановись",
        "расскажи про стоп-кран",
        "stop the music",
        "please stop the music",
        "стоп " + "и ещё очень много лишних слов " * 4,
    ],
)
def test_stop_phrase_rejects_normal_speech(text: str) -> None:
    assert not is_stop_phrase(text)


def test_stop_phrase_custom_list() -> None:
    assert is_stop_phrase("Хватит!", phrases=["хватит"])
    assert not is_stop_phrase("Хватит!", phrases=["стоп"])
    controller = BargeInController(phrases=["хватит"])
    assert controller.check("хватит") is True
    assert controller.is_stopped is True


def test_barge_in_request_stop_and_callback() -> None:
    calls: list[str] = []
    controller = BargeInController(on_stop=lambda: calls.append("one"))

    assert controller.is_stopped is False
    assert controller.request_stop() is True
    assert controller.is_stopped is True
    assert calls == ["one"]
    assert controller.request_stop() is False
    assert calls == ["one"]


def test_barge_in_callback_errors_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[str] = []

    def broken() -> None:
        raise RuntimeError("callback boom")

    controller = BargeInController(on_stop=broken)
    controller.on_stop(lambda: calls.append("after"))

    with caplog.at_level(logging.ERROR, logger="kivi.senses.voice"):
        assert controller.request_stop() is True

    assert calls == ["after"]
    assert controller.is_stopped is True
    assert "barge-in stop callback failed" in caplog.text


def test_barge_in_reset_allows_new_stop() -> None:
    calls: list[str] = []
    controller = BargeInController()
    controller.on_stop(lambda: calls.append("stop"))

    controller.request_stop()
    controller.reset()
    assert controller.is_stopped is False
    assert controller.wait(0.01) is False

    controller.request_stop()
    assert calls == ["stop", "stop"]


def test_barge_in_check_and_wait() -> None:
    controller = BargeInController()

    assert controller.check("Киви, стоп") is True
    assert controller.is_stopped is True
    assert controller.wait(0) is True

    controller.reset()
    assert controller.check("Киви, который час?") is False
    assert controller.is_stopped is False


@pytest.mark.parametrize(
    "text",
    [
        "да",
        "Да!",
        "yes",
        "конечно",
        "подтверждаю",
        "ага",
        "да, конечно",
        "yes please",
    ],
)
def test_confirmation_yes(text: str) -> None:
    assert VoiceConfirmation.parse(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "нет",
        "No.",
        "отмена",
        "не надо",
        "не подтверждаю",
        "no thanks",
        "нет, не надо",
        "неа",
    ],
)
def test_confirmation_no(text: str) -> None:
    assert VoiceConfirmation.parse(text) is False


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "да нет",
        "конечно нет",
        "не знаю",
        "может быть",
        "stop",
        "42",
    ],
)
def test_confirmation_ambiguous(text: str | None) -> None:
    assert VoiceConfirmation.parse(text) is None


def test_confirmation_ask_parses_listener_response() -> None:
    confirmation = VoiceConfirmation()

    assert confirmation.ask("Подтвердить?", lambda question: "да") is True
    assert confirmation.ask("Подтвердить?", lambda question: "нет") is False
    assert confirmation.ask("Подтвердить?", lambda question: FakeTranscript("ага")) is True
    assert confirmation.ask("Подтвердить?", lambda question: True) is True
    assert confirmation.ask("Подтвердить?", lambda question: False) is False


def test_confirmation_ask_never_auto_yes() -> None:
    confirmation = VoiceConfirmation()

    assert confirmation.ask("Подтвердить?", lambda question: None) is None
    assert confirmation.ask("Подтвердить?", lambda question: "не знаю") is None
    assert confirmation.ask("Подтвердить?", lambda question: 42) is None

    def broken(question: str) -> str:
        raise RuntimeError("listener boom")

    assert confirmation.ask("Подтвердить?", broken) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Киви, который час?", "который час?"),
        ("Kivi, what time is it?", "what time is it?"),
        ("КИВИ   привет", "привет"),
        ("киви", ""),
        ("скажи киви", "скажи киви"),
        ("киви-та", "киви-та"),
    ],
)
def test_strip_wake_word(text: str, expected: str) -> None:
    assert strip_wake_word(text) == expected


def test_has_wake_word() -> None:
    assert has_wake_word("Киви, привет") is True
    assert has_wake_word("Kivi, hello") is True
    assert has_wake_word("привет, киви") is False


def test_frame_wake_gate_accepts_and_rejects() -> None:
    accepted, _, _, _ = make_session(
        text="привет",
        wake=None,
        frame_wake=lambda frame: True,
    )
    turn = accepted.run_once()
    assert turn.addressed is True
    assert turn.answer == "ответ"

    rejected, _, _, _ = make_session(
        text="привет",
        wake=None,
        frame_wake=lambda frame: False,
    )
    turn = rejected.run_once()
    assert turn.addressed is False
    assert turn.answer == ""


def test_buffered_wake_detector_waits_for_full_window() -> None:
    from kivi.senses.audio import BufferedWakeDetector

    class Detector:
        def __init__(self) -> None:
            self.windows: list[int] = []

        def detect(self, frame: Any) -> bool:
            self.windows.append(len(frame))
            return len(self.windows) == 2

        def reset(self) -> None:
            pass

    detector = Detector()
    buffered = BufferedWakeDetector(detector, frame_samples=8)
    assert buffered.feed(np.array([1, 2, 3], dtype=np.int16)) is False
    assert buffered.feed(np.array([4, 5, 6], dtype=np.int16)) is False
    assert buffered.feed(np.array([7, 8, 9, 10, 11, 12, 13, 14, 15, 16], dtype=np.int16)) is True
    assert detector.windows == [8, 8]


def test_run_once_happy_path() -> None:
    answers: list[str] = []
    spoken: list[str] = []
    source = FakeSource([b"f1", b"f2", b"f3"])
    segmenter = FakeSegmenter([FakeSegment(b"pcm")])
    transcriber = FakeTranscriber("Киви, который час?")

    def answer(text: str) -> str:
        answers.append(text)
        return "Три часа."

    session = VoiceSession(
        source,
        segmenter,
        transcriber,
        answer=answer,
        speak=lambda value: spoken.append(value),
    )
    turn = session.run_once()

    assert turn.text == "который час?"
    assert turn.answer == "Три часа."
    assert turn.stopped is False
    assert turn.addressed is True
    assert answers == ["который час?"]
    assert spoken == ["Три часа."]
    assert segmenter.fed == [b"f1"]
    assert transcriber.calls == [b"pcm"]
    assert session.last_turn is turn
    metrics = turn.metrics
    assert metrics.wake_ms == 0.0
    assert metrics.stt_ms >= 0.0
    assert metrics.answer_ms >= 0.0
    assert metrics.tts_ms >= 0.0
    assert metrics.total_ms >= metrics.stt_ms + metrics.answer_ms + metrics.tts_ms


def test_run_once_no_segment() -> None:
    answers: list[str] = []
    session, _, segmenter, _ = make_session(
        frames=[b"f1", b"f2"],
        segments=[],
        answer=lambda text: answers.append(text) or "ответ",
    )
    turn = session.run_once()

    assert turn.text == ""
    assert turn.answer == ""
    assert turn.stopped is False
    assert answers == []
    assert segmenter.fed == [b"f1", b"f2"]


def test_transcriber_error_is_handled(caplog: pytest.LogCaptureFixture) -> None:
    answers: list[str] = []
    spoken: list[str] = []
    session, _, _, transcriber = make_session(
        text="ignored",
        answer=lambda text: answers.append(text) or "ответ",
        speak=lambda value: spoken.append(value),
    )
    transcriber.error = RuntimeError("stt down")

    with caplog.at_level(logging.ERROR, logger="kivi.senses.voice"):
        turn = session.run_once()

    assert turn.text == ""
    assert turn.answer == ""
    assert turn.stopped is False
    assert answers == []
    assert spoken == []
    assert "voice transcription failed" in caplog.text


def test_stop_during_failed_transcription_returns_stopped() -> None:
    answers: list[str] = []
    session, _, _, transcriber = make_session(
        text="ignored",
        answer=lambda text: answers.append(text) or "ответ",
    )
    transcriber.error = RuntimeError("stt down")
    transcriber.on_call = lambda pcm: session.stop()
    turn = session.run_once()

    assert turn.stopped is True
    assert turn.answer == ""
    assert answers == []


def test_stop_phrase_in_transcript() -> None:
    answers: list[str] = []
    spoken: list[str] = []
    session, _, _, _ = make_session(
        text="Киви, стоп",
        answer=lambda text: answers.append(text) or "ответ",
        speak=lambda value: spoken.append(value),
    )
    turn = session.run_once()

    assert turn.text == "стоп"
    assert turn.answer == ""
    assert turn.stopped is True
    assert answers == []
    assert spoken == []
    assert session.is_stopped is True


def test_stop_before_reading() -> None:
    session, _, segmenter, transcriber = make_session()
    session.stop()
    turn = session.run_once()

    assert turn.stopped is True
    assert turn.text == ""
    assert turn.answer == ""
    assert segmenter.fed == []
    assert transcriber.calls == []


def test_stop_during_transcription_skips_answer() -> None:
    answers: list[str] = []
    session, _, _, transcriber = make_session(
        text="привет",
        answer=lambda text: answers.append(text) or "ответ",
    )
    transcriber.on_call = lambda pcm: session.stop()
    turn = session.run_once()

    assert turn.text == "привет"
    assert turn.answer == ""
    assert turn.stopped is True
    assert answers == []


def test_stop_during_answer_skips_speak() -> None:
    spoken: list[str] = []
    session: VoiceSession

    def answer(text: str) -> str:
        session.stop()
        return "готово"

    session, _, _, _ = make_session(
        answer=answer,
        speak=lambda value: spoken.append(value),
    )
    turn = session.run_once()

    assert turn.text == "привет"
    assert turn.answer == "готово"
    assert turn.stopped is True
    assert spoken == []


def test_stop_during_speak_marks_turn_stopped() -> None:
    answers: list[str] = []
    session: VoiceSession

    def speak(text: str) -> object:
        session.stop()
        return object()

    session, _, _, _ = make_session(
        answer=lambda text: answers.append(text) or "ответ",
        speak=speak,
    )
    turn = session.run_once()

    assert turn.text == "привет"
    assert turn.answer == "ответ"
    assert turn.stopped is True
    assert answers == ["привет"]


def test_metrics_as_dict_and_format() -> None:
    metrics = VoiceMetrics(
        wake_ms=1.0,
        stt_ms=2.5,
        answer_ms=3.0,
        tts_ms=4.0,
        total_ms=10.5,
    )

    assert metrics.as_dict() == {
        "wake_ms": 1.0,
        "stt_ms": 2.5,
        "answer_ms": 3.0,
        "tts_ms": 4.0,
        "total_ms": 10.5,
    }
    line = format_metrics(metrics)
    assert "wake=1.0 ms" in line
    assert "stt=2.5 ms" in line
    assert "total=10.5 ms" in line
    assert line.count("|") == 4


def test_run_loop_respects_max_turns() -> None:
    source = FakeSource([b"1", b"2", b"3"])
    segmenter = FakeSegmenter(
        [FakeSegment(b"p1"), FakeSegment(b"p2"), FakeSegment(b"p3")]
    )
    answers: list[str] = []
    session = VoiceSession(
        source,
        segmenter,
        FakeTranscriber("Киви, привет"),
        answer=lambda text: answers.append(text) or "ответ",
        max_turns=3,
    )

    turns = session.run_loop(max_turns=2)

    assert len(turns) == 2
    assert [turn.text for turn in turns] == ["привет", "привет"]
    assert answers == ["привет", "привет"]
    assert segmenter.remaining == [FakeSegment(b"p3")]


def test_run_loop_breaks_on_barge_in() -> None:
    session: VoiceSession
    answers: list[str] = []

    def answer(text: str) -> str:
        answers.append(text)
        session.stop()
        return "ответ"

    session, _, _, _ = make_session(
        frames=[b"1", b"2", b"3"],
        segments=[FakeSegment(b"p1"), FakeSegment(b"p2"), FakeSegment(b"p3")],
        answer=answer,
        max_turns=3,
    )
    turns = session.run_loop(max_turns=3)

    assert len(turns) == 1
    assert turns[0].stopped is True
    assert turns[0].answer == "ответ"
    assert answers == ["привет"]


def test_run_loop_on_turn_callback() -> None:
    seen: list[Any] = []
    session, _, _, _ = make_session(
        frames=[b"1", b"2"],
        segments=[FakeSegment(b"p1"), FakeSegment(b"p2")],
        max_turns=2,
    )
    turns = session.run_loop(max_turns=2, on_turn=seen.append)

    assert seen == turns
    assert len(turns) == 2


def test_run_loop_on_turn_error_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    def broken(turn: Any) -> None:
        raise RuntimeError("observer boom")

    session, _, _, _ = make_session()

    with caplog.at_level(logging.ERROR, logger="kivi.senses.voice"):
        turns = session.run_loop(max_turns=1, on_turn=broken)

    assert len(turns) == 1
    assert "on_turn callback failed" in caplog.text


def test_run_loop_without_speech_returns_empty() -> None:
    session, _, _, _ = make_session(frames=[b"1"], segments=[])
    assert session.run_loop(max_turns=3) == []


class SequenceTranscriber:
    """Transcriber returning a different transcript for each call."""

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.calls: list[bytes] = []

    def transcribe(self, pcm: bytes) -> FakeTranscript:
        self.calls.append(pcm)
        return FakeTranscript(self._texts.pop(0))


def test_run_loop_skips_wake_misses_and_counts_addressed_turns() -> None:
    source = FakeSource([b"1", b"2"])
    segmenter = FakeSegmenter([FakeSegment(b"p1"), FakeSegment(b"p2")])
    transcriber = SequenceTranscriber(["привет", "Киви, привет"])
    session = VoiceSession(
        source,
        segmenter,
        transcriber,
        answer=lambda text: "ответ",
        wake=lambda text: "киви" in text.lower(),
        max_turns=1,
    )

    turns = session.run_loop()

    assert len(turns) == 1
    assert turns[0].text == "привет"
    assert turns[0].addressed is True
    assert len(transcriber.calls) == 2


def test_run_loop_returns_only_actual_turns() -> None:
    session, _, _, _ = make_session(
        frames=[b"1", b"2", b"3"],
        segments=[FakeSegment(b"p1"), FakeSegment(b"p2"), FakeSegment(b"p3")],
        max_turns=5,
    )
    turns = session.run_loop()

    assert len(turns) == 3
    assert all(turn.text == "привет" for turn in turns)


def test_session_reset_allows_listening_after_stop() -> None:
    session, _, _, _ = make_session()
    session.stop()
    assert session.run_once().stopped is True

    session.reset()
    turn = session.run_once()

    assert turn.stopped is False
    assert turn.answer == "ответ"


def test_wake_gate_rejects_unaddressed_speech() -> None:
    answers: list[str] = []
    session, _, _, _ = make_session(
        text="привет",
        wake=lambda text: False,
        answer=lambda value: answers.append(value) or "ответ",
    )
    turn = session.run_once()

    assert turn.addressed is False
    assert turn.text == "привет"
    assert turn.answer == ""
    assert answers == []


def test_push_to_talk_bypasses_wake_gate() -> None:
    wake_calls: list[str] = []
    session, _, _, _ = make_session(
        text="привет",
        wake=lambda text: wake_calls.append(text) or False,
    )
    turn = session.run_once(push_to_talk=True)

    assert turn.addressed is True
    assert turn.text == "привет"
    assert turn.answer == "ответ"
    assert wake_calls == []


class ReadSource:
    """Block-style source exposing read() instead of frames()."""

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = list(frames)

    def read(self) -> bytes | None:
        if not self._frames:
            return None
        return self._frames.pop(0)


class StreamSource:
    """Source exposing an iterable stream attribute."""

    def __init__(self, frames: list[bytes]) -> None:
        self.stream = iter(frames)


class FramesListSource:
    """Source exposing frames as a plain iterable attribute."""

    frames = [b"f1"]


class CountedReadSource:
    """Block-style source with the real ``read(frames)`` signature."""

    samplerate = 16000
    chunk_ms = 30

    def __init__(self, frames: list[Any]) -> None:
        self._frames = list(frames)
        self.sizes: list[int] = []

    @property
    def frames_per_chunk(self) -> int:
        return 4

    def read(self, frames: int) -> Any:
        self.sizes.append(frames)
        if not self._frames:
            return []
        return self._frames.pop(0)


@pytest.mark.parametrize(
    "source_factory",
    [
        lambda: [b"f1"],
        lambda: ReadSource([b"f1"]),
        lambda: StreamSource([b"f1"]),
        lambda: FramesListSource(),
        lambda: CountedReadSource([b"f1"]),
    ],
)
def test_source_shapes(source_factory: Callable[[], Any]) -> None:
    session = VoiceSession(
        source_factory(),
        FakeSegmenter([FakeSegment(b"pcm")]),
        FakeTranscriber("Киви, привет"),
        answer=lambda text: "ответ",
    )
    turn = session.run_once()

    assert turn.text == "привет"
    assert turn.answer == "ответ"


def test_counted_read_source_gets_frame_size_and_stops_on_empty() -> None:
    source = CountedReadSource([b"f1", []])
    segmenter = FakeSegmenter([])
    session = VoiceSession(
        source,
        segmenter,
        FakeTranscriber("Киви, привет"),
        answer=lambda text: "ответ",
    )
    turn = session.run_once()

    assert turn.text == ""
    assert source.sizes == [4, 4]
    assert segmenter.fed == [b"f1"]


def test_constructor_validation() -> None:
    transcriber = FakeTranscriber()
    segmenter = FakeSegmenter()
    with pytest.raises(TypeError):
        VoiceSession(object(), object(), transcriber, answer=lambda text: "ответ")
    with pytest.raises(TypeError):
        VoiceSession(object(), segmenter, object(), answer=lambda text: "ответ")
    with pytest.raises(TypeError):
        VoiceSession(object(), segmenter, transcriber, answer="not callable")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        VoiceSession(
            object(),
            segmenter,
            transcriber,
            answer=lambda text: "ответ",
            max_turns=-1,
        )
