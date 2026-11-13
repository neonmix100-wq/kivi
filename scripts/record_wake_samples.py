"""Record a small speaker-specific wake-word dataset.

Examples::

    .venv\\Scripts\\python.exe scripts\\record_wake_samples.py --label positive --count 20
    .venv\\Scripts\\python.exe scripts\\record_wake_samples.py --label negative --count 20

Each press of Enter records one 1.6 s clip at 16 kHz.  Positive clips should be
just the word "Киви" (and occasional short "Эй, Киви"); negative clips should be
normal speech that should not wake Kivi.
"""

from __future__ import annotations

import argparse
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POSITIVE = ROOT / "data" / "wake-samples" / "positive"
DEFAULT_NEGATIVE = ROOT / "data" / "wake-samples" / "negative"

NEGATIVE_PROMPTS = (
    "Сегодня хорошая погода.",
    "Открой окно, пожалуйста.",
    "Проверь список задач.",
    "Сколько сейчас времени?",
    "Нужно позвонить клиенту.",
    "Включи музыку потише.",
    "Подготовь краткий отчёт.",
    "Спасибо за помощь.",
)


def record_clip(seconds: float, rate: int) -> np.ndarray:
    import sounddevice as sd

    frames = int(seconds * rate)
    audio = sd.rec(frames, samplerate=rate, channels=1, dtype="int16")
    sd.wait()
    return np.asarray(audio).reshape(-1)


def save_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.astype("<i2").tobytes())


def quality_report(samples: np.ndarray) -> str:
    """Return a short warning for silent or clipped recordings."""
    if samples.size == 0:
        return "пустая запись"
    peak = int(np.max(np.abs(samples)))
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    clipped = int(np.sum(np.abs(samples) >= 32767))
    warnings: list[str] = []
    if peak < 300 or rms < 40:
        warnings.append("слишком тихо")
    if clipped > samples.size * 0.01:
        warnings.append("клиппинг")
    return f"peak={peak}, rms={rms:.0f}" + (f" — {', '.join(warnings)}" if warnings else " — ok")


def main() -> int:
    parser = argparse.ArgumentParser(description="Record Kivi wake samples")
    parser.add_argument("--label", choices=("positive", "negative"), required=True)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--seconds", type=float, default=1.6)
    parser.add_argument("--rate", type=int, default=16_000)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    out = args.out or (DEFAULT_POSITIVE if args.label == "positive" else DEFAULT_NEGATIVE)
    out.mkdir(parents=True, exist_ok=True)
    existing = len(list(out.glob("*.wav")))
    print("Пиши коротко и естественно; фон — как в комнате, где будешь говорить с Kivi.")
    for index in range(args.count):
        prompt = (
            'Скажи: «Киви»'
            if args.label == "positive"
            else f"Скажи: {NEGATIVE_PROMPTS[index % len(NEGATIVE_PROMPTS)]}"
        )
        input(f"[{index + 1}/{args.count}] {prompt} — Enter, затем говори…")
        samples = record_clip(args.seconds, args.rate)
        path = out / f"{args.label}-{existing + index:03d}.wav"
        save_wav(path, samples, args.rate)
        print(f"  сохранено: {path.name}; {quality_report(samples)}")
        time.sleep(0.15)
    print(f"Готово: {out}")
    print("Теперь обучи модель:")
    print(
        ".venv\\Scripts\\python.exe scripts\\train_wake_model.py "
        f'--positive-dir "{DEFAULT_POSITIVE}" --negative-dir "{DEFAULT_NEGATIVE}"'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
