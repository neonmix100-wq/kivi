"""Unit tests for the Gmail integration: scopes, token files, fake service.

No test touches the network: OAuth is injected through ``flow_factory`` and the
Gmail API is a small dict-backed fake that records every method call. The fake
also exposes ``send`` and raises if anything ever calls it, which turns the
"drafts only" invariant into an executable check.
"""

from __future__ import annotations

import base64
import copy
import email
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kivi.integrations import gmail as gmail_module
from kivi.integrations.gmail import (
    FORBIDDEN_SCOPES,
    GMAIL_COMPOSE_SCOPE,
    GMAIL_FULL_SCOPE,
    GMAIL_MODIFY_SCOPE,
    GMAIL_READONLY_SCOPE,
    GMAIL_SEND_SCOPE,
    MAX_ATTACHMENT_BYTES,
    MAX_BODY_CHARS,
    MAX_DRAFT_BODY_CHARS,
    MAX_SUBJECT_CHARS,
    SCOPES,
    GmailAuth,
    GmailAuthError,
    GmailClient,
    GmailClientError,
    sanitize_filename,
    validate_address,
    validate_recipients,
    validate_scopes,
)


# -- fakes ---------------------------------------------------------------
class _Request:
    def __init__(self, callback: Any) -> None:
        self._callback = callback

    def execute(self) -> Any:
        return self._callback()


class _FakeAttachments:
    def __init__(self, root: FakeGmailService) -> None:
        self._root = root

    def get(self, **params: Any) -> _Request:
        return self._root.request("attachments.get", params, self._root.attachment)


class _FakeMessages:
    def __init__(self, root: FakeGmailService) -> None:
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
    def __init__(self, root: FakeGmailService) -> None:
        self._root = root

    def create(self, **params: Any) -> _Request:
        return self._root.request("drafts.create", params, self._root.create_draft)

    def send(self, **params: Any) -> None:
        self._root.calls.append(("send", dict(params)))
        raise AssertionError("send must never be called")


class FakeGmailService:
    """Dict-backed stand-in for the Gmail v1 discovery service."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}
        self.attachment_payloads: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.draft_payloads: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None

    # -- resource chain --------------------------------------------------
    def users(self) -> FakeGmailService:
        return self

    def messages(self) -> _FakeMessages:
        return _FakeMessages(self)

    def drafts(self) -> _FakeDrafts:
        return _FakeDrafts(self)

    def request(self, name: str, params: dict[str, Any], handler: Any) -> _Request:
        self.calls.append((name, dict(params)))
        return _Request(lambda: handler(params))

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    # -- handlers --------------------------------------------------------
    def list_messages(self, params: dict[str, Any]) -> dict[str, Any]:
        self._maybe_fail()
        query = str(params.get("q") or "").lower()
        limit = int(params.get("maxResults") or 100)
        label_ids = set(params.get("labelIds") or ())
        found: list[dict[str, str]] = []
        for message_id, raw in self.store.items():
            if label_ids and not label_ids <= set(raw.get("labelIds") or ()):
                continue
            haystack = " ".join(
                [str(raw.get("snippet") or "")] + _header_values(raw)
            ).lower()
            if query and query not in haystack:
                continue
            found.append({"id": message_id, "threadId": raw.get("threadId", "")})
        return {"messages": found[:limit], "resultSizeEstimate": len(found)}

    def get_message(self, params: dict[str, Any]) -> dict[str, Any]:
        self._maybe_fail()
        raw = self.store.get(str(params.get("id")))
        if raw is None:
            raise RuntimeError("HttpError 404: requested entity was not found")
        return copy.deepcopy(raw)

    def create_draft(self, params: dict[str, Any]) -> dict[str, Any]:
        self._maybe_fail()
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

    def attachment(self, params: dict[str, Any]) -> dict[str, Any]:
        self._maybe_fail()
        item = self.attachment_payloads.get(str(params.get("id")))
        if item is None:
            raise RuntimeError("HttpError 404: attachment was not found")
        return {"data": item["data"], "size": item["size"]}


def _header_values(raw: dict[str, Any]) -> list[str]:
    payload = raw.get("payload") or {}
    return [
        str(item.get("value") or "")
        for item in payload.get("headers") or ()
        if isinstance(item, dict)
    ]


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def gmail_message(
    message_id: str,
    *,
    thread_id: str = "thread-1",
    subject: str = "Hello",
    sender: str = "Alice <alice@example.com>",
    to: str = "Bob <bob@example.com>",
    date: str = "Mon, 1 Jan 2024 10:00:00 +0000",
    body: str | None = "hello",
    html: str | None = None,
    labels: tuple[str, ...] = ("INBOX", "UNREAD"),
    attachments: tuple[tuple[str, str, bytes, str], ...] = (),
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    if body is not None:
        parts.append(
            {
                "mimeType": "text/plain",
                "body": {"size": len(body.encode("utf-8")), "data": b64(body.encode("utf-8"))},
            }
        )
    if html is not None:
        parts.append(
            {
                "mimeType": "text/html",
                "body": {"size": len(html.encode("utf-8")), "data": b64(html.encode("utf-8"))},
            }
        )
    for filename, mime, payload, attachment_id in attachments:
        parts.append(
            {
                "mimeType": mime,
                "filename": filename,
                "body": {"attachmentId": attachment_id, "size": len(payload)},
            }
        )
    headers = [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": sender},
        {"name": "To", "value": to},
        {"name": "Date", "value": date},
    ]
    if len(parts) == 1:
        payload: dict[str, Any] = {
            "mimeType": parts[0]["mimeType"],
            "headers": headers,
            "body": parts[0]["body"],
        }
        if parts[0].get("filename"):
            payload["filename"] = parts[0]["filename"]
    else:
        payload = {
            "mimeType": "multipart/mixed",
            "headers": headers,
            "body": {},
            "parts": parts,
        }
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": list(labels),
        "snippet": (body or "")[:80],
        "payload": payload,
    }


class FakeCredentials:
    def __init__(
        self,
        scopes: tuple[str, ...] = SCOPES,
        *,
        expired: bool = False,
        refresh_token: str | None = "refresh-token",
    ) -> None:
        self.scopes = list(scopes)
        self.expired = expired
        self.refresh_token = refresh_token
        self.refreshed = False

    def to_json(self) -> str:
        return json.dumps(
            {
                "token": "access-token",
                "refresh_token": self.refresh_token,
                "client_id": "client-id.apps.googleusercontent.com",
                "client_secret": "client-secret",
                "token_uri": "https://oauth2.googleapis.com/token",
                "scopes": list(self.scopes),
            }
        )

    def refresh(self, request: Any) -> None:
        assert request == "fake-request"
        self.refreshed = True
        self.expired = False


class FakeFlow:
    def __init__(self, credentials: FakeCredentials) -> None:
        self.credentials = credentials
        self.kwargs: dict[str, Any] = {}

    def run_local_server(self, **kwargs: Any) -> FakeCredentials:
        self.kwargs = kwargs
        return self.credentials


def token_payload(
    scopes: tuple[str, ...] = SCOPES,
    *,
    expired: bool = False,
    refresh_token: str | None = "refresh-token",
) -> dict[str, Any]:
    expiry = datetime.now(UTC) + (timedelta(hours=-1) if expired else timedelta(hours=1))
    payload: dict[str, Any] = {
        "token": "access-token",
        "client_id": "client-id.apps.googleusercontent.com",
        "client_secret": "client-secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": list(scopes),
        "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if refresh_token is not None:
        payload["refresh_token"] = refresh_token
    return payload


def write_token(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# -- scopes -------------------------------------------------------------
def test_default_scopes_are_exactly_readonly_and_compose() -> None:
    assert set(SCOPES) == {GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE}
    assert GMAIL_SEND_SCOPE not in SCOPES
    assert GMAIL_MODIFY_SCOPE not in SCOPES
    assert frozenset(SCOPES) == gmail_module.GMAIL_ALLOWED_SCOPES
    assert {GMAIL_SEND_SCOPE, GMAIL_MODIFY_SCOPE, GMAIL_FULL_SCOPE} <= FORBIDDEN_SCOPES


def test_validate_scopes_returns_the_pair() -> None:
    assert validate_scopes([GMAIL_COMPOSE_SCOPE, GMAIL_READONLY_SCOPE]) == SCOPES


@pytest.mark.parametrize("scope", [GMAIL_SEND_SCOPE, GMAIL_MODIFY_SCOPE, GMAIL_FULL_SCOPE])
def test_validate_scopes_rejects_forbidden_scopes(scope: str) -> None:
    with pytest.raises(GmailAuthError, match="forbidden"):
        validate_scopes([GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE, scope])


@pytest.mark.parametrize(
    "scopes",
    [
        [GMAIL_READONLY_SCOPE],
        [GMAIL_COMPOSE_SCOPE],
        [],
        None,
        [
            GMAIL_READONLY_SCOPE,
            GMAIL_COMPOSE_SCOPE,
            "https://www.googleapis.com/auth/userinfo.email",
        ],
    ],
)
def test_validate_scopes_requires_exactly_the_pair(scopes: list[str] | None) -> None:
    with pytest.raises(GmailAuthError):
        validate_scopes(scopes)


# -- auth: paths, loading, refresh --------------------------------------
def test_default_paths_point_at_config_and_oauth_data(tmp_path: Path) -> None:
    auth = GmailAuth()
    assert auth.client_secret_path.name == "gmail-client.json"
    assert auth.client_secret_path.parent.name == "config"
    assert auth.token_path.name == "gmail-token.json"
    assert auth.token_path.parent.name == "oauth"
    profiled = GmailAuth(profile=tmp_path / "profile")
    assert profiled.token_path == tmp_path / "profile" / "gmail-token.json"


def test_missing_token_raises_a_clear_error(tmp_path: Path) -> None:
    auth = GmailAuth(token_path=tmp_path / "gmail-token.json")
    with pytest.raises(GmailAuthError, match="authorize"):
        auth.load_token()


def test_corrupt_token_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    path.write_text("not json", encoding="utf-8")
    auth = GmailAuth(token_path=path)
    with pytest.raises(GmailAuthError, match="corrupt"):
        auth.load_token()


def test_token_without_recorded_scopes_is_rejected(tmp_path: Path) -> None:
    payload = token_payload()
    payload.pop("scopes")
    auth = GmailAuth(token_path=write_token(tmp_path / "token.json", payload))
    with pytest.raises(GmailAuthError, match="scopes"):
        auth.load_token()


def test_token_with_send_scope_is_rejected(tmp_path: Path) -> None:
    payload = token_payload((GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE, GMAIL_SEND_SCOPE))
    auth = GmailAuth(token_path=write_token(tmp_path / "token.json", payload))
    with pytest.raises(GmailAuthError, match="gmail.send"):
        auth.load_token()


def test_token_missing_compose_scope_is_rejected(tmp_path: Path) -> None:
    payload = token_payload((GMAIL_READONLY_SCOPE,))
    auth = GmailAuth(token_path=write_token(tmp_path / "token.json", payload))
    with pytest.raises(GmailAuthError, match="missing required scopes"):
        auth.load_token()


def test_load_token_returns_scoped_credentials(tmp_path: Path) -> None:
    auth = GmailAuth(token_path=write_token(tmp_path / "token.json", token_payload()))
    credentials = auth.load_token()
    assert set(credentials.scopes) == set(SCOPES)
    assert credentials.refresh_token == "refresh-token"


def test_save_token_writes_scopes_and_restricts_permissions(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    auth = GmailAuth(token_path=path)
    saved = auth.save_token(FakeCredentials())
    assert saved == path
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["scopes"] == list(SCOPES)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_token_refuses_forbidden_scopes(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    auth = GmailAuth(token_path=path)
    credentials = FakeCredentials((GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE, GMAIL_SEND_SCOPE))
    with pytest.raises(GmailAuthError, match="forbidden"):
        auth.save_token(credentials)
    assert not path.exists()


def test_credentials_refreshes_an_expired_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    auth = GmailAuth(token_path=tmp_path / "token.json")
    fake = FakeCredentials(expired=True)
    monkeypatch.setattr(auth, "load_token", lambda: fake)
    monkeypatch.setattr(auth, "_build_request", lambda: "fake-request")
    credentials = auth.credentials()
    assert credentials.refreshed is True
    assert credentials.expired is False
    saved = json.loads(auth.token_path.read_text(encoding="utf-8"))
    assert saved["scopes"] == list(SCOPES)
    assert "access-token" not in caplog.text


def test_expired_token_without_refresh_token_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = GmailAuth(token_path=tmp_path / "token.json")
    fake = FakeCredentials(expired=True, refresh_token=None)
    monkeypatch.setattr(auth, "load_token", lambda: fake)
    with pytest.raises(GmailAuthError, match="refresh token"):
        auth.credentials()


def test_authorize_runs_injected_flow_and_saves_token(tmp_path: Path) -> None:
    secret = tmp_path / "gmail-client.json"
    secret.write_text("{}", encoding="utf-8")
    auth = GmailAuth(client_secret_path=secret, token_path=tmp_path / "token.json")
    flow = FakeFlow(FakeCredentials())
    seen: dict[str, Any] = {}

    def factory(path: Path, scopes: list[str]) -> FakeFlow:
        seen["path"] = path
        seen["scopes"] = scopes
        return flow

    credentials = auth.authorize(factory, open_browser=False)
    assert credentials is flow.credentials
    assert seen["path"] == secret
    assert seen["scopes"] == list(SCOPES)
    assert flow.kwargs["open_browser"] is False
    payload = json.loads(auth.token_path.read_text(encoding="utf-8"))
    assert payload["scopes"] == list(SCOPES)


def test_authorize_rejects_a_flow_with_send_scope(tmp_path: Path) -> None:
    secret = tmp_path / "gmail-client.json"
    secret.write_text("{}", encoding="utf-8")
    auth = GmailAuth(client_secret_path=secret, token_path=tmp_path / "token.json")
    credentials = FakeCredentials((GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE, GMAIL_SEND_SCOPE))
    flow = FakeFlow(credentials)
    with pytest.raises(GmailAuthError, match="forbidden"):
        auth.authorize(lambda path, scopes: flow, open_browser=False)
    assert not auth.token_path.exists()


def test_authorize_reuses_a_valid_token_without_a_flow(tmp_path: Path) -> None:
    auth = GmailAuth(token_path=write_token(tmp_path / "token.json", token_payload()))
    credentials = auth.authorize(lambda path, scopes: pytest.fail("flow must not run"))
    assert set(credentials.scopes) == set(SCOPES)


def test_authorize_requires_the_client_secret(tmp_path: Path) -> None:
    auth = GmailAuth(
        client_secret_path=tmp_path / "missing-client.json",
        token_path=tmp_path / "token.json",
    )
    with pytest.raises(GmailAuthError, match="client secret"):
        auth.authorize()


# -- client: reading -----------------------------------------------------
def make_client(service: FakeGmailService, **kwargs: Any) -> GmailClient:
    return GmailClient(service=service, **kwargs)


def test_client_needs_credentials_or_service() -> None:
    with pytest.raises(GmailClientError, match="credentials"):
        GmailClient().list_messages()


def test_client_uses_the_injected_service() -> None:
    service = FakeGmailService()
    client = make_client(service)
    assert client.service is service
    assert service.calls == []


def test_list_messages_returns_summaries() -> None:
    service = FakeGmailService()
    service.store["m1"] = gmail_message("m1", subject="Quarterly report")
    client = make_client(service)
    summaries = client.list_messages(max_results=5)
    assert [item.id for item in summaries] == ["m1"]
    summary = summaries[0]
    assert summary.subject == "Quarterly report"
    assert summary.from_ == "Alice <alice@example.com>"
    assert summary.to == "Bob <bob@example.com>"
    assert summary.thread_id == "thread-1"
    assert summary.labels == ("INBOX", "UNREAD")
    assert summary.snippet == "hello"


def test_list_messages_passes_query_labels_and_respects_the_ceiling() -> None:
    service = FakeGmailService()
    service.store["m1"] = gmail_message("m1", subject="Invoice", labels=("INBOX",))
    client = make_client(service, max_results=2)
    client.list_messages(query="invoice", max_results=50, label_ids=["INBOX"])
    name, params = service.calls[0]
    assert name == "messages.list"
    assert params["q"] == "invoice"
    assert params["labelIds"] == ["INBOX"]
    assert params["maxResults"] == 2


def test_search_is_an_alias_for_list() -> None:
    service = FakeGmailService()
    service.store["m1"] = gmail_message("m1", subject="Invoice")
    service.store["m2"] = gmail_message("m2", subject="Holiday")
    client = make_client(service)
    found = client.search("invoice", max_results=5)
    assert [item.id for item in found] == ["m1"]


def test_search_without_matches_returns_an_empty_list() -> None:
    client = make_client(FakeGmailService())
    assert client.search("nothing", max_results=5) == []


def test_get_message_extracts_headers_body_and_attachments() -> None:
    service = FakeGmailService()
    service.store["m1"] = gmail_message(
        "m1",
        subject="Files",
        body="hello\nworld",
        attachments=(("report.pdf", "application/pdf", b"%PDF-1.4", "att-1"),),
    )
    message = make_client(service).get_message("m1")
    assert message.subject == "Files"
    assert message.body == "hello\nworld"
    assert message.attachments == (
        gmail_module.AttachmentInfo(
            filename="report.pdf",
            mime_type="application/pdf",
            size=8,
            attachment_id="att-1",
        ),
    )
    assert "attachments" in message.as_dict()


def test_get_message_strips_html_and_scripts() -> None:
    service = FakeGmailService()
    service.store["m1"] = gmail_message(
        "m1",
        body=None,
        html="<html><body><p>Hi <b>there</b></p><script>alert(1)</script></body></html>",
    )
    message = make_client(service).get_message("m1")
    assert "Hi there" in message.body
    assert "alert" not in message.body


def test_get_message_prefers_plain_text_over_html() -> None:
    service = FakeGmailService()
    service.store["m1"] = gmail_message(
        "m1",
        body="plain body",
        html="<p>html body</p>",
    )
    message = make_client(service).get_message("m1")
    assert message.body == "plain body"


def test_get_message_truncates_a_huge_body() -> None:
    service = FakeGmailService()
    service.store["m1"] = gmail_message("m1", body="x" * (MAX_BODY_CHARS + 50))
    message = make_client(service).get_message("m1")
    assert message.truncated is True
    assert len(message.body) == MAX_BODY_CHARS


# -- client: drafts ------------------------------------------------------
def decode_draft(service: FakeGmailService) -> email.message.Message:
    raw = service.draft_payloads[0]["message"]["raw"]
    return email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


def test_create_draft_builds_mime_and_returns_an_id() -> None:
    service = FakeGmailService()
    client = make_client(service)
    info = client.create_draft(["bob@example.com"], "Hello", "Hi Bob")
    assert info.draft_id == "draft-1"
    assert info.message_id == "msg-draft-1"
    assert info.to == ("bob@example.com",)
    assert info.body_chars == len("Hi Bob")
    parsed = decode_draft(service)
    assert parsed["To"] == "bob@example.com"
    assert parsed["Subject"] == "Hello"
    assert parsed.get_content_type() == "text/plain"
    assert parsed.get_payload(decode=True).decode("utf-8").strip() == "Hi Bob"
    assert parsed["Cc"] is None
    assert parsed["Bcc"] is None
    assert [name for name, _ in service.calls] == ["drafts.create"]
    assert "send" not in [name for name, _ in service.calls]


def test_create_draft_keeps_the_thread_when_asked() -> None:
    service = FakeGmailService()
    info = make_client(service).create_draft(
        ["bob@example.com"],
        "Re: Hello",
        "body",
        thread_id="thread-9",
    )
    assert service.draft_payloads[0]["message"]["threadId"] == "thread-9"
    assert info.thread_id == "thread-9"


@pytest.mark.parametrize(
    "recipients",
    [
        [],
        [f"user{index}@example.com" for index in range(21)],
        ["not-an-address"],
        ["a@b"],
        [""],
        ["bob@example.com\nBcc: evil@example.com"],
    ],
)
def test_create_draft_rejects_invalid_recipients(recipients: list[str]) -> None:
    service = FakeGmailService()
    with pytest.raises(GmailClientError, match="recipient"):
        make_client(service).create_draft(recipients, "Hello", "body")
    assert service.draft_payloads == []


def test_create_draft_rejects_a_long_subject() -> None:
    service = FakeGmailService()
    with pytest.raises(GmailClientError, match="subject"):
        make_client(service).create_draft(
            ["bob@example.com"], "s" * (MAX_SUBJECT_CHARS + 1), "body"
        )


def test_create_draft_rejects_header_injection_in_the_subject() -> None:
    service = FakeGmailService()
    with pytest.raises(GmailClientError, match="subject"):
        make_client(service).create_draft(
            ["bob@example.com"], "Hi\r\nBcc: evil@example.com", "body"
        )


def test_create_draft_rejects_a_long_body() -> None:
    service = FakeGmailService()
    with pytest.raises(GmailClientError, match="body"):
        make_client(service).create_draft(
            ["bob@example.com"], "Hello", "b" * (MAX_DRAFT_BODY_CHARS + 1)
        )


# -- client: attachments -------------------------------------------------
def attachment_message() -> dict[str, Any]:
    return gmail_message(
        "m1",
        attachments=(("../../evil.bat", "application/octet-stream", b"payload", "att-1"),),
    )


def test_save_attachment_writes_a_sanitized_name(tmp_path: Path) -> None:
    service = FakeGmailService()
    service.store["m1"] = attachment_message()
    service.attachment_payloads["att-1"] = {"data": b64(b"payload"), "size": 7}
    path = make_client(service).save_attachment("m1", "att-1", tmp_path / "attachments")
    assert path.parent == (tmp_path / "attachments").resolve()
    assert path.name == "evil.bat"
    assert path.read_bytes() == b"payload"


def test_save_attachment_never_overwrites(tmp_path: Path) -> None:
    service = FakeGmailService()
    service.store["m1"] = attachment_message()
    service.attachment_payloads["att-1"] = {"data": b64(b"payload"), "size": 7}
    client = make_client(service)
    first = client.save_attachment("m1", "att-1", tmp_path / "attachments")
    second = client.save_attachment("m1", "att-1", tmp_path / "attachments")
    assert first.name == "evil.bat"
    assert second.name == "evil-1.bat"
    assert first.read_bytes() == second.read_bytes() == b"payload"


def test_save_attachment_requires_an_absolute_directory(tmp_path: Path) -> None:
    service = FakeGmailService()
    service.store["m1"] = attachment_message()
    service.attachment_payloads["att-1"] = {"data": b64(b"payload"), "size": 7}
    with pytest.raises(GmailClientError, match="absolute"):
        make_client(service).save_attachment("m1", "att-1", Path("attachments"))


def test_save_attachment_rejects_an_unknown_attachment(tmp_path: Path) -> None:
    service = FakeGmailService()
    service.store["m1"] = attachment_message()
    with pytest.raises(GmailClientError, match="not part"):
        make_client(service).save_attachment("m1", "att-404", tmp_path)


def test_save_attachment_rejects_an_oversized_declared_size(tmp_path: Path) -> None:
    service = FakeGmailService()
    service.store["m1"] = gmail_message(
        "m1",
        attachments=(
            (
                "big.bin",
                "application/octet-stream",
                b"x" * (MAX_ATTACHMENT_BYTES + 1),
                "att-big",
            ),
        ),
    )
    service.attachment_payloads["att-big"] = {
        "data": b64(b"x"),
        "size": MAX_ATTACHMENT_BYTES + 1,
    }
    with pytest.raises(GmailClientError, match="larger than"):
        make_client(service).save_attachment("m1", "att-big", tmp_path / "attachments")
    assert not (tmp_path / "attachments" / "big.bin").exists()


def test_save_attachment_rejects_an_oversized_payload(tmp_path: Path) -> None:
    service = FakeGmailService()
    service.store["m1"] = gmail_message(
        "m1",
        attachments=(
            ("big.bin", "application/octet-stream", b"x" * (MAX_ATTACHMENT_BYTES + 1), "att-big"),
        ),
    )
    # Declared size lies; the encoded payload length is checked before decoding.
    service.attachment_payloads["att-big"] = {
        "data": b64(b"x" * (MAX_ATTACHMENT_BYTES + 1)),
        "size": 1,
    }
    with pytest.raises(GmailClientError, match="larger than"):
        make_client(service).save_attachment("m1", "att-big", tmp_path / "attachments")


# -- redaction and the no-send invariant --------------------------------
def test_api_errors_do_not_leak_secrets() -> None:
    service = FakeGmailService()
    service.fail_with = RuntimeError("failure Authorization: Bearer sk-canary1234567890123456")
    with pytest.raises(GmailClientError) as info:
        make_client(service).list_messages()
    assert "sk-canary" not in str(info.value)
    assert "[REDACTED]" in str(info.value)


def test_client_has_no_send_api() -> None:
    source = Path(gmail_module.__file__).read_text(encoding="utf-8")
    assert not hasattr(GmailClient, "send")
    assert ".send(" not in source
    assert "drafts().send" not in source
    assert "messages().send" not in source


# -- small helpers -------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../evil.bat", "evil.bat"),
        ("C:\\temp\\note.txt", "note.txt"),
        ("CON.txt", "_CON.txt"),
        ("lpt1.log", "_lpt1.log"),
        ("  ", "attachment.bin"),
        ("..", "attachment.bin"),
        ("bad<>name?.txt", "bad__name_.txt"),
        (".hidden", "hidden"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_keeps_the_suffix_when_truncating() -> None:
    name = sanitize_filename("a" * 300 + ".pdf")
    assert len(name) <= 120
    assert name.endswith(".pdf")


def test_validate_address_and_recipients() -> None:
    assert validate_address("Alice <alice@example.com>") == "alice@example.com"
    assert validate_recipients(["A <a@example.com>", "b@example.com"]) == (
        "a@example.com",
        "b@example.com",
    )
    for bad in ("a@b", "not-an-address", "a@b.com\nBcc: x@y.com", "a@b.com, c@d.com", ""):
        with pytest.raises(ValueError):
            validate_address(bad)
