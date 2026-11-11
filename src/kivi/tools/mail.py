"""Gmail tools: list, search, read, save an attachment and create a draft.

Every result is labelled ``untrusted:email``: a message may describe facts but
it can never instruct Kivi. Bodies and snippets are passed through the
sanitizer, secret-looking tokens are redacted, and output is capped by
``ctx.limits.max_output_chars``.

There is no tool that sends mail and no capability that could. ``mail.draft``
is R2 and therefore always needs an explicit approval from the executor; the
tool only calls ``drafts.create``. Attachments are written under
``<workspace>/data/attachments`` (quarantine), capped at 20 MB and never
opened. Credentials are resolved lazily through ``client_factory`` so building
a tool never touches OAuth or the network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kivi.integrations.gmail import (
    MAX_ATTACHMENT_BYTES,
    MAX_DRAFT_BODY_CHARS,
    MAX_RECIPIENTS,
    MAX_SUBJECT_CHARS,
    GmailAuth,
    GmailAuthError,
    GmailClient,
    GmailClientError,
    Message,
    MessageSummary,
    redact,
    validate_recipients,
)
from kivi.safety.sanitize import redact_secrets, sanitize_text
from kivi.safety.zones import ZoneError
from kivi.tools.base import Preview, Tool, ToolContext, ToolError, ToolResult

ClientFactory = Callable[[], GmailClient]

TRUST = "untrusted:email"
ATTACHMENTS_DIR = Path("data") / "attachments"
SNIPPET_CHARS = 500
HEADER_CHARS = 300
DRAFT_SECRET_MESSAGE = (
    "draft body contains secret-like token(s); remove them before creating a draft"
)


def _authorized_client() -> GmailClient:
    """Build a client from the stored token; never called during ``dry_run``."""
    auth = GmailAuth()
    return GmailClient(credentials=auth.credentials())


def _client_call[T](operation: Callable[[], T]) -> T:
    """Run a client operation and convert failures into a redacted ToolError."""
    try:
        return operation()
    except (GmailAuthError, GmailClientError) as exc:
        raise ToolError(redact(str(exc))) from exc


def _clean(text: str, limit: int) -> tuple[str, bool]:
    """Sanitize header/body text, redacting secrets and capping the length."""
    sanitized = sanitize_text(text, html=False, max_chars=max(1, limit))
    return sanitized.text, sanitized.truncated


def _fit(text: str, limit: int) -> tuple[str, bool]:
    limit = max(1, limit)
    if len(text) <= limit:
        return text, False
    return text[:limit], True


class _MailArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class MailListArgs(_MailArgs):
    query: str = Field(default="", max_length=500, description="Optional Gmail search query.")
    max_results: int = Field(default=10, ge=1, le=50)
    label_ids: list[str] = Field(default_factory=list, max_length=20)


class MailSearchArgs(_MailArgs):
    query: str = Field(min_length=1, max_length=500, description="Gmail search query.")
    max_results: int = Field(default=10, ge=1, le=50)


class MailReadArgs(_MailArgs):
    message_id: str = Field(min_length=1, max_length=1024)


class MailAttachmentArgs(_MailArgs):
    message_id: str = Field(min_length=1, max_length=1024)
    attachment_id: str = Field(min_length=1, max_length=1024)


class MailDraftArgs(_MailArgs):
    to: list[str] = Field(min_length=1, max_length=MAX_RECIPIENTS)
    subject: str = Field(default="", max_length=MAX_SUBJECT_CHARS)
    body: str = Field(default="", max_length=MAX_DRAFT_BODY_CHARS)
    thread_id: str = Field(default="", max_length=1024)

    @field_validator("to")
    @classmethod
    def _recipients(cls, values: list[str]) -> list[str]:
        return list(validate_recipients(values))

    @field_validator("subject")
    @classmethod
    def _subject(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("subject must be a single line without control characters")
        return value


class _MailTool:
    """Shared lazy-client plumbing for all mail tools."""

    trust = TRUST

    def __init__(self, client_factory: ClientFactory | None = None) -> None:
        self._client_factory = client_factory or _authorized_client

    def _client(self) -> GmailClient:
        return _client_call(self._client_factory)


def _summaries_result(
    summaries: list[MessageSummary],
    *,
    query: str,
    max_output: int,
) -> ToolResult:
    items: list[dict[str, object]] = []
    for summary in summaries:
        snippet, _ = _clean(summary.snippet, SNIPPET_CHARS)
        subject, _ = _clean(summary.subject, HEADER_CHARS)
        sender, _ = _clean(summary.from_, HEADER_CHARS)
        recipient, _ = _clean(summary.to, HEADER_CHARS)
        items.append(
            {
                "id": summary.id,
                "thread_id": summary.thread_id,
                "subject": subject,
                "from": sender,
                "to": recipient,
                "date": _clean(summary.date, HEADER_CHARS)[0],
                "snippet": snippet,
                "labels": list(summary.labels),
            }
        )
    payload = {"query": query, "count": len(items), "messages": items}
    output, truncated = _fit(
        json.dumps(payload, ensure_ascii=False, indent=2),
        max_output,
    )
    return ToolResult(
        ok=True,
        output=output,
        meta={"query": query, "count": len(items)},
        truncated=truncated,
        trust=TRUST,
    )


class MailListTool(_MailTool):
    name = "mail.list"
    description = "List recent Gmail messages as untrusted data (read-only)."
    risk = "R0"
    capabilities = ("mail.read",)
    args_model = MailListArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, MailListArgs)
        return Preview(
            "mail-list",
            {
                "query": args.query,
                "max_results": args.max_results,
                "label_ids": list(args.label_ids),
            },
            reversible=True,
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, MailListArgs)
        client = self._client()
        summaries = _client_call(
            lambda: client.list_messages(
                query=args.query,
                max_results=args.max_results,
                label_ids=args.label_ids or None,
            )
        )
        return _summaries_result(
            summaries,
            query=args.query,
            max_output=ctx.limits.max_output_chars,
        )


class MailSearchTool(_MailTool):
    name = "mail.search"
    description = "Search Gmail with a query; results are untrusted:email (read-only)."
    risk = "R0"
    capabilities = ("mail.read",)
    args_model = MailSearchArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, MailSearchArgs)
        return Preview(
            "mail-search",
            {"query": args.query, "max_results": args.max_results},
            reversible=True,
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, MailSearchArgs)
        client = self._client()
        summaries = _client_call(
            lambda: client.search(args.query, max_results=args.max_results)
        )
        return _summaries_result(
            summaries,
            query=args.query,
            max_output=ctx.limits.max_output_chars,
        )


class MailReadTool(_MailTool):
    name = "mail.read"
    description = "Read one Gmail message: headers, plain text and attachment names."
    risk = "R0"
    capabilities = ("mail.read",)
    args_model = MailReadArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, MailReadArgs)
        return Preview("read-message", {"message_id": args.message_id}, reversible=True)

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, MailReadArgs)
        client = self._client()
        message = _client_call(lambda: client.get_message(args.message_id))
        return self._result(message, ctx)

    def _result(self, message: Message, ctx: ToolContext) -> ToolResult:
        limit = max(200, ctx.limits.max_output_chars)
        body, body_truncated = _clean(message.body, max(200, limit - 512))
        subject = _clean(message.subject, HEADER_CHARS)[0]
        sender = _clean(message.from_, HEADER_CHARS)[0]
        recipient = _clean(message.to, HEADER_CHARS)[0]
        date = _clean(message.date, HEADER_CHARS)[0]
        lines = [
            f"From: {sender}",
            f"To: {recipient}",
            f"Date: {date}",
            f"Subject: {subject}",
        ]
        if message.labels:
            lines.append(f"Labels: {', '.join(message.labels)}")
        attachments = []
        for item in message.attachments:
            name = _clean(item.filename, HEADER_CHARS)[0]
            attachments.append(
                {
                    "filename": name,
                    "mime_type": item.mime_type,
                    "size": item.size,
                    "attachment_id": item.attachment_id,
                }
            )
            lines.append(f"Attachment: {name} ({item.mime_type}, {item.size} B)")
        lines.extend(("", body))
        output, truncated = _fit("\n".join(lines), limit)
        return ToolResult(
            ok=True,
            output=output,
            meta={
                "id": message.id,
                "thread_id": message.thread_id,
                "subject": subject,
                "from": sender,
                "to": recipient,
                "date": date,
                "labels": list(message.labels),
                "attachments": attachments,
            },
            truncated=truncated or body_truncated or message.truncated,
            trust=TRUST,
        )


class MailAttachmentTool(_MailTool):
    name = "mail.attachment"
    description = (
        "Save one Gmail attachment under data/attachments (quarantine, max 20 MB)."
    )
    risk = "R1"
    capabilities = ("mail.read",)
    args_model = MailAttachmentArgs

    @staticmethod
    def _directory(ctx: ToolContext) -> Path:
        candidate = ctx.workspace / ATTACHMENTS_DIR
        decision = ctx.zones.check(candidate, "write")
        if not decision.allowed:
            raise ZoneError(f"{decision.rule}: {decision.reason}")
        if decision.resolved is None:
            raise ZoneError("attachment directory could not be resolved")
        return decision.resolved

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, MailAttachmentArgs)
        directory = self._directory(ctx)
        return Preview(
            "save-attachment",
            {
                "message_id": args.message_id,
                "attachment_id": args.attachment_id,
                "directory": str(directory),
                "max_bytes": MAX_ATTACHMENT_BYTES,
            },
            reversible=True,
            warnings=(
                "attachment bytes are untrusted and are not scanned for malware",
                "the file is quarantined under data/attachments and never opened",
            ),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, MailAttachmentArgs)
        directory = self._directory(ctx)
        client = self._client()
        target = _client_call(
            lambda: client.save_attachment(
                args.message_id,
                args.attachment_id,
                directory,
            )
        )
        resolved = target.resolve()
        if resolved.parent != directory:
            resolved.unlink(missing_ok=True)
            raise ToolError("attachment path escaped the attachments directory")
        size = resolved.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            resolved.unlink(missing_ok=True)
            raise ToolError(f"attachment is larger than {MAX_ATTACHMENT_BYTES} bytes")
        return ToolResult(
            ok=True,
            output=f"saved {size} bytes to {resolved}",
            artifacts=[str(resolved)],
            meta={
                "path": str(resolved),
                "bytes": size,
                "message_id": args.message_id,
                "attachment_id": args.attachment_id,
                "quarantined": True,
            },
            trust=TRUST,
        )


class MailDraftTool(_MailTool):
    name = "mail.draft"
    description = (
        "Create a Gmail draft from plain text (R2, approval required); never sends."
    )
    risk = "R2"
    capabilities = ("mail.draft",)
    args_model = MailDraftArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, MailDraftArgs)
        secret_like = redact_secrets(args.body)[1]
        warnings = [
            "this creates a draft only; Kivi cannot send mail",
            "the draft is stored in the connected Gmail account",
        ]
        if secret_like:
            warnings.append(
                f"body contains {secret_like} secret-like token(s); it would be refused"
            )
        return Preview(
            "create-draft",
            {
                "to": list(args.to),
                "subject": args.subject,
                "body_chars": len(args.body),
                "thread_id": args.thread_id or None,
                "secret_like_tokens": secret_like,
            },
            reversible=True,
            warnings=tuple(warnings),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, MailDraftArgs)
        if redact_secrets(args.body)[1]:
            # The draft body is not logged or echoed; only the count is refused.
            raise ToolError(DRAFT_SECRET_MESSAGE)
        client = self._client()
        info = _client_call(
            lambda: client.create_draft(
                to=args.to,
                subject=args.subject,
                body=args.body,
                thread_id=args.thread_id or None,
            )
        )
        payload = info.as_dict()
        payload["sent"] = False
        output, truncated = _fit(
            json.dumps({"draft": payload}, ensure_ascii=False, indent=2),
            ctx.limits.max_output_chars,
        )
        return ToolResult(
            ok=True,
            output=output,
            meta={
                "draft_id": info.draft_id,
                "message_id": info.message_id,
                "thread_id": info.thread_id,
                "to": list(info.to),
                "subject": info.subject,
                "body_chars": info.body_chars,
                "sent": False,
            },
            truncated=truncated,
            trust=TRUST,
        )


def build_mail_tools(
    *,
    client_factory: ClientFactory | None = None,
) -> tuple[Tool, ...]:
    """Build the five mail tools sharing one lazy client factory."""
    factory = client_factory or _authorized_client
    return (
        MailListTool(factory),
        MailSearchTool(factory),
        MailReadTool(factory),
        MailAttachmentTool(factory),
        MailDraftTool(factory),
    )


__all__ = [
    "ATTACHMENTS_DIR",
    "ClientFactory",
    "MailAttachmentArgs",
    "MailAttachmentTool",
    "MailDraftArgs",
    "MailDraftTool",
    "MailListArgs",
    "MailListTool",
    "MailReadArgs",
    "MailReadTool",
    "MailSearchArgs",
    "MailSearchTool",
    "TRUST",
    "build_mail_tools",
]
