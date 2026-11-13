"""Train a small local "Kivi" wake-word model without large datasets.

The script synthesizes positive and negative Russian phrases with the local
Piper voice, extracts openWakeWord embeddings and trains a tiny classifier.
The result is a small ONNX model that openWakeWord can load directly.

Usage::

    .venv\\Scripts\\python.exe scripts\\train_wake_model.py --positives 80 --negatives 160

This is a prototype model: synthetic speech and a single Piper voice limit
robustness.  A production model needs real recordings from the target user.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import random
import wave
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from scipy.signal import resample_poly
from torch import nn

ROOT = Path(__file__).resolve().parents[1]
VOICE = ROOT / "assets" / "voice-samples" / "piper-voices" / "ru_RU-irina-medium.onnx"
OUT = ROOT / "assets" / "wake" / "kivi.onnx"
METRICS = ROOT / "data" / "wake-metrics.json"
SAMPLE_RATE = 16_000
CLIP_SAMPLES = SAMPLE_RATE * 2

POSITIVE_PHRASES = (
    "Киви",
    "Киви!",
    "Эй, Киви",
    "Киви, привет",
    "Слушай, Киви",
    "Киви, ты здесь",
    "Окей, Киви",
    "Киви, помоги",
)

NEGATIVE_PHRASES = (
    "Сегодня хорошая погода",
    "Открой пожалуйста окно",
    "Я хочу проверить настройки",
    "Это тестовая фраза",
    "Привет как дела",
    "Нужно позвонить клиенту",
    "Включи музыку потише",
    "Закажи доставку на завтра",
    "Проверь список задач",
    "Найди файл в папке",
    "Сколько сейчас времени",
    "Расскажи короткую новость",
    "Сделай заметку на память",
    "Покажи последние документы",
    "Спасибо за помощь",
    "Доброе утро",
    "Мне нужно отдохнуть",
    "Переключи на другой трек",
    "Закрой это окно",
    "Подготовь краткий отчёт",
    "Кивок и кивать",
    "Вики и кино",
    "Киса и килька",
    "Кивать головой",
    "Кино идёт вечером",
)


class SmallWakeNet(nn.Module):
    """A small ONNX-friendly classifier over 16x96 openWakeWord features."""

    def __init__(self, input_dim: int = 16 * 96) -> None:
        super().__init__()
        self.flatten = nn.Flatten()
        self.bn = nn.BatchNorm1d(input_dim)
        self.net = nn.Sequential(
            nn.Linear(input_dim, 64),
            nn.ReLU(),
            nn.Dropout(0.15),
            nn.Linear(64, 32),
            nn.ReLU(),
            nn.Linear(32, 1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(self.bn(self.flatten(x)))


class WakeOutput(nn.Module):
    """ONNX wrapper that exposes a 0..1 score."""

    def __init__(self, model: SmallWakeNet) -> None:
        super().__init__()
        self.model = model

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return torch.sigmoid(self.model(x))


def _to_16k(data: np.ndarray, rate: int) -> np.ndarray:
    if rate != SAMPLE_RATE:
        data = resample_poly(data, SAMPLE_RATE, rate)
    return data.astype(np.float32)


def synthesize(voice, text: str, length_scale: float, rng: random.Random) -> np.ndarray:
    """Synthesize one phrase and return 16 kHz float mono audio."""
    from piper.config import SynthesisConfig

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        voice.synthesize_wav(
            text,
            handle,
            SynthesisConfig(
                length_scale=length_scale,
                noise_scale=rng.uniform(0.55, 0.8),
                noise_w_scale=rng.uniform(0.6, 0.9),
            ),
        )
    buffer.seek(0)
    data, rate = sf.read(buffer, dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    return _to_16k(data, rate)


def place_phrase(audio: np.ndarray, rng: random.Random) -> np.ndarray:
    """Place the phrase near the end of a 2 s clip with random padding."""
    clip = np.zeros(CLIP_SAMPLES, dtype=np.float32)
    audio = audio[: CLIP_SAMPLES - 2000]
    offset = CLIP_SAMPLES - len(audio) - rng.randint(0, 1800)
    clip[offset : offset + len(audio)] = audio
    return clip


def augment(clip: np.ndarray, rng: random.Random) -> np.ndarray:
    """Cheap gain/noise/speed augmentation; keeps the model small."""
    gain = rng.uniform(0.55, 1.15)
    clip = clip * gain
    if rng.random() < 0.6:
        noise = np.random.default_rng(rng.randrange(2**32)).normal(
            0, rng.uniform(0.002, 0.015), clip.shape
        )
        clip = clip + noise.astype(np.float32)
    if rng.random() < 0.35:
        factor = rng.uniform(0.9, 1.1)
        clip = resample_poly(clip, int(factor * 1000), 1000)[:CLIP_SAMPLES]
        pad = np.zeros(CLIP_SAMPLES, dtype=np.float32)
        pad[: len(clip)] = clip
        clip = pad
    return np.clip(clip, -1.0, 1.0)


def load_recordings(
    directory: Path,
    label: int,
    rng: random.Random,
) -> tuple[list[np.ndarray], list[int]]:
    """Load real user recordings, resample to 16 kHz and augment."""
    clips: list[np.ndarray] = []
    labels: list[int] = []
    for path in sorted(directory.glob("*.wav")):
        data, rate = sf.read(path, dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.mean(axis=1)
        audio = _to_16k(data, rate)
        for _ in range(3):
            clips.append(augment(place_phrase(audio, rng), rng))
            labels.append(label)
    return clips, labels


def build_dataset(
    count_pos: int,
    count_neg: int,
    seed: int,
    positive_dir: Path | None = None,
    negative_dir: Path | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    if positive_dir is not None and negative_dir is not None:
        rng = random.Random(seed)
        pos_clips, pos_labels = load_recordings(positive_dir, 1, rng)
        neg_clips, neg_labels = load_recordings(negative_dir, 0, rng)
        if not pos_clips or not neg_clips:
            raise SystemExit("recording directories must contain .wav files")
        clips = pos_clips + neg_clips
        labels = pos_labels + neg_labels
        return np.stack(clips).astype(np.int16), np.asarray(labels, dtype=np.float32)

    from piper.voice import PiperVoice

    rng = random.Random(seed)
    voice = PiperVoice.load(VOICE)
    clips: list[np.ndarray] = []
    labels: list[int] = []
    for index in range(count_pos):
        text = POSITIVE_PHRASES[index % len(POSITIVE_PHRASES)]
        audio = synthesize(voice, text, rng.uniform(0.82, 1.12), rng)
        clips.append(augment(place_phrase(audio, rng), rng))
        labels.append(1)
    for index in range(count_neg):
        text = NEGATIVE_PHRASES[index % len(NEGATIVE_PHRASES)]
        audio = synthesize(voice, text, rng.uniform(0.85, 1.15), rng)
        clips.append(augment(place_phrase(audio, rng), rng))
        labels.append(0)
    # A few pure noise/silence negatives help reject non-speech sounds.
    noise_rng = np.random.default_rng(seed + 1)
    for _ in range(max(0, count_neg // 10)):
        clips.append(
            np.clip(noise_rng.normal(0, 0.01, CLIP_SAMPLES), -1, 1).astype(np.float32)
        )
        labels.append(0)
    return np.stack(clips).astype(np.int16), np.asarray(labels, dtype=np.float32)


def extract_features(clips: np.ndarray) -> np.ndarray:
    from openwakeword.utils import AudioFeatures

    features = AudioFeatures(device="cpu", ncpu=1).embed_clips(clips, batch_size=16)
    return features.reshape(len(clips), -1).astype(np.float32)


def train(
    features: np.ndarray,
    labels: np.ndarray,
    *,
    epochs: int,
    seed: int,
) -> tuple[SmallWakeNet, dict[str, float]]:
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(features))
    split = max(1, int(len(order) * 0.2))
    test_idx, train_idx = order[:split], order[split:]
    x_train = torch.from_numpy(features[train_idx])
    y_train = torch.from_numpy(labels[train_idx])[:, None]
    x_test = torch.from_numpy(features[test_idx])
    y_test = labels[test_idx]

    model = SmallWakeNet()
    positives = float(labels.sum())
    negatives = float(len(labels) - positives)
    pos_weight = torch.tensor([negatives / max(1.0, positives)])
    loss_fn = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
    for _ in range(epochs):
        model.train()
        optimizer.zero_grad()
        logits = model(x_train)
        loss = loss_fn(logits, y_train)
        loss.backward()
        optimizer.step()
    model.eval()
    with torch.no_grad():
        scores = torch.sigmoid(model(x_test)).numpy().ravel()
    # Pick a threshold that keeps false positives low, then fold it into the
    # final bias so openWakeWord's fixed 0.5 threshold has the same effect.
    best_threshold = 0.5
    best_score = -1.0
    for candidate in (0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.98, 0.99):
        predicted = scores >= candidate
        tp = int(((predicted == 1) & (y_test == 1)).sum())
        fn = int(((predicted == 0) & (y_test == 1)).sum())
        fp = int(((predicted == 1) & (y_test == 0)).sum())
        tn = int(((predicted == 0) & (y_test == 0)).sum())
        recall = tp / max(1, tp + fn)
        fpr = fp / max(1, fp + tn)
        score = recall - 2.0 * fpr
        if fpr <= 0.03 and score > best_score:
            best_score = score
            best_threshold = candidate
    with torch.no_grad():
        model.net[-1].bias -= math.log(best_threshold / (1.0 - best_threshold))
        scores = torch.sigmoid(model(x_test)).numpy().ravel()
    predicted = scores >= 0.5
    tp = int(((predicted == 1) & (y_test == 1)).sum())
    fn = int(((predicted == 0) & (y_test == 1)).sum())
    fp = int(((predicted == 1) & (y_test == 0)).sum())
    tn = int(((predicted == 0) & (y_test == 0)).sum())
    recall = tp / max(1, tp + fn)
    false_positive_rate = fp / max(1, fp + tn)
    return model, {
        "recall": round(recall, 4),
        "false_positive_rate": round(false_positive_rate, 4),
        "tp": tp,
        "fn": fn,
        "fp": fp,
        "tn": tn,
        "threshold": 0.5,
        "calibration": best_threshold,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Train a local Kivi wake model")
    parser.add_argument("--positives", type=int, default=80)
    parser.add_argument("--negatives", type=int, default=160)
    parser.add_argument("--epochs", type=int, default=80)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--positive-dir", type=Path, default=None)
    parser.add_argument("--negative-dir", type=Path, default=None)
    args = parser.parse_args()
    if (args.positive_dir is None) != (args.negative_dir is None):
        raise SystemExit("pass both --positive-dir and --negative-dir")
    if args.positive_dir is None and not VOICE.is_file():
        raise SystemExit(f"Piper voice not found: {VOICE}")

    clips, labels = build_dataset(
        args.positives,
        args.negatives,
        args.seed,
        args.positive_dir,
        args.negative_dir,
    )
    print(f"clips: {len(clips)} (positive={int(labels.sum())})")
    features = extract_features(clips)
    model, metrics = train(features, labels, epochs=args.epochs, seed=args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    dummy = torch.zeros(1, 16, 96)
    torch.onnx.export(
        WakeOutput(model),
        dummy,
        args.out,
        input_names=["input"],
        output_names=["kivi"],
        dynamic_axes={"input": {0: "batch"}, "kivi": {0: "batch"}},
        opset_version=17,
        dynamo=False,
    )
    metrics.update(
        {
            "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "positives": args.positives,
            "negatives": args.negatives,
            "epochs": args.epochs,
            "voice": VOICE.name,
            "model": str(args.out),
        }
    )
    METRICS.parent.mkdir(parents=True, exist_ok=True)
    METRICS.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
