from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import torch

from scripts.train_wake_model import (
    CLIP_SAMPLES,
    SmallWakeNet,
    WakeOutput,
    augment,
    build_dataset,
    load_recordings,
    place_phrase,
)


def write_wav(path: Path, samples: np.ndarray, rate: int = 16_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.astype("<i2").tobytes())


def test_load_recordings_augments_each_clip(tmp_path: Path) -> None:
    positive = tmp_path / "positive"
    write_wav(positive / "a.wav", np.zeros(8_000, dtype=np.int16))
    write_wav(positive / "b.wav", np.ones(9_000, dtype=np.int16))
    import random

    clips, labels = load_recordings(positive, 1, random.Random(3))
    assert len(clips) == 6
    assert labels == [1] * 6
    assert all(clip.shape == (CLIP_SAMPLES,) for clip in clips)
    assert all(clip.dtype == np.float32 for clip in clips)


def test_load_recordings_rejects_empty_directory(tmp_path: Path) -> None:
    import random

    clips, labels = load_recordings(tmp_path / "empty", 0, random.Random(1))
    assert clips == []
    assert labels == []


def test_build_dataset_from_directories(tmp_path: Path) -> None:
    positive = tmp_path / "positive"
    negative = tmp_path / "negative"
    write_wav(positive / "p.wav", np.zeros(16_000, dtype=np.int16))
    write_wav(negative / "n.wav", np.zeros(16_000, dtype=np.int16))
    clips, labels = build_dataset(0, 0, 1, positive, negative)
    assert clips.shape == (6, CLIP_SAMPLES)
    assert clips.dtype == np.int16
    assert sorted(labels.tolist()) == [0, 0, 0, 1, 1, 1]


def test_augment_and_place_phrase_are_bounded() -> None:
    import random

    rng = random.Random(7)
    raw = np.ones(10_000, dtype=np.float32) * 0.5
    placed = place_phrase(raw, rng)
    assert placed.shape == (CLIP_SAMPLES,)
    assert np.max(np.abs(placed)) <= 1.0
    augmented = augment(placed, rng)
    assert augmented.shape == (CLIP_SAMPLES,)
    assert np.isfinite(augmented).all()
    assert np.max(np.abs(augmented)) <= 1.0


def test_small_wake_net_accepts_feature_windows() -> None:
    model = SmallWakeNet()
    output = WakeOutput(model)(torch.zeros(2, 16, 96))
    assert output.shape == (2, 1)
    assert torch.all((output > 0) & (output < 1))
