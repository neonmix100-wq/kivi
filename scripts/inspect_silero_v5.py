"""Inspect the Silero v5_ru model: speakers, sample rates, API.

Run from the repository root:
    .venv\\Scripts\\python.exe scripts\\inspect_silero_v5.py
"""

import sys

import torch


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    torch.set_num_threads(4)

    from silero import silero_tts

    model, example_text = silero_tts(language="ru", speaker="v5_ru")
    print("example_text:", example_text)
    print("model type:", type(model))
    for attr in ("speakers", "sample_rate", "supported_sample_rates", "models"):
        if hasattr(model, attr):
            print(f"{attr}:", getattr(model, attr))
    print("dir:", [name for name in dir(model) if not name.startswith("__")])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
