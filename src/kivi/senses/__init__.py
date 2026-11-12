"""Senses: voice, hearing, vision and text input."""

from kivi.senses.qwen_tts import (
    QwenTtsConfig,
    QwenTtsEngine,
    QwenTtsError,
    QwenVoiceDesigner,
)
from kivi.senses.tts import (
    NullTts,
    PiperTts,
    SileroTts,
    SpeechPipeline,
    TtsEngine,
    TtsError,
    TtsResult,
)

__all__ = [
    "NullTts",
    "PiperTts",
    "QwenTtsConfig",
    "QwenTtsEngine",
    "QwenTtsError",
    "QwenVoiceDesigner",
    "SileroTts",
    "SpeechPipeline",
    "TtsEngine",
    "TtsError",
    "TtsResult",
]
