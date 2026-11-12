from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from kivi.senses.qwen_tts import (
    QwenTtsConfig,
    QwenTtsEngine,
    QwenTtsError,
    QwenVoiceDesigner,
)


class FakeModel:
    sr = 24000

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def create_voice_clone_prompt(self, *, ref_audio: str, ref_text: str) -> dict[str, str]:
        self.calls.append(("prompt", ref_text))
        return {"ref": ref_audio, "text": ref_text}

    def generate_voice_clone(self, **kwargs):
        self.calls.append(("clone", kwargs))
        return [np.zeros(2400, dtype=np.float32)], self.sr

    def generate_voice_design(self, **kwargs):
        self.calls.append(("design", kwargs))
        return [np.full(2400, 0.1, dtype=np.float32)], self.sr


def make_reference(tmp_path: Path) -> Path:
    path = tmp_path / "ref.wav"
    path.write_bytes(b"")
    path.with_suffix(".txt").write_text("Привет, это Киви.", encoding="utf-8")
    return path


def test_engine_synthesizes_and_writes_wav(tmp_path: Path) -> None:
    model = FakeModel()
    config = QwenTtsConfig(reference_wav=make_reference(tmp_path), device="cpu")
    engine = QwenTtsEngine(config, model_loader=lambda: model)
    result = engine.synth("Привет!", tmp_path / "out.wav")
    assert result.engine == "qwen3-tts"
    assert result.sample_rate == 24000
    assert result.path.is_file()
    with wave.open(str(result.path), "rb") as handle:
        assert handle.getnframes() == 2400
    assert model.calls[0][0] == "prompt"
    assert model.calls[1][0] == "clone"


def test_missing_reference_fails_before_loading(tmp_path: Path) -> None:
    engine = QwenTtsEngine(
        QwenTtsConfig(reference_wav=tmp_path / "missing.wav", device="cpu"),
        model_loader=lambda: FakeModel(),
    )
    with pytest.raises(QwenTtsError, match="reference voice"):
        engine.synth("Привет", tmp_path / "out.wav")


def test_missing_transcript_is_reported(tmp_path: Path) -> None:
    reference = tmp_path / "ref.wav"
    reference.write_bytes(b"")
    model = FakeModel()
    engine = QwenTtsEngine(
        QwenTtsConfig(reference_wav=reference, device="cpu"),
        model_loader=lambda: model,
    )
    with pytest.raises(QwenTtsError, match="transcript"):
        engine.synth("Привет", tmp_path / "out.wav")


def test_designer_writes_reference_and_transcript(tmp_path: Path) -> None:
    model = FakeModel()
    designer = QwenVoiceDesigner(
        QwenTtsConfig(design_dir=tmp_path, device="cpu"),
        model_loader=lambda: model,
    )
    result = designer.design("Привет, это Киви.", tmp_path / "voice.wav", instruct="cute")
    assert result.path.with_suffix(".txt").read_text(encoding="utf-8") == "Привет, это Киви."
    assert model.calls[0][0] == "design"
    assert model.calls[0][1]["instruct"] == "cute"


def test_empty_text_is_rejected(tmp_path: Path) -> None:
    model = FakeModel()
    engine = QwenTtsEngine(
        QwenTtsConfig(reference_wav=make_reference(tmp_path), device="cpu"),
        model_loader=lambda: model,
    )
    with pytest.raises(QwenTtsError, match="empty"):
        engine.synth("   ", tmp_path / "out.wav")


def test_engine_reuses_prompt(tmp_path: Path) -> None:
    model = FakeModel()
    engine = QwenTtsEngine(
        QwenTtsConfig(reference_wav=make_reference(tmp_path), device="cpu"),
        model_loader=lambda: model,
    )
    engine.synth("раз", tmp_path / "a.wav")
    engine.synth("два", tmp_path / "b.wav")
    assert sum(1 for call in model.calls if call[0] == "prompt") == 1
