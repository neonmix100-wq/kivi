"""Generate Kivi concept art through the local SD WebUI API.

The cloud image tool is unavailable, so this is the local fallback:
SD 1.5 at modest resolution, several seeds. Treat the output as a draft.

Start WebUI first (from D:\\stable-diffusion-webui):
    venv\\Scripts\\python.exe launch.py --api --nowebui --lowvram --xformers --port 7861

Then run from the repository root:
    .venv\\Scripts\\python.exe scripts\\generate_concept_sd.py
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "assets" / "avatar" / "concept"

BASE_POSITIVE = (
    "masterpiece, best quality, highres, anime style, soft cel shading, 1girl, solo, "
    "adult, 18 years old, slender, long turquoise hair, wavy hair, turquoise eyes, "
    "detailed eyes, gentle smile, fair skin, no accessories, bare arms, "
    "light green camisole dress, mint one-piece dress, sleeveless dress, knee-length, "
    "white bolero, short-sleeved bolero, flat sandals, bare legs, "
    "neutral light gray studio background, soft diffused lighting, summer, character design"
)
NEGATIVE = (
    "lowres, worst quality, bad anatomy, bad hands, extra digits, fewer digits, deformed, "
    "mutated, text, watermark, signature, logo, frame, child, loli, nsfw, nude, cleavage, "
    "school uniform, serafuku, sailor collar, necktie, bow, ribbon, waist ribbon, belt, sash, "
    "brooch, hat, hair ribbon, headwear, vest, black skirt, pleated skirt, thigh strap, garter, "
    "gloves, arm warmers, detached sleeves, glasses, headphones, jewelry, choker, bag, "
    "hair ornament, chibi, 3d, photorealistic, multiple girls, cropped head, out of frame"
)

JOBS = [
    {
        "name": "kivi_anime_fullbody_v5",
        "width": 512,
        "height": 768,
        "seed": 18052301,
        "prompt": BASE_POSITIVE
        + ", full body, standing, front view, arms at sides, looking at viewer",
    },
    {
        "name": "kivi_anime_fullbody_v6",
        "width": 512,
        "height": 768,
        "seed": 18052302,
        "prompt": BASE_POSITIVE
        + ", full body, standing, three-quarter view, arms at sides, looking at viewer",
    },
    {
        "name": "kivi_anime_portrait_v5",
        "width": 512,
        "height": 512,
        "seed": 18052303,
        "prompt": BASE_POSITIVE
        + ", upper body, portrait, close-up face, looking at viewer, soft smile",
    },
    {
        "name": "kivi_anime_sheet_v3",
        "width": 768,
        "height": 512,
        "seed": 18052304,
        "prompt": BASE_POSITIVE
        + ", character reference sheet, full body turnaround, front view, side view, back view, "
        "same character, multiple views, clean layout",
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7861")
    parser.add_argument("--steps", type=int, default=28)
    parser.add_argument("--cfg", type=float, default=7.0)
    parser.add_argument("--sampler", default="Euler a")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()

    for job in JOBS:
        payload = {
            "prompt": job["prompt"],
            "negative_prompt": NEGATIVE,
            "width": job["width"],
            "height": job["height"],
            "steps": args.steps,
            "cfg_scale": args.cfg,
            "sampler_name": args.sampler,
            "seed": job["seed"],
            "batch_size": 1,
            "n_iter": 1,
            "restore_faces": False,
            "tiling": False,
        }
        started = time.perf_counter()
        response = session.post(f"{args.url}/sdapi/v1/txt2img", json=payload, timeout=600)
        response.raise_for_status()
        data = response.json()
        if not data.get("images"):
            raise RuntimeError(f"no image returned for {job['name']}")

        image_bytes = base64.b64decode(data["images"][0].split(",", 1)[-1])
        out = OUT_DIR / f"{job['name']}.png"
        out.write_bytes(image_bytes)
        meta = {
            "name": job["name"],
            "seed": job["seed"],
            "width": job["width"],
            "height": job["height"],
            "steps": args.steps,
            "cfg_scale": args.cfg,
            "sampler": args.sampler,
            "model": "Counterfeit-V2.5_fp16.safetensors",
            "prompt": job["prompt"],
            "negative_prompt": NEGATIVE,
            "note": "local SD 1.5 draft; cloud image tool unavailable",
            "seconds": round(time.perf_counter() - started, 1),
        }
        out.with_suffix(".json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"wrote {out.relative_to(ROOT)} ({meta['seconds']}s)", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
