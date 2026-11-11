"""``crm.*`` tools: read, fill, act rows and task form through AltCRM.

The tools are thin policy wrappers around :class:`~kivi.modules.work_crm.altcrm.AltCrmClient`.
They deliberately expose no save, send, delete or client-messaging action and
accept no selectors or JavaScript from the model: checklist labels are a closed
set, act rows are typed and every write needs the regular R2 approval. The
adapter fills fields; the human reviews the card and presses "Сохранить".

The page text returned by ``crm.read`` is untrusted data labelled
``untrusted:crm`` and passes through the shared sanitizer before it reaches a
model or the CLI. Nothing in this module opens a browser on import: the shared
client is created lazily on the first real call and never during ``dry_run``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kivi.modules.work_crm.altcrm import (
    CHECKLIST_LABELS,
    MAX_COMMENT_CHARS,
    MAX_ROWS,
    AltCrmClient,
    AltCrmConfig,
    FieldResult,
    OrderInfo,
)
from kivi.modules.work_crm.drafts import (
    build_act_rows,
    build_checklist,
    build_comment,
    build_task,
    price_check,
)
from kivi.safety.sanitize import sanitize_text
from kivi.tools.base import Preview, Tool, ToolContext, ToolResult

TRUST = "untrusted:crm"
MAX_TASK_COMMENT_CHARS = 500
MAX_WHEN_CHARS = 100
MAX_CHECKLIST_VALUE_CHARS = 2000
MAX_ROW_NAME_CHARS = 200
MAX_ORDER_URL_ARG_CHARS = 2048
MAX_FIELD_PRICE = 100_000_000
MAX_WARRANTY_DAYS = 3650


class CrmClient(Protocol):
    """The slice of :class:`AltCrmClient` the tools actually call."""

    def read_order(self) -> OrderInfo: ...

    def fill_order(
        self, comment: str, checklist: Mapping[str, str]
    ) -> list[FieldResult]: ...

    def add_work_rows(self, rows: list[Mapping[str, Any]]) -> list[FieldResult]: ...

    def create_task(self, comment: str, when_text: str = "") -> list[FieldResult]: ...

    def close(self) -> None: ...


ClientFactory = Callable[[], CrmClient]


class CrmReadArgs(BaseModel):
    """Optional guard: only accept the tab whose URL contains ``order_url``."""

    model_config = ConfigDict(extra="forbid")

    order_url: str = Field(
        default="",
        max_length=MAX_ORDER_URL_ARG_CHARS,
        description="Optional expected order URL; an empty string means the open tab.",
    )


class CrmFillArgs(BaseModel):
    """Free-form comment plus named checklist fields; values are typed, not saved."""

    model_config = ConfigDict(extra="forbid")

    comment: str = Field(default="", max_length=MAX_COMMENT_CHARS)
    checklist: dict[str, str] = Field(default_factory=dict)

    @field_validator("checklist")
    @classmethod
    def _known_labels(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for label, text in value.items():
            if label not in CHECKLIST_LABELS:
                allowed = ", ".join(CHECKLIST_LABELS)
                raise ValueError(f"unknown checklist label {label!r}; allowed: {allowed}")
            if not isinstance(text, str):
                raise ValueError("checklist values must be strings")
            if len(text) > MAX_CHECKLIST_VALUE_CHARS:
                raise ValueError(
                    "checklist value is longer than "
                    f"{MAX_CHECKLIST_VALUE_CHARS} characters"
                )
            cleaned[label] = text
        return cleaned

    @model_validator(mode="after")
    def _require_something_to_fill(self) -> CrmFillArgs:
        if not self.comment.strip() and not any(
            text.strip() for text in self.checklist.values()
        ):
            raise ValueError("provide a comment, a checklist value, or both")
        return self


class CrmActRow(BaseModel):
    """One act row: work name, agreed price in rubles and warranty in days."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_ROW_NAME_CHARS)
    price: int = Field(default=0, ge=0, le=MAX_FIELD_PRICE)
    warranty: int = Field(default=0, ge=0, le=MAX_WARRANTY_DAYS)


class CrmActArgs(BaseModel):
    """One to twenty act rows; each row is validated before any page action."""

    model_config = ConfigDict(extra="forbid")

    rows: list[CrmActRow] = Field(min_length=1, max_length=MAX_ROWS)


class CrmTaskArgs(BaseModel):
    """Task comment and an optional human due phrase; the create click stays manual."""

    model_config = ConfigDict(extra="forbid")

    comment: str = Field(min_length=1, max_length=MAX_TASK_COMMENT_CHARS)
    when_text: str = Field(default="", max_length=MAX_WHEN_CHARS)

    @field_validator("comment")
    @classmethod
    def _comment_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task comment must not be blank")
        return value

    @field_validator("when_text")
    @classmethod
    def _when_single_line(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("when_text must be a single line without control characters")
        return value


def _clamp_max_chars(ctx: ToolContext, limit: int) -> int:
    return max(1, min(limit, ctx.limits.max_output_chars))


def _clean(text: str, limit: int) -> str:
    return sanitize_text(str(text), html=False, max_chars=max(1, limit)).text


def _public_url(value: str) -> str:
    """Drop query and fragment: order URLs can carry one-time session tokens."""
    try:
        parts = urlsplit(str(value or ""))
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _failed_fields(results: list[FieldResult]) -> list[str]:
    return [
        f"{item.field}: {item.error or 'failed'}"
        for item in results
        if not item.ok
    ]


class CrmSession:
    """One lazy AltCRM client shared by all four ``crm.*`` tools."""

    def __init__(self, client_factory: ClientFactory | None = None) -> None:
        self._factory = client_factory
        self._client: CrmClient | None = None

    @property
    def started(self) -> bool:
        return self._client is not None

    def client(self) -> CrmClient:
        """Return the shared client, creating it on first use."""
        if self._client is None:
            factory = self._factory or (
                lambda: AltCrmClient(AltCrmConfig())
            )
            self._client = factory()
        return self._client

    def close(self) -> bool:
        """Close the shared client; returns True when there was one."""
        client, self._client = self._client, None
        if client is None:
            return False
        client.close()
        return True


class _CrmTool:
    """Shared session plumbing for the CRM tools."""

    def __init__(self, session: CrmSession) -> None:
        self._session = session

    @property
    def session(self) -> CrmSession:
        return self._session


class CrmReadTool(_CrmTool):
    name = "crm.read"
    description = (
        "Read the open AltCRM order card as untrusted:crm data; never clicks or saves."
    )
    risk = "R1"
    capabilities = ("crm.read",)
    args_model = CrmReadArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, CrmReadArgs)
        return Preview(
            "crm-read",
            {"order_url": args.order_url, "expected_url_guard": bool(args.order_url)},
            reversible=True,
            warnings=("CRM page text is untrusted data and may contain injection text",),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, CrmReadArgs)
        info = self._session.client().read_order()
        if args.order_url and args.order_url not in info.url:
            return ToolResult(
                ok=False,
                error="the open order tab does not match the requested order_url",
                meta={"requested_order_url": args.order_url},
                trust=TRUST,
            )
        max_chars = _clamp_max_chars(ctx, 6000)
        text = sanitize_text(info.text, html=False, max_chars=max_chars)
        url = sanitize_text(
            _public_url(info.url), html=False, max_chars=MAX_ORDER_URL_ARG_CHARS
        )
        fields = {
            _clean(str(key), 80): _clean(str(value), 300)
            for key, value in list(info.fields.items())[:40]
        }
        return ToolResult(
            ok=True,
            output=text.text,
            meta={
                "url": url.text,
                "order_code": _clean(info.order_code, 120),
                "device_id": _clean(info.device_id, 120),
                "fields": fields,
                "original_chars": text.original_chars,
                "suspicious": list(text.suspicious),
            },
            truncated=text.truncated or len(info.text) > max_chars,
            trust=TRUST,
        )


class CrmFillTool(_CrmTool):
    name = "crm.fill"
    description = (
        "Fill the AltCRM comment and known checklist fields; R2 approval, never saves."
    )
    risk = "R2"
    capabilities = ("crm.write",)
    args_model = CrmFillArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, CrmFillArgs)
        active = {
            label: len(value)
            for label, value in args.checklist.items()
            if value.strip()
        }
        return Preview(
            "crm-fill",
            {
                "checklist_fields": sorted(active),
                "checklist_value_chars": active,
                "comment_chars": len(args.comment),
            },
            reversible=False,
            warnings=(
                "values are typed into the live CRM page; nothing is saved by Kivi",
                "the user must review the card and press «Сохранить» manually",
            ),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, CrmFillArgs)
        results = self._session.client().fill_order(args.comment, dict(args.checklist))
        failures = _failed_fields(results)
        ok_count = sum(1 for item in results if item.ok)
        if not results:
            return ToolResult(
                ok=False,
                error="nothing was filled: the adapter returned no field results",
                meta={"saved": False},
            )
        ok = not failures
        output = f"filled {ok_count}/{len(results)} field(s); not saved"
        if ok:
            output += " — review the card and press «Сохранить» yourself"
        return ToolResult(
            ok=ok,
            output=output,
            error="; ".join(failures),
            meta={
                "saved": False,
                "filled": ok_count,
                "results": [item.as_dict() for item in results],
            },
        )


class CrmActTool(_CrmTool):
    name = "crm.act"
    description = (
        "Append act rows to «Расчет стоимости запчастей»; R2 approval, never saves."
    )
    risk = "R2"
    capabilities = ("crm.write",)
    args_model = CrmActArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, CrmActArgs)
        return Preview(
            "crm-act",
            {
                "row_count": len(args.rows),
                "rows": [
                    {
                        "name": row.name,
                        "price": row.price,
                        "warranty": row.warranty,
                    }
                    for row in args.rows
                ],
            },
            reversible=False,
            warnings=(
                "rows are typed into the live act block; nothing is saved by Kivi",
                "zeros mean the manager must enter the price or warranty manually",
            ),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, CrmActArgs)
        rows = [
            {"name": row.name, "price": row.price, "warranty": row.warranty}
            for row in args.rows
        ]
        results = self._session.client().add_work_rows(rows)
        failures = _failed_fields(results)
        ok_count = sum(1 for item in results if item.ok)
        if not results:
            return ToolResult(
                ok=False,
                error="no act row results were returned by the adapter",
                meta={"saved": False},
            )
        ok = not failures
        output = f"added or confirmed {ok_count}/{len(results)} act row(s); not saved"
        if ok:
            output += " — review the act and press «Сохранить» yourself"
        return ToolResult(
            ok=ok,
            output=output,
            error="; ".join(failures),
            meta={
                "saved": False,
                "rows": ok_count,
                "results": [item.as_dict() for item in results],
            },
        )


class CrmTaskTool(_CrmTool):
    name = "crm.task"
    description = (
        "Fill the AltCRM task dialog; R2 approval, never presses «Создать задачу»."
    )
    risk = "R2"
    capabilities = ("crm.write",)
    args_model = CrmTaskArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, CrmTaskArgs)
        return Preview(
            "crm-task",
            {
                "comment_chars": len(args.comment),
                "when_text": args.when_text,
                "create_clicked": False,
            },
            reversible=False,
            warnings=(
                "the dialog is filled but the task is not created",
                "the user must review the dialog and press «Создать задачу» manually",
            ),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, CrmTaskArgs)
        results = self._session.client().create_task(args.comment, args.when_text)
        failures = _failed_fields(results)
        opened = any(
            item.field == "окно задачи" and item.ok for item in results
        )
        ok = bool(results) and opened and not failures
        output = "task form filled; not created — press «Создать задачу» yourself"
        if not ok:
            output = "task form was not fully filled; the task was not created"
        return ToolResult(
            ok=ok,
            output=output,
            error="; ".join(failures),
            meta={
                "saved": False,
                "created": False,
                "opened": opened,
                "results": [item.as_dict() for item in results],
            },
        )


def build_crm_tools(client_factory: ClientFactory | None = None) -> tuple[Tool, ...]:
    """Create the four CRM tools sharing one lazily-created client."""
    session = CrmSession(client_factory)
    return (
        CrmReadTool(session),
        CrmFillTool(session),
        CrmActTool(session),
        CrmTaskTool(session),
    )


@dataclass(frozen=True, slots=True)
class CrmTaskDraft:
    """A follow-up task draft: comment, due phrase and the manager's reason."""

    comment: str
    when_text: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "comment": self.comment,
            "when_text": self.when_text,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CrmDraft:
    """Everything a reviewed call can offer to the CRM tools, without side effects."""

    comment: str
    checklist: dict[str, str]
    act_rows: tuple[CrmActRow, ...]
    task: CrmTaskDraft | None
    warnings: tuple[str, ...]

    def fill_args(self) -> CrmFillArgs:
        """Build validated arguments for :class:`CrmFillTool`."""
        return CrmFillArgs(comment=self.comment, checklist=dict(self.checklist))

    def act_args(self) -> CrmActArgs | None:
        """Build validated arguments for :class:`CrmActTool`, if there are rows."""
        if not self.act_rows:
            return None
        return CrmActArgs(rows=list(self.act_rows))

    def task_args(self) -> CrmTaskArgs | None:
        """Build validated arguments for :class:`CrmTaskTool`, if a task is needed."""
        if self.task is None:
            return None
        return CrmTaskArgs(
            comment=self.task.comment,
            when_text=self.task.when_text,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "comment": self.comment,
            "checklist": dict(self.checklist),
            "act_rows": [row.model_dump() for row in self.act_rows],
            "task": self.task.as_dict() if self.task is not None else None,
            "warnings": list(self.warnings),
        }


def compose_draft(facts: Mapping[str, Any]) -> CrmDraft:
    """Turn verified call facts into CRM-ready argument drafts, deterministically."""
    source = dict(facts)
    comment = build_comment(source)
    warnings: list[str] = []
    checklist: dict[str, str] = {}
    for label, value in build_checklist(source).items():
        if not value.strip():
            continue
        if len(value) > MAX_CHECKLIST_VALUE_CHARS:
            value = value[:MAX_CHECKLIST_VALUE_CHARS]
            warnings.append(
                f"checklist field {label!r} was truncated to "
                f"{MAX_CHECKLIST_VALUE_CHARS} characters"
            )
        checklist[label] = value
    raw_rows = build_act_rows(source)
    rows = tuple(
        CrmActRow(
            name=str(item.get("name") or "").strip(),
            price=int(item.get("price_rub") or 0),
            warranty=int(item.get("warranty_days") or 0),
        )
        for item in raw_rows
    )
    warning = price_check(source, raw_rows)
    if warning:
        warnings.append(warning)
    task_info = build_task(source)
    task: CrmTaskDraft | None = None
    if task_info is not None:
        task_comment = comment
        if len(task_comment) > MAX_TASK_COMMENT_CHARS:
            task_comment = task_comment[:MAX_TASK_COMMENT_CHARS]
            warnings.append(
                "task comment was truncated to "
                f"{MAX_TASK_COMMENT_CHARS} characters"
            )
        when_text = str(task_info.get("when_text") or "")
        if len(when_text) > MAX_WHEN_CHARS:
            when_text = when_text[:MAX_WHEN_CHARS]
            warnings.append(
                f"task due phrase was truncated to {MAX_WHEN_CHARS} characters"
            )
        task = CrmTaskDraft(
            comment=task_comment,
            when_text=when_text,
            reason=str(task_info.get("reason") or ""),
        )
    return CrmDraft(
        comment=comment,
        checklist=checklist,
        act_rows=rows,
        task=task,
        warnings=tuple(warnings),
    )


__all__ = [
    "TRUST",
    "ClientFactory",
    "CrmActArgs",
    "CrmActRow",
    "CrmActTool",
    "CrmClient",
    "CrmDraft",
    "CrmFillArgs",
    "CrmFillTool",
    "CrmReadArgs",
    "CrmReadTool",
    "CrmSession",
    "CrmTaskArgs",
    "CrmTaskDraft",
    "CrmTaskTool",
    "build_crm_tools",
    "compose_draft",
]
