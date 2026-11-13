"""Loopback WebSocket bridge between Kivi and the AltCRM browser extension.

Only a browser extension may connect (Origin check) and it must present the
shared token.  The extension exposes a fixed command set and never presses
"Сохранить".
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import secrets
import threading
import uuid
from pathlib import Path

from .ws_server import WSConnection, serve

log = logging.getLogger("bridge")


class BridgeError(RuntimeError):
    """The bridge is not running, the extension is absent or a request failed."""


def parse_netstat_owner(text: str, port: int) -> int | None:
    """Return the PID listening on ``port`` from ``netstat -ano`` output."""
    for line in text.splitlines():
        parts = line.split()
        if (
            len(parts) >= 5
            and parts[0].upper() == "TCP"
            and parts[1].rsplit(":", 1)[-1] == str(port)
            and parts[2] in ("0.0.0.0:0", "[::]:0", "*:*")
        ):
            try:
                return int(parts[-1])
            except ValueError:
                return None
    return None


def port_owner(port: int) -> str:
    """Return a human-readable owner of a busy port, or an empty string."""
    import subprocess
    import sys

    if sys.platform != "win32":
        return ""
    try:
        flags = 0x08000000
        output = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            creationflags=flags,
            timeout=10,
        )
        pid = parse_netstat_owner(output.stdout.decode("cp866", errors="replace"), port)
        if pid is None:
            return ""
        task = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            creationflags=flags,
            timeout=10,
        )
        first = task.stdout.decode("cp866", errors="replace").strip().splitlines()[0:1]
        name = first[0].split(",")[0].strip('"') if first else "?"
        return f"{name} (PID {pid})"
    except Exception:  # noqa: BLE001 - diagnostics are best effort
        return ""


def get_or_create_token(path: Path) -> str:
    """Read the shared token file, creating a new token if it is empty."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8").strip():
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(24)
    path.write_text(token, encoding="utf-8")
    return token


class ExtensionBridge:
    """Thread-hosted WebSocket server with one active extension connection."""

    def __init__(self, token: str, host: str = "127.0.0.1", port: int = 8765):
        self.token, self.host, self.port = token, host, port
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server = None
        self._conn: WSConnection | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._ready = threading.Event()
        self._start_error: Exception | None = None

    def start(self) -> None:
        """Start the loopback server and wait until it is listening."""
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="bridge")
        self._thread.start()
        self._ready.wait(9)
        if self._start_error:
            owner = port_owner(self.port)
            who = f"Его занимает процесс {owner}." if owner else (
                f"Кто его занимает: netstat -ano | findstr :{self.port}"
            )
            raise BridgeError(
                f"Порт {self.port} занят. {who} Если это python.exe, вероятно, "
                "агент уже запущен: закройте его. Если процесс чужой, смените "
                f"порт в config и обновите расширение. ({self._start_error})"
            )

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        import time

        last: Exception | None = None
        for _ in range(12):
            try:
                self._server = self._loop.run_until_complete(
                    serve(self._handler, self.host, self.port)
                )
                sock = self._server.sockets[0].getsockname()
                self.port = sock[1]
                last = None
                break
            except OSError as exc:
                last = exc
                time.sleep(0.5)
        if last is not None:
            self._start_error = last
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()

    def stop(self) -> None:
        """Close the extension connection, server and loop."""
        if self._loop and self._thread:
            async def _close() -> None:
                if self._conn:
                    await self._conn.close()
                if self._server:
                    self._server.close()

            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(_close(), self._loop).result(3)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread = None

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.closed

    def wait_connected(self, timeout: float = 10) -> bool:
        """Wait for the extension to complete the token handshake."""
        import time

        end = time.time() + timeout
        while time.time() < end:
            if self.connected:
                return True
            time.sleep(0.1)
        return False

    async def _handler(self, ws: WSConnection) -> None:
        try:
            raw = await asyncio.wait_for(ws.recv(), 5)
            hello = json.loads(raw or "{}")
        except Exception:  # noqa: BLE001 - malformed handshake
            return
        token = str(hello.get("token", ""))
        if hello.get("type") != "hello" or not hmac.compare_digest(token, self.token):
            log.warning("extension handshake rejected")
            await ws.close(4401)
            return
        old = self._conn
        self._conn = ws
        if old and not old.closed:
            await old.close(4000)
        await ws.send(json.dumps({"type": "welcome"}))
        log.info("extension connected")
        while True:
            raw = await ws.recv()
            if raw is None:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")
            if kind == "ping":
                await ws.send(json.dumps({"type": "pong"}))
            elif kind == "result":
                future = self._pending.pop(msg.get("id"), None)
                if future and not future.done():
                    future.set_result(msg)
        if self._conn is ws:
            self._conn = None
            log.info("extension disconnected")

    def request(self, action: str, payload: dict | None = None, timeout: float = 30):
        """Run one action in the browser and return its data."""
        if not self._loop:
            raise BridgeError("Мост не запущен")
        future = asyncio.run_coroutine_threadsafe(
            self._request(action, payload or {}, timeout),
            self._loop,
        )
        try:
            return future.result(timeout + 3)
        except BridgeError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport boundary
            raise BridgeError(str(exc) or exc.__class__.__name__) from exc

    async def _request(self, action: str, payload: dict, timeout: float):
        if not self.connected:
            raise BridgeError(
                "Расширение браузера не подключено. Проверьте, что оно установлено, "
                "включено и на значке написано ON."
            )
        request_id = uuid.uuid4().hex
        future = self._loop.create_future()
        self._pending[request_id] = future
        assert self._conn is not None
        await self._conn.send(
            json.dumps(
                {
                    "type": "command",
                    "id": request_id,
                    "action": action,
                    "payload": payload,
                }
            )
        )
        try:
            msg = await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise BridgeError(
                f"Расширение не ответило за {timeout:.0f} с"
            ) from exc
        if not msg.get("ok"):
            raise BridgeError(str(msg.get("error") or "ошибка расширения"))
        return msg.get("data")
