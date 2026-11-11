"""Gmail integration: OAuth with readonly + compose, drafts and reading only.

There is deliberately no way to send mail in this module. The OAuth scopes are
exactly ``gmail.readonly`` and ``gmail.compose`` (no ``gmail.send`` and no
``gmail.modify``), the client exposes only ``messages.get/list``,
``drafts.create`` and ``attachments.get``, and the token loader rejects any
token whose recorded scopes are wider than that pair. A separate stage would
have to add sending on purpose, with its own policy and approval flow.

The module imports ``google-api-python-client``, ``google-auth`` and
``google-auth-oauthlib`` lazily, so importing Kivi does not require the optional
mail dependencies and unit tests can inject a fake service.

Secrets never enter logs or error messages: token files are written with
owner-only permissions where the OS allows it, every API error passes through
:func:`redact`, and only the token path (never its contents) is logged.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from kivi.config import (
    GMAIL_ALLOWED_SCOPES,
    GMAIL_COMPOSE_SCOPE,
    GMAIL_READONLY_SCOPE,
)
from kivi.paths import get_paths, project_root
from kivi.safety.sanitize import redact_secrets, sanitize_text

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials

log = logging.getLogger(__name__)

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_FULL_SCOPE = "https://mail.google.com/"

# Exactly the two scopes Kivi needs; ``config.MailSettings`` validates the same set.
SCOPES: tuple[str, ...] = (GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE)
FORBIDDEN_SCOPES: frozenset[str] = frozenset(
    {GMAIL_SEND_SCOPE, GMAIL_MODIFY_SCOPE, GMAIL_FULL_SCOPE}
)
if frozenset(SCOPES) != GMAIL_ALLOWED_SCOPES:
    # The config module is the single source of truth; fail at import, not at auth.
    raise RuntimeError("Gmail scopes in config do not match the readonly+compose pair")

DEFAULT_CLIENT_SECRET_PATH = Path("config") / "gmail-client.json"
TOKEN_FILENAME = "gmail-token.json"
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_BODY_CHARS = 100_000
MAX_DRAFT_BODY_CHARS = 100_000
MAX_SUBJECT_CHARS = 200
MAX_RECIPIENTS = 20
MAX_RESULTS_CEILING = 500

_SUMMARY_HEADERS = ("Subject", "From", "To", "Date")
_ADDRESS = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ID_VALUE = re.compile(r"^[A-Za-z0-9_.:-]{1,1024}$")
_FILENAME_BAD = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


class GmailAuthError(RuntimeError):
    """An OAuth problem with a message safe to show to the user."""


class GmailClientError(RuntimeError):
    """A Gmail API or payload problem with secrets already redacted."""


def redact(text: str) -> str:
    """Remove secret-looking tokens before text reaches a log or an error."""
    return redact_secrets(str(text))[0]


def _credential_scopes(credentials: Any) -> tuple[str, ...]:
    scopes = getattr(credentials, "scopes", None)
    if scopes is None:
        return ()
    if isinstance(scopes, str):
        return tuple(part for part in scopes.split() if part)
    try:
        return tuple(str(part) for part in scopes if str(part).strip())
    except TypeError:
        return ()


def validate_scopes(scopes: Iterable[str] | str | None) -> tuple[str, ...]:
    """Return canonical readonly+compose scopes or raise :class:`GmailAuthError`.

    A token must prove both required scopes and must not carry any send,
    modify or full-mailbox scope. Missing scope metadata is rejected instead
    of assumed: an old token without ``scopes`` cannot prove it is safe.
    """
    if scopes is None:
        raise GmailAuthError(
            "Gmail credentials have no recorded scopes; re-run GmailAuth.authorize()"
        )
    granted = {part for part in (scopes.split() if isinstance(scopes, str) else scopes) if part}
    forbidden = granted & FORBIDDEN_SCOPES
    if forbidden:
        names = ", ".join(sorted(forbidden))
        raise GmailAuthError(f"Gmail token has forbidden scopes (send/modify/full): {names}")
    unknown = granted - GMAIL_ALLOWED_SCOPES
    if unknown:
        names = ", ".join(sorted(unknown))
        raise GmailAuthError(f"Gmail token has unexpected scopes: {names}")
    missing = GMAIL_ALLOWED_SCOPES - granted
    if missing:
        names = ", ".join(sorted(missing))
        raise GmailAuthError(f"Gmail token is missing required scopes: {names}")
    return tuple(scope for scope in SCOPES if scope in granted)


def validate_address(value: str) -> str:
    """Return a bare ``addr-spec`` for one recipient, rejecting header injection."""
    if not isinstance(value, str):
        raise ValueError("recipient must be text")
    candidate = value.strip()
    if not candidate:
        raise ValueError("recipient is empty")
    if len(candidate) > 254:
        raise ValueError("recipient is too long")
    if _CONTROL.search(candidate):
        raise ValueError("recipient contains control characters")
    pairs = getaddresses([candidate])
    if len(pairs) != 1:
        raise ValueError("each entry must contain exactly one recipient address")
    _name, address = parseaddr(candidate)
    if not address or not _ADDRESS.fullmatch(address):
        raise ValueError("recipient is not a valid email address")
    local, _at, domain = address.partition("@")
    if local.startswith(".") or local.endswith(".") or ".." in local or not domain:
        raise ValueError("recipient is not a valid email address")
    return address


def validate_recipients(values: Iterable[str]) -> tuple[str, ...]:
    """Validate one to twenty recipients and return their bare addresses."""
    items = list(values)
    if not items:
        raise ValueError("at least one recipient is required")
    if len(items) > MAX_RECIPIENTS:
        raise ValueError(f"at most {MAX_RECIPIENTS} recipients are allowed")
    return tuple(validate_address(item) for item in items)


def validate_subject(subject: str) -> str:
    """Return a single-line subject of at most 200 characters."""
    if not isinstance(subject, str):
        raise ValueError("subject must be text")
    if len(subject) > MAX_SUBJECT_CHARS:
        raise ValueError(f"subject must be at most {MAX_SUBJECT_CHARS} characters")
    if _CONTROL.search(subject):
        raise ValueError("subject must be a single line without control characters")
    return subject


def validate_body(body: str) -> str:
    """Return a draft body of at most 100000 characters, unchanged."""
    if not isinstance(body, str):
        raise ValueError("body must be text")
    if len(body) > MAX_DRAFT_BODY_CHARS:
        raise ValueError(f"body must be at most {MAX_DRAFT_BODY_CHARS} characters")
    return body


def sanitize_filename(
    name: str,
    *,
    fallback: str = "attachment.bin",
    max_length: int = 120,
) -> str:
    """Turn a sender-controlled attachment name into a safe bare filename."""
    candidate = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    candidate = _INVISIBLE.sub("", candidate)
    candidate = _FILENAME_BAD.sub("_", candidate).strip().strip(".")
    if not candidate or candidate in {".", ".."}:
        return fallback
    stem, dot, suffix = candidate.rpartition(".")
    if not dot:
        stem, suffix = candidate, ""
    if stem.lower() in _RESERVED_STEMS:
        stem = f"_{stem}"
    suffix = f".{suffix}" if suffix else ""
    room = max(1, max_length - len(suffix))
    stem = stem[:room].rstrip(". ")
    if not stem:
        return fallback
    return f"{stem}{suffix}"[:max_length]


def _clean_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GmailClientError(f"{label} is required")
    candidate = value.strip()
    if not _ID_VALUE.fullmatch(candidate):
        raise GmailClientError(f"{label} is invalid")
    return candidate


def _decode_base64url(value: str) -> bytes:
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise GmailClientError("Gmail returned a non-ASCII payload") from exc
    try:
        return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
    except (binascii.Error, ValueError) as exc:
        raise GmailClientError("Gmail returned an invalid base64 payload") from exc


def _encode_base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _iter_payloads(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield payload
    for part in payload.get("parts") or ():
        if isinstance(part, dict):
            yield from _iter_payloads(part)


def _html_to_text(raw: str) -> str:
    """Strip HTML to visible text; scripts, styles and hidden nodes are dropped."""
    sanitized = sanitize_text(raw, html=True, max_chars=MAX_BODY_CHARS, redact=False)
    return sanitized.text


def _normalise_body(text: str) -> tuple[str, bool]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > MAX_BODY_CHARS:
        return text[:MAX_BODY_CHARS].rstrip(), True
    return text, False


def _restrict_permissions(path: Path) -> None:
    """Best effort 0600: full effect on POSIX, write-flag semantics on Windows."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        log.warning("could not restrict permissions on the Gmail token file")


def default_token_path(profile: str | Path | None = None) -> Path:
    """Return ``<profile>/gmail-token.json`` or ``<home>/oauth/gmail-token.json``."""
    if profile is not None:
        return Path(profile).expanduser() / TOKEN_FILENAME
    return get_paths().home / "oauth" / TOKEN_FILENAME


def default_client_secret_path() -> Path:
    """Return ``<repo>/config/gmail-client.json`` (gitignored, never committed)."""
    return project_root() / DEFAULT_CLIENT_SECRET_PATH


@dataclass(frozen=True, slots=True)
class MessageSummary:
    """Metadata returned by ``list``/``search``; the body is not fetched."""

    id: str
    thread_id: str = ""
    subject: str = ""
    from_: str = ""
    to: str = ""
    date: str = ""
    snippet: str = ""
    labels: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "subject": self.subject,
            "from": self.from_,
            "to": self.to,
            "date": self.date,
            "snippet": self.snippet,
            "labels": list(self.labels),
        }


@dataclass(frozen=True, slots=True)
class AttachmentInfo:
    """Attachment metadata: name, MIME type, declared size and API id."""

    filename: str
    mime_type: str = "application/octet-stream"
    size: int = 0
    attachment_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size": self.size,
            "attachment_id": self.attachment_id,
        }


@dataclass(frozen=True, slots=True)
class Message:
    """A full message: headers, plain-text body and attachment metadata."""

    id: str
    thread_id: str = ""
    subject: str = ""
    from_: str = ""
    to: str = ""
    date: str = ""
    snippet: str = ""
    body: str = ""
    labels: tuple[str, ...] = ()
    attachments: tuple[AttachmentInfo, ...] = ()
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "subject": self.subject,
            "from": self.from_,
            "to": self.to,
            "date": self.date,
            "snippet": self.snippet,
            "body": self.body,
            "labels": list(self.labels),
            "attachments": [item.as_dict() for item in self.attachments],
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class DraftInfo:
    """The result of ``drafts.create``: an id, never a sent message."""

    draft_id: str
    message_id: str = ""
    thread_id: str = ""
    to: tuple[str, ...] = ()
    subject: str = ""
    body_chars: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "to": list(self.to),
            "subject": self.subject,
            "body_chars": self.body_chars,
            "sent": False,
        }


class GmailAuth:
    """OAuth for Gmail: readonly + compose scopes, a token on disk, refresh."""

    def __init__(
        self,
        *,
        client_secret_path: str | Path | None = None,
        token_path: str | Path | None = None,
        profile: str | Path | None = None,
        scopes: Sequence[str] | None = None,
        request_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.client_secret_path = (
            Path(client_secret_path).expanduser()
            if client_secret_path is not None
            else default_client_secret_path()
        )
        if token_path is not None:
            self.token_path = Path(token_path).expanduser()
        else:
            self.token_path = default_token_path(profile)
        self.scopes = validate_scopes(SCOPES if scopes is None else scopes)
        self._request_factory = request_factory

    # -- token storage ---------------------------------------------------
    @property
    def has_token(self) -> bool:
        """True when a token file exists; its scopes are checked on load."""
        return self.token_path.is_file()

    def load_token(self) -> Credentials:
        """Load and validate the stored token; the token itself is never logged."""
        if not self.token_path.is_file():
            raise GmailAuthError(
                f"no Gmail token at {self.token_path}; run GmailAuth.authorize() first"
            )
        try:
            info = json.loads(self.token_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise GmailAuthError(f"cannot read the Gmail token at {self.token_path}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GmailAuthError(
                "the Gmail token file is corrupt; re-run GmailAuth.authorize()"
            ) from exc
        if not isinstance(info, dict):
            raise GmailAuthError("the Gmail token file has an unexpected shape")
        scopes = validate_scopes(info.get("scopes"))
        try:
            from google.oauth2.credentials import Credentials
        except ImportError as exc:
            raise GmailAuthError("google-auth is not installed") from exc
        try:
            credentials = Credentials.from_authorized_user_info(info, scopes=list(scopes))
        except (KeyError, TypeError, ValueError) as exc:
            raise GmailAuthError(
                "the Gmail token file is incomplete; re-run GmailAuth.authorize()"
            ) from exc
        validate_scopes(_credential_scopes(credentials) or scopes)
        return credentials

    def save_token(self, credentials: Any) -> Path:
        """Write the token as JSON atomically and restrict its permissions."""
        scopes = validate_scopes(_credential_scopes(credentials) or self.scopes)
        try:
            info = json.loads(credentials.to_json())
        except (AttributeError, TypeError, ValueError) as exc:
            raise GmailAuthError("cannot serialise Gmail credentials") from exc
        if not isinstance(info, dict):
            raise GmailAuthError("cannot serialise Gmail credentials")
        info["scopes"] = list(scopes)
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.token_path.with_name(f".{self.token_path.name}.{uuid4().hex}.tmp")
        payload = json.dumps(info, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            temporary.write_text(payload, encoding="utf-8")
            _restrict_permissions(temporary)
            os.replace(temporary, self.token_path)
        except OSError as exc:
            raise GmailAuthError(
                f"cannot write the Gmail token at {self.token_path}"
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)
        _restrict_permissions(self.token_path)
        return self.token_path

    # -- credentials -----------------------------------------------------
    def credentials(self) -> Credentials:
        """Return usable credentials, refreshing an expired access token once."""
        credentials = self.load_token()
        if not bool(getattr(credentials, "expired", False)):
            return credentials
        if not getattr(credentials, "refresh_token", None):
            raise GmailAuthError(
                "the Gmail token expired and has no refresh token; re-run "
                "GmailAuth.authorize()"
            )
        return self.refresh(credentials)

    def refresh(self, credentials: Any) -> Any:
        """Refresh an expired token and persist it; errors never echo the token."""
        request = self._build_request()
        try:
            credentials.refresh(request)
        except Exception as exc:  # noqa: BLE001 - turn any transport failure into auth error
            raise GmailAuthError(
                f"cannot refresh the Gmail token: {redact(str(exc))[:200]}"
            ) from exc
        scopes = validate_scopes(_credential_scopes(credentials) or self.scopes)
        if scopes != self.scopes:
            raise GmailAuthError("the refreshed Gmail token changed its scopes")
        self.save_token(credentials)
        log.info("refreshed the Gmail OAuth token")
        return credentials

    def _build_request(self) -> Any:
        if self._request_factory is not None:
            return self._request_factory()
        try:
            from google.auth.transport.requests import Request
        except ImportError as exc:
            raise GmailAuthError("google-auth is not installed") from exc
        return Request()

    # -- interactive OAuth ----------------------------------------------
    def authorize(
        self,
        flow_factory: Callable[[Path, Sequence[str]], Any] | None = None,
        *,
        open_browser: bool = True,
    ) -> Credentials:
        """Reuse a stored token or run the OAuth flow; tests inject the flow."""
        if self.token_path.is_file():
            return self.credentials()
        if not self.client_secret_path.is_file():
            raise GmailAuthError(
                f"Gmail OAuth client secret not found: {self.client_secret_path}; "
                "download the desktop client JSON from Google Cloud Console"
            )
        factory = flow_factory or self._default_flow
        try:
            flow = factory(self.client_secret_path, list(self.scopes))
            credentials = flow.run_local_server(
                host="localhost",
                port=0,
                open_browser=open_browser,
                authorization_prompt_message="Open this URL to authorize Kivi: {url}",
                success_message="Kivi is authorized. You can close this window.",
            )
        except GmailAuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed OAuth flow is an auth error
            raise GmailAuthError(f"Gmail OAuth failed: {redact(str(exc))[:200]}") from exc
        self.save_token(credentials)
        log.info("authorized Gmail with readonly and compose scopes")
        return credentials

    @staticmethod
    def _default_flow(client_secret_path: Path, scopes: Sequence[str]) -> Any:
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:
            raise GmailAuthError("google-auth-oauthlib is not installed") from exc
        try:
            return InstalledAppFlow.from_client_secrets_file(
                str(client_secret_path), list(scopes)
            )
        except (KeyError, OSError, ValueError) as exc:
            raise GmailAuthError(
                f"cannot read the Gmail client secret: {type(exc).__name__}"
            ) from exc


class GmailClient:
    """Small, injectable Gmail v1 client: read, search, draft, attachments."""

    def __init__(
        self,
        service: Any = None,
        credentials: Any = None,
        *,
        max_results: int = 50,
    ) -> None:
        if not 1 <= int(max_results) <= MAX_RESULTS_CEILING:
            raise ValueError(f"max_results must be between 1 and {MAX_RESULTS_CEILING}")
        self._service = service
        self._credentials = credentials
        self.max_results = int(max_results)

    # -- service ---------------------------------------------------------
    @property
    def service(self) -> Any:
        """Build the discovery service lazily; an injected service is used as-is."""
        if self._service is None:
            if self._credentials is None:
                raise GmailClientError(
                    "Gmail client needs credentials or an injected service"
                )
            try:
                from googleapiclient.discovery import build
            except ImportError as exc:
                raise GmailClientError(
                    "google-api-python-client is not installed"
                ) from exc
            try:
                self._service = build(
                    "gmail",
                    "v1",
                    credentials=self._credentials,
                    cache_discovery=False,
                )
            except Exception as exc:  # noqa: BLE001 - typed client error, secrets redacted
                raise GmailClientError(
                    f"cannot build the Gmail service: {redact(str(exc))[:200]}"
                ) from exc
        return self._service

    def _execute(self, request: Any) -> dict[str, Any]:
        try:
            response = request.execute()
        except Exception as exc:  # noqa: BLE001 - every API failure becomes a client error
            raise GmailClientError(
                f"Gmail API error ({type(exc).__name__}): {redact(str(exc))[:300]}"
            ) from exc
        if not isinstance(response, dict):
            raise GmailClientError("Gmail API returned an unexpected response")
        return response

    def _limit(self, value: int) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError) as exc:
            raise GmailClientError("max_results must be an integer") from exc
        return max(1, min(limit, self.max_results, MAX_RESULTS_CEILING))

    def _get_raw(
        self,
        message_id: str,
        *,
        fmt: str = "full",
        metadata_headers: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "userId": "me",
            "id": _clean_id(message_id, "message id"),
            "format": fmt,
        }
        if metadata_headers:
            params["metadataHeaders"] = list(metadata_headers)
        return self._execute(self.service.users().messages().get(**params))

    # -- reading ---------------------------------------------------------
    def list_messages(
        self,
        query: str = "",
        max_results: int = 10,
        label_ids: Sequence[str] | None = None,
    ) -> list[MessageSummary]:
        """Return message summaries matching ``query`` (Gmail search syntax)."""
        limit = self._limit(max_results)
        params: dict[str, Any] = {"userId": "me", "maxResults": limit}
        if query.strip():
            params["q"] = query.strip()
        if label_ids:
            labels = [str(item).strip() for item in label_ids if str(item).strip()]
            if len(labels) > 50:
                raise GmailClientError("at most 50 label ids are allowed")
            if labels:
                params["labelIds"] = labels
        response = self._execute(self.service.users().messages().list(**params))
        summaries: list[MessageSummary] = []
        for reference in (response.get("messages") or [])[:limit]:
            if not isinstance(reference, dict):
                continue
            message_id = str(reference.get("id") or "").strip()
            if not message_id:
                continue
            raw = self._get_raw(
                message_id,
                fmt="metadata",
                metadata_headers=_SUMMARY_HEADERS,
            )
            summaries.append(self._summary(raw))
        return summaries

    def search(self, query: str, max_results: int = 10) -> list[MessageSummary]:
        """Alias for :meth:`list_messages` with an explicit query."""
        return self.list_messages(query=query, max_results=max_results)

    def get_message(self, message_id: str) -> Message:
        """Return headers, the plain-text body and attachment metadata."""
        raw = self._get_raw(message_id)
        summary = self._summary(raw)
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        body, attachments, truncated = self._parse_payload(payload)
        return Message(
            id=summary.id or message_id,
            thread_id=summary.thread_id,
            subject=summary.subject,
            from_=summary.from_,
            to=summary.to,
            date=summary.date,
            snippet=summary.snippet,
            body=body,
            labels=summary.labels,
            attachments=attachments,
            truncated=truncated,
        )

    @staticmethod
    def _summary(raw: dict[str, Any]) -> MessageSummary:
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        headers: dict[str, str] = {}
        for item in payload.get("headers") or ():
            if isinstance(item, dict) and item.get("name"):
                headers[str(item["name"]).lower()] = str(item.get("value") or "")
        return MessageSummary(
            id=str(raw.get("id") or ""),
            thread_id=str(raw.get("threadId") or ""),
            subject=headers.get("subject", ""),
            from_=headers.get("from", ""),
            to=headers.get("to", ""),
            date=headers.get("date", ""),
            snippet=str(raw.get("snippet") or ""),
            labels=tuple(str(label) for label in raw.get("labelIds") or ()),
        )

    @staticmethod
    def _parse_payload(
        payload: dict[str, Any],
    ) -> tuple[str, tuple[AttachmentInfo, ...], bool]:
        plain: list[str] = []
        html: list[str] = []
        attachments: list[AttachmentInfo] = []
        for part in _iter_payloads(payload):
            mime = str(part.get("mimeType") or "").lower()
            body = part.get("body") if isinstance(part.get("body"), dict) else {}
            filename = str(part.get("filename") or "")
            attachment_id = str(body.get("attachmentId") or "")
            if filename or attachment_id:
                attachments.append(
                    AttachmentInfo(
                        filename=sanitize_filename(filename or "attachment.bin"),
                        mime_type=mime or "application/octet-stream",
                        size=_as_int(body.get("size")),
                        attachment_id=attachment_id,
                    )
                )
                continue
            data = body.get("data")
            if not isinstance(data, str) or not data:
                continue
            if mime == "text/html":
                html.append(data)
            elif mime in ("", "text/plain"):
                plain.append(data)
        if plain:
            text = _decode_base64url(plain[0]).decode("utf-8", errors="replace")
        elif html:
            text = _html_to_text(
                _decode_base64url(html[0]).decode("utf-8", errors="replace")
            )
        else:
            text = ""
        body_text, truncated = _normalise_body(text)
        return body_text, tuple(attachments), truncated

    # -- drafts ----------------------------------------------------------
    def create_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        thread_id: str | None = None,
    ) -> DraftInfo:
        """Create a Gmail draft from a plain-text MIME message; never send it."""
        try:
            recipients = validate_recipients(to)
            clean_subject = validate_subject(subject)
            clean_body = validate_body(body)
        except ValueError as exc:
            raise GmailClientError(str(exc)) from exc
        message = EmailMessage()
        message["To"] = ", ".join(recipients)
        message["Subject"] = clean_subject
        message["MIME-Version"] = "1.0"
        message.set_content(clean_body, subtype="plain", charset="utf-8")
        payload: dict[str, Any] = {
            "message": {"raw": _encode_base64url(message.as_bytes())}
        }
        if thread_id is not None and str(thread_id).strip():
            payload["message"]["threadId"] = _clean_id(thread_id, "thread id")
        response = self._execute(
            self.service.users().drafts().create(userId="me", body=payload)
        )
        draft_id = str(response.get("id") or "").strip()
        if not draft_id:
            raise GmailClientError("Gmail did not return a draft id")
        message_info = response.get("message")
        if not isinstance(message_info, dict):
            message_info = {}
        return DraftInfo(
            draft_id=draft_id,
            message_id=str(message_info.get("id") or ""),
            thread_id=str(message_info.get("threadId") or thread_id or ""),
            to=recipients,
            subject=clean_subject,
            body_chars=len(clean_body),
        )

    # -- attachments -----------------------------------------------------
    def save_attachment(
        self,
        message_id: str,
        attachment_id: str,
        target_dir: str | Path,
    ) -> Path:
        """Save one attachment under an already-validated absolute directory.

        The caller (the ``mail.attachment`` tool) is responsible for passing a
        workspace directory that passed the Protected Zones check; this method
        adds filename sanitisation, a 20 MB cap and no-overwrite naming.
        """
        message_key = _clean_id(message_id, "message id")
        attachment_key = _clean_id(attachment_id, "attachment id")
        directory = Path(target_dir)
        if not directory.is_absolute():
            raise GmailClientError("attachment directory must be absolute")
        message = self.get_message(message_key)
        info = next(
            (item for item in message.attachments if item.attachment_id == attachment_key),
            None,
        )
        if info is None:
            raise GmailClientError("the attachment is not part of this message")
        response = self._execute(
            self.service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_key, id=attachment_key)
        )
        encoded = response.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise GmailClientError("Gmail returned an empty attachment")
        declared = _as_int(response.get("size")) or info.size
        if declared > MAX_ATTACHMENT_BYTES:
            raise GmailClientError(
                f"attachment is larger than {MAX_ATTACHMENT_BYTES} bytes"
            )
        if len(encoded) > (MAX_ATTACHMENT_BYTES * 4) // 3 + 8:
            raise GmailClientError(
                f"attachment is larger than {MAX_ATTACHMENT_BYTES} bytes"
            )
        data = _decode_base64url(encoded)
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise GmailClientError(
                f"attachment is larger than {MAX_ATTACHMENT_BYTES} bytes"
            )
        directory.mkdir(parents=True, exist_ok=True)
        root = directory.resolve()
        target = _unique_path(root / sanitize_filename(info.filename))
        if target.parent != root:
            raise GmailClientError("attachment path escaped the target directory")
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target


def _unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    for index in range(1, 1000):
        candidate = target.with_name(f"{target.stem}-{index}{target.suffix}")
        if not candidate.exists():
            return candidate
    raise GmailClientError("cannot find a free filename for the attachment")


__all__ = [
    "DEFAULT_CLIENT_SECRET_PATH",
    "FORBIDDEN_SCOPES",
    "GMAIL_COMPOSE_SCOPE",
    "GMAIL_FULL_SCOPE",
    "GMAIL_MODIFY_SCOPE",
    "GMAIL_READONLY_SCOPE",
    "GMAIL_SEND_SCOPE",
    "MAX_ATTACHMENT_BYTES",
    "MAX_BODY_CHARS",
    "MAX_DRAFT_BODY_CHARS",
    "MAX_RECIPIENTS",
    "MAX_RESULTS_CEILING",
    "MAX_SUBJECT_CHARS",
    "SCOPES",
    "AttachmentInfo",
    "DraftInfo",
    "GmailAuth",
    "GmailAuthError",
    "GmailClient",
    "GmailClientError",
    "Message",
    "MessageSummary",
    "default_client_secret_path",
    "default_token_path",
    "redact",
    "sanitize_filename",
    "validate_address",
    "validate_body",
    "validate_recipients",
    "validate_scopes",
    "validate_subject",
]
