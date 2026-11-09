from __future__ import annotations

from pathlib import Path

import pytest

from kivi.models.registry import (
    DEFAULT_MODELS,
    ModelRegistry,
    ModelSpec,
    RegistryError,
    canonical_model_id,
    load_registry,
)
from kivi.paths import project_root


def test_default_registry_has_router_and_chat() -> None:
    registry = ModelRegistry()
    assert len(registry) == len(DEFAULT_MODELS)
    assert registry.default_for("router").id == "qwen3.5:4b"
    assert registry.default_for("chat").tier == 1
    assert "qwen3.5:4b" in registry


def test_duplicate_ids_are_rejected() -> None:
    with pytest.raises(RegistryError, match="duplicate"):
        ModelRegistry([ModelSpec(id="a"), ModelSpec(id="a")])


def test_canonical_model_id_adds_latest() -> None:
    assert canonical_model_id("bge-m3") == "bge-m3:latest"
    assert canonical_model_id("qwen3.5:4b") == "qwen3.5:4b"


def test_unknown_model_raises() -> None:
    registry = ModelRegistry([ModelSpec(id="a", role="router")])
    assert registry.get("missing") is None
    with pytest.raises(RegistryError, match="unknown"):
        registry.require("missing")
    with pytest.raises(RegistryError, match="role"):
        registry.default_for("chat")


def test_spec_validation() -> None:
    with pytest.raises(RegistryError, match="empty"):
        ModelSpec(id=" ")
    with pytest.raises(RegistryError, match="tier"):
        ModelSpec(id="a", tier=9)
    with pytest.raises(RegistryError, match="non-negative"):
        ModelSpec(id="a", vram_mb=-1)


def test_with_footprint_replaces_only_one_model() -> None:
    registry = ModelRegistry([ModelSpec(id="a", vram_mb=100), ModelSpec(id="b", vram_mb=200)])
    updated = registry.with_footprint("a", 150)
    assert updated.require("a").vram_mb == 150
    assert updated.require("b").vram_mb == 200
    assert registry.require("a").vram_mb == 100


def test_by_tier_and_role() -> None:
    registry = ModelRegistry(
        [
            ModelSpec(id="a", tier=0, role="router"),
            ModelSpec(id="b", tier=1, role="chat"),
            ModelSpec(id="c", tier=1, role="chat"),
        ]
    )
    assert [spec.id for spec in registry.by_tier(1)] == ["b", "c"]
    assert [spec.id for spec in registry.by_role("chat")] == ["b", "c"]


def test_missing_file_falls_back_to_defaults(tmp_path: Path) -> None:
    assert len(load_registry(tmp_path / "nope.yaml")) == len(DEFAULT_MODELS)


def test_load_custom_registry(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(
        "models:\n  - id: custom:1b\n    tier: 0\n    role: router\n    vram_mb: 1000\n",
        encoding="utf-8",
    )
    registry = load_registry(path)
    assert registry.require("custom:1b").vram_mb == 1000


def test_invalid_registry_root_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="models"):
        load_registry(path)


def test_registry_yaml_has_no_duplicates_and_a_router() -> None:
    registry = load_registry(project_root() / "config" / "models.yaml")
    assert len(registry) >= 6
    assert registry.default_for("router").tier == 0
    assert registry.get("qwen2.5-coder:7b") is not None
