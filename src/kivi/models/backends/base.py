"""Backend protocol shared by Ollama, llama.cpp, ComfyUI and cloud workers."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol, runtime_checkable


class BackendError(RuntimeError):
    """A model backend is unreachable, rejected the request or failed."""


class BackendUnavailable(BackendError):
    """The backend process is not running."""


@runtime_checkable
class ModelBackend(Protocol):
    name: str

    def list_models(self) -> list[str]:
        """Names currently installed in the backend."""

    def running(self) -> dict[str, int]:
        """Loaded model -> VRAM used in MB (0 when the backend cannot tell)."""

    def load(self, model_id: str, keep_alive: str = "5m") -> None: ...

    def unload(self, model_id: str) -> None: ...

    def chat(
        self,
        model_id: str,
        messages: list[dict[str, str]],
        *,
        keep_alive: str = "5m",
        options: dict[str, Any] | None = None,
    ) -> str: ...

    def chat_stream(
        self,
        model_id: str,
        messages: list[dict[str, str]],
        *,
        keep_alive: str = "5m",
        options: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        """Yield answer chunks as the backend produces them."""
