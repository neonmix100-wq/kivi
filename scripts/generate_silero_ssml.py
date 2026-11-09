"""Generate a Silero v5 SSML sample with natural pauses and gentle prosody.

Run from the repository root:
    .venv\\Scripts\\python.exe scripts\\generate_silero_ssml.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

from generate_silero_v5 import high_pass, normalize, trim_silence


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "voice-samples" / "silero_v5_baya_48k_ssml.wav"

SSML = """<speak>
<s>Привет.</s>
<s>Я Киви — ваш локальный ассистент.</s>
<s>Помогу с кодом, документами и управлением компьютером.</s>
<s><prosody rate="95%" pitch="-2%">Говорю кратко,</prosody><break strength="medium"/> но могу объяснить подробнее, если попросите.</s>
</speak>"""


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    torch.set_num_threads(4)

    from silero import silero_tts

    model, _ = silero_tts(language="ru", speaker="v5_ru")
    audio = model.apply_tts(ssml_text=SSML, speaker="baya", sample_rate=48000)
    audio = audio.detach().cpu().numpy().astype(np.float32)

    sf.write(OUT, audio, 48000, subtype="PCM_16")
    clean = normalize(high_pass(trim_silence(audio), 48000))
    sf.write(OUT.with_name(OUT.stem + "_clean.wav"), clean, 48000, subtype="PCM_16")
    print(f"wrote {OUT.relative_to(ROOT)} ({audio.size / 48000:.2f}s)")
    print(f"wrote {OUT.with_name(OUT.stem + '_clean.wav').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
