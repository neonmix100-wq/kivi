from __future__ import annotations

from pathlib import Path

import pytest

from kivi.cloud.providers import (
    CloudProvider,
    CloudRegistry,
    CloudRegistryError,
    load_cloud_registry,
)
from kivi.paths import project_root


def test_repository_cloud_registry_is_valid() -> None:
    registry = load_cloud_registry(project_root() / "config" / "cloud.yaml")
    assert registry.require("openrouter").endpoint.endswith("/chat/completions")
    assert registry.require("groq").allows_private_data is False


def test_unknown_provider_is_rejected() -> None:
    registry = CloudRegistry({"groq": make_provider()})
    with pytest.raises(CloudRegistryError, match="unknown"):
        registry.require("missing")


def test_https_is_required() -> None:
    with pytest.raises(CloudRegistryError, match="https"):
        CloudProvider(
            id="bad",
            kind="openai",
            base_url="http://insecure.test/v1",
            api_key_env="BAD_KEY",
        )


def test_duplicate_and_unknown_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "cloud.yaml"
    path.write_text(
        "providers:\n"
        "  - id: a\n"
        "    kind: openai\n"
        "    base_url: https://a.test/v1\n"
        "    api_key_env: A_KEY\n"
        "    extra: nope\n",
        encoding="utf-8",
    )
    with pytest.raises(CloudRegistryError, match="unknown provider keys"):
        load_cloud_registry(path)


def make_provider() -> CloudProvider:
    return CloudProvider(
        id="groq",
        kind="openai",
        base_url="https://example.test/v1",
        api_key_env="TEST_KEY",
        free_requests_per_day=10,
    )
