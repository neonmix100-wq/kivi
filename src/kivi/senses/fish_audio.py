"""Fish Audio cloud TTS engine.

Fish Audio voice IDs are cloud models, not downloadable checkpoints.  This
engine sends text to ``POST /v1/tts`` with ``reference_id`` and writes the WAV
response.  The API key is read only from ``FISH_AUDIO_API_KEY`` (never from a
config file or the repository).

The public voice library is user-generated.  Before commercial use, verify the
voice owner's permission and the Fish Audio plan/credits; this module only
provides the transport.
"""

from __future__ import annotations

import io
import os
import struct
import wave
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from kivi.senses.tts import TtsError, TtsResult

DEFAULT_BASE_URL = "https://api.fish.audio/v1"
DEFAULT_REFERENCE_ID = "7b220f0a97de48e7a32cf1f09381d0d3"
DEFAULT_API_KEY_ENV = "FISH_AUDIO_API_KEY"
MAX_AUDIO_BYTES = 20 * 1024 * 1024


class FishTtsError(TtsError):
    """Raised when Fish Audio is not configured or synthesis fails."""


@dataclass(frozen=True, slots=True)
class FishTtsConfig:
    reference_id: str = DEFAULT_REFERENCE_ID
    model: str = "s2.1-pro-free"
    base_url: str = DEFAULT_BASE_URL
    api_key_env: str = DEFAULT_API_KEY_ENV
    format: str = "wav"
    sample_rate: int = 44100
    latency: str = "balanced"
    timeout_s: float = 60.0
    api_key: str = ""


def _normalize_wav(data: bytes) -> bytes:
    """Repair streaming WAV headers with 0xFFFFFFxx placeholder sizes.

    Fish Audio returns a RIFF/WAVE stream whose ``data`` chunk size is
    ``0xFFFFFF00`` while the actual payload is much smaller.  ``wave`` then
    reports billions of frames, and some players refuse the file.  Clamp the
    chunk sizes to the bytes actually received.
    """
    if not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
        return data
    out = bytearray(data)
    pos = 12
    while pos + 8 <= len(out):
        chunk_id = bytes(out[pos : pos + 4])
        declared = struct.unpack("<I", out[pos + 4 : pos + 8])[0]
        if chunk_id == b"data":
            available = len(out) - (pos + 8)
            actual = min(declared, available)
            out[pos + 4 : pos + 8] = struct.pack("<I", actual)
            end = pos + 8 + actual
            out[4:8] = struct.pack("<I", max(4, end - 8))
            return bytes(out[:end])
        step = 8 + declared + (declared & 1)
        if step < 8 or pos + step > len(out):
            break
        pos += step
    return data


def _duration(data: bytes, fallback_rate: int) -> tuple[int, float]:
    data = _normalize_wav(data)
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            rate = handle.getframerate()
            return rate, handle.getnframes() / float(rate or 1)
    except (wave.Error, OSError):
        return fallback_rate, 0.0


class FishAudioTts:
    """Fish Audio TTS through the public v1 HTTP API."""

    name = "fish-audio"

    def __init__(
        self,
        config: FishTtsConfig | None = None,
        *,
        client: Any = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config or FishTtsConfig()
        self._client = client
        self._client_factory = client_factory

    def _key(self) -> str:
        key = self.config.api_key or os.environ.get(self.config.api_key_env, "")
        if not key.strip():
            raise FishTtsError(
                f"{self.config.api_key_env} is not set; create a Fish Audio API key "
                "and export it before using this voice"
            )
        return key.strip()

    def _http(self) -> Any:
        if self._client is not None:
            return self._client
        self._client = (self._client_factory or (lambda: httpx.Client(
            timeout=self.config.timeout_s,
            follow_redirects=False,
        )))()
        return self._client

    def synth(
        self,
        text: str,
        output: Path,
        *,
        speaker: str = "",
        sample_rate: int = 48000,
    ) -> TtsResult:
        """Synthesize text with the configured Fish Audio voice ID."""
        del speaker
        cleaned = text.strip()
        if not cleaned:
            raise FishTtsError("cannot synthesize empty text")
        reference_id = self.config.reference_id.strip()
        if not reference_id:
            raise FishTtsError("Fish Audio reference_id is empty")
        payload = {
            "text": cleaned,
            "reference_id": reference_id,
            "format": self.config.format,
            "sample_rate": self.config.sample_rate,
            "latency": self.config.latency,
        }
        try:
            response = self._http().post(
                f"{self.config.base_url.rstrip('/')}/tts",
                headers={
                    "Authorization": f"Bearer {self._key()}",
                    "Content-Type": "application/json",
                    "model": self.config.model,
                },
                json=payload,
            )
        except FishTtsError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport boundary
            raise FishTtsError(f"Fish Audio request failed: {type(exc).__name__}") from exc
        status = getattr(response, "status_code", 0)
        if status != 200:
            detail = ""
            with suppress(Exception):
                detail = response.text[:300]
            raise FishTtsError(
                f"Fish Audio returned HTTP {status}"
                + (f": {detail}" if detail else "")
            )
        data = bytes(response.content)
        if not data:
            raise FishTtsError("Fish Audio returned empty audio")
        if len(data) > MAX_AUDIO_BYTES:
            raise FishTtsError("Fish Audio audio exceeds 20 MB")
        if not data.startswith(b"RIFF"):
            raise FishTtsError("Fish Audio did not return a WAV file")
        data = _normalize_wav(data)
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        actual_rate, duration = _duration(data, self.config.sample_rate)
        del sample_rate
        return TtsResult(
            path=path,
            sample_rate=actual_rate,
            duration_s=duration,
            engine=self.name,
        )

    def close(self) -> None:
        client, self._client = self._client, None
        close = getattr(client, "close", None)
        if close is not None:
            close()


__all__ = [
    "DEFAULT_API_KEY_ENV",
    "DEFAULT_BASE_URL",
    "DEFAULT_REFERENCE_ID",
    "FishAudioTts",
    "FishTtsConfig",
    "FishTtsError",
]
