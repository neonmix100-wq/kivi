"""Minimal RFC 6455 WebSocket server (stdlib only, text messages)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import struct
from collections.abc import Awaitable, Callable

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 96 * 1024 * 1024


class WSClosed(Exception):
    """The WebSocket peer is closed."""


class WSConnection:
    """One text-only WebSocket connection."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: dict[str, str],
    ) -> None:
        self.reader, self.writer, self.headers = reader, writer, headers
        self.closed = False

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        length = len(payload)
        head = bytes([0x80 | opcode])
        if length < 126:
            head += bytes([length])
        elif length < 65536:
            head += bytes([126]) + struct.pack(">H", length)
        else:
            head += bytes([127]) + struct.pack(">Q", length)
        self.writer.write(head + payload)
        await self.writer.drain()

    async def send(self, text: str) -> None:
        if self.closed:
            raise WSClosed()
        await self._send_frame(0x1, text.encode("utf-8"))

    async def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(Exception):
            await self._send_frame(0x8, struct.pack(">H", code))
        with contextlib.suppress(Exception):
            self.writer.close()

    async def recv(self) -> str | None:
        """Return the next text message, or None after close."""
        parts: list[bytes] = []
        while True:
            try:
                b1, b2 = await self.reader.readexactly(2)
                fin, opcode = bool(b1 & 0x80), b1 & 0x0F
                masked, length = bool(b2 & 0x80), b2 & 0x7F
                if length == 126:
                    length = struct.unpack(">H", await self.reader.readexactly(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", await self.reader.readexactly(8))[0]
                if length > MAX_MESSAGE or not masked:
                    await self.close(1002)
                    return None
                mask = await self.reader.readexactly(4)
                data = await self.reader.readexactly(length) if length else b""
            except (asyncio.IncompleteReadError, ConnectionError):
                self.closed = True
                return None
            if length:
                key = (mask * (length // 4 + 1))[:length]
                data = (
                    int.from_bytes(data, "big") ^ int.from_bytes(key, "big")
                ).to_bytes(length, "big")
            if opcode == 0x8:
                await self.close(1000)
                return None
            if opcode == 0x9:
                await self._send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8", errors="replace")


async def _handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    origin_ok: Callable[[str], bool],
    http_handler=None,
) -> WSConnection | None:
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    except Exception:  # noqa: BLE001 - malformed request
        writer.close()
        return None
    lines = raw.decode("latin-1").split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    is_ws = headers.get("upgrade", "").lower() == "websocket"
    if http_handler is not None and not is_ws:
        parts = lines[0].split()
        path = parts[1] if len(parts) > 1 else "/"
        try:
            status, ctype, body = http_handler(parts[0] if parts else "GET", path, headers)
        except Exception:  # noqa: BLE001 - HTTP fallback boundary
            status, ctype, body = 500, "text/plain; charset=utf-8", b"internal error"
        reason = {
            200: "OK",
            404: "Not Found",
            403: "Forbidden",
            500: "Internal Server Error",
        }.get(status, "OK")
        writer.write(
            (
                f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\n"
                f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            + body
        )
        await writer.drain()
        writer.close()
        return None
    bad = (
        not lines[0].startswith("GET ")
        or not is_ws
        or "sec-websocket-key" not in headers
    )
    origin = headers.get("origin")
    if not bad and origin is not None and not origin_ok(origin):
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        return None
    if bad:
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        return None
    accept = base64.b64encode(
        hashlib.sha1((headers["sec-websocket-key"] + GUID).encode()).digest()
    ).decode()
    writer.write(
        (
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    return WSConnection(reader, writer, headers)


def default_origin_ok(origin: str) -> bool:
    """Only browser-extension origins may connect, never web pages."""
    return origin.startswith(
        ("chrome-extension://", "moz-extension://", "safari-web-extension://")
    )


async def serve(
    handler: Callable[[WSConnection], Awaitable[None]],
    host: str,
    port: int,
    origin_ok: Callable[[str], bool] = default_origin_ok,
    http_handler=None,
):
    """Start a minimal WebSocket server and dispatch each connection."""

    async def on_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        conn = await _handshake(reader, writer, origin_ok, http_handler)
        if conn is None:
            return
        try:
            await handler(conn)
        except Exception:  # noqa: BLE001 - per-client boundary
            pass
        finally:
            await conn.close()

    return await asyncio.start_server(on_client, host, port)
