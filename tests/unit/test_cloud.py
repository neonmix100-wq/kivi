from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from kivi.cloud.broker import (
    BudgetExceeded,
    CloudBroker,
    CloudDisabled,
    DataPolicyError,
    ProviderUnavailable,
    QuotaExhausted,
    build_handoff,
)
from kivi.cloud.providers import CloudProvider, CloudRegistry
from kivi.config import CloudSettings
from kivi.kernel.db import Database

DAY = "2026-11-10"


def make_provider(**overrides) -> CloudProvider:
    values = {
        "id": "groq",
        "kind": "openai",
        "base_url": "https://example.test/v1",
        "api_key_env": "TEST_CLOUD_KEY",
        "default_model": "test-model",
        "free_requests_per_day": 10,
        "free_tokens_per_day": None,
        "allows_private_data": False,
    }
    values.update(overrides)
    return CloudProvider(**values)


def make_broker(tmp_path: Path, handler, **settings) -> tuple[CloudBroker, Database]:
    db = Database(tmp_path / "kivi.db")
    provider = make_provider()
    broker = CloudBroker(
        db,
        CloudSettings(**settings),
        CloudRegistry({provider.id: provider}),
        transport=httpx.MockTransport(handler),
        today=lambda: DAY,
    )
    return broker, db


def success_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["Authorization"] == "Bearer test-secret"
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "hello from cloud"}}],
            "usage": {"total_tokens": 12},
        },
    )


def test_secret_is_redacted_before_the_cloud_call(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_CLOUD_KEY", "test-secret")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["content"] = body["messages"][0]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"total_tokens": 1},
            },
        )

    broker, db = make_broker(tmp_path, handler)
    try:
        broker.complete(
            "groq",
            [{"role": "user", "content": "token sk-canary12345678901234567890"}],
            data_class="public",
        )
        audit = db.recent_audit()
    finally:
        broker.close()
        db.close()
    assert "sk-canary" not in seen["content"]
    assert "[REDACTED]" in seen["content"]
    assert any(row["reason"] == "secrets redacted" for row in audit)


def test_public_call_is_recorded_and_audited(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_CLOUD_KEY", "test-secret")
    broker, db = make_broker(tmp_path, success_handler)
    try:
        result = broker.complete(
            "groq",
            [{"role": "user", "content": "hi"}],
            data_class="public",
        )
    finally:
        broker.close()
        db.close()
    assert result.text == "hello from cloud"
    assert result.tokens == 12
    assert result.provider == "groq"
    with Database(tmp_path / "kivi.db") as reopened:
        assert reopened.cloud_usage("groq", DAY)["requests"] == 1
        assert reopened.recent_audit()[0]["decision"] == "allow"


def test_private_data_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_CLOUD_KEY", "test-secret")
    broker, db = make_broker(tmp_path, success_handler)
    try:
        with pytest.raises(DataPolicyError, match="private"):
            broker.complete(
                "groq",
                [{"role": "user", "content": "secret"}],
                data_class="private",
            )
    finally:
        broker.close()
        db.close()


def test_missing_key_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TEST_CLOUD_KEY", raising=False)
    broker, db = make_broker(tmp_path, success_handler)
    try:
        with pytest.raises(ProviderUnavailable, match="TEST_CLOUD_KEY"):
            broker.complete(
                "groq",
                [{"role": "user", "content": "hi"}],
                data_class="public",
            )
    finally:
        broker.close()
        db.close()


def test_cost_limit_is_enforced(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_CLOUD_KEY", "test-secret")
    broker, db = make_broker(tmp_path, success_handler, cost_limit_usd=0.0)
    try:
        with pytest.raises(BudgetExceeded, match="exceeds"):
            broker.complete(
                "groq",
                [{"role": "user", "content": "hi"}],
                data_class="public",
                estimated_cost_usd=0.5,
            )
    finally:
        broker.close()
        db.close()


def test_daily_quota_is_enforced(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_CLOUD_KEY", "test-secret")
    broker, db = make_broker(tmp_path, success_handler)
    db.add_cloud_usage("groq", DAY, requests=10)
    try:
        with pytest.raises(QuotaExhausted, match="quota exhausted"):
            broker.complete(
                "groq",
                [{"role": "user", "content": "hi"}],
                data_class="public",
            )
    finally:
        broker.close()
        db.close()


def test_http_429_counts_as_quota_exhausted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_CLOUD_KEY", "test-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    broker, db = make_broker(tmp_path, handler)
    try:
        with pytest.raises(QuotaExhausted):
            broker.complete(
                "groq",
                [{"role": "user", "content": "hi"}],
                data_class="public",
            )
    finally:
        broker.close()
        db.close()
    with Database(tmp_path / "kivi.db") as reopened:
        assert reopened.cloud_usage("groq", DAY)["requests"] == 1


def test_off_mode_refuses_everything(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_CLOUD_KEY", "test-secret")
    broker, db = make_broker(tmp_path, success_handler, mode="off")
    try:
        with pytest.raises(CloudDisabled):
            broker.complete(
                "groq",
                [{"role": "user", "content": "hi"}],
                data_class="public",
            )
    finally:
        broker.close()
        db.close()


def test_status_never_exposes_the_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_CLOUD_KEY", "test-secret")
    broker, db = make_broker(tmp_path, success_handler)
    try:
        status = broker.status()[0]
    finally:
        broker.close()
        db.close()
    assert status.key_configured is True
    assert "test-secret" not in repr(status)


def test_handoff_pack_keeps_context() -> None:
    pack = build_handoff(
        "finish the report",
        "quota exhausted",
        plan=["read draft", "write final"],
        artifacts=["data/report.md"],
    )
    prompt = pack.to_prompt()
    assert "finish the report" in prompt
    assert "quota exhausted" in prompt
    assert "read draft" in prompt
    assert "data/report.md" in prompt
