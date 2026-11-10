"""HTTP GET with an SSRF guard and an explicit ``untrusted:web`` label.

The tool is deliberately narrow: HTTPS only, port 443, no credentials in the
URL, no redirects, a small allow-list of text content types, a byte cap and a
timeout. Every resolved address must be globally routable, so a hostname that
points at localhost or a LAN address is refused before any request is sent.

The response is sanitised before it becomes a tool result: hidden HTML, control
characters and secret-looking tokens are removed and the text is truncated.
Even then the result is marked ``untrusted:web`` and may not issue instructions.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from kivi.safety.sanitize import sanitize_text
from kivi.tools.base import Preview, ToolContext, ToolError, ToolResult

Resolver = Callable[..., list[tuple[Any, ...]]]

_ALLOWED_CONTENT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/rss+xml",
    "application/atom+xml",
)
_DEFAULT_HEADERS = {
    "User-Agent": "Kivi/0.1 (+local assistant)",
    "Accept": "text/html, text/plain, application/json;q=0.9, */*;q=0.1",
}


def _default_resolver(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
    return socket.getaddrinfo(host, port, **kwargs)


def _pin_url(url: str, address: str) -> str:
    """Replace the host in ``url`` with a resolved IP, keeping path and query."""
    parts = urlsplit(url)
    host = f"[{address}]" if ":" in address else address
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"https://{host}{path}{query}"


class HttpGetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=8, max_length=2048, description="HTTPS URL to fetch.")
    max_bytes: int = Field(default=65536, ge=1, le=1_048_576)
    timeout_s: float = Field(default=20.0, ge=1.0, le=120.0)


class HttpGetTool:
    name = "http.get"
    description = "Fetch a public HTTPS text page; the result is untrusted:web."
    risk = "R1"
    capabilities = ("net.http",)
    args_model = HttpGetArgs

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver | None = None,
        blocked_hosts: tuple[str, ...] | list[str] = (),
    ) -> None:
        self._transport = transport
        self._resolver = resolver or _default_resolver
        self._blocked_hosts = tuple(
            item.strip().lower() for item in blocked_hosts if item.strip()
        )

    def _parse(self, url: str) -> tuple[str, str]:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise ToolError(f"invalid URL: {exc}") from exc
        if parts.scheme != "https":
            raise ToolError("only https URLs are allowed")
        if parts.username or parts.password:
            raise ToolError("credentials in the URL are not allowed")
        host = parts.hostname
        if not host:
            raise ToolError("URL has no host")
        if any(fnmatch.fnmatchcase(host, pattern) for pattern in self._blocked_hosts):
            raise ToolError(f"host is blocked by policy: {host}")
        if port not in (None, 443):
            raise ToolError("only the default HTTPS port is allowed")
        return host, url

    def _resolve_public(self, host: str) -> str:
        try:
            answers = self._resolver(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ToolError(f"cannot resolve {host}: {exc}") from exc
        if not answers:
            raise ToolError(f"cannot resolve {host}")
        chosen: str | None = None
        for answer in answers:
            address = answer[4][0]
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError as exc:
                raise ToolError(f"invalid resolved address: {address}") from exc
            if not parsed.is_global:
                raise ToolError(f"{host} resolves to a non-public address")
            if chosen is None:
                chosen = address
        if chosen is None:
            raise ToolError(f"cannot resolve {host}")
        return chosen

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, HttpGetArgs)
        host, safe_url = self._parse(args.url)
        address = self._resolve_public(host)
        return Preview(
            "http-get",
            {
                "url": safe_url,
                "host": host,
                "address": address,
                "max_bytes": args.max_bytes,
                "timeout_s": args.timeout_s,
            },
            reversible=True,
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, HttpGetArgs)
        host, safe_url = self._parse(args.url)
        address = self._resolve_public(host)
        pinned_url = _pin_url(safe_url, address)
        timeout = min(args.timeout_s, 60.0)
        limit = args.max_bytes
        try:
            with httpx.Client(
                timeout=timeout,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = client.get(
                    pinned_url,
                    headers={**_DEFAULT_HEADERS, "Host": host},
                    extensions={"sni_hostname": host},
                )
                if 300 <= response.status_code < 400:
                    raise ToolError("redirects are not followed")
                if response.status_code >= 400:
                    raise ToolError(f"HTTP {response.status_code}")

                content_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                )
                if not content_type.startswith(_ALLOWED_CONTENT_TYPES):
                    raise ToolError(f"unsupported content type: {content_type or 'unknown'}")

                chunks: list[bytes] = []
                size = 0
                truncated = False
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > limit:
                        chunks.append(chunk[: max(0, limit - (size - len(chunk)))])
                        truncated = True
                        break
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise ToolError(f"network error: {type(exc).__name__}") from exc

        raw = b"".join(chunks)
        body = raw.decode(response.encoding or "utf-8", errors="backslashreplace")
        sanitized = sanitize_text(
            body,
            html=content_type.startswith("text/html"),
            max_chars=ctx.limits.max_output_chars,
        )
        return ToolResult(
            ok=True,
            output=sanitized.text,
            meta={
                "url": safe_url,
                "host": host,
                "address": address,
                "status": response.status_code,
                "content_type": content_type,
                "bytes": len(raw),
                "removed": sanitized.removed,
                "suspicious": list(sanitized.suspicious),
            },
            truncated=truncated or sanitized.truncated,
            trust="untrusted:web",
        )


__all__ = ["HttpGetArgs", "HttpGetTool"]
