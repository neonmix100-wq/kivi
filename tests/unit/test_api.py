from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx
import pytest

from kivi import __version__
from kivi.interfaces.api import MAX_BODY_BYTES, LocalApi


@contextmanager
def running_api(
    handler: Callable[[str], str] | None = None,
) -> Iterator[LocalApi]:
    """Start an ephemeral loopback API and stop it afterwards."""
    api = LocalApi(handler or (lambda message: f"echo: {message}"))
    api.start()
    try:
        yield api
    finally:
        api.stop()


def _client(api: LocalApi) -> httpx.Client:
    """Return a client bound to the ephemeral loopback URL, without proxies."""
    return httpx.Client(base_url=api.url, timeout=5.0, trust_env=False)


@pytest.fixture()
def api() -> Iterator[LocalApi]:
    with running_api() as instance:
        yield instance


def test_health_returns_json_version(api: LocalApi) -> None:
    with _client(api) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.json() == {"status": "ok", "version": __version__}


def test_api_binds_only_loopback(api: LocalApi) -> None:
    assert api.url.startswith("http://127.0.0.1:")


def test_chat_returns_handler_answer(api: LocalApi) -> None:
    with _client(api) as client:
        response = client.post("/chat", json={"message": "привет"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.json() == {"answer": "echo: привет", "task_id": None}
    assert "привет" in response.text


def test_chat_echoes_task_id() -> None:
    def handler(message: str) -> str:
        return f"answer for {message}"

    with running_api(handler) as api, _client(api) as client:
        response = client.post(
            "/chat",
            json={"message": "hello", "task_id": "tsk_abc123"},
        )

    assert response.status_code == 200
    assert response.json() == {"answer": "answer for hello", "task_id": "tsk_abc123"}


@pytest.mark.parametrize(
    "body",
    [b"", b"{not-json", b"null", b"[]", b'{"message": 42}'],
)
def test_chat_rejects_invalid_json_or_shape(api: LocalApi, body: bytes) -> None:
    with _client(api) as client:
        response = client.post(
            "/chat",
            content=body,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert "error" in response.json()


def test_chat_rejects_missing_message(api: LocalApi) -> None:
    with _client(api) as client:
        response = client.post("/chat", json={"task_id": "tsk_1"})

    assert response.status_code == 400
    assert response.json() == {"error": "message must be a non-empty string"}


@pytest.mark.parametrize("message", ["", "   ", "\n\t"])
def test_chat_rejects_empty_message(api: LocalApi, message: str) -> None:
    with _client(api) as client:
        response = client.post("/chat", json={"message": message})

    assert response.status_code == 400
    assert response.json() == {"error": "message must be a non-empty string"}


def test_chat_rejects_non_string_task_id(api: LocalApi) -> None:
    with _client(api) as client:
        response = client.post("/chat", json={"message": "hi", "task_id": 7})

    assert response.status_code == 400
    assert response.json() == {"error": "task_id must be a string"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/health"),
        ("GET", "/chat"),
        ("DELETE", "/chat"),
        ("PUT", "/chat"),
    ],
)
def test_wrong_method_is_json_404(api: LocalApi, method: str, path: str) -> None:
    with _client(api) as client:
        response = client.request(method, path)

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.json() == {"error": "not found"}


def test_unknown_path_is_json_404(api: LocalApi) -> None:
    with _client(api) as client:
        response = client.get("/nope")

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


def test_unknown_http_method_is_json_404(api: LocalApi) -> None:
    with _client(api) as client:
        response = client.request("BREW", "/health")

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


def test_oversized_body_is_413(api: LocalApi) -> None:
    body = b'{"message":"' + b"x" * MAX_BODY_BYTES + b'"}'

    with _client(api) as client:
        response = client.post(
            "/chat",
            content=body,
            headers={"content-type": "application/json"},
        )

    assert len(body) > MAX_BODY_BYTES
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.json() == {"error": "request body too large"}


def test_handler_exception_returns_500_without_details() -> None:
    def handler(message: str) -> str:
        raise RuntimeError("do-not-leak-this-detail")

    with running_api(handler) as api, _client(api) as client:
        response = client.post("/chat", json={"message": "hi"})
        health = client.get("/health")

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.json() == {"error": "internal error"}
    assert "do-not-leak-this-detail" not in response.text
    assert "Traceback" not in response.text
    assert health.status_code == 200


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.test"])
def test_non_loopback_host_is_refused(host: str) -> None:
    with pytest.raises(ValueError, match="allow_remote"):
        LocalApi(lambda message: message, host=host)


def test_allow_remote_is_an_explicit_opt_in() -> None:
    # Construction is enough here: starting a wildcard bind would leave loopback.
    api = LocalApi(lambda message: message, host="0.0.0.0", allow_remote=True)
    assert api is not None


def test_stop_is_idempotent() -> None:
    api = LocalApi(lambda message: message)
    api.start()
    assert api.url.startswith("http://127.0.0.1:")

    api.stop()
    api.stop()

    with pytest.raises(RuntimeError, match="not running"):
        _ = api.url


def test_handler_calls_are_serialized() -> None:
    active = 0
    peak = 0
    state_lock = threading.Lock()

    def handler(message: str) -> str:
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with state_lock:
            active -= 1
        return f"echo: {message}"

    def chat(base_url: str) -> int:
        response = httpx.post(
            f"{base_url}/chat",
            json={"message": "hi"},
            timeout=5.0,
            trust_env=False,
        )
        return response.status_code

    with running_api(handler) as api, ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(chat, [api.url] * 4))

    assert statuses == [200, 200, 200, 200]
    assert peak == 1
