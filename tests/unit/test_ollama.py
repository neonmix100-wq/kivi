from __future__ import annotations

import httpx
import pytest

from kivi.models.backends.base import BackendError, BackendUnavailable
from kivi.models.backends.ollama import OllamaBackend


def make_backend(handler) -> OllamaBackend:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(base_url="http://ollama.test", transport=transport)
    return OllamaBackend("http://ollama.test", client=client)


def test_list_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={"models": [{"name": "qwen3.5:4b"}, {"name": "llama3:8b"}, {"bad": True}]},
        )

    with make_backend(handler) as backend:
        assert backend.list_models() == ["qwen3.5:4b", "llama3:8b"]


def test_running_reports_vram_in_mb() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"models": [{"name": "qwen3.5:4b", "size_vram": 3 * 1024**3}]},
        )

    with make_backend(handler) as backend:
        assert backend.running() == {"qwen3.5:4b": 3072}


def test_load_and_unload_use_keep_alive() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read().decode())
        return httpx.Response(200, json={})

    with make_backend(handler) as backend:
        backend.load("m")
        backend.unload("m")

    assert '"keep_alive":"5m"' in bodies[0]
    assert '"prompt":""' in bodies[0]
    assert '"keep_alive":0' in bodies[1]


def test_chat_returns_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "привет"}})

    with make_backend(handler) as backend:
        answer = backend.chat("m", [{"role": "user", "content": "hi"}], options={"temperature": 0})
    assert answer == "привет"


def test_http_error_becomes_backend_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="model not found")

    with make_backend(handler) as backend, pytest.raises(BackendError, match="404"):
        backend.load("missing")


def test_unreachable_backend() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with make_backend(handler) as backend, pytest.raises(
        BackendUnavailable, match="not running"
    ):
        backend.list_models()


def test_invalid_json_is_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with make_backend(handler) as backend, pytest.raises(BackendError, match="JSON"):
        backend.list_models()


def test_chat_without_content_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"role": "assistant"}})

    with make_backend(handler) as backend, pytest.raises(BackendError, match="content"):
        backend.chat("m", [{"role": "user", "content": "hi"}])
