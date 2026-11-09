"""Model Gateway: registry, VRAM/RAM budgets, leases and fallback."""

from kivi.models.backends.base import BackendError, BackendUnavailable, ModelBackend
from kivi.models.backends.ollama import OllamaBackend
from kivi.models.gateway import (
    GatewayError,
    InsufficientVram,
    Lease,
    ModelBusy,
    ModelGateway,
    gateway_from_config,
)
from kivi.models.registry import (
    DEFAULT_MODELS,
    ModelRegistry,
    ModelSpec,
    RegistryError,
    load_registry,
)
from kivi.models.vram import VramSnapshot, parse_smi_output, query_vram

__all__ = [
    "DEFAULT_MODELS",
    "BackendError",
    "BackendUnavailable",
    "GatewayError",
    "InsufficientVram",
    "Lease",
    "ModelBackend",
    "ModelBusy",
    "ModelGateway",
    "ModelRegistry",
    "ModelSpec",
    "OllamaBackend",
    "RegistryError",
    "VramSnapshot",
    "gateway_from_config",
    "load_registry",
    "parse_smi_output",
    "query_vram",
]
