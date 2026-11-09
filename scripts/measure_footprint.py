"""Measure the real VRAM footprint of a model at a fixed context.

Registry footprints start as estimates. Run this with the desktop in the state
you normally use, then put the reported delta into `config/models.yaml`
(`vram_mb`). The delta is measured with the model loaded and unloaded again.
"""

from __future__ import annotations

import argparse
from typing import Any

from kivi.models.backends.ollama import OllamaBackend
from kivi.models.vram import query_vram


def measure(
    model_id: str,
    *,
    context: int,
    backend: OllamaBackend,
    vram_source: Any = query_vram,
) -> dict[str, int | None]:
    before = vram_source()
    backend.load(model_id, options={"num_ctx": context})
    loaded = vram_source()
    backend.unload(model_id)
    after = vram_source()
    if before is None or loaded is None:
        return {"before": None, "loaded": None, "after": None, "delta_mb": None}
    return {
        "before": before.free_mb,
        "loaded": loaded.free_mb,
        "after": after.free_mb if after else None,
        "delta_mb": max(0, before.free_mb - loaded.free_mb),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", help="Ollama model id")
    parser.add_argument("--context", type=int, default=4096)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(argv)

    with OllamaBackend(timeout=args.timeout) as backend:
        result = measure(args.model, context=args.context, backend=backend)
    print(f"model: {args.model}")
    print(f"context: {args.context}")
    print(f"free before: {result['before']} MB")
    print(f"free loaded: {result['loaded']} MB")
    print(f"free after:  {result['after']} MB")
    print(f"measured footprint: {result['delta_mb']} MB")
    return 0 if result["delta_mb"] is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
