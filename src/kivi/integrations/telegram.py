"""Telegram Bot API integration: bot-only, allowlisted and rate-limited.

Only the HTTP Bot API is used: a bot token goes into the URL path of a fixed
set of methods (``getMe``, ``getUpdates``, ``sendMessage``). There is no
user-account (MTProto) login and no file upload or download here, so a stolen
token cannot be used by this module to pull files from Telegram.

The token is never part of a repr, a result or an exception message. Even the
URL that ``httpx`` writes at INFO level is scrubbed by a logging filter, because
the Bot API requires the token in the path. HTTP errors are translated into a
generic :class:`TelegramError`; the original exception is detached with
``from None`` so its message (which contains the request URL) cannot leak.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

API_BASE = "https://api.telegram.org"
TOKEN_ENV = "KIVI_TELEGRAM_TOKEN"
CHAT_IDS_ENV = "KIVI_TELEGRAM_CHAT_IDS"
HTTP_TIMEOUT_S = 15.0
MAX_MESSAGE_CHARS = 4096
MAX_UPDATES_LIMIT = 100
DEFAULT_UPDATES_LIMIT = 20
MIN_SEND_INTERVAL_S = 1.0
GLOBAL_SEND_LIMIT = 20
GLOBAL_WINDOW_S = 60.0
ALLOWED_PARSE_MODES: frozenset[str] = frozenset({"HTML", "Markdown", "MarkdownV2"})

_METHODS = frozenset({"getMe", "getUpdates", "sendMessage"})
_MEDIA_KEYS = (
    "animation",
    "audio",
    "document",
    "photo",
    "sticker",
    "video",
    "video_note",
    "voice",
)
_TOKEN_CHARS = re.compile(r"[A-Za-z0-9:_-]{1,256}")
_BOT_URL_TOKEN = re.compile(r"/bot[^/\s\"']+/")


class TelegramError(RuntimeError):
    """A generic Bot API failure with no token or raw URL in its message."""


class RateLimitError(RuntimeError):
    """The local send budget is exhausted; the message was not sent."""


def _scrub_url_token(value: object) -> object:
    """Replace ``/bot<token>/`` inside a log argument, keeping its type if clean."""
    if isinstance(value, str):
        return _BOT_URL_TOKEN.sub("/bot[REDACTED]/", value)
    text = str(value)
    scrubbed = _BOT_URL_TOKEN.sub("/bot[REDACTED]/", text)
    return scrubbed if scrubbed != text else value


class _RedactBotToken(logging.Filter):
    """Remove bot tokens from httpx request log lines (the token is a path)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _BOT_URL_TOKEN.sub("/bot[REDACTED]/", record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_scrub_url_token(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _scrub_url_token(arg) for key, arg in record.args.items()}
        return True


_LOG_FILTER_INSTALLED = False


def _install_log_redaction() -> None:
    """Install the httpx token filter once, lazily, on first client creation."""
    global _LOG_FILTER_INSTALLED
    if _LOG_FILTER_INSTALLED:
        return
    logging.getLogger("httpx").addFilter(_RedactBotToken())
    _LOG_FILTER_INSTALLED = True


def _validate_token(value: str) -> str:
    """Strip and validate a token without ever echoing its value."""
    token = (value or "").strip()
    if not token:
        return ""
    if not _TOKEN_CHARS.fullmatch(token):
        raise ValueError("telegram token contains invalid characters")
    return token


def _validate_api_base(value: str) -> str:
    base = (value or "").strip().rstrip("/")
    if not base.startswith("https://") or any(char.isspace() for char in base):
        raise ValueError("telegram api_base must be an https URL")
    if "?" in base or "#" in base:
        raise ValueError("telegram api_base must not contain a query or fragment")
    return base


def _normalise_chat_ids(values: Iterable[int]) -> frozenset[int]:
    result: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("telegram chat ids must be integers")
        if value == 0:
            raise ValueError("telegram chat id 0 is not valid")
        result.add(value)
    return frozenset(result)


def _parse_chat_ids(raw: str) -> frozenset[int]:
    """Parse ``KIVI_TELEGRAM_CHAT_IDS``; an empty string allows nobody."""
    values: list[int] = []
    for part in (raw or "").split(","):
        item = part.strip()
        if not item:
            continue
        try:
            values.append(int(item))
        except ValueError:
            raise ValueError("telegram chat ids must be comma-separated integers") from None
    return _normalise_chat_ids(values)


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    """Bot credentials plus a fail-closed chat allowlist.

    ``token`` comes from the explicit argument or ``KIVI_TELEGRAM_TOKEN``.
    ``allowed_chat_ids`` comes from the explicit argument or
    ``KIVI_TELEGRAM_CHAT_IDS``; an empty set means nobody is allowed, which is
    the safe default for a fresh install.
    """

    token: str = field(default="", repr=False)
    allowed_chat_ids: frozenset[int] | None = None
    api_base: str = API_BASE

    def __post_init__(self) -> None:
        token = _validate_token(self.token) or _validate_token(os.environ.get(TOKEN_ENV, ""))
        object.__setattr__(self, "token", token)
        source = self.allowed_chat_ids
        if source is None:
            ids = _parse_chat_ids(os.environ.get(CHAT_IDS_ENV, ""))
        else:
            ids = _normalise_chat_ids(source)
        object.__setattr__(self, "allowed_chat_ids", ids)
        object.__setattr__(self, "api_base", _validate_api_base(self.api_base))

    @property
    def configured(self) -> bool:
        """True when a bot token is available."""
        return bool(self.token)

    def allows(self, chat_id: int) -> bool:
        """Return True only for a chat that is explicitly listed."""
        return chat_id in (self.allowed_chat_ids or frozenset())


@dataclass(frozen=True, slots=True)
class BotInfo:
    """The subset of ``getMe`` that is safe to show."""

    id: int
    username: str
    first_name: str = ""
    can_join_groups: bool = False
    can_read_all_group_messages: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "username": self.username,
            "first_name": self.first_name,
            "can_join_groups": self.can_join_groups,
            "can_read_all_group_messages": self.can_read_all_group_messages,
        }


@dataclass(frozen=True, slots=True)
class Update:
    """One incoming message; ``text`` is untrusted and is never logged."""

    update_id: int
    chat_id: int
    message_id: int
    date: int = 0
    sender: str = ""
    text: str = ""
    chat_title: str = ""
    has_media: bool = False


@dataclass(frozen=True, slots=True)
class SentMessage:
    """The delivery receipt of ``sendMessage``."""

    message_id: int
    chat_id: int
    date: int = 0


class TelegramClient:
    """A small synchronous client for the three Bot API methods Kivi needs."""

    def __init__(
        self,
        config: TelegramConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not config.token:
            raise TelegramError("telegram bot token is not configured")
        _install_log_redaction()
        self._config = config
        self._client = httpx.Client(
            timeout=HTTP_TIMEOUT_S,
            transport=transport,
            follow_redirects=False,
        )
        self._closed = False

    # -- lifecycle -------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Close the underlying connection pool; safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        self._client.close()

    def __enter__(self) -> TelegramClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- internals -------------------------------------------------------
    def _url(self, method: str) -> str:
        return f"{self._config.api_base}/bot{self._config.token}/{method}"

    def _redact(self, value: object) -> str:
        text = str(value or "")[:300]
        if self._config.token:
            text = text.replace(self._config.token, "[REDACTED]")
        return _BOT_URL_TOKEN.sub("/bot[REDACTED]/", text)

    def _request(self, method: str, payload: dict[str, Any]) -> Any:
        if method not in _METHODS:
            raise TelegramError(f"telegram method is not allowed: {method}")
        try:
            response = self._client.post(self._url(method), json=payload)
        except httpx.HTTPError as exc:
            # Never format the httpx exception: its text contains the request URL.
            raise TelegramError(
                f"telegram {method} failed: transport error ({type(exc).__name__})"
            ) from None
        if response.status_code != 200:
            raise TelegramError(f"telegram {method} failed: HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise TelegramError(f"telegram {method} returned malformed JSON") from None
        if not isinstance(data, dict):
            raise TelegramError(f"telegram {method} returned an unexpected response")
        if data.get("ok") is not True:
            description = self._redact(data.get("description", ""))
            suffix = f": {description}" if description else ""
            raise TelegramError(f"telegram {method} was rejected{suffix}")
        return data.get("result")

    @staticmethod
    def _parse_update(raw: object) -> Update | None:
        if not isinstance(raw, dict):
            return None
        update_id = raw.get("update_id")
        message = raw.get("message") or raw.get("edited_message")
        if not isinstance(update_id, int) or not isinstance(message, dict):
            return None
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return None
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return None
        sender_obj = message.get("from")
        sender = ""
        if isinstance(sender_obj, dict):
            sender = str(
                sender_obj.get("username")
                or sender_obj.get("first_name")
                or sender_obj.get("id")
                or ""
            )
        text = message.get("text") or message.get("caption") or ""
        if not isinstance(text, str):
            text = str(text)
        raw_date = message.get("date")
        raw_message_id = message.get("message_id")
        return Update(
            update_id=update_id,
            chat_id=chat_id,
            message_id=raw_message_id if isinstance(raw_message_id, int) else 0,
            date=raw_date if isinstance(raw_date, int) else 0,
            sender=sender,
            text=text,
            chat_title=str(chat.get("title") or chat.get("username") or ""),
            has_media=any(key in message for key in _MEDIA_KEYS),
        )

    # -- public API ------------------------------------------------------
    def get_me(self) -> BotInfo:
        """Return the bot identity; the token is not part of the result."""
        result = self._request("getMe", {})
        if not isinstance(result, dict):
            raise TelegramError("telegram getMe returned an unexpected payload")
        raw_id = result.get("id")
        username = result.get("username")
        if not isinstance(raw_id, int) or not isinstance(username, str):
            raise TelegramError("telegram getMe returned an unexpected payload")
        return BotInfo(
            id=raw_id,
            username=username,
            first_name=str(result.get("first_name") or ""),
            can_join_groups=bool(result.get("can_join_groups", False)),
            can_read_all_group_messages=bool(result.get("can_read_all_group_messages", False)),
        )

    def get_updates(
        self,
        offset: int | None = None,
        limit: int = DEFAULT_UPDATES_LIMIT,
        timeout_s: float = 0,
    ) -> list[Update]:
        """Fetch incoming message updates; non-message updates are skipped."""
        if offset is not None and offset < 0:
            raise ValueError("updates offset must not be negative")
        if not 1 <= limit <= MAX_UPDATES_LIMIT:
            raise ValueError(f"updates limit must be between 1 and {MAX_UPDATES_LIMIT}")
        if not 0 <= timeout_s <= 10:
            raise ValueError("long-poll timeout must be between 0 and 10 seconds")
        payload: dict[str, Any] = {"limit": limit, "timeout": int(timeout_s)}
        if offset is not None:
            payload["offset"] = offset
        result = self._request("getUpdates", payload)
        if not isinstance(result, list):
            raise TelegramError("telegram getUpdates returned an unexpected payload")
        updates: list[Update] = []
        for item in result:
            parsed = self._parse_update(item)
            if parsed is not None:
                updates.append(parsed)
        return updates

    def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
    ) -> SentMessage:
        """Send one text message; the chat must already be allowed by policy."""
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise ValueError("chat_id must be an integer")
        if not 1 <= len(text) <= MAX_MESSAGE_CHARS:
            raise ValueError(f"text must be between 1 and {MAX_MESSAGE_CHARS} characters")
        if parse_mode is not None and parse_mode not in ALLOWED_PARSE_MODES:
            raise ValueError("unsupported Telegram parse_mode")
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        result = self._request("sendMessage", payload)
        if not isinstance(result, dict):
            raise TelegramError("telegram sendMessage returned an unexpected payload")
        chat = result.get("chat")
        message_id = result.get("message_id")
        if not isinstance(chat, dict) or not isinstance(chat.get("id"), int):
            raise TelegramError("telegram sendMessage returned an unexpected payload")
        if not isinstance(message_id, int):
            raise TelegramError("telegram sendMessage returned an unexpected payload")
        raw_date = result.get("date")
        return SentMessage(
            message_id=message_id,
            chat_id=chat["id"],
            date=raw_date if isinstance(raw_date, int) else 0,
        )


@dataclass(slots=True)
class TokenBucket:
    """A monotonic-clock token bucket with injectable clock and refill rate."""

    capacity: float = float(GLOBAL_SEND_LIMIT)
    refill_per_s: float = GLOBAL_SEND_LIMIT / GLOBAL_WINDOW_S
    clock: Callable[[], float] = time.monotonic
    _tokens: float = field(init=False, repr=False)
    _updated: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("token bucket capacity must be positive")
        if self.refill_per_s <= 0:
            raise ValueError("token bucket refill rate must be positive")
        self._tokens = self.capacity
        self._updated = self.clock()

    def _refill(self) -> None:
        now = self.clock()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_s)
            self._updated = now

    @property
    def tokens(self) -> float:
        """Currently available tokens after refilling at the injected clock."""
        self._refill()
        return self._tokens

    def take(self, amount: float = 1.0) -> bool:
        """Consume ``amount`` tokens when available; never blocks."""
        if amount <= 0:
            raise ValueError("token amount must be positive")
        self._refill()
        if self._tokens + 1e-9 < amount:
            return False
        self._tokens -= amount
        return True


@dataclass(slots=True)
class RateLimiter:
    """Per-chat spacing plus a global token budget for outgoing messages.

    ``min_interval_s`` is enforced by sleeping; the global budget fails fast
    with :class:`RateLimitError` rather than queueing messages. Both the clock
    and the sleep function are injectable so tests never wait in real time.
    """

    min_interval_s: float = MIN_SEND_INTERVAL_S
    global_limit: int = GLOBAL_SEND_LIMIT
    global_window_s: float = GLOBAL_WINDOW_S
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _last: dict[int, float] = field(default_factory=dict, init=False, repr=False)
    _bucket: TokenBucket = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.min_interval_s < 0:
            raise ValueError("min_interval_s must not be negative")
        if self.global_limit <= 0:
            raise ValueError("global_limit must be positive")
        if self.global_window_s <= 0:
            raise ValueError("global_window_s must be positive")
        self._bucket = TokenBucket(
            capacity=float(self.global_limit),
            refill_per_s=self.global_limit / self.global_window_s,
            clock=self.clock,
        )

    def acquire(self, chat_id: int) -> None:
        """Wait for the chat interval, then reserve one global token."""
        now = self.clock()
        last = self._last.get(chat_id)
        if last is not None:
            wait = self.min_interval_s - (now - last)
            if wait > 0:
                self.sleep(wait)
                now = self.clock()
        if not self._bucket.take():
            raise RateLimitError(
                f"global Telegram rate limit reached "
                f"({self.global_limit} messages/{self.global_window_s:g}s)"
            )
        self._last[chat_id] = now


__all__ = [
    "ALLOWED_PARSE_MODES",
    "API_BASE",
    "BotInfo",
    "CHAT_IDS_ENV",
    "DEFAULT_UPDATES_LIMIT",
    "GLOBAL_SEND_LIMIT",
    "GLOBAL_WINDOW_S",
    "HTTP_TIMEOUT_S",
    "MAX_MESSAGE_CHARS",
    "MAX_UPDATES_LIMIT",
    "MIN_SEND_INTERVAL_S",
    "RateLimitError",
    "RateLimiter",
    "SentMessage",
    "TOKEN_ENV",
    "TelegramClient",
    "TelegramConfig",
    "TelegramError",
    "TokenBucket",
    "Update",
]
