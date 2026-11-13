"""Local text-to-speech engines and the sentence streaming pipeline.

The default engine is Silero ``v5_ru`` loaded from the package model that is
already installed in ``.venv``. Nothing is downloaded at runtime: if the model
or the Piper binary is missing, the caller gets a clear :class:`TtsError`.
"""

from __future__ import annotations

import re
import struct
import subprocess
import wave
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from kivi.paths import project_root

MAX_SENTENCE_CHARS = 200
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_SPEAKER = "baya"
PIPER_SAMPLE_RATE = 22050

DEFAULT_SILERO_MODEL = (
    project_root() / ".venv" / "Lib" / "site-packages" / "silero" / "model" / "v5_ru.pt"
)
DEFAULT_PIPER_EXE = project_root() / ".venv" / "Scripts" / "piper.exe"
DEFAULT_PIPER_MODEL = (
    project_root()
    / "assets"
    / "voice-samples"
    / "piper-voices"
    / "ru_RU-irina-medium.onnx"
)


class TtsError(RuntimeError):
    """Raised when speech synthesis cannot produce a WAV file."""


@dataclass(frozen=True, slots=True)
class TtsResult:
    """A synthesized mono WAV file and its basic audio metadata."""

    path: Path
    sample_rate: int
    duration_s: float
    engine: str


@runtime_checkable
class TtsEngine(Protocol):
    """Minimal interface shared by Silero, Piper and test engines."""

    name: str

    def synth(
        self,
        text: str,
        output: Path,
        *,
        speaker: str = DEFAULT_SPEAKER,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> TtsResult:
        """Synthesize ``text`` into ``output`` and return the WAV metadata."""


def _encode_pcm16(samples: Iterable[float | int]) -> bytes:
    """Encode float samples in [-1, 1] (or integers) as little-endian PCM16."""
    chunks: list[bytes] = []
    for sample in samples:
        value = sample if isinstance(sample, int) else int(round(float(sample) * 32767))
        value = max(-32768, min(32767, value))
        chunks.append(struct.pack("<h", value))
    return b"".join(chunks)


def _as_pcm16_bytes(samples: bytes | bytearray | memoryview) -> bytes:
    data = bytes(samples)
    if len(data) % 2:
        raise ValueError("PCM16 data must contain an even number of bytes")
    return data


def write_wav(
    output: Path | str,
    samples: Sequence[float | int] | bytes | bytearray | memoryview,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> Path:
    """Write a 16-bit mono WAV file and return its path.

    ``bytes`` are treated as already encoded little-endian PCM16. A sequence of
    floats is clamped to ``[-1, 1]`` and scaled to int16; integers are treated
    as raw int16 values. Parent directories are created automatically.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    path = Path(output)
    if isinstance(samples, (bytes, bytearray, memoryview)):
        pcm = _as_pcm16_bytes(samples)
    else:
        pcm = _encode_pcm16(samples)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return path


def _audio_to_pcm16(audio: Any) -> bytes:
    """Convert a torch tensor, numpy array or sequence into PCM16 bytes."""
    if hasattr(audio, "detach"):
        audio = audio.detach()
    if hasattr(audio, "cpu"):
        audio = audio.cpu()
    if hasattr(audio, "numpy"):
        audio = audio.numpy()
    if hasattr(audio, "tolist"):
        audio = audio.tolist()
    if isinstance(audio, (bytes, bytearray, memoryview)):
        return _as_pcm16_bytes(audio)
    if isinstance(audio, (int, float)):
        audio = [audio]
    return _encode_pcm16(audio)


def _wav_metadata(path: Path, engine: str) -> TtsResult:
    """Read sample rate and duration back from a written WAV file."""
    try:
        with wave.open(str(path), "rb") as handle:
            sample_rate = handle.getframerate()
            frames = handle.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise TtsError(f"cannot read synthesized WAV {path}: {exc}") from exc
    duration = frames / sample_rate if sample_rate else 0.0
    return TtsResult(
        path=path,
        sample_rate=sample_rate,
        duration_s=duration,
        engine=engine,
    )


def _load_silero_package(model_path: Path) -> Any:
    """Load a packaged Silero model without downloading anything."""
    try:
        from torch import package
    except ImportError as exc:  # pragma: no cover - torch is installed here
        raise TtsError("Silero TTS requires torch in the active environment") from exc
    importer = package.PackageImporter(str(model_path))
    return importer.load_pickle("tts_models", "model")


class SileroTts:
    """Silero v5 Russian TTS with a lazy, package-first model load."""

    name = "silero"

    def __init__(
        self,
        model_path: Path | str | None = None,
        *,
        model_loader: Callable[[Path], Any] | None = None,
    ) -> None:
        self.model_path = Path(model_path) if model_path is not None else DEFAULT_SILERO_MODEL
        self._model_loader = model_loader
        self._model: Any | None = None

    @property
    def model_loaded(self) -> bool:
        """Whether the expensive model load has already happened."""
        return self._model is not None

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        if not self.model_path.is_file():
            raise TtsError(f"Silero model not found: {self.model_path}")
        loader = self._model_loader or _load_silero_package
        try:
            model = loader(self.model_path)
        except TtsError:
            raise
        except Exception as exc:
            raise TtsError(f"failed to load Silero model {self.model_path}: {exc}") from exc
        if model is None:
            raise TtsError("Silero model loader returned None")
        if hasattr(model, "eval"):
            model.eval()
        self._model = model
        return model

    def synth(
        self,
        text: str,
        output: Path,
        *,
        speaker: str = DEFAULT_SPEAKER,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> TtsResult:
        """Synthesize Russian text with the selected Silero speaker."""
        cleaned = text.strip()
        if not cleaned:
            raise TtsError("cannot synthesize empty text")
        model = self._load_model()
        try:
            audio = model.apply_tts(
                text=cleaned,
                speaker=speaker,
                sample_rate=sample_rate,
            )
        except Exception as exc:
            raise TtsError(f"Silero synthesis failed: {exc}") from exc
        pcm = _audio_to_pcm16(audio)
        if not pcm:
            raise TtsError("Silero returned no audio samples")
        path = write_wav(output, pcm, sample_rate)
        return TtsResult(
            path=path,
            sample_rate=sample_rate,
            duration_s=len(pcm) / 2 / sample_rate,
            engine=self.name,
        )


PiperRunner = Callable[[list[str], str], None]


def _default_piper_runner(argv: list[str], text: str) -> None:
    """Run Piper with a UTF-8 input file (its stdin path is unreliable)."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="kivi-piper-") as directory:
        input_path = Path(directory) / "input.txt"
        input_path.write_text(text, encoding="utf-8")
        completed = subprocess.run(
            [*argv, "-i", str(input_path)],
            capture_output=True,
            check=False,
        )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise TtsError(f"Piper exited with {completed.returncode}: {stderr}")


class PiperTts:
    """Offline Piper fallback using the bundled Irina ONNX voice."""

    name = "piper"

    def __init__(
        self,
        executable: Path | str | None = None,
        model: Path | str | None = None,
        *,
        runner: PiperRunner | None = None,
        sample_rate: int = PIPER_SAMPLE_RATE,
    ) -> None:
        self.executable = Path(executable) if executable is not None else DEFAULT_PIPER_EXE
        self.model = Path(model) if model is not None else DEFAULT_PIPER_MODEL
        self._runner = runner
        self.sample_rate = sample_rate

    def _argv(self, output: Path) -> list[str]:
        return [
            str(self.executable),
            "-m",
            str(self.model),
            "-f",
            str(output),
        ]

    def synth(
        self,
        text: str,
        output: Path,
        *,
        speaker: str = DEFAULT_SPEAKER,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> TtsResult:
        """Synthesize with Piper; ``speaker``/``sample_rate`` are compatibility args."""
        del speaker, sample_rate
        cleaned = text.strip()
        if not cleaned:
            raise TtsError("cannot synthesize empty text")
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        runner = self._runner
        if runner is None:
            if not self.executable.is_file():
                raise TtsError(f"Piper executable not found: {self.executable}")
            if not self.model.is_file():
                raise TtsError(f"Piper voice model not found: {self.model}")
            runner = _default_piper_runner
        try:
            runner(self._argv(path), cleaned)
        except TtsError:
            raise
        except Exception as exc:
            raise TtsError(f"Piper invocation failed: {exc}") from exc
        if not path.is_file():
            raise TtsError("Piper did not create the requested WAV file")
        result = _wav_metadata(path, self.name)
        if result.sample_rate <= 0:
            raise TtsError("Piper produced a WAV with an invalid sample rate")
        return result


class NullTts:
    """Deterministic silent engine for tests and for disabling voice output."""

    name = "null"

    def __init__(
        self,
        *,
        duration_s: float = 0.05,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        self.duration_s = duration_s
        self.sample_rate = sample_rate

    def synth(
        self,
        text: str,
        output: Path,
        *,
        speaker: str = DEFAULT_SPEAKER,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> TtsResult:
        """Write a short silent WAV independent of the input text."""
        del speaker, sample_rate
        frames = max(1, int(round(self.duration_s * self.sample_rate)))
        pcm = b"\x00\x00" * frames
        path = write_wav(output, pcm, self.sample_rate)
        return TtsResult(
            path=path,
            sample_rate=self.sample_rate,
            duration_s=frames / self.sample_rate,
            engine=self.name,
        )


_SENTENCE_BOUNDARY = re.compile(r"[.!?…]+[»\"'”’)\]]*(?=\s|$)")
_WHITESPACE = re.compile(r"\s+")


def _chunk_sentence(sentence: str, max_chars: int) -> list[str]:
    """Split one overlong sentence on word boundaries, never exceeding the limit."""
    if len(sentence) <= max_chars:
        return [sentence]
    chunks: list[str] = []
    current = ""
    for word in sentence.split(" "):
        while len(word) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(word[:max_chars])
            word = word[max_chars:]
        if not word:
            continue
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= max_chars:
            current = f"{current} {word}"
        else:
            chunks.append(current)
            current = word
    if current:
        chunks.append(current)
    return chunks


def split_sentences(text: str, *, max_chars: int = MAX_SENTENCE_CHARS) -> list[str]:
    """Split text into sentences of at most ``max_chars`` characters.

    Sentence boundaries are runs of ``.``, ``!``, ``?`` or ``…`` optionally
    followed by closing quotes/brackets. Whitespace is normalized; empty
    fragments are skipped. Overlong sentences are additionally chunked on
    spaces so every returned item fits the limit.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    normalized = _WHITESPACE.sub(" ", text).strip()
    if not normalized:
        return []
    pieces: list[str] = []
    start = 0
    for match in _SENTENCE_BOUNDARY.finditer(normalized):
        end = match.end()
        piece = normalized[start:end].strip()
        if piece:
            pieces.append(piece)
        start = end
    tail = normalized[start:].strip()
    if tail:
        pieces.append(tail)
    result: list[str] = []
    for piece in pieces:
        result.extend(_chunk_sentence(piece, max_chars))
    return [piece for piece in result if piece]


class SpeechPipeline:
    """Split an answer into sentences, synthesize them and optionally play each one."""

    def __init__(
        self,
        engine: TtsEngine | None = None,
        *,
        fallback: TtsEngine | None = None,
        player: Callable[[Path], object] | None = None,
        enable_fallback: bool = True,
        speaker: str = DEFAULT_SPEAKER,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        self.engine = engine if engine is not None else SileroTts()
        self.fallback = fallback if fallback is not None else PiperTts()
        self.player = player
        self.enable_fallback = enable_fallback
        self.speaker = speaker
        self.sample_rate = sample_rate

    def _synthesize_one(
        self,
        engine: TtsEngine,
        sentence: str,
        output: Path,
    ) -> TtsResult:
        return engine.synth(
            sentence,
            output,
            speaker=self.speaker,
            sample_rate=self.sample_rate,
        )

    def synthesize(
        self,
        text: str,
        output_dir: Path,
        *,
        engine: TtsEngine | None = None,
    ) -> list[TtsResult]:
        """Synthesize all sentences into numbered WAV files.

        The per-call ``engine`` overrides the pipeline primary. If a sentence
        fails and ``enable_fallback`` is true, the pipeline switches to the
        configured fallback engine for the rest of the answer. Playback through
        an injected ``player`` happens once per result and never touches audio
        hardware on its own.
        """
        sentences = split_sentences(text)
        if not sentences:
            return []
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        active = engine if engine is not None else self.engine
        results: list[TtsResult] = []
        for index, sentence in enumerate(sentences):
            target = output_path / f"speech_{index:03d}.wav"
            try:
                result = self._synthesize_one(active, sentence, target)
            except Exception as exc:
                if not self.enable_fallback or self.fallback is None or active is self.fallback:
                    raise TtsError(f"TTS failed for sentence {index + 1}: {exc}") from exc
                try:
                    result = self._synthesize_one(self.fallback, sentence, target)
                except Exception as fallback_exc:
                    raise TtsError(
                        f"TTS failed for sentence {index + 1}: {exc}; "
                        f"fallback also failed: {fallback_exc}"
                    ) from fallback_exc
                active = self.fallback
            results.append(result)
            if self.player is not None:
                try:
                    self.player(result.path)
                except Exception as exc:
                    raise TtsError(f"TTS playback failed for {result.path}: {exc}") from exc
        return results
