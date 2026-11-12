from __future__ import annotations

import io
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from kivi.senses.fish_audio import FishAudioTts, FishTtsConfig, FishTtsError


def wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\x00\x00" * 4410)
    return buffer.getvalue()


class FakeClient:
    def __init__(self, status: int = 200, content: bytes | None = None) -> None:
        self.status = status
        self.content = wav_bytes() if content is None else content
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return SimpleNamespace(
            status_code=self.status,
            content=self.content,
            text="error body",
        )


def test_fish_synth_sends_reference_and_writes_wav(tmp_path: Path) -> None:
    client = FakeClient()
    engine = FishAudioTts(
        FishTtsConfig(reference_id="7b220f0a97de48e7a32cf1f09381d0d3", api_key="test"),
        client=client,
    )
    result = engine.synth("Привет", tmp_path / "out.wav")
    assert result.engine == "fish-audio"
    assert result.path.read_bytes().startswith(b"RIFF")
    assert result.duration_s == pytest.approx(0.1)
    call = client.calls[0]
    assert call["url"].endswith("/v1/tts")
    assert call["json"]["reference_id"] == "7b220f0a97de48e7a32cf1f09381d0d3"
    assert call["headers"]["Authorization"] == "Bearer test"


def test_fish_requires_api_key(tmp_path: Path) -> None:
    engine = FishAudioTts(FishTtsConfig(api_key=""), client=FakeClient())
    with pytest.raises(FishTtsError, match="FISH_AUDIO_API_KEY"):
        engine.synth("Привет", tmp_path / "out.wav")


def test_fish_reports_http_error(tmp_path: Path) -> None:
    engine = FishAudioTts(
        FishTtsConfig(api_key="test"),
        client=FakeClient(status=401),
    )
    with pytest.raises(FishTtsError, match="401"):
        engine.synth("Привет", tmp_path / "out.wav")


def test_fish_rejects_non_wav(tmp_path: Path) -> None:
    engine = FishAudioTts(
        FishTtsConfig(api_key="test"),
        client=FakeClient(content=b"not-a-wav"),
    )
    with pytest.raises(FishTtsError, match="WAV"):
        engine.synth("Привет", tmp_path / "out.wav")


def test_fish_rejects_empty_text(tmp_path: Path) -> None:
    engine = FishAudioTts(FishTtsConfig(api_key="test"), client=FakeClient())
    with pytest.raises(FishTtsError, match="empty"):
        engine.synth("  ", tmp_path / "out.wav")
