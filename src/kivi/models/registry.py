"""Model registry: what can be loaded, how much it costs and who may use it.

Footprints are estimates until the benchmark on stage 2 replaces them with
measured values. The registry is data, not policy: the gateway decides what to
load, the policy engine decides what a task may do.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_TIER0 = "qwen3.5:4b"
DEFAULT_CHAT = "hf.co/Qwen/Qwen3-8B-GGUF:Q4_K_M"
DEFAULT_CODER = "qwen2.5-coder:7b"


class RegistryError(ValueError):
    """Raised for an unreadable registry or an unknown model id."""


def canonical_model_id(model_id: str) -> str:
    """Ollama reports the implicit tag as ``:latest``; compare canonically."""
    return model_id if ":" in model_id else f"{model_id}:latest"


@dataclass(frozen=True, slots=True)
class ModelSpec:
    id: str
    backend: str = "ollama"
    tier: int = 0
    role: str = "chat"
    vram_mb: int = 0
    ram_mb: int = 0
    context: int = 4096
    exclusive: bool = False
    pinned: bool = False
    think: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise RegistryError("model id must not be empty")
        if self.tier not in (0, 1, 2, 3, 4):
            raise RegistryError(f"{self.id}: tier must be 0..4")
        if self.vram_mb < 0 or self.ram_mb < 0:
            raise RegistryError(f"{self.id}: memory sizes must be non-negative")


DEFAULT_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        id=DEFAULT_TIER0,
        tier=0,
        role="router",
        vram_mb=3800,
        ram_mb=1200,
        context=4096,
        notes="fast path, may stay resident",
    ),
    ModelSpec(
        id=DEFAULT_CHAT,
        tier=1,
        role="chat",
        vram_mb=5800,
        ram_mb=1500,
        context=8192,
        exclusive=True,
    ),
    ModelSpec(
        id=DEFAULT_CODER,
        tier=2,
        role="code",
        vram_mb=4800,
        ram_mb=1400,
        context=8192,
        exclusive=True,
    ),
    ModelSpec(
        id="qwen2.5vl:3b",
        tier=2,
        role="vision",
        vram_mb=3600,
        ram_mb=1200,
        context=4096,
        exclusive=True,
    ),
    ModelSpec(
        id="faster-whisper-large-v3-turbo-int8",
        backend="faster-whisper",
        tier=3,
        role="asr",
        vram_mb=2000,
        ram_mb=800,
        context=0,
        notes="coexists with tier 0 if budget allows",
    ),
    ModelSpec(
        id="bge-m3",
        backend="cpu",
        tier=3,
        role="embedding",
        vram_mb=0,
        ram_mb=500,
        context=8192,
    ),
    ModelSpec(
        id="sdxl",
        backend="comfyui",
        tier=4,
        role="image",
        vram_mb=6800,
        ram_mb=2500,
        exclusive=True,
    ),
)


class ModelRegistry:
    def __init__(self, models: tuple[ModelSpec, ...] | list[ModelSpec] = DEFAULT_MODELS) -> None:
        self._models: dict[str, ModelSpec] = {}
        for spec in models:
            if spec.id in self._models:
                raise RegistryError(f"duplicate model id: {spec.id}")
            self._models[spec.id] = spec

    def __len__(self) -> int:
        return len(self._models)

    def __contains__(self, model_id: object) -> bool:
        return model_id in self._models

    def get(self, model_id: str) -> ModelSpec | None:
        return self._models.get(model_id)

    def require(self, model_id: str) -> ModelSpec:
        spec = self.get(model_id)
        if spec is None:
            raise RegistryError(f"unknown model: {model_id}")
        return spec

    def all(self) -> tuple[ModelSpec, ...]:
        return tuple(self._models.values())

    def by_tier(self, tier: int) -> tuple[ModelSpec, ...]:
        return tuple(spec for spec in self._models.values() if spec.tier == tier)

    def by_role(self, role: str) -> tuple[ModelSpec, ...]:
        return tuple(spec for spec in self._models.values() if spec.role == role)

    def default_for(self, role: str) -> ModelSpec:
        candidates = self.by_role(role)
        if not candidates:
            raise RegistryError(f"no model registered for role: {role}")
        return min(candidates, key=lambda spec: (spec.tier, spec.vram_mb))

    def with_footprint(self, model_id: str, vram_mb: int) -> ModelRegistry:
        """Return a copy with a measured footprint for ``model_id``."""
        updated = []
        for spec in self._models.values():
            if spec.id == model_id:
                updated.append(
                    ModelSpec(
                        id=spec.id,
                        backend=spec.backend,
                        tier=spec.tier,
                        role=spec.role,
                        vram_mb=max(0, vram_mb),
                        ram_mb=spec.ram_mb,
                        context=spec.context,
                        exclusive=spec.exclusive,
                        pinned=spec.pinned,
                        notes=spec.notes,
                    )
                )
            else:
                updated.append(spec)
        return ModelRegistry(updated)


def _spec_from_mapping(raw: dict[str, Any]) -> ModelSpec:
    if not isinstance(raw, dict):
        raise RegistryError("each model entry must be a mapping")
    try:
        return ModelSpec(**raw)
    except TypeError as exc:
        raise RegistryError(f"invalid model entry: {exc}") from exc


def load_registry(path: Path) -> ModelRegistry:
    """Load ``config/models.yaml``; a missing file falls back to defaults."""
    if not path.is_file():
        return ModelRegistry()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RegistryError(f"invalid YAML in {path}: {exc}") from exc

    if raw is None:
        return ModelRegistry()
    if not isinstance(raw, dict) or not isinstance(raw.get("models"), list):
        raise RegistryError(f"{path}: expected a mapping with a 'models' list")
    return ModelRegistry([_spec_from_mapping(item) for item in raw["models"]])
