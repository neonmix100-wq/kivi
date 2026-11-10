"""Cloud providers: metadata only, never secrets.

A provider entry names the environment variable that holds the key. The key
itself is read at call time and never stored in the repository or the database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_KINDS = frozenset({"openai"})


class CloudRegistryError(ValueError):
    """Raised for an unreadable provider registry."""


@dataclass(frozen=True)
class CloudProvider:
    id: str
    kind: str
    base_url: str
    api_key_env: str
    default_model: str = ""
    free_requests_per_day: int | None = None
    free_tokens_per_day: int | None = None
    allows_private_data: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.id or not self.id.replace("-", "").replace("_", "").isalnum():
            raise CloudRegistryError(f"invalid provider id: {self.id!r}")
        if self.kind not in SUPPORTED_KINDS:
            raise CloudRegistryError(f"unsupported provider kind: {self.kind!r}")
        if not self.base_url.startswith("https://"):
            raise CloudRegistryError(f"{self.id}: base_url must use https")
        if not self.api_key_env:
            raise CloudRegistryError(f"{self.id}: api_key_env is required")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", self.api_key_env):
            raise CloudRegistryError(
                f"{self.id}: api_key_env must be an upper-case environment name"
            )
        for name, value in (
            ("free_requests_per_day", self.free_requests_per_day),
            ("free_tokens_per_day", self.free_tokens_per_day),
        ):
            if value is not None and value < 0:
                raise CloudRegistryError(f"{self.id}: {name} must be >= 0")

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


@dataclass(frozen=True)
class CloudRegistry:
    providers: dict[str, CloudProvider] = field(default_factory=dict)

    def get(self, provider_id: str) -> CloudProvider | None:
        return self.providers.get(provider_id)

    def require(self, provider_id: str) -> CloudProvider:
        provider = self.get(provider_id)
        if provider is None:
            raise CloudRegistryError(f"unknown cloud provider: {provider_id}")
        return provider

    def all(self) -> tuple[CloudProvider, ...]:
        return tuple(self.providers[key] for key in sorted(self.providers))


def _provider_from_mapping(raw: Any) -> CloudProvider:
    if not isinstance(raw, dict):
        raise CloudRegistryError("each provider must be a mapping")
    allowed = {
        "id",
        "kind",
        "base_url",
        "api_key_env",
        "default_model",
        "free_requests_per_day",
        "free_tokens_per_day",
        "allows_private_data",
        "notes",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise CloudRegistryError(f"unknown provider keys: {', '.join(sorted(unknown))}")
    try:
        return CloudProvider(**raw)
    except TypeError as exc:
        raise CloudRegistryError(f"invalid provider entry: {exc}") from exc


def load_cloud_registry(path: Path) -> CloudRegistry:
    if not path.is_file():
        raise CloudRegistryError(f"cloud registry not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise CloudRegistryError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or "providers" not in data:
        raise CloudRegistryError("registry must contain a 'providers' list")
    entries = data["providers"]
    if not isinstance(entries, list):
        raise CloudRegistryError("'providers' must be a list")

    providers: dict[str, CloudProvider] = {}
    for raw in entries:
        provider = _provider_from_mapping(raw)
        if provider.id in providers:
            raise CloudRegistryError(f"duplicate provider id: {provider.id}")
        providers[provider.id] = provider
    if not providers:
        raise CloudRegistryError("registry has no providers")
    return CloudRegistry(providers)
