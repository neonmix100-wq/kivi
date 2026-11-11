"""Unit tests for the Telegram Bot API client, config and rate limiter.

Every HTTP call goes through ``httpx.MockTransport``. No test uses a real bot
token and no test opens a socket.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from kivi.integrations.telegram import (
    API_BASE,
    CHAT_IDS_ENV,
    TOKEN_ENV,
    RateLimiter,
    RateLimitError,
    TelegramClient,
    TelegramConfig,
    TelegramError,
    TokenBucket,
)

TOKEN = "123456:fake-token-for-tests"
CHAT_ID = 100


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(CHAT_IDS_ENV, raising=False)


class FakeClock:
    """A monotonic fake clock whose ``sleep`` advances time instantly."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def bot_config(**kwargs: object) -> TelegramConfig:
    values: dict[str, object] = {
        "token": TOKEN,
        "allowed_chat_ids": frozenset({CHAT_ID}),
    }
    values.update(kwargs)
    return TelegramConfig(**values)  # type: ignore[arg-type]


def make_client(
    handler,
    config: TelegramConfig | None = None,
) -> TelegramClient:
    return TelegramClient(
        config or bot_config(),
        transport=httpx.MockTransport(handler),
    )


# -- config ------------------------------------------------------------------
def test_token_and_allowlist_come_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, f"  {TOKEN}  ")
    monkeypatch.setenv(CHAT_IDS_ENV, "1, -100200 ,3,")
    config = TelegramConfig()
    assert config.token == TOKEN
    assert config.allowed_chat_ids == frozenset({1, -100200, 3})
    assert config.configured is True
    assert config.api_base == API_BASE


def test_explicit_token_and_allowlist_win_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "999:from-environment")
    monkeypatch.setenv(CHAT_IDS_ENV, "1,2,3")
    config = bot_config(token="777:explicit", allowed_chat_ids=frozenset({42}))
    assert config.token == "777:explicit"
    assert config.allowed_chat_ids == frozenset({42})


def test_empty_allowlist_fails_closed() -> None:
    config = bot_config(allowed_chat_ids=frozenset())
    assert config.allowed_chat_ids == frozenset()
    assert config.allows(CHAT_ID) is False
    assert config.allows(0) is False


def test_explicit_empty_allowlist_does_not_fall_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CHAT_IDS_ENV, "1,2,3")
    config = bot_config(allowed_chat_ids=frozenset())
    assert config.allowed_chat_ids == frozenset()


def test_token_is_absent_from_repr() -> None:
    config = bot_config()
    assert TOKEN not in repr(config)
    assert TOKEN not in str(config)
    assert "allowed_chat_ids" in repr(config)


def test_invalid_token_is_rejected_without_echo() -> None:
    with pytest.raises(ValueError) as excinfo:
        TelegramConfig(token="bad token/with slash")
    assert "bad token" not in str(excinfo.value)


def test_invalid_chat_ids_env_is_rejected_without_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CHAT_IDS_ENV, "1,not-a-number")
    with pytest.raises(ValueError) as excinfo:
        TelegramConfig(token=TOKEN)
    assert "not-a-number" not in str(excinfo.value)


def test_api_base_defaults_to_https_and_strips_slash() -> None:
    assert bot_config().api_base == API_BASE
    assert bot_config(api_base="https://tg.example/").api_base == "https://tg.example"
    with pytest.raises(ValueError):
        bot_config(api_base="http://tg.example")


def test_client_requires_a_token() -> None:
    with pytest.raises(TelegramError) as excinfo:
        TelegramClient(TelegramConfig(token="", allowed_chat_ids=frozenset()))
    assert "not configured" in str(excinfo.value)
    assert TOKEN not in str(excinfo.value)


# -- client: getMe -----------------------------------------------------------
def test_get_me_uses_token_in_path_only() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "id": 42,
                    "is_bot": True,
                    "first_name": "Kivi",
                    "username": "kivi_bot",
                },
            },
        )

    with make_client(handler) as client:
        info = client.get_me()

    assert seen["path"] == f"/bot{TOKEN}/getMe"
    assert seen["authorization"] is None
    assert info.id == 42
    assert info.username == "kivi_bot"
    assert info.first_name == "Kivi"
    assert TOKEN not in json.dumps(info.as_dict())


# -- client: getUpdates ------------------------------------------------------
def test_get_updates_parses_messages_and_skips_other_updates() -> None:
    result = [
        {
            "update_id": 10,
            "message": {
                "message_id": 5,
                "date": 1700000000,
                "from": {"id": 7, "username": "alice"},
                "chat": {"id": CHAT_ID, "type": "private"},
                "text": "hello",
            },
        },
        {
            "update_id": 11,
            "edited_message": {
                "message_id": 6,
                "date": 1700000001,
                "from": {"id": 7, "first_name": "Alice"},
                "chat": {"id": CHAT_ID, "type": "private"},
                "text": "edited",
            },
        },
        {"update_id": 12, "callback_query": {"id": "x"}},
        {"update_id": 13, "message": {"message_id": 7, "chat": {"id": CHAT_ID}}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": result})

    with make_client(handler) as client:
        updates = client.get_updates(limit=20)

    assert [item.update_id for item in updates] == [10, 11, 13]
    assert updates[0].text == "hello"
    assert updates[0].sender == "alice"
    assert updates[1].text == "edited"
    assert updates[1].sender == "Alice"
    assert updates[2].text == ""
    assert updates[2].message_id == 7


def test_get_updates_sends_offset_limit_and_timeout() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": []})

    with make_client(handler) as client:
        assert client.get_updates(offset=9, limit=5, timeout_s=2) == []

    assert captured == {"offset": 9, "limit": 5, "timeout": 2}


@pytest.mark.parametrize("limit", [0, 101, -1])
def test_get_updates_rejects_out_of_range_limit(limit: int) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"ok": True, "result": []})

    with make_client(handler) as client, pytest.raises(ValueError):
        client.get_updates(limit=limit)
    assert called is False


# -- client: sendMessage -----------------------------------------------------
def test_send_message_posts_text_and_parse_mode() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": 9,
                    "date": 1700000002,
                    "chat": {"id": CHAT_ID, "type": "private"},
                },
            },
        )

    with make_client(handler) as client:
        sent = client.send_message(CHAT_ID, "hello", parse_mode="HTML")

    assert captured == {"chat_id": CHAT_ID, "text": "hello", "parse_mode": "HTML"}
    assert sent.message_id == 9
    assert sent.chat_id == CHAT_ID
    assert sent.date == 1700000002


@pytest.mark.parametrize(
    ("chat_id", "text", "parse_mode"),
    [
        (CHAT_ID, "", None),
        (CHAT_ID, "x" * 4097, None),
        (True, "hello", None),
        (CHAT_ID, "hello", "evil"),
    ],
)
def test_send_message_validates_arguments(
    chat_id: int,
    text: str,
    parse_mode: str | None,
) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"ok": True, "result": {}})

    with make_client(handler) as client, pytest.raises(ValueError):
        client.send_message(chat_id, text, parse_mode=parse_mode)
    assert called is False


# -- client: failure modes ---------------------------------------------------
def test_httpx_error_does_not_leak_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    with make_client(handler) as client, pytest.raises(TelegramError) as excinfo:
        client.get_me()

    message = str(excinfo.value)
    assert TOKEN not in message
    assert "transport error" in message
    assert excinfo.value.__cause__ is None


def test_api_rejection_redacts_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "description": f"bad token {TOKEN}"},
        )

    with make_client(handler) as client, pytest.raises(TelegramError) as excinfo:
        client.get_me()

    message = str(excinfo.value)
    assert TOKEN not in message
    assert "[REDACTED]" in message


def test_http_status_error_is_generic() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"upstream down")

    with make_client(handler) as client, pytest.raises(TelegramError) as excinfo:
        client.get_me()

    assert "HTTP 502" in str(excinfo.value)
    assert TOKEN not in str(excinfo.value)


def test_malformed_json_is_generic() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    with make_client(handler) as client, pytest.raises(TelegramError) as excinfo:
        client.get_me()
    assert "malformed JSON" in str(excinfo.value)
    assert TOKEN not in str(excinfo.value)


def test_context_manager_and_close_are_idempotent() -> None:
    client = make_client(lambda request: httpx.Response(200, json={"ok": True, "result": {}}))
    with client:
        assert client.closed is False
    assert client.closed is True
    client.close()
    assert client.closed is True


# -- no file / arbitrary methods --------------------------------------------
def test_no_file_methods_or_arbitrary_api_calls() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"ok": True, "result": {}})

    with make_client(handler) as client:
        for name in ("get_file", "download", "send_document", "send_photo"):
            assert not hasattr(client, name)
        with pytest.raises(TelegramError) as excinfo:
            client._request("getFile", {"file_id": "x"})
    assert "not allowed" in str(excinfo.value)
    assert called is False


# -- logging -----------------------------------------------------------------
def test_httpx_info_log_does_not_contain_token(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "b"}})

    with make_client(handler) as client, caplog.at_level(logging.INFO, logger="httpx"):
        client.get_me()

    assert TOKEN not in caplog.text
    assert "/bot[REDACTED]/getMe" in caplog.text


# -- rate limiting -----------------------------------------------------------
def test_token_bucket_capacity_and_refill() -> None:
    clock = FakeClock()
    bucket = TokenBucket(capacity=3, refill_per_s=1.0, clock=clock)
    assert [bucket.take() for _ in range(3)] == [True, True, True]
    assert bucket.take() is False
    clock.now = 1.0
    assert bucket.take() is True
    assert bucket.tokens == pytest.approx(0.0)


def test_rate_limiter_spaces_one_chat() -> None:
    clock = FakeClock()
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)

    limiter.acquire(CHAT_ID)
    assert clock.sleeps == []

    limiter.acquire(CHAT_ID)
    assert clock.sleeps == [1.0]
    assert clock.now == 1.0

    limiter.acquire(CHAT_ID + 1)
    assert clock.sleeps == [1.0]


def test_rate_limiter_global_limit_fails_fast() -> None:
    clock = FakeClock()
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)

    for chat_id in range(1, 21):
        limiter.acquire(chat_id)
    assert clock.sleeps == []

    with pytest.raises(RateLimitError) as excinfo:
        limiter.acquire(21)
    assert "20" in str(excinfo.value)
    assert "rate limit" in str(excinfo.value)


def test_rate_limiter_refills_after_window() -> None:
    clock = FakeClock()
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    for chat_id in range(1, 21):
        limiter.acquire(chat_id)
    with pytest.raises(RateLimitError):
        limiter.acquire(21)

    clock.now = 60.0
    limiter.acquire(21)
    assert limiter._bucket.tokens <= 20.0
