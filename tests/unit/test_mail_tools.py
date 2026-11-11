"""Unit tests for the mail tools: risks, approvals, trust and quarantine.

The tests wire the real :class:`~kivi.tools.executor.ToolExecutor` with the real
:class:`~kivi.integrations.gmail.GmailClient` on top of a dict-backed fake
service, so permission checks, audit rows and tool behaviour are exercised
together without OAuth or the network.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from kivi.integrations.gmail import (
    MAX_ATTACHMENT_BYTES,
    GmailAuthError,
    GmailClient,
)
from kivi.kernel.db import Database
from kivi.safety.permissions import PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools import ToolExecutor, ToolRegistry
from kivi.tools.base import ToolResult
from kivi.tools.mail import (
    ATTACHMENTS_DIR,
    TRUST,
    MailListTool,
    build_mail_tools,
)


# -- fake Gmail service -------------------------------------------------
class _Request:
    def __init__(self, callback: Any) -> None:
        self._callback = callback

    def execute(self) -> Any:
        return self._callback()


class _FakeAttachments:
    def __init__(self, root: FakeMailService) -> None:
        self._root = root

    def get(self, **params: Any) -> _Request:
        return self._root.request("attachments.get", params, self._root.get_attachment)


class _FakeMessages:
    def __init__(self, root: FakeMailService) -> None:
        self._root = root

    def list(self, **params: Any) -> _Request:
        return self._root.request("messages.list", params, self._root.list_messages)

    def get(self, **params: Any) -> _Request:
        return self._root.request("messages.get", params, self._root.get_message)

    def attachments(self) -> _FakeAttachments:
        return _FakeAttachments(self._root)

    def send(self, **params: Any) -> None:
        self._root.calls.append(("send", dict(params)))
        raise AssertionError("send must never be called")


class _FakeDrafts:
    def __init__(self, root: FakeMailService) -> None:
        self._root = root

    def create(self, **params: Any) -> _Request:
        return self._root.request("drafts.create", params, self._root.create_draft)

    def send(self, **params: Any) -> None:
        self._root.calls.append(("send", dict(params)))
        raise AssertionError("send must never be called")


class FakeMailService:
    """Dict-backed fake of the Gmail v1 service used by the mail tools."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}
        self.attachment_payloads: dict[str, dict[str, Any]] = {}
        self.draft_payloads: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def users(self) -> FakeMailService:
        return self

    def messages(self) -> _FakeMessages:
        return _FakeMessages(self)

    def drafts(self) -> _FakeDrafts:
        return _FakeDrafts(self)

    def request(self, name: str, params: dict[str, Any], handler: Any) -> _Request:
        self.calls.append((name, dict(params)))
        return _Request(lambda: handler(params))

    def list_messages(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = int(params.get("maxResults") or 100)
        found = [
            {"id": message_id, "threadId": raw.get("threadId", "")}
            for message_id, raw in self.store.items()
        ][:limit]
        return {"messages": found, "resultSizeEstimate": len(found)}

    def get_message(self, params: dict[str, Any]) -> dict[str, Any]:
        raw = self.store.get(str(params.get("id")))
        if raw is None:
            raise RuntimeError("HttpError 404: requested entity was not found")
        return raw

    def create_draft(self, params: dict[str, Any]) -> dict[str, Any]:
        payload = params.get("body") or {}
        message = payload.get("message") or {}
        if "raw" not in message:
            raise ValueError("draft payload has no raw MIME message")
        self.draft_payloads.append(payload)
        number = len(self.draft_payloads)
        return {
            "id": f"draft-{number}",
            "message": {
                "id": f"msg-draft-{number}",
                "threadId": message.get("threadId", "thread-1"),
            },
        }

    def get_attachment(self, params: dict[str, Any]) -> dict[str, Any]:
        item = self.attachment_payloads.get(str(params.get("id")))
        if item is None:
            raise RuntimeError("HttpError 404: attachment was not found")
        return {"data": item["data"], "size": item["size"]}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def gmail_message(
    message_id: str = "m1",
    *,
    subject: str = "Hello",
    sender: str = "Alice <alice@example.com>",
    body: str = "hello",
    html: str | None = None,
    labels: tuple[str, ...] = ("INBOX",),
    attachments: tuple[tuple[str, str, bytes, str], ...] = (),
    attachment_size: int | None = None,
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    if body:
        parts.append(
            {
                "mimeType": "text/plain",
                "body": {"size": len(body), "data": b64(body.encode("utf-8"))},
            }
        )
    if html is not None:
        parts.append(
            {
                "mimeType": "text/html",
                "body": {"size": len(html), "data": b64(html.encode("utf-8"))},
            }
        )
    for filename, mime, payload, attachment_id in attachments:
        parts.append(
            {
                "mimeType": mime,
                "filename": filename,
                "body": {
                    "attachmentId": attachment_id,
                    "size": attachment_size if attachment_size is not None else len(payload),
                },
            }
        )
    headers = [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": sender},
        {"name": "To", "value": "Bob <bob@example.com>"},
        {"name": "Date", "value": "Mon, 1 Jan 2024 10:00:00 +0000"},
    ]
    payload: dict[str, Any] = {
        "mimeType": "multipart/mixed" if len(parts) > 1 else "text/plain",
        "headers": headers,
        "body": {},
    }
    if len(parts) == 1:
        payload["mimeType"] = parts[0]["mimeType"]
        payload["body"] = parts[0]["body"]
    if len(parts) > 1:
        payload["parts"] = parts
    return {
        "id": message_id,
        "threadId": "thread-1",
        "labelIds": list(labels),
        "snippet": body[:80],
        "payload": payload,
    }


class MailEnv:
    def __init__(
        self,
        workspace: Path,
        db: Database,
        executor: ToolExecutor,
        grant: Any,
        service: FakeMailService,
    ) -> None:
        self.workspace = workspace
        self.db = db
        self.executor = executor
        self.grant = grant
        self.service = service

    def run(self, name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> ToolResult:
        return self.executor.run(name, args or {}, grant=self.grant, **kwargs)


@pytest.fixture()
def env(tmp_path: Path) -> MailEnv:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = Database(tmp_path / "kivi.db")
    engine = PermissionEngine(db, workspace=str(workspace))
    grant = engine.issue("mail-task", ["mail.read", "mail.draft"])
    service = FakeMailService()
    client = GmailClient(service=service)
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
    mail_env = MailEnv(workspace, db, executor, grant, service)
    yield mail_env
    db.close()


# -- construction and specs ---------------------------------------------
def test_tools_build_without_credentials() -> None:
    tools = build_mail_tools()
    assert {tool.name for tool in tools} == {
        "mail.list",
        "mail.search",
        "mail.read",
        "mail.attachment",
        "mail.draft",
    }
    assert all(tool.args_model is not None for tool in tools)
    assert all(not hasattr(tool, "send") for tool in tools)


def test_specs_publish_risks_and_capabilities() -> None:
    specs = {tool.name: tool for tool in build_mail_tools()}
    assert specs["mail.list"].risk == "R0"
    assert specs["mail.list"].capabilities == ("mail.read",)
    assert specs["mail.search"].risk == "R0"
    assert specs["mail.read"].risk == "R0"
    assert specs["mail.attachment"].risk == "R1"
    assert specs["mail.attachment"].capabilities == ("mail.read",)
    assert specs["mail.draft"].risk == "R2"
    assert specs["mail.draft"].capabilities == ("mail.draft",)


def test_fixture_registers_all_mail_tools(env: MailEnv) -> None:
    assert env.executor.registry.names() == (
        "mail.attachment",
        "mail.draft",
        "mail.list",
        "mail.read",
        "mail.search",
    )


# -- list and search -----------------------------------------------------
def test_list_returns_messages_with_email_trust(env: MailEnv) -> None:
    env.service.store["m1"] = gmail_message("m1", subject="Quarterly report")
    result = env.run("mail.list")
    assert result.ok
    assert result.trust == TRUST
    assert "Quarterly report" in result.output
    assert result.meta["count"] == 1


def test_list_redacts_secrets_in_the_snippet(env: MailEnv) -> None:
    env.service.store["m1"] = gmail_message(
        "m1",
        body="token sk-canary1234567890123456",
    )
    result = env.run("mail.list")
    assert result.ok
    assert "sk-canary" not in result.output
    assert "[REDACTED]" in result.output


def test_search_passes_the_query_and_returns_email_trust(env: MailEnv) -> None:
    env.service.store["m1"] = gmail_message("m1", subject="Invoice")
    result = env.run("mail.search", {"query": "invoice", "max_results": 5})
    assert result.ok
    assert result.trust == TRUST
    assert env.service.calls[0][1]["q"] == "invoice"


def test_search_requires_a_query(env: MailEnv) -> None:
    result = env.run("mail.search", {"query": ""})
    assert not result.ok
    assert "invalid arguments" in result.error
    assert env.service.calls == []


# -- read ---------------------------------------------------------------
def test_read_returns_headers_body_and_email_trust(env: MailEnv) -> None:
    env.service.store["m1"] = gmail_message("m1", subject="Files", body="hello\nworld")
    result = env.run("mail.read", {"message_id": "m1"})
    assert result.ok
    assert result.trust == TRUST
    assert "Subject: Files" in result.output
    assert "hello\nworld" in result.output
    assert result.meta["id"] == "m1"


def test_read_strips_html(env: MailEnv) -> None:
    env.service.store["m1"] = gmail_message(
        "m1",
        body="",
        html="<p>Hi <b>there</b></p><script>alert(1)</script>",
    )
    result = env.run("mail.read", {"message_id": "m1"})
    assert result.ok
    assert "Hi there" in result.output
    assert "alert" not in result.output


def test_read_redacts_secret_tokens(env: MailEnv) -> None:
    env.service.store["m1"] = gmail_message(
        "m1",
        body="password sk-canary1234567890123456",
    )
    result = env.run("mail.read", {"message_id": "m1"})
    assert result.ok
    assert "sk-canary" not in result.output
    assert "[REDACTED]" in result.output


def test_read_missing_message_is_a_tool_error(env: MailEnv) -> None:
    result = env.run("mail.read", {"message_id": "missing"})
    assert not result.ok
    assert "Gmail API error" in result.error
    assert "crashed" not in result.error
    assert env.db.recent_audit()[0]["decision"] == "error"


# -- attachments ---------------------------------------------------------
def attachment_message() -> dict[str, Any]:
    return gmail_message(
        "m1",
        attachments=(("../../evil.bat", "application/octet-stream", b"payload", "att-1"),),
    )


def test_attachment_writes_into_the_quarantine_directory(env: MailEnv) -> None:
    env.service.store["m1"] = attachment_message()
    env.service.attachment_payloads["att-1"] = {"data": b64(b"payload"), "size": 7}
    result = env.run("mail.attachment", {"message_id": "m1", "attachment_id": "att-1"})
    assert result.ok
    target = env.workspace / ATTACHMENTS_DIR / "evil.bat"
    assert target.is_file()
    assert target.read_bytes() == b"payload"
    assert result.artifacts == [str(target.resolve())]
    assert result.meta["quarantined"] is True
    assert result.trust == TRUST


def test_attachment_stays_inside_the_workspace(env: MailEnv) -> None:
    env.service.store["m1"] = gmail_message(
        "m1",
        attachments=(("..\\..\\..\\escape.bin", "application/octet-stream", b"x", "att-1"),),
    )
    env.service.attachment_payloads["att-1"] = {"data": b64(b"x"), "size": 1}
    result = env.run("mail.attachment", {"message_id": "m1", "attachment_id": "att-1"})
    assert result.ok
    target = Path(result.meta["path"])
    assert target.parent == (env.workspace / ATTACHMENTS_DIR).resolve()
    assert not (env.workspace.parent / "escape.bin").exists()


def test_attachment_dry_run_does_not_call_the_api_or_write(env: MailEnv) -> None:
    result = env.run(
        "mail.attachment",
        {"message_id": "m1", "attachment_id": "att-1"},
        dry_run=True,
    )
    assert result.ok
    assert '"action": "save-attachment"' in result.output
    assert env.service.calls == []
    assert not (env.workspace / ATTACHMENTS_DIR).exists()


def test_attachment_rejects_an_oversized_file(env: MailEnv) -> None:
    env.service.store["m1"] = gmail_message(
        "m1",
        attachments=(("big.bin", "application/octet-stream", b"x", "att-big"),),
        attachment_size=MAX_ATTACHMENT_BYTES + 1,
    )
    env.service.attachment_payloads["att-big"] = {
        "data": b64(b"x"),
        "size": MAX_ATTACHMENT_BYTES + 1,
    }
    result = env.run("mail.attachment", {"message_id": "m1", "attachment_id": "att-big"})
    assert not result.ok
    assert "larger than" in result.error
    assert not (env.workspace / ATTACHMENTS_DIR / "big.bin").exists()


# -- drafts --------------------------------------------------------------
def draft_args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "to": ["bob@example.com"],
        "subject": "Hello",
        "body": "Hi Bob",
    }
    args.update(overrides)
    return args


def test_draft_requires_explicit_approval(env: MailEnv) -> None:
    result = env.run("mail.draft", draft_args())
    assert not result.ok
    assert "confirmation required" in result.error
    assert env.service.draft_payloads == []
    actions = [row["decision"] for row in env.db.recent_audit()]
    assert "confirm" in actions


def test_draft_with_approval_returns_an_id_and_never_sends(env: MailEnv) -> None:
    result = env.run("mail.draft", draft_args(), approved=True)
    assert result.ok
    assert "draft-1" in result.output
    assert "Hi Bob" not in result.output
    assert result.meta["sent"] is False
    assert result.trust == TRUST
    assert [name for name, _ in env.service.calls] == ["drafts.create"]


def test_draft_body_is_not_written_to_the_audit_log(env: MailEnv) -> None:
    env.run("mail.draft", draft_args(body="secret plan for the weekend"), approved=True)
    rows = [row for row in env.db.recent_audit() if row["action"] == "tool.mail.draft"]
    assert rows
    assert all("secret plan for the weekend" not in json.dumps(row) for row in rows)


def test_draft_dry_run_previews_without_calling_the_api(env: MailEnv) -> None:
    result = env.run("mail.draft", draft_args(), dry_run=True)
    assert result.ok
    assert '"action": "create-draft"' in result.output
    assert env.service.draft_payloads == []
    assert env.service.calls == []


def test_draft_secret_like_body_is_refused_and_not_echoed(env: MailEnv) -> None:
    body = "token sk-canary1234567890123456"
    result = env.run("mail.draft", draft_args(body=body), approved=True)
    assert not result.ok
    assert "secret-like" in result.error
    assert "sk-canary" not in result.error
    assert "sk-canary" not in result.output
    assert env.service.draft_payloads == []


def test_draft_dry_run_reports_secret_like_body_without_the_body(env: MailEnv) -> None:
    result = env.run(
        "mail.draft",
        draft_args(body="token sk-canary1234567890123456"),
        dry_run=True,
    )
    assert result.ok
    assert '"secret_like_tokens": 1' in result.output
    assert "sk-canary" not in result.output


def test_draft_rejects_too_many_recipients(env: MailEnv) -> None:
    recipients = [f"user{index}@example.com" for index in range(21)]
    result = env.run("mail.draft", draft_args(to=recipients), approved=True)
    assert not result.ok
    assert "invalid arguments" in result.error
    assert env.service.draft_payloads == []


def test_draft_rejects_an_invalid_address(env: MailEnv) -> None:
    result = env.run(
        "mail.draft",
        draft_args(to=["bob@example.com\nBcc: evil@example.com"]),
        approved=True,
    )
    assert not result.ok
    assert "invalid arguments" in result.error
    assert "evil@example.com" not in result.error
    assert env.service.draft_payloads == []


def test_draft_rejects_a_too_long_subject(env: MailEnv) -> None:
    result = env.run("mail.draft", draft_args(subject="s" * 201), approved=True)
    assert not result.ok
    assert "invalid arguments" in result.error


def test_draft_rejects_a_too_long_body(env: MailEnv) -> None:
    result = env.run("mail.draft", draft_args(body="b" * 100_001), approved=True)
    assert not result.ok
    assert "invalid arguments" in result.error


# -- failure handling ----------------------------------------------------
def test_client_factory_failure_is_a_redacted_tool_error(env: MailEnv) -> None:
    def boom() -> GmailClient:
        raise GmailAuthError(
            "no Gmail token; token sk-canary1234567890123456 is missing"
        )

    registry = ToolRegistry()
    registry.register(MailListTool(client_factory=boom))
    env.executor.registry = registry
    result = env.run("mail.list", {})
    assert not result.ok
    assert "no Gmail token" in result.error
    assert "sk-canary" not in result.error
    assert "crashed" not in result.error


def test_read_only_tools_do_not_need_the_draft_capability(env: MailEnv) -> None:
    read_only = env.executor.permissions.issue("read-task", ["mail.read"])
    env.service.store["m1"] = gmail_message("m1")
    result = env.executor.run("mail.list", {}, grant=read_only)
    assert result.ok
    denied = env.executor.run("mail.draft", draft_args(), grant=read_only, approved=True)
    assert not denied.ok
    assert "not granted" in denied.error
