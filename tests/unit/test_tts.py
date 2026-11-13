from __future__ import annotations

import struct
import wave
from pathlib import Path

import pytest

from kivi.senses.tts import (
    DEFAULT_SILERO_MODEL,
    NullTts,
    PiperTts,
    SileroTts,
    SpeechPipeline,
    TtsError,
    TtsResult,
    split_sentences,
    write_wav,
)


class FakeEngine:
    """In-memory TTS engine that records calls and writes tiny silent WAVs."""

    def __init__(self, name: str = "fake", *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.calls: list[str] = []

    def synth(
        self,
        text: str,
        output: Path,
        *,
        speaker: str = "baya",
        sample_rate: int = 48000,
    ) -> TtsResult:
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("synthetic failure")
        frames = max(1, int(sample_rate * 0.01))
        write_wav(output, b"\x00\x00" * frames, sample_rate)
        return TtsResult(
            path=output,
            sample_rate=sample_rate,
            duration_s=frames / sample_rate,
            engine=self.name,
        )


class FakeSileroModel:
    """Minimal stand-in for the packaged Silero model."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.evaluated = False

    def eval(self) -> None:
        self.evaluated = True

    def apply_tts(self, *, text: str, speaker: str, sample_rate: int) -> list[float]:
        self.calls.append((text, speaker, sample_rate))
        return [0.0, 0.25, -0.25]


def test_write_wav_header_for_raw_pcm16(tmp_path: Path) -> None:
    path = tmp_path / "tiny.wav"
    raw = struct.pack("<4h", 0, 32767, -32768, 1000)
    assert write_wav(path, raw, 16000) == path

    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 16000
        assert handle.getnframes() == 4
        assert handle.readframes(4) == raw
    assert path.read_bytes()[:4] == b"RIFF"


def test_write_wav_encodes_float_samples(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "floats.wav", [0.0, 0.5, -0.5], 48000)
    with wave.open(str(path), "rb") as handle:
        assert handle.getframerate() == 48000
        assert handle.getnframes() == 3
        assert handle.readframes(3) == struct.pack("<3h", 0, 16384, -16384)


def test_split_sentences_handles_russian_punctuation_and_ellipsis() -> None:
    text = "Привет! Как дела? Всё хорошо… «Правда?» Да."
    assert split_sentences(text) == [
        "Привет!",
        "Как дела?",
        "Всё хорошо…",
        "«Правда?»",
        "Да.",
    ]


def test_split_sentences_normalizes_whitespace_and_keeps_tail() -> None:
    assert split_sentences("  Первая!\n\nВторая без точки  ") == [
        "Первая!",
        "Вторая без точки",
    ]


def test_split_sentences_respects_max_chars() -> None:
    text = " ".join(["слово"] * 100)
    pieces = split_sentences(text, max_chars=200)
    assert len(pieces) > 1
    assert all(len(piece) <= 200 for piece in pieces)
    assert " ".join(pieces) == text


def test_split_sentences_empty_text_returns_empty_list() -> None:
    assert split_sentences("") == []
    assert split_sentences(" \n\t ") == []


def test_pipeline_synthesizes_each_sentence_and_plays_results(tmp_path: Path) -> None:
    primary = FakeEngine("primary")
    played: list[Path] = []
    pipeline = SpeechPipeline(primary, fallback=FakeEngine("fallback"), player=played.append)

    results = pipeline.synthesize("Привет. Как дела?", tmp_path / "voice")

    assert primary.calls == ["Привет.", "Как дела?"]
    assert [result.path.name for result in results] == ["speech_000.wav", "speech_001.wav"]
    assert all(result.path.is_file() for result in results)
    assert played == [result.path for result in results]


def test_pipeline_empty_text_produces_no_files(tmp_path: Path) -> None:
    engine = FakeEngine()
    output = tmp_path / "voice"
    assert SpeechPipeline(engine, fallback=FakeEngine()).synthesize("  \n ", output) == []
    assert engine.calls == []
    assert not output.exists()


def test_pipeline_falls_back_when_primary_fails(tmp_path: Path) -> None:
    primary = FakeEngine("primary", fail=True)
    fallback = FakeEngine("fallback")
    pipeline = SpeechPipeline(primary, fallback=fallback)

    results = pipeline.synthesize("Привет. Как дела?", tmp_path)

    assert len(results) == 2
    assert results[0].engine == "fallback"
    assert primary.calls == ["Привет."]
    assert fallback.calls == ["Привет.", "Как дела?"]


def test_pipeline_does_not_fall_back_when_disabled(tmp_path: Path) -> None:
    primary = FakeEngine("primary", fail=True)
    fallback = FakeEngine("fallback")
    pipeline = SpeechPipeline(primary, fallback=fallback, enable_fallback=False)

    with pytest.raises(TtsError, match="sentence 1"):
        pipeline.synthesize("Привет.", tmp_path)
    assert fallback.calls == []


def test_piper_runner_uses_utf8_input_file(tmp_path: Path, monkeypatch) -> None:
    from kivi.senses import tts as tts_module

    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object):
        seen["argv"] = argv
        seen["creationflags"] = kwargs.get("creationflags")
        seen["startupinfo"] = kwargs.get("startupinfo")
        return type("Completed", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr(tts_module.subprocess, "run", fake_run)
    tts_module._default_piper_runner(
        ["piper", "-m", "voice.onnx", "-f", str(tmp_path / "out.wav")],
        "Привет, Киви!",
    )
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert "-i" in argv
    input_path = Path(argv[argv.index("-i") + 1])
    assert not input_path.exists()


@pytest.mark.skipif(
    __import__("sys").platform != "win32",
    reason="Windows subprocess flags",
)
def test_piper_runner_hides_console_window(monkeypatch) -> None:
    from kivi.senses import tts as tts_module

    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object):
        seen.update(kwargs)
        return type("Completed", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr(tts_module.subprocess, "run", fake_run)
    tts_module._default_piper_runner(["piper"], "Киви")
    assert seen["creationflags"] == tts_module.subprocess.CREATE_NO_WINDOW
    assert seen["startupinfo"] is not None


def test_piper_runner_is_injected_and_argv_is_exact(tmp_path: Path) -> None:
    executable = tmp_path / "piper.exe"
    model = tmp_path / "voice.onnx"
    captured: list[tuple[list[str], str]] = []

    def runner(argv: list[str], text: str) -> None:
        captured.append((argv, text))
        output = Path(argv[argv.index("-f") + 1])
        write_wav(output, b"\x00\x00" * 220, 22050)

    engine = PiperTts(executable=executable, model=model, runner=runner)
    result = engine.synth("Привет", tmp_path / "piper.wav")

    assert captured == [
        (
            [str(executable), "-m", str(model), "-f", str(tmp_path / "piper.wav")],
            "Привет",
        )
    ]
    assert result.engine == "piper"
    assert result.sample_rate == 22050
    assert result.path.is_file()


def test_silero_fake_loader_is_lazy_and_converts_audio(tmp_path: Path) -> None:
    model_path = tmp_path / "v5_ru.pt"
    model_path.write_bytes(b"fake model")
    model = FakeSileroModel()
    loaded: list[Path] = []

    def loader(path: Path) -> FakeSileroModel:
        loaded.append(path)
        return model

    engine = SileroTts(model_path, model_loader=loader)
    assert engine.model_loaded is False
    assert loaded == []

    result = engine.synth("привет", tmp_path / "silero.wav", speaker="baya")

    assert loaded == [model_path]
    assert engine.model_loaded is True
    assert model.evaluated is True
    assert model.calls == [("привет", "baya", 48000)]
    assert result.engine == "silero"
    assert result.sample_rate == 48000
    with wave.open(str(result.path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getnframes() == 3


def test_silero_missing_model_raises_without_loading(tmp_path: Path) -> None:
    engine = SileroTts(tmp_path / "absent.pt", model_loader=lambda _path: FakeSileroModel())
    with pytest.raises(TtsError, match="not found"):
        engine.synth("привет", tmp_path / "out.wav")


def test_silero_runtime_and_model_are_optional() -> None:
    pytest.importorskip("torch")
    if not DEFAULT_SILERO_MODEL.is_file():
        pytest.skip(f"Silero package model is absent: {DEFAULT_SILERO_MODEL}")
    assert DEFAULT_SILERO_MODEL.stat().st_size > 1_000_000


def test_null_tts_writes_silence(tmp_path: Path) -> None:
    engine = NullTts(duration_s=0.02, sample_rate=8000)
    result = engine.synth("любой текст", tmp_path / "null.wav")
    assert result.engine == "null"
    assert result.duration_s == pytest.approx(0.02)
    with wave.open(str(result.path), "rb") as handle:
        assert handle.getnframes() == 160
        assert set(handle.readframes(160)) == {0}
