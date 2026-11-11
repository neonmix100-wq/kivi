"""Local audio input: capture, voice activity detection, STT and wake words.

The module is deliberately lazy and hardware-free at import time. A microphone,
Whisper and openWakeWord are touched only when a caller actually needs them, so
unit tests and text-only sessions never open a device or reach the network.
Frames are mono 16-bit PCM at 16 kHz unless a source explicitly says otherwise.
"""

from __future__ import annotations

import contextlib
import re
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np

from kivi.paths import get_paths, project_root

DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHUNK_MS = 30
WAKE_MODEL_ASSET = project_root() / "assets" / "wake" / "kivi.onnx"
WAKE_MODEL_DATA = Path("models") / "wake" / "kivi.onnx"


class AudioUnavailable(RuntimeError):
    """Audio hardware, a model or a decoding dependency is not available."""


class SttError(RuntimeError):
    """Speech-to-text failed before it could return a transcript."""


class SttUnavailable(SttError):
    """faster-whisper is not installed, or the requested model is missing."""


class WakeWordUnavailable(RuntimeError):
    """The custom Kivi wake-word model or openWakeWord is unavailable."""


def _as_array(
    audio: np.ndarray | bytes | bytearray | memoryview,
) -> np.ndarray:
    """Return a one-dimensional numpy view of mono PCM audio.

    ``bytes`` are decoded as little-endian PCM16. Two-dimensional arrays are
    accepted only when they carry a single channel, because silently mixing
    stereo would change the signal and hide a caller mistake.
    """
    if isinstance(audio, (bytes, bytearray, memoryview)):
        data = bytes(audio)
        if len(data) % 2:
            raise ValueError("PCM16 data must contain an even number of bytes")
        array = np.frombuffer(data, dtype="<i2")
    elif isinstance(audio, np.ndarray):
        array = audio
    else:
        raise TypeError("audio must be a numpy array or PCM16 bytes")
    if array.ndim == 2:
        if array.shape[1] != 1:
            raise ValueError("audio must be mono: use shape (frames,) or (frames, 1)")
        array = array[:, 0]
    elif array.ndim != 1:
        raise ValueError(f"audio must be one-dimensional, got shape {array.shape}")
    return array.reshape(-1)


def _as_int16(audio: np.ndarray | bytes | bytearray | memoryview) -> np.ndarray:
    """Convert mono audio to contiguous int16 samples, clipping out-of-range data."""
    array = _as_array(audio)
    if array.size == 0:
        return np.zeros(0, dtype=np.int16)
    if array.dtype == np.int16:
        return np.ascontiguousarray(array, dtype=np.int16)
    if np.issubdtype(array.dtype, np.integer):
        clipped = np.clip(array, -32768, 32767)
        return clipped.astype(np.int16)
    if np.issubdtype(array.dtype, np.floating):
        clean = np.nan_to_num(array, nan=0.0, posinf=0.0, neginf=0.0)
        scaled = np.clip(clean, -1.0, 1.0) * 32767.0
        return np.rint(scaled).astype(np.int16)
    raise ValueError(f"unsupported audio dtype: {array.dtype}")


def _as_float32(audio: np.ndarray | bytes | bytearray | memoryview) -> np.ndarray:
    """Convert mono audio to float32 samples in ``[-1, 1]``."""
    array = _as_array(audio)
    if array.size == 0:
        return np.zeros(0, dtype=np.float32)
    if np.issubdtype(array.dtype, np.floating):
        clean = np.nan_to_num(array, nan=0.0, posinf=0.0, neginf=0.0)
        return np.clip(clean.astype(np.float32), -1.0, 1.0)
    if np.issubdtype(array.dtype, np.integer):
        return np.clip(array.astype(np.float32) / 32768.0, -1.0, 1.0)
    raise ValueError(f"unsupported audio dtype: {array.dtype}")


def _duration_s(samples: int, samplerate: int) -> float:
    """Convert a sample count to seconds."""
    if samplerate <= 0:
        raise ValueError("samplerate must be positive")
    return samples / samplerate


def default_wake_model_path() -> Path:
    """Return where the custom Kivi wake model is expected.

    A per-profile model under ``<data>/models/wake/kivi.onnx`` wins; otherwise
    the repository asset path is returned even when it is missing, so callers
    get a useful error instead of an empty string.
    """
    data_path = get_paths().data / WAKE_MODEL_DATA
    if data_path.is_file():
        return data_path
    return WAKE_MODEL_ASSET


@runtime_checkable
class AudioSource(Protocol):
    """A blocking source of mono audio frames."""

    samplerate: int

    def read(self, frames: int) -> np.ndarray:
        """Read up to ``frames`` samples; an empty array marks end of stream."""

    def close(self) -> None:
        """Release the device or file handle. Safe to call more than once."""


class ArraySource:
    """Replay an in-memory array; the deterministic source used by tests."""

    def __init__(
        self,
        samples: np.ndarray | bytes | bytearray | memoryview,
        samplerate: int = DEFAULT_SAMPLE_RATE,
        chunk_ms: int = DEFAULT_CHUNK_MS,
    ) -> None:
        if samplerate <= 0:
            raise ValueError("samplerate must be positive")
        if chunk_ms <= 0:
            raise ValueError("chunk_ms must be positive")
        self._samples = _as_int16(samples)
        self._position = 0
        self._closed = False
        self.samplerate = samplerate
        self.chunk_ms = chunk_ms

    @property
    def frames_per_chunk(self) -> int:
        """Samples in the default chunk at the configured chunk length."""
        return max(1, round(self.samplerate * self.chunk_ms / 1000))

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has been called."""
        return self._closed

    def read(self, frames: int) -> np.ndarray:
        """Return the next slice of the array, or an empty array at the end."""
        if self._closed:
            raise AudioUnavailable("audio source is closed")
        if frames <= 0:
            raise ValueError("frames must be positive")
        if self._position >= self._samples.size:
            return np.zeros(0, dtype=np.int16)
        end = min(self._position + frames, self._samples.size)
        chunk = self._samples[self._position : end].copy()
        self._position = end
        return chunk

    def close(self) -> None:
        """Mark the array source as closed."""
        self._closed = True


class WaveFileSource:
    """Read a mono 16-bit PCM WAV file in fixed-size chunks."""

    def __init__(self, path: Path | str, samplerate: int = DEFAULT_SAMPLE_RATE) -> None:
        if samplerate <= 0:
            raise ValueError("samplerate must be positive")
        self.path = Path(path)
        self.samplerate = samplerate
        self.chunk_ms = DEFAULT_CHUNK_MS
        self._closed = False
        try:
            # The handle must stay open across read() calls, so no context manager.
            handle = wave.open(str(self.path), "rb")  # noqa: SIM115
        except (OSError, wave.Error) as exc:
            raise AudioUnavailable(f"cannot open WAV file {self.path}: {exc}") from exc
        if handle.getnchannels() != 1:
            handle.close()
            raise AudioUnavailable(f"WAV file must be mono: {self.path}")
        if handle.getsampwidth() != 2:
            handle.close()
            raise AudioUnavailable(f"WAV file must be 16-bit PCM: {self.path}")
        if handle.getframerate() != samplerate:
            actual = handle.getframerate()
            handle.close()
            raise AudioUnavailable(
                f"WAV file is {actual} Hz, expected {samplerate} Hz: {self.path}"
            )
        self._handle = handle

    @property
    def frames_per_chunk(self) -> int:
        """Samples in the default chunk at the configured chunk length."""
        return max(1, round(self.samplerate * self.chunk_ms / 1000))

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has been called."""
        return self._closed

    def read(self, frames: int) -> np.ndarray:
        """Read the next ``frames`` samples, or an empty array at the end."""
        if self._closed:
            raise AudioUnavailable("audio source is closed")
        if frames <= 0:
            raise ValueError("frames must be positive")
        raw = self._handle.readframes(frames)
        return np.frombuffer(raw, dtype="<i2").copy()

    def close(self) -> None:
        """Close the WAV file handle."""
        if not self._closed:
            self._handle.close()
        self._closed = True

    def __enter__(self) -> WaveFileSource:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class SoundDeviceSource:
    """Microphone input through ``sounddevice``, opened on first read.

    Construction only records parameters: importing ``sounddevice`` and opening
    an input stream happens lazily so importing Kivi never touches a microphone.
    """

    def __init__(
        self,
        samplerate: int = DEFAULT_SAMPLE_RATE,
        chunk_ms: int = DEFAULT_CHUNK_MS,
        *,
        device: int | str | None = None,
        stream_factory: Callable[[], Any] | None = None,
    ) -> None:
        if samplerate <= 0:
            raise ValueError("samplerate must be positive")
        if chunk_ms <= 0:
            raise ValueError("chunk_ms must be positive")
        self.samplerate = samplerate
        self.chunk_ms = chunk_ms
        self.device = device
        self._stream_factory = stream_factory
        self._stream: Any | None = None
        self._closed = False
        self.last_overflow = False

    @property
    def frames_per_chunk(self) -> int:
        """Samples in the default chunk at the configured chunk length."""
        return max(1, round(self.samplerate * self.chunk_ms / 1000))

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has been called."""
        return self._closed

    @property
    def stream_open(self) -> bool:
        """Whether the microphone stream has already been opened."""
        return self._stream is not None

    def _open_stream(self) -> Any:
        """Import ``sounddevice`` and open the input stream on first use."""
        if self._stream is not None:
            return self._stream
        if self._stream_factory is not None:
            stream = self._stream_factory()
        else:
            try:
                import sounddevice as sd
            except ImportError as exc:
                raise AudioUnavailable(
                    "sounddevice is not installed; install it to use a microphone"
                ) from exc
            try:
                stream = sd.InputStream(
                    samplerate=self.samplerate,
                    channels=1,
                    dtype="int16",
                    device=self.device,
                )
            except Exception as exc:
                raise AudioUnavailable(f"cannot open microphone: {exc}") from exc
        if stream is None:
            raise AudioUnavailable("microphone stream factory returned None")
        try:
            stream.start()
        except Exception as exc:
            raise AudioUnavailable(f"cannot start microphone stream: {exc}") from exc
        self._stream = stream
        return stream

    def read(self, frames: int) -> np.ndarray:
        """Read ``frames`` samples from the microphone as int16 mono."""
        if self._closed:
            raise AudioUnavailable("audio source is closed")
        if frames <= 0:
            raise ValueError("frames must be positive")
        stream = self._open_stream()
        try:
            result = stream.read(frames)
        except Exception as exc:
            raise AudioUnavailable(f"microphone read failed: {exc}") from exc
        if isinstance(result, tuple) and len(result) == 2:
            data, overflowed = result
        else:
            data, overflowed = result, False
        self.last_overflow = bool(overflowed)
        return _as_int16(np.asarray(data))

    def close(self) -> None:
        """Stop and close the microphone stream, if it was opened."""
        stream = self._stream
        self._stream = None
        if stream is not None:
            stop = getattr(stream, "stop", None)
            if callable(stop):
                # Closing is best-effort; the device may already be gone.
                with contextlib.suppress(Exception):
                    stop()
            close = getattr(stream, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
        self._closed = True


@runtime_checkable
class Vad(Protocol):
    """Frame-level voice activity detector with internal state."""

    def is_speech(self, frame: np.ndarray) -> bool:
        """Return whether this frame contains speech."""

    def reset(self) -> None:
        """Forget the current stream state."""


class EnergyVad:
    """RMS energy VAD with onset and hangover hysteresis.

    ``min_speech_ms`` suppresses short clicks and keyboard noise: speech is
    reported only after that much voiced audio. ``hangover_ms`` keeps reporting
    speech through a short pause so a phrase is not cut in the middle.
    """

    def __init__(
        self,
        threshold: float = 0.01,
        min_speech_ms: float = 250,
        hangover_ms: float = 500,
        *,
        samplerate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        if threshold < 0:
            raise ValueError("threshold must not be negative")
        if min_speech_ms < 0:
            raise ValueError("min_speech_ms must not be negative")
        if hangover_ms < 0:
            raise ValueError("hangover_ms must not be negative")
        if samplerate <= 0:
            raise ValueError("samplerate must be positive")
        self.threshold = float(threshold)
        self.min_speech_ms = float(min_speech_ms)
        self.hangover_ms = float(hangover_ms)
        self.samplerate = samplerate
        self.last_rms = 0.0
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._active = False

    @staticmethod
    def rms(frame: np.ndarray | bytes | bytearray | memoryview) -> float:
        """Return the root-mean-square level of a frame in ``[0, 1]``."""
        samples = _as_float32(frame)
        if samples.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))

    @property
    def active(self) -> bool:
        """Whether the detector currently reports speech."""
        return self._active

    def reset(self) -> None:
        """Clear onset and hangover counters."""
        self.last_rms = 0.0
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._active = False

    def is_speech(self, frame: np.ndarray) -> bool:
        """Update the energy state and return the debounced speech decision."""
        samples = _as_array(frame)
        frame_ms = _duration_s(samples.size, self.samplerate) * 1000.0
        self.last_rms = self.rms(samples) if samples.size else 0.0
        speech = self.last_rms >= self.threshold
        if not self._active:
            if not speech:
                self._speech_ms = 0.0
                return False
            self._speech_ms += frame_ms
            if self._speech_ms >= self.min_speech_ms:
                self._active = True
                self._silence_ms = 0.0
                return True
            return False
        if speech:
            self._silence_ms = 0.0
            self._speech_ms += frame_ms
            return True
        self._silence_ms += frame_ms
        if self._silence_ms > self.hangover_ms:
            self._active = False
            self._speech_ms = 0.0
            self._silence_ms = 0.0
            return False
        return True


class SileroVad:
    """Silero VAD with a lazy model load and internal 30 ms buffering.

    Silero expects 256 samples at 8 kHz or 512 samples at 16 kHz per call. This
    wrapper buffers arbitrary frame sizes (including Kivi's 30 ms chunks) and
    reports speech when any completed window crosses ``threshold``.
    """

    def __init__(
        self,
        threshold: float = 0.5,
        *,
        samplerate: int = DEFAULT_SAMPLE_RATE,
        onnx: bool = True,
        model_loader: Callable[[], Any] | None = None,
    ) -> None:
        if samplerate not in (8000, 16000):
            raise ValueError("Silero VAD supports 8000 or 16000 Hz")
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        self.threshold = float(threshold)
        self.samplerate = samplerate
        self.onnx = onnx
        self._model_loader = model_loader
        self._model: Any | None = None
        self._buffer = np.zeros(0, dtype=np.float32)

    @property
    def model_loaded(self) -> bool:
        """Whether the Silero model has already been loaded."""
        return self._model is not None

    @property
    def window_samples(self) -> int:
        """Samples Silero expects per inference window."""
        return 512 if self.samplerate == 16000 else 256

    def load(self) -> Any:
        """Load the bundled Silero model once; no download is performed by Kivi."""
        if self._model is not None:
            return self._model
        if self._model_loader is not None:
            loader = self._model_loader
        else:

            def loader() -> Any:
                return _load_silero_vad(onnx=self.onnx)

        try:
            model = loader()
        except AudioUnavailable:
            raise
        except ImportError as exc:
            raise AudioUnavailable("silero-vad is not installed") from exc
        except Exception as exc:
            raise AudioUnavailable(f"failed to load Silero VAD: {exc}") from exc
        if model is None:
            raise AudioUnavailable("Silero VAD loader returned None")
        self._model = model
        return model

    def reset(self) -> None:
        """Reset model state and drop any partially filled inference window."""
        if self._model is not None:
            reset_states = getattr(self._model, "reset_states", None)
            if callable(reset_states):
                reset_states()
        self._buffer = np.zeros(0, dtype=np.float32)

    def is_speech(self, frame: np.ndarray) -> bool:
        """Buffer the frame, run Silero on full windows and return any speech."""
        model = self.load()
        pcm = _as_int16(frame)
        if pcm.size:
            floats = pcm.astype(np.float32) / 32768.0
            self._buffer = np.concatenate((self._buffer, floats))
        speech = False
        window = self.window_samples
        while self._buffer.size >= window:
            chunk = self._buffer[:window]
            self._buffer = self._buffer[window:]
            if self._probability(model, chunk) >= self.threshold:
                speech = True
        return speech

    def _probability(self, model: Any, chunk: np.ndarray) -> float:
        """Run one Silero window and normalise the scalar probability."""
        try:
            import torch
        except ImportError as exc:
            raise AudioUnavailable("Silero VAD requires torch") from exc
        tensor = torch.from_numpy(chunk.copy()).unsqueeze(0)
        try:
            with torch.no_grad():
                raw = model(tensor, self.samplerate)
        except Exception as exc:
            raise AudioUnavailable(f"Silero VAD inference failed: {exc}") from exc
        if hasattr(raw, "item"):
            try:
                return float(raw.item())
            except (RuntimeError, ValueError):
                pass
        values = np.asarray(raw, dtype=np.float64).reshape(-1)
        return float(values[0]) if values.size else 0.0


@dataclass(frozen=True, slots=True)
class Segment:
    """A complete utterance as int16 PCM plus its duration."""

    pcm: np.ndarray
    duration_s: float
    sample_rate: int = DEFAULT_SAMPLE_RATE


class VadSegmenter:
    """Turn per-frame VAD decisions into complete utterances.

    The segmenter starts a segment on the first speech frame, keeps a short
    pre-roll so the first syllable is not clipped, bridges pauses shorter than
    ``hangover_ms``, and force-flushes at ``max_segment_s``. Trailing silence is
    trimmed down to ``speech_pad_ms`` so Whisper does not receive long pauses.
    """

    def __init__(
        self,
        vad: Vad,
        samplerate: int = DEFAULT_SAMPLE_RATE,
        chunk_ms: int = DEFAULT_CHUNK_MS,
        max_segment_s: float = 30.0,
        *,
        hangover_ms: float = 0.0,
        preroll_ms: float = 300.0,
        speech_pad_ms: float = 100.0,
    ) -> None:
        if samplerate <= 0:
            raise ValueError("samplerate must be positive")
        if chunk_ms <= 0:
            raise ValueError("chunk_ms must be positive")
        if max_segment_s <= 0:
            raise ValueError("max_segment_s must be positive")
        if hangover_ms < 0 or preroll_ms < 0 or speech_pad_ms < 0:
            raise ValueError("hangover_ms, preroll_ms and speech_pad_ms must not be negative")
        self.vad = vad
        self.samplerate = samplerate
        self.chunk_ms = chunk_ms
        self.max_segment_s = float(max_segment_s)
        self.hangover_ms = float(hangover_ms)
        self.preroll_ms = float(preroll_ms)
        self.speech_pad_ms = float(speech_pad_ms)
        self._buffer = np.zeros(0, dtype=np.int16)
        self._preroll = np.zeros(0, dtype=np.int16)
        self._active = False
        self._silence_ms = 0.0
        self._speech_end = 0

    @property
    def frames_per_chunk(self) -> int:
        """Samples in the nominal chunk at ``chunk_ms``."""
        return max(1, round(self.samplerate * self.chunk_ms / 1000))

    @property
    def active(self) -> bool:
        """Whether an utterance is currently being collected."""
        return self._active

    def reset(self) -> None:
        """Drop buffered audio and reset the underlying VAD."""
        self._buffer = np.zeros(0, dtype=np.int16)
        self._preroll = np.zeros(0, dtype=np.int16)
        self._active = False
        self._silence_ms = 0.0
        self._speech_end = 0
        self.vad.reset()

    def feed(self, frame: np.ndarray) -> Segment | None:
        """Consume one frame and return a segment when an utterance ends."""
        pcm = _as_int16(frame)
        if pcm.size == 0:
            return None
        if not self._active:
            self._append_preroll(pcm)
            if not self.vad.is_speech(pcm):
                return None
            self._active = True
            # With preroll disabled the onset frame itself is the first sample.
            self._buffer = (
                self._preroll.copy() if self._preroll.size else pcm.copy()
            )
            self._preroll = np.zeros(0, dtype=np.int16)
            self._silence_ms = 0.0
            self._speech_end = self._buffer.size
        else:
            self._buffer = np.concatenate((self._buffer, pcm))
            if self.vad.is_speech(pcm):
                self._silence_ms = 0.0
                self._speech_end = self._buffer.size
            else:
                self._silence_ms += _duration_s(pcm.size, self.samplerate) * 1000.0
                if self._silence_ms >= self.hangover_ms:
                    return self._emit()
        if self._current_duration_s() >= self.max_segment_s:
            return self._emit()
        return None

    def flush(self) -> Segment | None:
        """Return the pending utterance, if any, and reset the segmenter."""
        if not self._active:
            self._preroll = np.zeros(0, dtype=np.int16)
            return None
        return self._emit()

    def _append_preroll(self, pcm: np.ndarray) -> None:
        """Keep a bounded tail of recent non-speech frames for onset pre-roll."""
        if self.preroll_ms <= 0:
            self._preroll = np.zeros(0, dtype=np.int16)
            return
        self._preroll = np.concatenate((self._preroll, pcm))
        limit = max(1, round(self.samplerate * self.preroll_ms / 1000))
        if self._preroll.size > limit:
            self._preroll = self._preroll[-limit:]

    def _current_duration_s(self) -> float:
        """Duration of the audio buffered for the current utterance."""
        return _duration_s(self._buffer.size, self.samplerate)

    def _emit(self) -> Segment | None:
        """Trim and return the current utterance, then reset its state."""
        end = self._speech_end if self._speech_end > 0 else self._buffer.size
        pad = round(self.samplerate * self.speech_pad_ms / 1000)
        end = min(self._buffer.size, end + pad)
        pcm = self._buffer[:end].copy()
        self._buffer = np.zeros(0, dtype=np.int16)
        self._preroll = np.zeros(0, dtype=np.int16)
        self._active = False
        self._silence_ms = 0.0
        self._speech_end = 0
        if pcm.size == 0:
            return None
        return Segment(
            pcm=pcm,
            duration_s=_duration_s(pcm.size, self.samplerate),
            sample_rate=self.samplerate,
        )


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """One timed piece of a transcript."""

    text: str
    start_s: float
    end_s: float


@dataclass(frozen=True, slots=True)
class Transcript:
    """A local speech-to-text result."""

    text: str
    language: str
    duration_s: float
    segments: tuple[TranscriptSegment, ...] = ()


@runtime_checkable
class Transcriber(Protocol):
    """Anything that turns mono audio into a :class:`Transcript`."""

    def transcribe(self, audio: np.ndarray) -> Transcript:
        """Transcribe int16 or float mono audio."""


class WhisperTranscriber:
    """faster-whisper STT with a lazy model load.

    The model is not loaded at import or construction. ``model_size`` is
    resolved by faster-whisper on the first :meth:`transcribe` call, which may
    download weights once; pass ``model_path`` to force a fully offline model.
    """

    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = "ru",
        *,
        model_path: Path | str | None = None,
        download_root: Path | str | None = None,
        beam_size: int = 5,
        model_loader: Callable[..., Any] | None = None,
    ) -> None:
        if beam_size <= 0:
            raise ValueError("beam_size must be positive")
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.model_path = Path(model_path) if model_path is not None else None
        self.download_root = str(download_root) if download_root is not None else None
        self.beam_size = beam_size
        self._model_loader = model_loader
        self._model: Any | None = None

    @property
    def model_loaded(self) -> bool:
        """Whether faster-whisper has already built the model."""
        return self._model is not None

    @property
    def target(self) -> str:
        """Model name or explicit path passed to faster-whisper."""
        return str(self.model_path) if self.model_path is not None else self.model_size

    def load(self) -> Any:
        """Load the Whisper model once and return it."""
        if self._model is not None:
            return self._model
        if self.model_path is not None and not self.model_path.exists():
            raise SttUnavailable(f"Whisper model not found: {self.model_path}")
        if self._model_loader is not None:
            loader = self._model_loader
        else:

            def loader(target: str, **kwargs: Any) -> Any:
                return _load_whisper_model(target, **kwargs)

        try:
            model = loader(
                self.target,
                device=self.device,
                compute_type=self.compute_type,
                download_root=self.download_root,
            )
        except SttUnavailable:
            raise
        except ImportError as exc:
            raise SttUnavailable(
                "faster-whisper is not installed; install it for speech-to-text"
            ) from exc
        except Exception as exc:
            raise SttUnavailable(
                f"failed to load faster-whisper model {self.target!r}: {exc}"
            ) from exc
        if model is None:
            raise SttUnavailable("faster-whisper loader returned None")
        self._model = model
        return model

    def transcribe(self, audio: np.ndarray | bytes | bytearray | memoryview) -> Transcript:
        """Transcribe mono int16 or float audio with faster-whisper."""
        waveform = _as_float32(audio)
        if waveform.size == 0:
            raise SttError("cannot transcribe empty audio")
        model = self.load()
        try:
            segments_iter, info = model.transcribe(
                waveform,
                language=self.language,
                beam_size=self.beam_size,
                vad_filter=False,
            )
            segments: list[TranscriptSegment] = []
            for segment in segments_iter:
                text = str(getattr(segment, "text", "")).strip()
                if not text:
                    continue
                segments.append(
                    TranscriptSegment(
                        text=text,
                        start_s=float(getattr(segment, "start", 0.0)),
                        end_s=float(getattr(segment, "end", 0.0)),
                    )
                )
        except SttError:
            raise
        except Exception as exc:
            raise SttError(f"Whisper transcription failed: {exc}") from exc
        text = " ".join(segment.text for segment in segments).strip()
        detected_language = getattr(info, "language", None) or self.language or ""
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        if duration <= 0:
            duration = _duration_s(waveform.size, DEFAULT_SAMPLE_RATE)
        return Transcript(
            text=text,
            language=str(detected_language),
            duration_s=duration,
            segments=tuple(segments),
        )


class FakeTranscriber:
    """Deterministic transcriber for tests and text-only pipelines."""

    def __init__(
        self,
        text: str = "привет",
        language: str = "ru",
        *,
        segments: tuple[TranscriptSegment, ...] | None = None,
        samplerate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        if samplerate <= 0:
            raise ValueError("samplerate must be positive")
        self.text = text
        self.language = language
        self.samplerate = samplerate
        self._segments = segments
        self.calls: list[np.ndarray] = []

    def transcribe(self, audio: np.ndarray | bytes | bytearray | memoryview) -> Transcript:
        """Record the audio and return the configured transcript."""
        pcm = _as_int16(audio)
        self.calls.append(pcm)
        duration = _duration_s(pcm.size, self.samplerate)
        segments = self._segments
        if segments is None:
            segments = (
                (TranscriptSegment(self.text, 0.0, duration),) if self.text else ()
            )
        return Transcript(
            text=self.text,
            language=self.language,
            duration_s=duration,
            segments=tuple(segments),
        )


def _load_silero_vad(*, onnx: bool) -> Any:
    """Import silero-vad and load its bundled model."""
    try:
        from silero_vad import load_silero_vad
    except ImportError as exc:
        raise AudioUnavailable("silero-vad is not installed") from exc
    return load_silero_vad(onnx=onnx)


def _load_whisper_model(
    target: str,
    *,
    device: str,
    compute_type: str,
    download_root: str | None,
) -> Any:
    """Import faster-whisper and construct a model."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise SttUnavailable("faster-whisper is not installed") from exc
    return WhisperModel(
        target,
        device=device,
        compute_type=compute_type,
        download_root=download_root,
    )


@runtime_checkable
class WakeWordDetector(Protocol):
    """Frame-level wake-word detector."""

    def detect(self, frame: np.ndarray) -> bool:
        """Return whether the wake word is present in this frame."""

    def reset(self) -> None:
        """Forget detector state after an utterance or a false trigger."""


class OpenWakeWordDetector:
    """openWakeWord detector for the custom Kivi model.

    There is intentionally no fallback to a stock phrase such as ``hey_jarvis``:
    a missing custom model raises :class:`WakeWordUnavailable` on first use.
    """

    def __init__(
        self,
        model_path: Path | str | None = None,
        threshold: float = 0.5,
        *,
        model_loader: Callable[[Path], Any] | None = None,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        self.model_path = (
            Path(model_path) if model_path is not None else default_wake_model_path()
        )
        self.threshold = float(threshold)
        self._model_loader = model_loader
        self._model: Any | None = None
        self._last_hit = False

    @property
    def model_loaded(self) -> bool:
        """Whether the openWakeWord model has already been loaded."""
        return self._model is not None

    def load(self) -> Any:
        """Load the custom model once; raises if it is not installed."""
        if self._model is not None:
            return self._model
        if not self.model_path.is_file():
            data_path = get_paths().data / WAKE_MODEL_DATA
            raise WakeWordUnavailable(
                "custom Kivi model is not installed: "
                f"expected {self.model_path} or {data_path}"
            )
        if self._model_loader is not None:
            loader = self._model_loader
        else:

            def loader(path: Path) -> Any:
                return _load_openwakeword_model(path)

        try:
            model = loader(self.model_path)
        except WakeWordUnavailable:
            raise
        except ImportError as exc:
            raise WakeWordUnavailable("openWakeWord is not installed") from exc
        except Exception as exc:
            raise WakeWordUnavailable(
                f"failed to load custom Kivi wake model {self.model_path}: {exc}"
            ) from exc
        if model is None:
            raise WakeWordUnavailable("openWakeWord loader returned None")
        self._model = model
        return model

    def detect(self, frame: np.ndarray) -> bool:
        """Return True once per threshold crossing; stock models are never used."""
        pcm = _as_int16(frame)
        if pcm.size == 0:
            return False
        model = self.load()
        try:
            scores = model.predict(pcm)
        except Exception as exc:
            raise WakeWordUnavailable(f"openWakeWord inference failed: {exc}") from exc
        if not isinstance(scores, dict) or not scores:
            raise WakeWordUnavailable("openWakeWord returned no scores")
        name = self.model_path.stem
        if name in scores:
            score = scores[name]
        elif len(scores) == 1:
            score = next(iter(scores.values()))
        else:
            raise WakeWordUnavailable(
                f"openWakeWord returned scores without the {name!r} model"
            )
        hit = float(score) >= self.threshold
        if hit and not self._last_hit:
            self._last_hit = True
            return True
        if not hit:
            self._last_hit = False
        return False

    def reset(self) -> None:
        """Clear the edge detector and, if loaded, the openWakeWord state."""
        self._last_hit = False
        if self._model is not None:
            reset = getattr(self._model, "reset", None)
            if callable(reset):
                reset()


class TextWakeWordDetector:
    """Detect a wake word at the start of an already transcribed phrase.

    This is a text-level helper for push-to-talk and for testing; it is not a
    frame detector. Only the first token is inspected, so "привет, Киви" does
    not trigger it.
    """

    def __init__(self, words: tuple[str, ...] = ("киви", "kivi")) -> None:
        cleaned = tuple(word.strip() for word in words if word.strip())
        if not cleaned:
            raise ValueError("at least one wake word is required")
        self.words = cleaned
        alternatives = "|".join(re.escape(word) for word in sorted(cleaned, key=len, reverse=True))
        self._pattern = re.compile(rf"^[\W_]*({alternatives})(?!\w)", re.IGNORECASE)

    def detect(self, text: str | Transcript) -> bool:
        """Return whether ``text`` starts with one of the configured words."""
        value = text.text if isinstance(text, Transcript) else text
        if not isinstance(value, str):
            raise TypeError("TextWakeWordDetector expects a string or Transcript")
        return self._pattern.match(value.strip()) is not None

    def strip_wake_word(self, text: str) -> str:
        """Remove a leading wake word and following punctuation, if present."""
        match = self._pattern.match(text.strip())
        if match is None:
            return text
        remainder = text.strip()[match.end() :]
        return remainder.lstrip(" \t,.:;!?—-")

    def reset(self) -> None:
        """Text detection is stateless; provided for protocol compatibility."""


class PushToTalkGate:
    """A wake-word bypass: while held, every frame is treated as activation."""

    def __init__(self, active: bool = False) -> None:
        self._active = bool(active)

    @property
    def active(self) -> bool:
        """Whether the push-to-talk key is currently held."""
        return self._active

    def press(self) -> None:
        """Mark push-to-talk as held."""
        self._active = True

    def release(self) -> None:
        """Mark push-to-talk as released."""
        self._active = False

    def toggle(self) -> bool:
        """Flip the gate and return the new state."""
        self._active = not self._active
        return self._active

    def detect(self, frame: np.ndarray) -> bool:
        """Bypass wake-word detection while the key is held."""
        del frame
        return self._active

    def reset(self) -> None:
        """Release the gate."""
        self._active = False

    def __enter__(self) -> PushToTalkGate:
        self.press()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _load_openwakeword_model(path: Path) -> Any:
    """Import openWakeWord and load exactly one explicit custom model."""
    try:
        from openwakeword.model import Model
    except ImportError as exc:
        raise WakeWordUnavailable("openWakeWord is not installed") from exc
    framework = "onnx" if path.suffix.lower() == ".onnx" else "tflite"
    return Model(wakeword_models=[str(path)], inference_framework=framework)
