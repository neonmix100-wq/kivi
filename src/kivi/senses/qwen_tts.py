"""Qwen3-TTS voice engine for Kivi.

Qwen3-TTS is a 1.7B multilingual TTS model with a Russian voice.  It is used in
two stages:

1. :class:`QwenVoiceDesigner` creates an original voice from a text description
   and writes a short reference WAV;
2. :class:`QwenTtsEngine` clones that reference for fast, consistent synthesis.

The imports are lazy: Kivi must start and run its tests without the optional
Qwen3-TTS dependency, torch or the model weights.  Set
``KIVI_QWEN_TTS_DIR`` or pass ``model_dir`` to use a local checkpoint.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kivi.senses.tts import TtsError, TtsResult, write_wav

DEFAULT_LANGUAGE = "Russian"
DEFAULT_VOICE_INSTRUCT = (
    "Young female voice, 18-20 years old, cute and playful anime-inspired "
    "timbre: bright, soft, slightly breathy, high pitch, warm smile in the "
    "voice, natural Russian pronunciation without a cartoonish accent, not "
    "childish and not sexualized."
)
DEFAULT_REFERENCE = Path("assets/voice-samples/kivi_anime_ref.wav")
DEFAULT_MODEL_DIR = Path("data/models/qwen3-tts/1.7B-Base")
DEFAULT_DESIGN_DIR = Path("data/models/qwen3-tts/1.7B-VoiceDesign")


class QwenTtsError(TtsError):
    """Raised when Qwen3-TTS is unavailable or a reference voice is missing."""


@dataclass(frozen=True, slots=True)
class QwenTtsConfig:
    """Paths and generation settings; all fields are optional."""

    model_dir: Path = DEFAULT_MODEL_DIR
    design_dir: Path = DEFAULT_DESIGN_DIR
    reference_wav: Path = DEFAULT_REFERENCE
    reference_text: str = ""
    language: str = DEFAULT_LANGUAGE
    device: str = "cuda"
    dtype: str = "bfloat16"
    attn_implementation: str = "eager"
    max_new_tokens: int = 512


def _torch_dtype(name: str) -> Any:
    import torch

    mapping = {
        "float32": torch.float32,
        "float16": torch.float16,
        "bfloat16": torch.bfloat16,
    }
    try:
        return mapping[name]
    except KeyError as exc:
        raise QwenTtsError(f"unsupported dtype: {name}") from exc


def _as_numpy(audio: Any) -> Any:
    """Convert a torch tensor/list to a float numpy array without importing torch."""
    if hasattr(audio, "detach"):
        audio = audio.detach().cpu().float().numpy()
    return audio


def _reference_text(reference: Path, explicit: str) -> str:
    if explicit.strip():
        return explicit.strip()
    sidecar = reference.with_suffix(".txt")
    if sidecar.is_file():
        return sidecar.read_text(encoding="utf-8").strip()
    return ""


def _write_result(audio: Any, sample_rate: int, output: Path) -> TtsResult:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = list(_as_numpy(audio))
    write_wav(path, samples, sample_rate)
    return TtsResult(
        path=path,
        sample_rate=sample_rate,
        duration_s=len(samples) / float(sample_rate or 1),
        engine="qwen3-tts",
    )


class QwenVoiceDesigner:
    """Create an original reference voice from a natural-language description."""

    def __init__(
        self,
        config: QwenTtsConfig | None = None,
        *,
        model_loader: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config or QwenTtsConfig()
        self._loader = model_loader
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from qwen_tts import Qwen3TTSModel
        except ImportError as exc:
            raise QwenTtsError(
                "qwen-tts is not installed; install the 'qwen' extra"
            ) from exc
        model_dir = Path(self.config.design_dir)
        if not model_dir.is_dir():
            raise QwenTtsError(f"Qwen3-TTS VoiceDesign model not found: {model_dir}")
        self._model = (self._loader or (lambda: Qwen3TTSModel.from_pretrained(
            str(model_dir),
            device_map=self.config.device,
            dtype=_torch_dtype(self.config.dtype),
            attn_implementation=self.config.attn_implementation,
        )))()
        return self._model

    def design(
        self,
        text: str,
        output: Path,
        *,
        instruct: str = DEFAULT_VOICE_INSTRUCT,
    ) -> TtsResult:
        """Generate a reference WAV and a matching ``.txt`` transcript."""
        cleaned = text.strip()
        if not cleaned:
            raise QwenTtsError("cannot design a voice from empty text")
        model = self._load()
        try:
            wavs, sample_rate = model.generate_voice_design(
                text=cleaned,
                language=self.config.language,
                instruct=instruct,
                max_new_tokens=self.config.max_new_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - model boundary
            raise QwenTtsError(f"voice design failed: {exc}") from exc
        result = _write_result(wavs[0], int(sample_rate), output)
        Path(output).with_suffix(".txt").write_text(cleaned, encoding="utf-8")
        return result


class QwenTtsEngine:
    """Clone one designed reference voice and synthesize Russian speech."""

    name = "qwen3-tts"

    def __init__(
        self,
        config: QwenTtsConfig | None = None,
        *,
        model_loader: Callable[[], Any] | None = None,
        prompt_factory: Callable[[Any, str], Any] | None = None,
    ) -> None:
        self.config = config or QwenTtsConfig()
        self._loader = model_loader
        self._prompt_factory = prompt_factory
        self._model: Any = None
        self._prompt: Any = None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        reference = Path(self.config.reference_wav)
        if not reference.is_file():
            raise QwenTtsError(
                f"reference voice not found: {reference}; run `kivi voice design`"
            )
        try:
            from qwen_tts import Qwen3TTSModel
        except ImportError as exc:
            raise QwenTtsError(
                "qwen-tts is not installed; install the 'qwen' extra"
            ) from exc
        model_dir = Path(self.config.model_dir)
        if not model_dir.is_dir():
            raise QwenTtsError(f"Qwen3-TTS Base model not found: {model_dir}")
        self._model = (self._loader or (lambda: Qwen3TTSModel.from_pretrained(
            str(model_dir),
            device_map=self.config.device,
            dtype=_torch_dtype(self.config.dtype),
            attn_implementation=self.config.attn_implementation,
        )))()
        return self._model

    def _voice_prompt(self) -> Any:
        if self._prompt is not None:
            return self._prompt
        model = self._load()
        reference = Path(self.config.reference_wav)
        text = _reference_text(reference, self.config.reference_text)
        if not text:
            raise QwenTtsError(
                "reference transcript is missing; put a .txt next to the WAV"
            )
        factory = self._prompt_factory or (
            lambda loaded, ref_text: loaded.create_voice_clone_prompt(
                ref_audio=str(reference),
                ref_text=ref_text,
            )
        )
        try:
            self._prompt = factory(model, text)
        except Exception as exc:  # noqa: BLE001 - model boundary
            raise QwenTtsError(f"cannot build voice prompt: {exc}") from exc
        return self._prompt

    def synth(
        self,
        text: str,
        output: Path,
        *,
        speaker: str = "",
        sample_rate: int = 48000,
    ) -> TtsResult:
        """Synthesize text with the designed reference; ``speaker`` is ignored."""
        del speaker
        cleaned = text.strip()
        if not cleaned:
            raise QwenTtsError("cannot synthesize empty text")
        model = self._load()
        prompt = self._voice_prompt()
        try:
            wavs, sr = model.generate_voice_clone(
                text=cleaned,
                language=self.config.language,
                voice_clone_prompt=prompt,
                max_new_tokens=self.config.max_new_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - model boundary
            raise QwenTtsError(f"synthesis failed: {exc}") from exc
        # Qwen controls its own sampling rate; 48 kHz is only a caller hint.
        del sample_rate
        return _write_result(wavs[0], int(sr), output)


__all__ = [
    "DEFAULT_LANGUAGE",
    "DEFAULT_MODEL_DIR",
    "DEFAULT_REFERENCE",
    "DEFAULT_VOICE_INSTRUCT",
    "QwenTtsConfig",
    "QwenTtsEngine",
    "QwenTtsError",
    "QwenVoiceDesigner",
]
