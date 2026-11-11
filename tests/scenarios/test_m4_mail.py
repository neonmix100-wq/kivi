from __future__ import annotations

from pathlib import Path
from typing import Any

from kivi.integrations.gmail import (
    AttachmentInfo,
    DraftInfo,
    GmailClient,
    Message,
    MessageSummary,
)
from kivi.kernel.db import Database
from kivi.safety.permissions import PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools import ToolExecutor, ToolRegistry
from kivi.tools.mail import build_mail_tools


class FakeGmail:
    def __init__(self) -> None:
        self.summary = MessageSummary(
            id="m1",
            subject="Договор",
            from_="sender@example.com",
            snippet="текст письма",
        )
        self.message = Message(
            id="m1",
            subject="Договор",
            from_="sender@example.com",
            body="тело письма sk-canary123456789012345",
            attachments=(AttachmentInfo("dogovor.pdf", "application/pdf", 10, "a1"),),
        )
        self.drafts: list[tuple[tuple[str, ...], str, str]] = []
        self.attachments: list[tuple[str, str, Path]] = []
        self.calls: list[tuple[Any, ...]] = []

    def list_messages(self, query="", max_results=10, label_ids=None):
        self.calls.append(("list", query, max_results))
        return [self.summary]

    def search(self, query, max_results=10):
        self.calls.append(("search", query, max_results))
        return [self.summary]

    def get_message(self, message_id):
        self.calls.append(("get", message_id))
        return self.message

    def create_draft(self, to, subject, body, thread_id=None):
        self.calls.append(("draft", tuple(to), subject, body))
        self.drafts.append((tuple(to), subject, body))
        return DraftInfo("d1", "m2", "", tuple(to), subject, len(body))

    def save_attachment(self, message_id, attachment_id, target_dir):
        self.calls.append(("attachment", message_id, attachment_id))
        target = Path(target_dir) / "dogovor.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"pdf")
        return target


def make_executor(tmp_path: Path, fake: FakeGmail):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = Database(tmp_path / "kivi.db")
    engine = PermissionEngine(db, workspace=str(workspace))
    client = GmailClient(service=object())
    client.list_messages = fake.list_messages  # type: ignore[method-assign]
    client.search = fake.search  # type: ignore[method-assign]
    client.get_message = fake.get_message  # type: ignore[method-assign]
    client.create_draft = fake.create_draft  # type: ignore[method-assign]
    client.save_attachment = fake.save_attachment  # type: ignore[method-assign]
    registry = ToolRegistry()
    for tool in build_mail_tools(client_factory=lambda: client):
        registry.register(tool)
    executor = ToolExecutor(
        registry,
        db,
        engine,
        ProtectedZones(workspace),
        workspace=workspace,
    )
    return executor, db, engine.issue("mail-scenario", ["mail.read", "mail.draft"])


def run(tmp_path: Path, name: str, args: dict[str, Any], *, approved=False):
    fake = FakeGmail()
    executor, db, grant = make_executor(tmp_path, fake)
    try:
        result = executor.run(name, args, grant=grant, approved=approved)
        return result, fake
    finally:
        db.close()


def test_mail_01_list(tmp_path: Path) -> None:
    result, fake = run(tmp_path, "mail.list", {"max_results": 5})
    assert result.ok
    assert "Договор" in result.output
    assert fake.calls[0][0] == "list"


def test_mail_02_search(tmp_path: Path) -> None:
    result, fake = run(tmp_path, "mail.search", {"query": "договор", "max_results": 5})
    assert result.ok
    assert fake.calls[0] == ("search", "договор", 5)


def test_mail_03_read(tmp_path: Path) -> None:
    result, fake = run(tmp_path, "mail.read", {"message_id": "m1"})
    assert result.ok
    assert "тело письма" in result.output
    assert fake.calls[0] == ("get", "m1")


def test_mail_04_read_attachment_metadata(tmp_path: Path) -> None:
    result, _ = run(tmp_path, "mail.read", {"message_id": "m1"})
    assert "dogovor.pdf" in result.output


def test_mail_05_draft_approved(tmp_path: Path) -> None:
    result, fake = run(
        tmp_path,
        "mail.draft",
        {"to": ["a@example.com"], "subject": "Тема", "body": "Текст"},
        approved=True,
    )
    assert result.ok
    assert fake.drafts
    assert fake.drafts[0][0] == ("a@example.com",)
    assert fake.drafts[0][1] == "Тема"


def test_mail_06_draft_requires_approval(tmp_path: Path) -> None:
    result, fake = run(
        tmp_path,
        "mail.draft",
        {"to": ["a@example.com"], "subject": "Тема", "body": "Текст"},
    )
    assert not result.ok
    assert fake.drafts == []


def test_mail_07_attachment_saves(tmp_path: Path) -> None:
    result, fake = run(
        tmp_path,
        "mail.attachment",
        {"message_id": "m1", "attachment_id": "a1"},
    )
    assert result.ok
    assert ("attachment", "m1", "a1") in fake.calls


def test_mail_08_no_send_method(tmp_path: Path) -> None:
    _result, fake = run(tmp_path, "mail.list", {})
    assert not any(call[0] == "send" for call in fake.calls)


def test_mail_09_invalid_recipient(tmp_path: Path) -> None:
    result, _ = run(
        tmp_path,
        "mail.draft",
        {"to": ["not-an-email"], "subject": "Тема", "body": "Текст"},
        approved=True,
    )
    assert not result.ok


def test_mail_10_secret_body_is_redacted(tmp_path: Path) -> None:
    result, _ = run(tmp_path, "mail.read", {"message_id": "m1"})
    assert result.trust == "untrusted:email"
    assert "sk-" not in result.output
