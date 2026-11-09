"""Generate Silero v5_ru voice samples for Kivi.

The v5 model is downloaded on first use and supports 8, 24 and 48 kHz output.
This script writes a native 48 kHz sample, a 24 kHz sample, and a lightly
post-processed 48 kHz version (DC removal, 70 Hz high-pass, peak normalisation,
edge silence trim).

Run from the repository root:
    .venv\\Scripts\\python.exe scripts\\generate_silero_v5.py
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEXT = ROOT / "assets" / "voice-samples" / "sample_text_ru.txt"
DEFAULT_OUT = ROOT / "assets" / "voice-samples"


def trim_silence(audio: np.ndarray, threshold_db: float = -50.0, pad: int = 1200) -> np.ndarray:
    """Trim leading/trailing silence, keeping a short natural pad."""
    if audio.size == 0:
        return audio
    peak = float(np.max(np.abs(audio)))
    if peak <= 0:
        return audio
    threshold = peak * (10 ** (threshold_db / 20.0))
    loud = np.flatnonzero(np.abs(audio) > threshold)
    if loud.size == 0:
        return audio
    start = max(0, int(loud[0]) - pad)
    end = min(audio.size, int(loud[-1]) + pad)
    return audio[start:end]


def high_pass(audio: np.ndarray, sample_rate: int, cutoff_hz: float = 70.0, order: int = 2) -> np.ndarray:
    """Remove DC and low-frequency rumble with a Butterworth high-pass."""
    from scipy.signal import butter, sosfilt

    sos = butter(order, cutoff_hz, btype="highpass", fs=sample_rate, output="sos")
    return sosfilt(sos, audio).astype(np.float32)


def normalize(audio: np.ndarray, peak_db: float = -1.0) -> np.ndarray:
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak <= 0:
        return audio
    target = 10 ** (peak_db / 20.0)
    return (audio * (target / peak)).astype(np.float32)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speaker", default="baya", help="Silero v5_ru speaker (default: baya)")
    parser.add_argument("--text-file", type=Path, default=DEFAULT_TEXT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    torch.set_num_threads(max(1, args.threads))
    text = args.text_file.read_text(encoding="utf-8").strip()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    print("loading Silero v5_ru ...", flush=True)
    from silero import silero_tts

    model, _ = silero_tts(language="ru", speaker="v5_ru")
    print("speakers:", model.speakers, flush=True)

    for rate in (48000, 24000):
        started = time.perf_counter()
        audio = model.apply_tts(
            text=text,
            speaker=args.speaker,
            sample_rate=rate,
            put_accent=True,
            put_yo=True,
        )
        audio = audio.detach().cpu().numpy().astype(np.float32)
        out = args.out_dir / f"silero_v5_{args.speaker}_{rate // 1000}k.wav"
        sf.write(out, audio, rate, subtype="PCM_16")
        print(f"wrote {out.relative_to(ROOT)} ({audio.size / rate:.2f}s, {time.perf_counter() - started:.1f}s)", flush=True)

        if rate == 48000:
            clean = normalize(high_pass(trim_silence(audio), rate))
            out_clean = args.out_dir / f"silero_v5_{args.speaker}_48k_clean.wav"
            sf.write(out_clean, clean, rate, subtype="PCM_16")
            print(f"wrote {out_clean.relative_to(ROOT)} (cleaned)", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
