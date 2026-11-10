"""Local-only HTTP API for Kivi chat.

The API is deliberately small and uses only the standard library: a
``ThreadingHTTPServer`` with two routes, JSON responses and no dependencies. It
is local-first by construction: a non-loopback bind is refused unless the caller
opts in with ``allow_remote=True``.

The handler is a blocking ``Callable[[str], str]``. Calls are serialized with a
lock so a local model is never asked to serve two chats at once. There is no
authentication: the loopback default is the security boundary. Every response is
JSON; the request body is capped at 64 KiB and exception details never reach the
client.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from kivi import __version__

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
"""Largest accepted ``POST /chat`` body, in bytes."""

# An oversized body is drained before the 413 so the client can read the response
# instead of seeing a connection reset. Very large bodies are not drained.
_MAX_DRAIN_BYTES = 16 * 1024 * 1024

Handler = Callable[[str], str]


def _is_loopback(host: str) -> bool:
    """Return whether ``host`` names only a loopback interface."""
    name = host.strip().lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


class LocalApi:
    """A tiny JSON HTTP server around a blocking chat handler.

    Args:
        handler: Callable that receives one non-empty message and returns the
            answer text. It runs in a request thread and may block.
        host: Bind address. Must be loopback unless ``allow_remote`` is true.
        port: Bind port; ``0`` selects an ephemeral port on :meth:`start`.
        allow_remote: Explicit opt-in for a non-loopback bind. The API has no
            authentication, so keep this false unless access is otherwise
            controlled.

    The handler is called one request at a time. :meth:`stop` is idempotent and
    an API instance may be started again after stopping.
    """

    def __init__(
        self,
        handler: Handler,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        allow_remote: bool = False,
    ) -> None:
        if not callable(handler):
            raise TypeError("handler must be callable")
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if not allow_remote and not _is_loopback(host):
            raise ValueError(
                "refusing non-loopback host without allow_remote=True; "
                "this API has no authentication"
            )

        self._handler = handler
        self._host = host
        self._port = port
        self._handler_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._server: _ApiServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """Base URL of the running server, for example ``http://127.0.0.1:54321``.

        Raises:
            RuntimeError: If the API has not been started or has been stopped.
        """
        with self._lifecycle_lock:
            server = self._server
        if server is None:
            raise RuntimeError("API is not running")
        host, port = server.server_address[:2]
        text_host = f"[{host}]" if ":" in str(host) else str(host)
        return f"http://{text_host}:{port}"

    def start(self) -> LocalApi:
        """Bind the socket and serve requests from a daemon thread.

        The method is idempotent: calling it on a running API returns ``self``
        without rebinding. ``port=0`` binds an ephemeral port, available through
        :attr:`url`.

        Returns:
            This instance, so ``LocalApi(...).start()`` can be used inline.
        """
        with self._lifecycle_lock:
            if self._server is not None:
                return self
            server_cls = _ApiServerV6 if ":" in self._host else _ApiServer
            server = server_cls((self._host, self._port), _ApiHandler, self)
            thread = threading.Thread(
                target=server.serve_forever,
                # Keep stop() responsive instead of waiting for the 0.5 s default.
                kwargs={"poll_interval": 0.05},
                name="kivi-local-api",
                daemon=True,
            )
            try:
                thread.start()
            except BaseException:
                server.server_close()
                raise
            self._server = server
            self._thread = thread
            return self

    def stop(self) -> None:
        """Shut the server down, close the socket and join the serving thread.

        Safe to call multiple times. A request already inside the handler is not
        interrupted; its request thread is a daemon and may finish after stop.
        """
        with self._lifecycle_lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
            if server is None:
                return
            server.shutdown()
            server.server_close()
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5.0)

    def _call_handler(self, message: str) -> str:
        """Call the user handler while holding the serialization lock."""
        with self._handler_lock:
            return self._handler(message)


class _ApiServer(ThreadingHTTPServer):
    """Threading HTTP server that carries its :class:`LocalApi` instance."""

    daemon_threads = True
    # Do not make stop() wait for a long model call; request threads are daemons.
    block_on_close = False

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        api: LocalApi,
    ) -> None:
        self.api = api
        super().__init__(server_address, handler_cls)


class _ApiServerV6(_ApiServer):
    """IPv6 variant used for ``::1`` and other colon-containing hosts."""

    address_family = socket.AF_INET6


class _ApiHandler(BaseHTTPRequestHandler):
    """Request handler for ``GET /health`` and ``POST /chat``."""

    server_version = "KiviLocalApi/0.1"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: object) -> None:
        """Disable the default stderr access log; callers audit if needed."""
        return

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        self._dispatch("OPTIONS")

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Return JSON for malformed requests and unsupported methods."""
        self.close_connection = True
        # The routing contract says unknown methods are 404, not 501.
        if code == HTTPStatus.NOT_IMPLEMENTED:
            code = HTTPStatus.NOT_FOUND
        if code >= 500:
            self._send_json(500, {"error": "internal error"})
        elif code == HTTPStatus.NOT_FOUND:
            self._send_json(404, {"error": "not found"})
        elif code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE:
            self._send_json(413, {"error": "request body too large"})
        else:
            self._send_json(400, {"error": "bad request"})

    def _dispatch(self, method: str) -> None:
        path = urlsplit(self.path).path
        if method == "GET" and path == "/health":
            self._send_json(200, {"status": "ok", "version": __version__})
        elif method == "POST" and path == "/chat":
            self._handle_chat()
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_chat(self) -> None:
        body = self._read_body()
        if body is None:
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid JSON"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "JSON object expected"})
            return
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            self._send_json(400, {"error": "message must be a non-empty string"})
            return
        task_id = payload.get("task_id")
        if task_id is not None and not isinstance(task_id, str):
            self._send_json(400, {"error": "task_id must be a string"})
            return
        try:
            answer = self.server.api._call_handler(message)
        except Exception:
            # Deliberately no exception text or traceback in the response.
            logger.warning("chat handler failed")
            self._send_json(500, {"error": "internal error"})
            return
        self._send_json(200, {"answer": answer, "task_id": task_id})

    def _read_body(self) -> bytes | None:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._send_json(400, {"error": "missing Content-Length"})
            return None
        try:
            length = int(raw_length)
        except ValueError:
            self._send_json(400, {"error": "invalid Content-Length"})
            return None
        if length < 0:
            self._send_json(400, {"error": "invalid Content-Length"})
            return None
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            if length <= _MAX_DRAIN_BYTES:
                self._discard_body(length)
            self._send_json(413, {"error": "request body too large"})
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self._send_json(400, {"error": "incomplete request body"})
            return None
        return body

    def _discard_body(self, length: int) -> None:
        """Read and drop an oversized body so the client can see the 413."""
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                return
            remaining -= len(chunk)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)


__all__ = ["MAX_BODY_BYTES", "LocalApi"]
