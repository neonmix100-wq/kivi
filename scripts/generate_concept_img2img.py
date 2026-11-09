"""Img2img refinement for a Kivi concept draft (local SD WebUI).

Example:
    D:\\stable-diffusion-webui\\venv\\Scripts\\python.exe scripts\\generate_concept_img2img.py ^
        --init assets/avatar/concept/kivi_anime_fullbody_v6.png ^
        --out assets/avatar/concept/kivi_anime_fullbody_v7_cardigan.png ^
        --denoise 0.42
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

PROMPT = (
    "masterpiece, best quality, highres, anime style, soft cel shading, 1girl, solo, "
    "adult, 18 years old, slender, long turquoise hair, wavy hair, turquoise eyes, "
    "gentle smile, fair skin, no accessories, bare arms, "
    "light green camisole dress, mint one-piece dress, sleeveless dress, knee-length, "
    "white cropped cardigan, open cardigan, short cardigan, flat sandals, bare legs, "
    "full body, standing, front view, arms at sides, looking at viewer, "
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7861")
    parser.add_argument("--init", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--denoise", type=float, default=0.42)
    parser.add_argument("--steps", type=int, default=32)
    parser.add_argument("--cfg", type=float, default=7.5)
    parser.add_argument("--seed", type=int, default=18052401)
    parser.add_argument("--prompt", default=PROMPT)
    parser.add_argument("--negative", default=NEGATIVE)
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    init_path = args.init if args.init.is_absolute() else ROOT / args.init
    out_path = args.out if args.out.is_absolute() else ROOT / args.out
    image_b64 = base64.b64encode(init_path.read_bytes()).decode("ascii")

    payload = {
        "init_images": [image_b64],
        "prompt": args.prompt,
        "negative_prompt": args.negative,
        "denoising_strength": args.denoise,
        "steps": args.steps,
        "cfg_scale": args.cfg,
        "sampler_name": "Euler a",
        "seed": args.seed,
        "batch_size": 1,
        "n_iter": 1,
        "resize_mode": 0,
        "restore_faces": False,
    }
    started = time.perf_counter()
    response = requests.post(f"{args.url}/sdapi/v1/img2img", json=payload, timeout=600)
    response.raise_for_status()
    data = response.json()
    if not data.get("images"):
        raise RuntimeError("no image returned")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(data["images"][0].split(",", 1)[-1]))
    meta = {
        "init": str(init_path.relative_to(ROOT)),
        "out": str(out_path.relative_to(ROOT)),
        "denoise": args.denoise,
        "steps": args.steps,
        "cfg_scale": args.cfg,
        "seed": args.seed,
        "model": "Counterfeit-V2.5_fp16.safetensors",
        "prompt": args.prompt,
        "negative_prompt": args.negative,
        "seconds": round(time.perf_counter() - started, 1),
    }
    out_path.with_suffix(".json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"wrote {out_path.relative_to(ROOT)} ({meta['seconds']}s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
