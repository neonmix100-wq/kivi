"""Gateway switching benchmark.

Measures the acceptance criterion "20 switches without OOM": acquire a model,
record load time and free VRAM, release it, repeat round-robin. Fake mode
validates the harness; real mode uses Ollama and ``nvidia-smi``.

Examples:
    python scripts/bench_gateway.py --fake --switches 20
    python scripts/bench_gateway.py --switches 20 --models qwen3.5:4b,llama3:8b
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kivi.models.backends.base import BackendError
from kivi.models.backends.ollama import OllamaBackend
from kivi.models.gateway import GatewayError, ModelGateway
from kivi.models.registry import ModelRegistry, load_registry
from kivi.models.vram import VramSnapshot, query_vram

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "config" / "models.yaml"
DEFAULT_OUTPUT = ROOT / "data" / "bench"
DEFAULT_ROLES = ("router", "chat", "code", "embedding")


class FakeBenchBackend:
    name = "fake"

    def __init__(self) -> None:
        self.loaded: dict[str, int] = {}

    def list_models(self) -> list[str]:
        return sorted(self.loaded)

    def running(self) -> dict[str, int]:
        return dict(self.loaded)

    def load(self, model_id: str, keep_alive: str = "5m") -> None:
        spec = FAKE_REGISTRY.require(model_id)
        self.loaded[model_id] = spec.vram_mb

    def unload(self, model_id: str) -> None:
        self.loaded.pop(model_id, None)

    def chat(
        self,
        model_id: str,
        messages: list[dict[str, str]],
        *,
        keep_alive: str = "5m",
        options: dict[str, Any] | None = None,
    ) -> str:
        return "fake"


FAKE_REGISTRY: ModelRegistry


def _fake_vram() -> VramSnapshot:
    used = sum(FAKE_REGISTRY.require(model_id).vram_mb for model_id in FAKE_BACKEND.loaded)
    total = 8192
    return VramSnapshot(total_mb=total, used_mb=used, free_mb=max(0, total - used))


FAKE_BACKEND = FakeBenchBackend()


@dataclass(slots=True)
class SwitchResult:
    index: int
    model_id: str
    ok: bool
    load_ms: int
    vram: dict[str, int] | None
    vram_after: dict[str, int] | None
    error: str | None


def _models_from_roles(registry: ModelRegistry) -> list[str]:
    models: list[str] = []
    for role in DEFAULT_ROLES:
        try:
            spec = registry.default_for(role)
        except Exception:
            continue
        if spec.id not in models:
            models.append(spec.id)
    return models


def run_switches(
    gateway: ModelGateway,
    models: list[str],
    switches: int,
    *,
    vram_source=query_vram,
    keep_alive: str = "0",
) -> dict[str, Any]:
    if switches <= 0:
        raise ValueError("switches must be positive")
    if not models:
        raise ValueError("at least one model is required")

    runs: list[SwitchResult] = []
    started = time.time()
    for index in range(switches):
        model_id = models[index % len(models)]
        before = gateway.metrics()["loads"]
        try:
            gateway.acquire(model_id, task_id=f"bench_{index}", ttl_s=60)
            metrics = gateway.metrics()
            load_ms = int(metrics["last_load_ms"]) if metrics["loads"] > before else 0
            snapshot = vram_source()
            runs.append(
                SwitchResult(
                    index=index + 1,
                    model_id=model_id,
                    ok=True,
                    load_ms=load_ms,
                    vram=snapshot.as_dict() if snapshot else None,
                    vram_after=None,
                    error=None,
                )
            )
            gateway.release(model_id)
            after = vram_source()
            runs[-1].vram_after = after.as_dict() if after else None
        except (GatewayError, BackendError) as exc:
            runs.append(
                SwitchResult(
                    index=index + 1,
                    model_id=model_id,
                    ok=False,
                    load_ms=0,
                    vram=None,
                    vram_after=None,
                    error=str(exc),
                )
            )

    metrics = gateway.metrics()
    free_values = [run.vram["free_mb"] for run in runs if run.vram]
    free_after = [run.vram_after["free_mb"] for run in runs if run.vram_after]
    return {
        "mode": "benchmark",
        "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
        "duration_s": round(time.time() - started, 1),
        "switches": switches,
        "models": models,
        "keep_alive": keep_alive,
        "runs": [
            {
                "index": run.index,
                "model": run.model_id,
                "ok": run.ok,
                "load_ms": run.load_ms,
                "vram": run.vram,
                "vram_after": run.vram_after,
                "error": run.error,
            }
            for run in runs
        ],
        "summary": {
            "ok": sum(1 for run in runs if run.ok),
            "failed": sum(1 for run in runs if not run.ok),
            "loads": metrics["loads"],
            "unloads": metrics["unloads"],
            "evictions": metrics["evictions"],
            "failures": metrics["failures"],
            "min_free_mb": min(free_values) if free_values else None,
            "min_free_after_mb": min(free_after) if free_after else None,
            "max_load_ms": max((run.load_ms for run in runs), default=0),
        },
    }


def _write_report(report: dict[str, Any], directory: Path = DEFAULT_OUTPUT) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"gateway-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _print_summary(report: dict[str, Any], path: Path | None) -> None:
    print(f"mode={report['mode']} switches={report['switches']} models={len(report['models'])}")
    for run in report["runs"]:
        status = "ok" if run["ok"] else "FAIL"
        free = run["vram"]["free_mb"] if run["vram"] else "-"
        print(
            f"{run['index']:>2}. {run['model']:<40} {status:<4} "
            f"load={run['load_ms']:>6} ms free={free} MB"
            + (f"  {run['error']}" if run["error"] else "")
        )
    summary = report["summary"]
    print(
        f"summary: ok={summary['ok']} failed={summary['failed']} "
        f"loads={summary['loads']} unloads={summary['unloads']} "
        f"evictions={summary['evictions']} "
        f"min_free={summary['min_free_mb']} MB "
        f"min_free_after={summary['min_free_after_mb']} MB"
    )
    if path is not None:
        print(f"report: {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark Model Gateway model switching.")
    parser.add_argument("--fake", action="store_true", help="use an in-process fake backend")
    parser.add_argument("--switches", type=int, default=20, help="number of switches")
    parser.add_argument(
        "--models",
        default="",
        help=(
            "comma-separated model ids (default: registry defaults for router/chat/code/embedding)"
        ),
    )
    parser.add_argument("--timeout", type=float, default=300.0, help="Ollama timeout, seconds")
    parser.add_argument("--no-write", action="store_true", help="do not write a JSON report")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="report directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.switches <= 0:
        print("--switches must be positive")
        return 2
    registry = load_registry(DEFAULT_REGISTRY)

    global FAKE_REGISTRY, FAKE_BACKEND
    if args.fake:
        FAKE_REGISTRY = registry
        FAKE_BACKEND = FakeBenchBackend()
        backend: Any = FAKE_BACKEND
        vram_source = _fake_vram
    else:
        backend = OllamaBackend(timeout=args.timeout)
        vram_source = query_vram

    if args.models:
        models = [item.strip() for item in args.models.split(",") if item.strip()]
    else:
        models = _models_from_roles(registry)

    if not args.fake:
        installed = set(backend.list_models())
        missing = [model_id for model_id in models if model_id not in installed]
        if missing:
            print("skipping models not installed in Ollama: " + ", ".join(missing))
        models = [model_id for model_id in models if model_id in installed]
        if not models:
            print("no requested models are installed; nothing to benchmark")
            return 2

    for model_id in models:
        try:
            registry.require(model_id)
        except Exception as exc:
            print(f"unknown model {model_id}: {exc}")
            return 2

    gateway = ModelGateway(
        registry,
        backend,
        vram_total_mb=8192,
        safety_margin_mb=512,
        vram_source=vram_source,
    )
    try:
        report = run_switches(
            gateway,
            models,
            args.switches,
            vram_source=vram_source,
            keep_alive="0",
        )
    finally:
        gateway.close()
        if not args.fake:
            backend.close()

    path = None if args.no_write else _write_report(report, args.output)
    _print_summary(report, path)
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
