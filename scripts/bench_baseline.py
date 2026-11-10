"""Measure the stage-0 baseline: load time, tokens/s and peak VRAM per model.

The script talks to the local Ollama HTTP API, polls NVML/nvidia-smi through
``kivi.models.vram`` and unloads every model after measuring it. Raw results are
written to ``data/bench/baseline-*.json`` (ignored by git); the Markdown table is
printed for ``docs/BASELINE.md``.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from kivi.models.backends.ollama import OllamaBackend
from kivi.models.vram import query_vram

DEFAULT_MODELS = (
    ("qwen3.5:4b", 4096),
    ("hf.co/Qwen/Qwen3-8B-GGUF:Q4_K_M", 8192),
    ("qwen2.5-coder:7b", 8192),
    ("deepseek-coder:6.7b", 8192),
    ("llama3:8b", 4096),
)
PROMPT = "Ответь одним предложением: что такое локальный ИИ-ассистент?"
MAX_PREDICT = 128


class VramPoller:
    def __init__(self, interval_s: float = 0.2) -> None:
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.peak_used_mb = 0
        self.min_free_mb = 10**9

    def _run(self) -> None:
        while not self._stop.is_set():
            snapshot = query_vram()
            if snapshot is not None:
                self.peak_used_mb = max(self.peak_used_mb, snapshot.used_mb)
                self.min_free_mb = min(self.min_free_mb, snapshot.free_mb)
            time.sleep(self.interval_s)

    def __enter__(self) -> VramPoller:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


def _unload_all(backend: OllamaBackend) -> None:
    for model_id in backend.running():
        try:
            backend.unload(model_id)
        except Exception:  # noqa: BLE001 - best effort before a measurement
            pass


def measure_model(
    client: httpx.Client,
    backend: OllamaBackend,
    model_id: str,
    context: int,
    *,
    keep_alive: str = "2m",
) -> dict[str, Any]:
    _unload_all(backend)
    for _ in range(20):
        if query_vram() is not None:
            break
        time.sleep(0.2)
    before = query_vram()
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"num_ctx": context, "num_predict": MAX_PREDICT, "temperature": 0},
    }
    started = time.perf_counter()
    with VramPoller() as poller:
        response = client.post("/api/chat", json=body)
        response.raise_for_status()
        payload = response.json()
    elapsed = time.perf_counter() - started
    backend.unload(model_id)
    after = query_vram()
    eval_count = int(payload.get("eval_count") or 0)
    eval_duration = int(payload.get("eval_duration") or 0)
    load_duration = int(payload.get("load_duration") or 0)
    tokens_per_s = eval_count / (eval_duration / 1e9) if eval_duration else 0.0
    return {
        "model": model_id,
        "context": context,
        "load_s": round(load_duration / 1e9, 3),
        "wall_s": round(elapsed, 3),
        "eval_tokens": eval_count,
        "tokens_per_s": round(tokens_per_s, 1),
        "peak_used_mb": poller.peak_used_mb,
        "free_before_mb": before.free_mb if before else None,
        "free_after_mb": after.free_mb if after else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:11434",
        help="Ollama base URL",
    )
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--keep-alive", default="2m")
    parser.add_argument(
        "--models",
        default="",
        help="comma-separated model:context pairs; empty uses the built-in list",
    )
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    if args.models:
        pairs: list[tuple[str, int]] = []
        for item in args.models.split(","):
            name, separator, maybe_context = item.strip().rpartition(":")
            if separator and maybe_context.isdigit():
                pairs.append((name, int(maybe_context)))
            else:
                pairs.append((item.strip(), 4096))
    else:
        pairs = list(DEFAULT_MODELS)

    results: list[dict[str, Any]] = []
    with httpx.Client(base_url=args.base_url, timeout=args.timeout) as client:
        with OllamaBackend(base_url=args.base_url, timeout=args.timeout) as backend:
            for model_id, context in pairs:
                print(f"measuring {model_id} @ {context} ...", flush=True)
                try:
                    results.append(
                        measure_model(
                            client,
                            backend,
                            model_id,
                            context,
                            keep_alive=args.keep_alive,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - keep measuring other models
                    results.append({"model": model_id, "context": context, "error": str(exc)})
                    print(f"  failed: {exc}")

    print("\n| Модель | Контекст | Загрузка, с | Tokens/s | Peak VRAM, МБ |")
    print("|---|---:|---:|---:|---:|")
    for row in results:
        if "error" in row:
            print(f"| `{row['model']}` | {row['context']} | ошибка | — | — |")
        else:
            print(
                f"| `{row['model']}` | {row['context']} | {row['load_s']} | "
                f"{row['tokens_per_s']} | {row['peak_used_mb']} |"
            )

    if not args.no_write:
        out_dir = Path("data") / "bench"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        target = out_dir / f"baseline-{stamp}.json"
        target.write_text(
            json.dumps(
                {
                    "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                    "prompt": PROMPT,
                    "max_predict": MAX_PREDICT,
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nraw results: {target}")
    return 0 if any("error" not in row for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
