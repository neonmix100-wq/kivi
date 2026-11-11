from __future__ import annotations

import sys
import types
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from kivi.senses.audio import (
    ArraySource,
    AudioSource,
    AudioUnavailable,
    EnergyVad,
    FakeTranscriber,
    OpenWakeWordDetector,
    PushToTalkGate,
    Segment,
    SileroVad,
    SoundDeviceSource,
    SttError,
    SttUnavailable,
    TextWakeWordDetector,
    Transcriber,
    Transcript,
    TranscriptSegment,
    Vad,
    VadSegmenter,
    WakeWordDetector,
    WakeWordUnavailable,
    WaveFileSource,
    WhisperTranscriber,
    default_wake_model_path,
)
from kivi.senses.tts import write_wav

SAMPLE_RATE = 16000
CHUNK = 480  # 30 ms at 16 kHz


def tone(ms: float = 30, amplitude: float = 0.3, samplerate: int = SAMPLE_RATE) -> np.ndarray:
    """Return a mono int16 sine wave of the requested duration."""
    samples = round(samplerate * ms / 1000)
    time = np.arange(samples, dtype=np.float32) / samplerate
    return (amplitude * np.sin(2 * np.pi * 440 * time) * 32767).astype(np.int16)


def silence(ms: float = 30, samplerate: int = SAMPLE_RATE) -> np.ndarray:
    """Return a mono int16 silence frame."""
    return np.zeros(round(samplerate * ms / 1000), dtype=np.int16)


class FakeVad:
    """Return a scripted sequence of VAD decisions and count resets."""

    def __init__(self, pattern: list[bool]) -> None:
        self.pattern = list(pattern)
        self.index = 0
        self.calls = 0
        self.reset_count = 0

    def is_speech(self, frame: np.ndarray) -> bool:
        del frame
        self.calls += 1
        if self.index >= len(self.pattern):
            return False
        value = self.pattern[self.index]
        self.index += 1
        return value

    def reset(self) -> None:
        self.index = 0
        self.reset_count += 1


class FakeSoundStream:
    """Stand-in for ``sounddevice.InputStream`` with scripted chunks."""

    def __init__(self, chunks: list[np.ndarray]) -> None:
        self.chunks = list(chunks)
        self.started = 0
        self.stopped = 0
        self.closed = 0
        self.reads: list[int] = []

    def start(self) -> None:
        self.started += 1

    def read(self, frames: int) -> tuple[np.ndarray, bool]:
        self.reads.append(frames)
        if self.chunks:
            return self.chunks.pop(0), False
        return np.zeros((frames, 1), dtype=np.int16), False

    def stop(self) -> None:
        self.stopped += 1

    def close(self) -> None:
        self.closed += 1


class FakeSileroModel:
    """Scripted scalar probabilities for the Silero wrapper."""

    def __init__(self, probabilities: list[float]) -> None:
        self.probabilities = list(probabilities)
        self.calls: list[tuple[Any, int]] = []
        self.reset_count = 0

    def __call__(self, tensor: Any, samplerate: int) -> np.float32:
        self.calls.append((tensor, samplerate))
        value = self.probabilities.pop(0) if self.probabilities else 0.0
        return np.float32(value)

    def reset_states(self) -> None:
        self.reset_count += 1


class FakeWhisperSegment:
    """Minimal faster-whisper segment."""

    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.end = end


class FakeWhisperModel:
    """In-memory Whisper model that records the audio it receives."""

    def __init__(
        self,
        segments: list[FakeWhisperSegment],
        *,
        language: str = "ru",
        duration: float = 1.0,
    ) -> None:
        self.segments = segments
        self.language = language
        self.duration = duration
        self.calls: list[dict[str, Any]] = []

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str | None = None,
        beam_size: int = 5,
        vad_filter: bool = False,
    ) -> tuple[list[FakeWhisperSegment], types.SimpleNamespace]:
        self.calls.append(
            {
                "audio": audio,
                "language": language,
                "beam_size": beam_size,
                "vad_filter": vad_filter,
            }
        )
        return iter(self.segments), types.SimpleNamespace(
            language=self.language,
            duration=self.duration,
        )


class FakeWakeModel:
    """Scripted openWakeWord model returning one score per frame."""

    def __init__(self, scores: list[float]) -> None:
        self.scores = list(scores)
        self.calls: list[np.ndarray] = []
        self.reset_count = 0

    def predict(self, frame: np.ndarray) -> dict[str, float]:
        self.calls.append(frame.copy())
        value = self.scores.pop(0) if self.scores else 0.0
        return {"kivi": value}

    def reset(self) -> None:
        self.reset_count += 1


# Audio sources -----------------------------------------------------------------


def test_array_source_reads_chunks_until_end() -> None:
    pcm = tone(100)
    source = ArraySource(pcm)

    chunks = []
    while True:
        chunk = source.read(CHUNK)
        if chunk.size == 0:
            break
        chunks.append(chunk)

    assert [chunk.size for chunk in chunks] == [480, 480, 480, 160]
    assert np.concatenate(chunks).tolist() == pcm.tolist()
    assert source.read(CHUNK).size == 0


def test_array_source_close_blocks_reads() -> None:
    source = ArraySource(tone())
    source.close()
    source.close()
    assert source.closed is True
    with pytest.raises(AudioUnavailable, match="closed"):
        source.read(CHUNK)


def test_array_source_accepts_float_and_single_column_frames() -> None:
    floats = np.array([[[0.0], [0.5], [-0.5]]], dtype=np.float32).reshape(3, 1)
    source = ArraySource(floats)
    chunk = source.read(3)
    assert chunk.dtype == np.int16
    assert chunk.tolist() == [0, 16384, -16384]


def test_sounddevice_source_is_lazy_until_first_read() -> None:
    stream = FakeSoundStream([tone().reshape(-1, 1)])
    source = SoundDeviceSource(stream_factory=lambda: stream)

    assert source.stream_open is False
    assert stream.started == 0

    chunk = source.read(CHUNK)

    assert source.stream_open is True
    assert stream.started == 1
    assert stream.reads == [CHUNK]
    assert chunk.tolist() == tone().tolist()

    source.close()
    assert stream.stopped == 1
    assert stream.closed == 1
    with pytest.raises(AudioUnavailable, match="closed"):
        source.read(CHUNK)


def test_sounddevice_source_reports_overflow() -> None:
    def read(frames: int) -> tuple[np.ndarray, bool]:
        return np.zeros((frames, 1), dtype=np.int16), True

    stream = FakeSoundStream([])
    stream.read = read  # type: ignore[method-assign]
    source = SoundDeviceSource(stream_factory=lambda: stream)

    assert source.read(CHUNK).size == CHUNK
    assert source.last_overflow is True


def test_sounddevice_source_raises_when_package_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    source = SoundDeviceSource()
    with pytest.raises(AudioUnavailable, match="sounddevice is not installed"):
        source.read(CHUNK)


def test_wave_file_source_reads_mono_pcm16(tmp_path: Path) -> None:
    pcm = tone(100)
    path = write_wav(tmp_path / "voice.wav", pcm.tobytes(), SAMPLE_RATE)
    source = WaveFileSource(path)

    assert source.read(CHUNK).tolist() == pcm[:CHUNK].tolist()
    assert source.read(CHUNK).tolist() == pcm[CHUNK : 2 * CHUNK].tolist()
    assert source.read(CHUNK).tolist() == pcm[2 * CHUNK : 3 * CHUNK].tolist()
    assert source.read(CHUNK).tolist() == pcm[3 * CHUNK :].tolist()
    assert source.read(CHUNK).size == 0

    source.close()
    with pytest.raises(AudioUnavailable, match="closed"):
        source.read(CHUNK)


def test_wave_file_source_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(AudioUnavailable, match="cannot open WAV"):
        WaveFileSource(tmp_path / "absent.wav")


def test_wave_file_source_rejects_stereo(tmp_path: Path) -> None:
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(b"\x00\x00" * 8)
    with pytest.raises(AudioUnavailable, match="mono"):
        WaveFileSource(path)


def test_wave_file_source_rejects_wrong_sample_rate(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "rate.wav", tone(30, samplerate=8000).tobytes(), 8000)
    with pytest.raises(AudioUnavailable, match="8000 Hz"):
        WaveFileSource(path, samplerate=SAMPLE_RATE)


def test_audio_source_protocol_runtime_check(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "check.wav", tone().tobytes(), SAMPLE_RATE)
    assert isinstance(ArraySource(tone()), AudioSource)
    assert isinstance(WaveFileSource(path), AudioSource)
    assert isinstance(SoundDeviceSource(), AudioSource)


# Energy VAD --------------------------------------------------------------------


def test_energy_vad_requires_minimum_speech_duration() -> None:
    vad = EnergyVad(threshold=0.01, min_speech_ms=250, hangover_ms=0)
    decisions = [vad.is_speech(tone()) for _ in range(9)]

    assert decisions == [False] * 8 + [True]


def test_energy_vad_hangover_keeps_speech_through_pause() -> None:
    vad = EnergyVad(threshold=0.01, min_speech_ms=0, hangover_ms=500)
    assert vad.is_speech(tone()) is True

    decisions = [vad.is_speech(silence()) for _ in range(17)]

    assert decisions[:16] == [True] * 16
    assert decisions[16] is False


def test_energy_vad_ignores_short_blip() -> None:
    vad = EnergyVad(threshold=0.01, min_speech_ms=250, hangover_ms=100)

    assert [vad.is_speech(tone()) for _ in range(3)] == [False, False, False]
    vad.is_speech(silence())
    decisions = [vad.is_speech(tone()) for _ in range(9)]

    assert decisions == [False] * 8 + [True]


def test_energy_vad_reset_clears_state() -> None:
    vad = EnergyVad(threshold=0.01, min_speech_ms=0, hangover_ms=0)
    assert vad.is_speech(tone()) is True
    vad.reset()
    assert vad.active is False
    assert vad.last_rms == 0.0
    assert vad.is_speech(silence()) is False


def test_energy_vad_rms_levels_and_validation() -> None:
    assert EnergyVad.rms(silence()) == 0.0
    assert EnergyVad.rms(np.array([0.5, -0.5], dtype=np.float32)) == pytest.approx(0.5)
    assert EnergyVad.rms(np.array([], dtype=np.int16)) == 0.0

    with pytest.raises(ValueError, match="threshold"):
        EnergyVad(threshold=-0.1)
    with pytest.raises(ValueError, match="min_speech_ms"):
        EnergyVad(min_speech_ms=-1)
    with pytest.raises(ValueError, match="samplerate"):
        EnergyVad(samplerate=0)


def test_energy_vad_accepts_float_frames() -> None:
    vad = EnergyVad(threshold=0.01, min_speech_ms=0, hangover_ms=0)
    assert vad.is_speech(np.array([0.4, -0.4], dtype=np.float32)) is True
    assert vad.is_speech(np.array([0.0, 0.0], dtype=np.float32)) is False


# Silero VAD --------------------------------------------------------------------


@pytest.fixture
def torch_runtime() -> None:
    pytest.importorskip("torch")


def test_silero_vad_is_lazy_and_buffers_30ms_frames(torch_runtime: None) -> None:
    del torch_runtime
    model = FakeSileroModel([0.9])
    loaded: list[bool] = []

    def loader() -> FakeSileroModel:
        loaded.append(True)
        return model

    vad = SileroVad(threshold=0.5, model_loader=loader)
    assert vad.model_loaded is False
    assert loaded == []

    assert vad.is_speech(tone()) is False  # 480 samples: no full 512 window yet
    assert vad.is_speech(tone()) is True  # 960 samples: one full window
    assert vad.model_loaded is True
    assert loaded == [True]
    assert len(model.calls) == 1
    tensor, samplerate = model.calls[0]
    assert tuple(tensor.shape) == (1, 512)
    assert samplerate == SAMPLE_RATE


def test_silero_vad_splits_large_frames(torch_runtime: None) -> None:
    del torch_runtime
    model = FakeSileroModel([0.1, 0.9])
    vad = SileroVad(threshold=0.5, model_loader=lambda: model)

    assert vad.is_speech(tone(64)) is True
    assert len(model.calls) == 2


def test_silero_vad_respects_threshold(torch_runtime: None) -> None:
    del torch_runtime
    model = FakeSileroModel([0.6])
    vad = SileroVad(threshold=0.9, model_loader=lambda: model)

    assert vad.is_speech(tone(32)) is False


def test_silero_vad_reset_clears_partial_window(torch_runtime: None) -> None:
    del torch_runtime
    model = FakeSileroModel([0.9])
    vad = SileroVad(threshold=0.5, model_loader=lambda: model)
    vad.is_speech(tone())
    vad.reset()

    assert model.reset_count == 1
    assert vad.is_speech(tone()) is False


def test_silero_vad_wraps_loader_errors() -> None:
    def broken() -> Any:
        raise RuntimeError("boom")

    vad = SileroVad(model_loader=broken)
    with pytest.raises(AudioUnavailable, match="failed to load Silero VAD"):
        vad.is_speech(tone())


def test_silero_vad_validates_parameters() -> None:
    with pytest.raises(ValueError, match="8000 or 16000"):
        SileroVad(samplerate=44100)
    with pytest.raises(ValueError, match="between 0 and 1"):
        SileroVad(threshold=1.5)


# Segmenter ---------------------------------------------------------------------


def test_segmenter_emits_trimmed_segment() -> None:
    frames = [silence(), silence(), tone(), tone(), silence()]
    vad = FakeVad([False, False, True, True, False])
    segmenter = VadSegmenter(vad, hangover_ms=0, preroll_ms=0, speech_pad_ms=0)

    assert segmenter.feed(frames[0]) is None
    assert segmenter.feed(frames[1]) is None
    assert segmenter.feed(frames[2]) is None
    assert segmenter.feed(frames[3]) is None
    segment = segmenter.feed(frames[4])

    assert isinstance(segment, Segment)
    assert segment.sample_rate == SAMPLE_RATE
    assert segment.pcm.dtype == np.int16
    assert segment.duration_s == pytest.approx(0.06)
    assert segment.pcm.tolist() == np.concatenate(frames[2:4]).tolist()


def test_segmenter_pre_roll_keeps_onset_audio() -> None:
    frames = [tone(10), tone(10), tone(10), silence(10)]
    vad = FakeVad([False, False, True, False])
    segmenter = VadSegmenter(
        vad,
        hangover_ms=0,
        preroll_ms=30,
        speech_pad_ms=0,
    )

    for frame in frames[:3]:
        assert segmenter.feed(frame) is None
    segment = segmenter.feed(frames[3])

    assert segment is not None
    assert segment.pcm.tolist() == np.concatenate(frames[:3]).tolist()


def test_segmenter_hangover_bridges_short_pause() -> None:
    frames = [tone(), silence(), silence(), tone(), silence(), silence(), silence()]
    vad = FakeVad([True, False, False, True, False, False, False])
    segmenter = VadSegmenter(vad, hangover_ms=90, preroll_ms=0, speech_pad_ms=0)

    results = [segmenter.feed(frame) for frame in frames]

    assert results[:6] == [None] * 6
    segment = results[6]
    assert segment is not None
    assert segment.pcm.tolist() == np.concatenate(frames[:4]).tolist()


def test_segmenter_max_segment_forces_emit() -> None:
    frames = [tone() for _ in range(3)]
    vad = FakeVad([True, True, True])
    segmenter = VadSegmenter(
        vad,
        max_segment_s=0.06,
        hangover_ms=0,
        preroll_ms=0,
        speech_pad_ms=0,
    )

    assert segmenter.feed(frames[0]) is None
    first = segmenter.feed(frames[1])
    assert first is not None
    assert first.duration_s == pytest.approx(0.06)
    assert segmenter.feed(frames[2]) is None

    rest = segmenter.flush()
    assert rest is not None
    assert rest.pcm.tolist() == frames[2].tolist()


def test_segmenter_flush_returns_pending_and_then_none() -> None:
    vad = FakeVad([True, True])
    segmenter = VadSegmenter(vad, preroll_ms=0, speech_pad_ms=0)
    segmenter.feed(tone())
    segmenter.feed(tone())

    segment = segmenter.flush()

    assert segment is not None
    assert segment.duration_s == pytest.approx(0.06)
    assert segmenter.flush() is None
    assert segmenter.active is False


def test_segmenter_reset_drops_audio_and_resets_vad() -> None:
    vad = FakeVad([True])
    segmenter = VadSegmenter(vad, preroll_ms=0, speech_pad_ms=0)
    segmenter.feed(tone())
    segmenter.reset()

    assert segmenter.active is False
    assert vad.reset_count == 1
    assert segmenter.flush() is None


def test_segmenter_validates_limits() -> None:
    with pytest.raises(ValueError, match="max_segment_s"):
        VadSegmenter(FakeVad([]), max_segment_s=0)
    with pytest.raises(ValueError, match="samplerate"):
        VadSegmenter(FakeVad([]), samplerate=0)


# Wake word ---------------------------------------------------------------------


def test_openwakeword_missing_model_raises_clear_error(tmp_path: Path) -> None:
    called: list[Path] = []

    def loader(path: Path) -> Any:
        called.append(path)
        return FakeWakeModel([0.9])

    detector = OpenWakeWordDetector(tmp_path / "kivi.onnx", model_loader=loader)

    assert detector.model_loaded is False
    with pytest.raises(WakeWordUnavailable, match="custom Kivi model is not installed"):
        detector.detect(tone())
    assert called == []


def test_openwakeword_detects_threshold_crossing_once(tmp_path: Path) -> None:
    path = tmp_path / "kivi.onnx"
    path.write_bytes(b"fake onnx")
    model = FakeWakeModel([0.2, 0.6, 0.7, 0.3, 0.9])
    detector = OpenWakeWordDetector(path, threshold=0.5, model_loader=lambda _p: model)

    results = [detector.detect(tone()) for _ in range(5)]

    assert results == [False, True, False, False, True]
    assert all(call.dtype == np.int16 for call in model.calls)


def test_openwakeword_loads_lazily_and_accepts_float_frames(tmp_path: Path) -> None:
    path = tmp_path / "kivi.onnx"
    path.write_bytes(b"fake onnx")
    model = FakeWakeModel([0.9])
    loaded: list[Path] = []

    def loader(value: Path) -> FakeWakeModel:
        loaded.append(value)
        return model

    detector = OpenWakeWordDetector(path, model_loader=loader)
    assert loaded == []
    assert detector.detect(np.array([0.0, 0.5, -0.5], dtype=np.float32)) is True
    assert loaded == [path]
    assert model.calls[0].dtype == np.int16


def test_openwakeword_reset_rearms_after_hit(tmp_path: Path) -> None:
    path = tmp_path / "kivi.onnx"
    path.write_bytes(b"fake onnx")
    model = FakeWakeModel([0.9, 0.9, 0.9])
    detector = OpenWakeWordDetector(path, threshold=0.5, model_loader=lambda _p: model)

    assert detector.detect(tone()) is True
    assert detector.detect(tone()) is False
    detector.reset()
    assert detector.detect(tone()) is True
    assert model.reset_count == 1


def test_openwakeword_wraps_loader_errors(tmp_path: Path) -> None:
    path = tmp_path / "kivi.onnx"
    path.write_bytes(b"fake onnx")

    def broken(_path: Path) -> Any:
        raise RuntimeError("boom")

    detector = OpenWakeWordDetector(path, model_loader=broken)
    with pytest.raises(WakeWordUnavailable, match="failed to load custom Kivi"):
        detector.detect(tone())


def test_default_wake_model_path_prefers_profile_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIVI_HOME", str(tmp_path))
    profile_model = tmp_path / "models" / "wake" / "kivi.onnx"
    profile_model.parent.mkdir(parents=True)
    profile_model.write_bytes(b"fake")

    assert default_wake_model_path() == profile_model


def test_default_wake_model_path_falls_back_to_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIVI_HOME", str(tmp_path))
    assert default_wake_model_path().as_posix().endswith("assets/wake/kivi.onnx")


def test_text_wake_word_detector_matches_only_at_start() -> None:
    detector = TextWakeWordDetector()

    assert detector.detect("Киви, привет") is True
    assert detector.detect("kivi what time is it") is True
    assert detector.detect("  «Киви»") is True
    assert detector.detect(Transcript("Киви, стоп", "ru", 1.0)) is True
    assert detector.detect("привет, Киви") is False
    assert detector.detect("кивик") is False
    assert detector.detect("") is False


def test_text_wake_word_detector_strips_prefix() -> None:
    detector = TextWakeWordDetector()

    assert detector.strip_wake_word("Киви, включи музыку") == "включи музыку"
    assert detector.strip_wake_word("kivi stop") == "stop"
    assert detector.strip_wake_word("просто текст") == "просто текст"


def test_text_wake_word_detector_validation() -> None:
    with pytest.raises(ValueError, match="wake word"):
        TextWakeWordDetector(words=())
    with pytest.raises(TypeError, match="string or Transcript"):
        TextWakeWordDetector().detect(42)  # type: ignore[arg-type]


def test_push_to_talk_gate_bypasses_wake_word() -> None:
    gate = PushToTalkGate()

    assert gate.detect(tone()) is False
    gate.press()
    assert gate.active is True
    assert gate.detect(silence()) is True
    gate.release()
    assert gate.detect(tone()) is False
    assert gate.toggle() is True
    with gate:
        assert gate.detect(tone()) is True
    assert gate.active is False
    gate.press()
    gate.reset()
    assert gate.active is False
    assert isinstance(gate, WakeWordDetector)


# STT ---------------------------------------------------------------------------


def test_transcript_dataclass_exposes_fields() -> None:
    segment = TranscriptSegment("привет", 0.0, 0.5)
    transcript = Transcript("привет", "ru", 0.5, (segment,))

    assert transcript.text == "привет"
    assert transcript.language == "ru"
    assert transcript.duration_s == 0.5
    assert transcript.segments == (segment,)


def test_whisper_transcriber_is_lazy_and_converts_int16() -> None:
    model = FakeWhisperModel(
        [FakeWhisperSegment(" привет ", 0.0, 0.4), FakeWhisperSegment("мир", 0.4, 0.8)],
        language="ru",
        duration=0.8,
    )
    loaded: list[tuple[str, dict[str, Any]]] = []

    def loader(target: str, **kwargs: Any) -> FakeWhisperModel:
        loaded.append((target, kwargs))
        return model

    transcriber = WhisperTranscriber(model_loader=loader)
    assert transcriber.model_loaded is False
    assert loaded == []

    transcript = transcriber.transcribe(tone(50))

    assert loaded[0][0] == "small"
    assert loaded[0][1]["device"] == "cpu"
    assert loaded[0][1]["compute_type"] == "int8"
    assert model.calls[0]["audio"].dtype == np.float32
    assert float(np.max(np.abs(model.calls[0]["audio"]))) <= 1.0
    assert model.calls[0]["language"] == "ru"
    assert model.calls[0]["vad_filter"] is False
    assert transcript.text == "привет мир"
    assert transcript.language == "ru"
    assert transcript.duration_s == 0.8
    assert [segment.text for segment in transcript.segments] == ["привет", "мир"]


def test_whisper_transcriber_uses_explicit_model_path(tmp_path: Path) -> None:
    model_path = tmp_path / "whisper-model"
    model_path.mkdir()
    model = FakeWhisperModel([FakeWhisperSegment("тест", 0.0, 0.1)])
    loaded: list[str] = []

    def loader(target: str, **kwargs: Any) -> FakeWhisperModel:
        del kwargs
        loaded.append(target)
        return model

    transcriber = WhisperTranscriber(model_path=model_path, model_loader=loader)
    transcript = transcriber.transcribe(np.array([0.0, 0.25, -0.25], dtype=np.float32))

    assert loaded == [str(model_path)]
    assert transcript.text == "тест"
    assert model.calls[0]["audio"].dtype == np.float32
    with pytest.raises(SttUnavailable, match="not found"):
        WhisperTranscriber(
            model_path=tmp_path / "absent",
            model_loader=loader,
        ).transcribe(tone())


def test_whisper_transcriber_wraps_loader_errors() -> None:
    def broken(_target: str, **_kwargs: Any) -> Any:
        raise RuntimeError("boom")

    with pytest.raises(SttUnavailable, match="failed to load faster-whisper"):
        WhisperTranscriber(model_loader=broken).transcribe(tone())

    def missing(_target: str, **_kwargs: Any) -> Any:
        raise ImportError("no module")

    with pytest.raises(SttUnavailable, match="faster-whisper is not installed"):
        WhisperTranscriber(model_loader=missing).transcribe(tone())


def test_whisper_transcriber_rejects_empty_audio() -> None:
    with pytest.raises(SttError, match="empty audio"):
        WhisperTranscriber(model_loader=lambda _target, **_kwargs: None).transcribe(
            np.zeros(0, dtype=np.int16)
        )


def test_whisper_transcriber_falls_back_to_audio_duration() -> None:
    model = FakeWhisperModel(
        [FakeWhisperSegment("слово", 0.0, 0.2)],
        duration=0.0,
    )
    transcriber = WhisperTranscriber(model_loader=lambda _target, **_kwargs: model)

    transcript = transcriber.transcribe(tone(100))

    assert transcript.duration_s == pytest.approx(0.1)


def test_fake_transcriber_records_audio_and_builds_transcript() -> None:
    transcriber = FakeTranscriber(text="привет", language="ru")
    pcm = tone(100)
    transcript = transcriber.transcribe(pcm)

    assert transcriber.calls[0].tolist() == pcm.tolist()
    assert transcript.text == "привет"
    assert transcript.language == "ru"
    assert transcript.duration_s == pytest.approx(0.1)
    assert transcript.segments[0].end_s == pytest.approx(0.1)


def test_transcriber_protocol_runtime_check() -> None:
    assert isinstance(FakeTranscriber(), Transcriber)
    assert isinstance(WhisperTranscriber(), Transcriber)
    assert isinstance(EnergyVad(), Vad)
    assert isinstance(SileroVad(model_loader=lambda: None), Vad)


# Integration-shaped checks -----------------------------------------------------


def test_array_source_drives_segmenter_and_fake_transcriber() -> None:
    frames = [silence(), tone(), tone(), silence()]
    source = ArraySource(np.concatenate(frames))
    vad = FakeVad([False, True, True, False])
    segmenter = VadSegmenter(vad, hangover_ms=0, preroll_ms=0, speech_pad_ms=0)
    transcriber = FakeTranscriber("Киви, привет")

    segments: list[Segment] = []
    while True:
        chunk = source.read(CHUNK)
        if chunk.size == 0:
            break
        segment = segmenter.feed(chunk)
        if segment is not None:
            segments.append(segment)
    pending = segmenter.flush()
    if pending is not None:
        segments.append(pending)

    assert len(segments) == 1
    transcript = transcriber.transcribe(segments[0].pcm)
    assert TextWakeWordDetector().detect(transcript) is True
