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
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from kivi import __version__

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
"""Largest accepted ``POST /chat`` body, in bytes."""

# An oversized body is drained before the 413 so the client can read the response
# instead of seeing a connection reset. Very large bodies are not drained.
_MAX_DRAIN_BYTES = 16 * 1024 * 1024

Handler = Callable[[str], str]
TtsHandler = Callable[[str], bytes]
SttHandler = Callable[[bytes], str]
MediaHandler = Callable[[dict[str, Any]], dict[str, Any]]
HistoryHandler = Callable[[int], list[dict[str, Any]]]
VoicesHandler = Callable[[], list[dict[str, Any]]]
SettingsHandler = Callable[[], dict[str, Any]]
VoiceSelectHandler = Callable[[str], dict[str, Any]]

_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


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
        static_dir: Path | str | None = None,
        docs_dir: Path | str | None = None,
        tts_handler: TtsHandler | None = None,
        stt_handler: SttHandler | None = None,
        media_handler: MediaHandler | None = None,
        history_handler: HistoryHandler | None = None,
        voices_handler: VoicesHandler | None = None,
        settings_handler: SettingsHandler | None = None,
        voice_select_handler: VoiceSelectHandler | None = None,
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
        self._static_dir = Path(static_dir).resolve() if static_dir is not None else None
        self._docs_dir = Path(docs_dir).resolve() if docs_dir is not None else None
        self._tts_handler = tts_handler
        self._stt_handler = stt_handler
        self._media_handler = media_handler
        self._history_handler = history_handler
        self._voices_handler = voices_handler
        self._settings_handler = settings_handler
        self._voice_select_handler = voice_select_handler
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
        split = urlsplit(self.path)
        path = split.path
        if method == "GET" and path == "/health":
            self._send_json(200, {"status": "ok", "version": __version__})
        elif method == "POST" and path == "/chat":
            self._handle_chat()
        elif method == "GET" and path in _STATIC_FILES:
            self._serve_static(path)
        elif method == "GET" and path.startswith("/docs/"):
            self._serve_doc(path)
        elif method == "POST" and path == "/tts":
            self._handle_tts()
        elif method == "POST" and path == "/transcribe":
            self._handle_transcribe()
        elif method == "POST" and path == "/media/image":
            self._handle_media()
        elif method == "GET" and path == "/history":
            self._handle_history(split.query)
        elif method == "GET" and path == "/voices":
            self._handle_voices()
        elif method == "POST" and path == "/voices/active":
            self._handle_voice_select()
        elif method == "GET" and path == "/settings":
            self._handle_settings()
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

    def _serve_static(self, path: str) -> None:
        api = self.server.api
        root = api._static_dir
        if root is None:
            self._send_json(404, {"error": "not found"})
            return
        name, content_type = _STATIC_FILES[path]
        target = (root / name).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            self._send_json(404, {"error": "not found"})
            return
        if not target.is_file():
            self._send_json(404, {"error": "not found"})
            return
        self._send_bytes(200, target.read_bytes(), content_type)

    def _serve_doc(self, path: str) -> None:
        root = self.server.api._docs_dir
        name = path.removeprefix("/docs/")
        if root is None or not name or "/" in name or "\\" in name:
            self._send_json(404, {"error": "not found"})
            return
        if not name.endswith(".md"):
            self._send_json(404, {"error": "not found"})
            return
        target = (root / name).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            self._send_json(404, {"error": "not found"})
            return
        if not target.is_file():
            self._send_json(404, {"error": "not found"})
            return
        self._send_bytes(200, target.read_bytes(), "text/markdown; charset=utf-8")

    def _handle_tts(self) -> None:
        api = self.server.api
        if api._tts_handler is None:
            self._send_json(503, {"error": "tts is not configured"})
            return
        payload = self._read_json_object()
        if payload is None:
            return
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send_json(400, {"error": "text must be a non-empty string"})
            return
        try:
            audio = api._tts_handler(text.strip())
        except Exception:
            logger.warning("tts handler failed")
            self._send_json(500, {"error": "tts failed"})
            return
        if not isinstance(audio, (bytes, bytearray)) or not audio:
            self._send_json(500, {"error": "tts returned empty audio"})
            return
        self._send_bytes(200, bytes(audio), "audio/wav")

    def _handle_transcribe(self) -> None:
        api = self.server.api
        if api._stt_handler is None:
            self._send_json(503, {"error": "speech recognition is not configured"})
            return
        body = self._read_body(10 * 1024 * 1024)
        if body is None:
            return
        if len(body) < 44 or not body.startswith(b"RIFF"):
            self._send_json(400, {"error": "audio/wav body expected"})
            return
        try:
            text = api._stt_handler(body)
        except Exception:
            logger.warning("stt handler failed")
            self._send_json(500, {"error": "transcription failed"})
            return
        self._send_json(200, {"text": str(text).strip()})

    def _handle_media(self) -> None:
        api = self.server.api
        if api._media_handler is None:
            self._send_json(503, {"error": "media generation is not configured"})
            return
        payload = self._read_json_object()
        if payload is None:
            return
        try:
            result = api._media_handler(payload)
        except Exception:
            logger.warning("media handler failed")
            self._send_json(500, {"error": "media generation failed"})
            return
        if not isinstance(result, dict) or not isinstance(result.get("image"), str):
            self._send_json(500, {"error": "media generation returned no image"})
            return
        self._send_json(200, result)

    def _handle_history(self, query: str) -> None:
        api = self.server.api
        if api._history_handler is None:
            self._send_json(200, {"messages": []})
            return
        try:
            raw = parse_qs(query).get("limit", ["50"])[0]
            limit = max(1, min(int(raw), 200))
        except (ValueError, TypeError):
            self._send_json(400, {"error": "limit must be an integer"})
            return
        try:
            messages = api._history_handler(limit)
        except Exception:
            logger.warning("history handler failed")
            self._send_json(500, {"error": "history failed"})
            return
        self._send_json(200, {"messages": messages})

    def _handle_voices(self) -> None:
        api = self.server.api
        if api._voices_handler is None:
            self._send_json(200, {"voices": []})
            return
        try:
            voices = api._voices_handler()
        except Exception:
            logger.warning("voices handler failed")
            self._send_json(500, {"error": "voices failed"})
            return
        self._send_json(200, {"voices": voices})

    def _handle_voice_select(self) -> None:
        api = self.server.api
        if api._voice_select_handler is None:
            self._send_json(503, {"error": "voice selection is not configured"})
            return
        payload = self._read_json_object()
        if payload is None:
            return
        voice_id = payload.get("id")
        if not isinstance(voice_id, str) or not voice_id.strip():
            self._send_json(400, {"error": "id must be a non-empty string"})
            return
        try:
            result = api._voice_select_handler(voice_id.strip())
        except Exception as exc:  # noqa: BLE001 - no details to the client
            logger.warning("voice select failed: %s", type(exc).__name__)
            self._send_json(400, {"error": "voice selection failed"})
            return
        self._send_json(200, result)

    def _handle_settings(self) -> None:
        api = self.server.api
        if api._settings_handler is None:
            self._send_json(200, {})
            return
        try:
            settings = api._settings_handler()
        except Exception:
            logger.warning("settings handler failed")
            self._send_json(500, {"error": "settings failed"})
            return
        self._send_json(200, settings)

    def _read_json_object(self) -> dict[str, Any] | None:
        body = self._read_body()
        if body is None:
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid JSON"})
            return None
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "JSON object expected"})
            return None
        return payload

    def _read_body(self, max_bytes: int = MAX_BODY_BYTES) -> bytes | None:
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
        if length > max_bytes:
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
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)


__all__ = ["MAX_BODY_BYTES", "LocalApi"]
