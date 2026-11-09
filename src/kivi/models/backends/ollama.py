"""Ollama backend.

Ollama owns model loading; the gateway only controls it through ``keep_alive``
and reads state back from ``/api/ps``. Requests are synchronous because the
kernel's first slices are synchronous; streaming arrives with the UI.
"""

from __future__ import annotations

from typing import Any

import httpx

from kivi.models.backends.base import BackendError, BackendUnavailable

DEFAULT_URL = "http://127.0.0.1:11434"


class OllamaBackend:
    name = "ollama"

    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        *,
        timeout: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(base_url=self.base_url, timeout=timeout)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OllamaBackend:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise BackendUnavailable(f"Ollama is not running at {self.base_url}") from exc
        except httpx.HTTPError as exc:
            raise BackendError(f"Ollama request failed: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text.strip()[:500]
            raise BackendError(f"Ollama HTTP {response.status_code}: {detail}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BackendError("Ollama returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise BackendError("Ollama returned an unexpected payload")
        return payload

    def list_models(self) -> list[str]:
        payload = self._request("GET", "/api/tags")
        models = payload.get("models", [])
        return [item.get("name", "") for item in models if item.get("name")]

    def running(self) -> dict[str, int]:
        try:
            payload = self._request("GET", "/api/ps")
        except BackendUnavailable:
            raise
        except BackendError:
            return {}
        loaded: dict[str, int] = {}
        for item in payload.get("models", []):
            name = item.get("name") or item.get("model")
            if not name:
                continue
            size = item.get("size_vram") or 0
            loaded[name] = int(size) // (1024 * 1024)
        return loaded

    def load(self, model_id: str, keep_alive: str = "5m") -> None:
        self._request(
            "POST",
            "/api/generate",
            json={"model": model_id, "prompt": "", "stream": False, "keep_alive": keep_alive},
        )

    def unload(self, model_id: str) -> None:
        self._request(
            "POST",
            "/api/generate",
            json={"model": model_id, "prompt": "", "stream": False, "keep_alive": 0},
        )

    def chat(
        self,
        model_id: str,
        messages: list[dict[str, str]],
        *,
        keep_alive: str = "5m",
        options: dict[str, Any] | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "stream": False,
            "keep_alive": keep_alive,
        }
        if options:
            body["options"] = options
        payload = self._request("POST", "/api/chat", json=body)
        message = payload.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise BackendError("Ollama response has no message.content")
        return message["content"]
