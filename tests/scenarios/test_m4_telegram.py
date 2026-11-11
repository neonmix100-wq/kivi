from __future__ import annotations

from pathlib import Path
from typing import Any

from kivi.integrations.telegram import BotInfo, SentMessage, TelegramConfig, Update
from kivi.kernel.db import Database
from kivi.safety.permissions import PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools import ToolExecutor, ToolRegistry
from kivi.tools.telegram import build_telegram_tools


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.closed = 0
        self.me = BotInfo(42, "kivi_bot")
        self.updates = [Update(1, 100, 7, text="привет")]

    def get_me(self) -> BotInfo:
        return self.me

    def get_updates(self, offset=None, limit=20, timeout_s=0):
        return list(self.updates)

    def send_message(self, chat_id: int, text: str, parse_mode=None) -> SentMessage:
        self.sent.append((chat_id, text))
        return SentMessage(99, chat_id)

    def close(self) -> None:
        self.closed += 1


def make_executor(tmp_path: Path, fake: FakeTelegram):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = Database(tmp_path / "kivi.db")
    engine = PermissionEngine(db, workspace=str(workspace))
    config = TelegramConfig(token="test-token", allowed_chat_ids=frozenset({100}))
    registry = ToolRegistry()
    for tool in build_telegram_tools(config=config, client_factory=lambda: fake):
        registry.register(tool)
    executor = ToolExecutor(
        registry,
        db,
        engine,
        ProtectedZones(workspace),
        workspace=workspace,
    )
    return executor, db, engine.issue("tg-scenario", ["telegram.read", "telegram.send"])


def run(tmp_path: Path, name: str, args: dict[str, Any], *, approved=False):
    fake = FakeTelegram()
    executor, db, grant = make_executor(tmp_path, fake)
    try:
        result = executor.run(name, args, grant=grant, approved=approved)
        return result, fake
    finally:
        db.close()


def test_telegram_01_status(tmp_path: Path) -> None:
    result, _ = run(tmp_path, "telegram.status", {})
    assert result.ok
    assert "kivi_bot" in result.output


def test_telegram_02_updates(tmp_path: Path) -> None:
    result, _ = run(tmp_path, "telegram.updates", {"limit": 5})
    assert result.ok
    assert "привет" in result.output


def test_telegram_03_send_allowlisted(tmp_path: Path) -> None:
    result, fake = run(
        tmp_path, "telegram.send", {"chat_id": 100, "text": "готово"}, approved=True
    )
    assert result.ok
    assert fake.sent == [(100, "готово")]


def test_telegram_04_send_rejects_unknown_chat(tmp_path: Path) -> None:
    result, fake = run(
        tmp_path, "telegram.send", {"chat_id": 999, "text": "нет"}, approved=True
    )
    assert not result.ok
    assert fake.sent == []


def test_telegram_05_send_rejects_secret(tmp_path: Path) -> None:
    result, fake = run(
        tmp_path,
        "telegram.send",
        {"chat_id": 100, "text": "sk-canary12345678901234567890"},
        approved=True,
    )
    assert not result.ok
    assert fake.sent == []
