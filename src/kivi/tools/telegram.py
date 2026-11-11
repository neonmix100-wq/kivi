"""Telegram tools: status, incoming updates and an allowlisted send.

The send tool is the only outbound path. It checks the chat allowlist, length
and secret-looking content *before* it reserves a rate-limit token, so a denied
call never consumes the budget. There is no broadcast, no media argument and no
"call any Bot API method" escape hatch.

Incoming message text is untrusted: it is sanitised, secret tokens are redacted,
and the result carries ``untrusted:message``. Message content is never logged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kivi.integrations.telegram import (
    MAX_MESSAGE_CHARS,
    RateLimiter,
    RateLimitError,
    TelegramClient,
    TelegramConfig,
    TelegramError,
)
from kivi.safety.sanitize import redact_secrets, sanitize_text
from kivi.tools.base import Preview, ToolContext, ToolError, ToolResult

ClientFactory = Callable[[], TelegramClient]

_MAX_UPDATE_TEXT_CHARS = 2000
_MAX_PREVIEW_CHARS = 120

_SECRETISH = re.compile(
    r"\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|bearer)\b"
    r"\s*[:=]\s*\S{4,}",
    re.IGNORECASE,
)


def contains_secret_like(text: str) -> bool:
    """True when ``text`` looks like a token, key or credential assignment."""
    _, count = redact_secrets(text)
    return count > 0 or bool(_SECRETISH.search(text))


def _limit_json(payload: Any, max_chars: int) -> tuple[str, bool]:
    """Serialise a payload and cap it at the tool output budget."""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip(), True


class TelegramRuntime:
    """Shared config, lazy client and one rate limiter for all Telegram tools."""

    def __init__(
        self,
        config: TelegramConfig,
        *,
        client_factory: ClientFactory | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.config = config
        self.limiter = limiter or RateLimiter()
        self._factory = client_factory
        self._client: TelegramClient | None = None
        self._closed = False

    def ensure_configured(self) -> None:
        """Fail before any network access when the bot token is missing."""
        if not self.config.configured:
            raise ToolError(
                "telegram bot token is not configured "
                "(set KIVI_TELEGRAM_TOKEN or pass TelegramConfig.token)"
            )

    def client(self) -> TelegramClient:
        """Create the client once so rate-limit and connection state persist."""
        if self._closed:
            raise ToolError("telegram client is closed")
        if self._client is None:
            self._client = self._factory() if self._factory else TelegramClient(self.config)
        return self._client

    def close(self) -> None:
        """Close the shared client; idempotent and safe before first use."""
        if self._closed:
            return
        self._closed = True
        if self._client is not None:
            self._client.close()
            self._client = None


class TelegramStatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TelegramUpdatesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=20, ge=1, le=50, description="Updates per call, 1..50.")
    offset: int | None = Field(default=None, ge=0, description="First update_id to return.")
    timeout_s: float = Field(
        default=0.0,
        ge=0.0,
        le=10.0,
        description="Long-poll timeout in seconds; 0 returns immediately.",
    )


class TelegramSendArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: int = Field(description="Chat id; it must be in the allowlist.")
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    parse_mode: Literal["HTML", "Markdown", "MarkdownV2"] | None = Field(
        default=None,
        description="Optional Telegram formatting mode.",
    )


class TelegramStatusTool:
    name = "telegram.status"
    description = "Show the bot identity via getMe; the token is never returned."
    risk = "R0"
    capabilities = ("telegram.read",)
    args_model = TelegramStatusArgs

    def __init__(self, runtime: TelegramRuntime) -> None:
        self.runtime = runtime

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, TelegramStatusArgs)
        self.runtime.ensure_configured()
        return Preview(
            "telegram-get-me",
            {"api_base": self.runtime.config.api_base, "network": True},
            reversible=True,
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, TelegramStatusArgs)
        self.runtime.ensure_configured()
        try:
            info = self.runtime.client().get_me()
        except TelegramError as exc:
            raise ToolError(str(exc)) from None
        output, truncated = _limit_json(info.as_dict(), ctx.limits.max_output_chars)
        return ToolResult(
            ok=True,
            output=output,
            meta={
                "api_base": self.runtime.config.api_base,
                "id": info.id,
                "username": info.username,
            },
            truncated=truncated,
        )


class TelegramUpdatesTool:
    name = "telegram.updates"
    description = "Read incoming messages; text is untrusted:message and never logged."
    risk = "R0"
    capabilities = ("telegram.read",)
    args_model = TelegramUpdatesArgs

    def __init__(self, runtime: TelegramRuntime) -> None:
        self.runtime = runtime

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, TelegramUpdatesArgs)
        self.runtime.ensure_configured()
        return Preview(
            "telegram-get-updates",
            {
                "limit": args.limit,
                "offset": args.offset,
                "timeout_s": args.timeout_s,
                "network": True,
            },
            reversible=True,
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, TelegramUpdatesArgs)
        self.runtime.ensure_configured()
        try:
            updates = self.runtime.client().get_updates(
                offset=args.offset,
                limit=args.limit,
                timeout_s=args.timeout_s,
            )
        except TelegramError as exc:
            raise ToolError(str(exc)) from None

        items: list[dict[str, Any]] = []
        content_truncated = False
        for update in updates:
            sanitized = sanitize_text(update.text, max_chars=_MAX_UPDATE_TEXT_CHARS)
            content_truncated = content_truncated or sanitized.truncated
            items.append(
                {
                    "update_id": update.update_id,
                    "chat_id": update.chat_id,
                    "message_id": update.message_id,
                    "date": update.date,
                    "sender": update.sender,
                    "chat_title": update.chat_title,
                    "has_media": update.has_media,
                    "text": sanitized.text,
                    "suspicious": list(sanitized.suspicious),
                    "removed": sanitized.removed,
                }
            )
        next_offset = max((item["update_id"] for item in items), default=args.offset)
        if items and isinstance(next_offset, int):
            next_offset += 1
        payload = {
            "count": len(items),
            "next_offset": next_offset,
            "updates": items,
        }
        output, truncated = _limit_json(payload, ctx.limits.max_output_chars)
        return ToolResult(
            ok=True,
            output=output,
            meta={
                "count": len(items),
                "next_offset": next_offset,
                "has_media": any(item["has_media"] for item in items),
            },
            truncated=truncated or content_truncated,
            trust="untrusted:message",
        )


class TelegramSendTool:
    name = "telegram.send"
    description = "Send one text message to an allowlisted chat; secrets are refused."
    risk = "R2"
    capabilities = ("telegram.send",)
    args_model = TelegramSendArgs

    def __init__(self, runtime: TelegramRuntime) -> None:
        self.runtime = runtime

    def _validate_target(self, chat_id: int) -> None:
        if chat_id == 0:
            raise ToolError("chat_id 0 is not a valid Telegram chat")
        if not self.runtime.config.allows(chat_id):
            raise ToolError(f"chat {chat_id} is not in the Telegram allowlist")

    @staticmethod
    def _validate_text(text: str) -> None:
        if not 1 <= len(text) <= MAX_MESSAGE_CHARS:
            raise ToolError(
                f"telegram text must be between 1 and {MAX_MESSAGE_CHARS} characters"
            )
        if contains_secret_like(text):
            raise ToolError("refusing to send secret-looking text to Telegram")

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, TelegramSendArgs)
        self.runtime.ensure_configured()
        self._validate_target(args.chat_id)
        self._validate_text(args.text)
        redacted, _ = redact_secrets(args.text)
        preview = redacted[:_MAX_PREVIEW_CHARS]
        return Preview(
            "telegram-send",
            {
                "chat_id": args.chat_id,
                "text_chars": len(args.text),
                "text_preview": preview,
                "parse_mode": args.parse_mode,
                "network": True,
            },
            reversible=False,
            warnings=("the message is delivered immediately and cannot be unsent",),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, TelegramSendArgs)
        self.runtime.ensure_configured()
        self._validate_target(args.chat_id)
        self._validate_text(args.text)
        try:
            self.runtime.limiter.acquire(args.chat_id)
        except RateLimitError as exc:
            raise ToolError(str(exc)) from None
        try:
            sent = self.runtime.client().send_message(
                args.chat_id,
                args.text,
                parse_mode=args.parse_mode,
            )
        except TelegramError as exc:
            raise ToolError(str(exc)) from None
        return ToolResult(
            ok=True,
            output=f"sent message {sent.message_id} to chat {sent.chat_id}",
            meta={
                "chat_id": sent.chat_id,
                "message_id": sent.message_id,
                "date": sent.date,
                "chars": len(args.text),
                "parse_mode": args.parse_mode,
            },
            trust="trusted:system",
        )


def build_telegram_tools(
    *,
    config: TelegramConfig | None = None,
    client_factory: ClientFactory | None = None,
    limiter: RateLimiter | None = None,
) -> tuple[TelegramStatusTool, TelegramUpdatesTool, TelegramSendTool]:
    """Create the three Telegram tools sharing one runtime and limiter.

    ``client_factory`` lets tests supply :class:`TelegramClient` with an
    ``httpx.MockTransport``; production callers omit it and the client is built
    lazily from ``config``. Call ``runtime.close()`` on shutdown.
    """
    cfg = config if config is not None else TelegramConfig()
    runtime = TelegramRuntime(cfg, client_factory=client_factory, limiter=limiter)
    return (
        TelegramStatusTool(runtime),
        TelegramUpdatesTool(runtime),
        TelegramSendTool(runtime),
    )


__all__ = [
    "ClientFactory",
    "TelegramRuntime",
    "TelegramSendArgs",
    "TelegramSendTool",
    "TelegramStatusArgs",
    "TelegramStatusTool",
    "TelegramUpdatesArgs",
    "TelegramUpdatesTool",
    "build_telegram_tools",
    "contains_secret_like",
]
