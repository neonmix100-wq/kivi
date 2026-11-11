"""Per-order call history: transcripts and structured summaries on disk.

Only text is stored: one JSON record per call under
``<root>/<safe_order_code>/`` (normally ``data/calls/<order>/``). Audio is never
written to this store. Writes are atomic (temporary file + ``os.replace``), so a
crash cannot leave a half-written record. The digest is ordered oldest to newest
and drops the oldest lines when the character budget is exhausted.

Adapted from D:\\ai-agent\\modes\\work
"""

# Adapted from D:\ai-agent\modes\work

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kivi.modules.work_crm.extractor import ExtractError, parse_json_loose

log = logging.getLogger(__name__)

LLMCallable = Callable[[list[dict[str, str]]], str]
TranscriptInput = str | Sequence[Any]

OUTCOMES = ["agreed", "refused", "thinking", "no_conversation", "unclear"]
OUTCOME_RU = {
    "agreed": "клиент согласился",
    "refused": "клиент отказался",
    "thinking": "клиент думает",
    "no_conversation": "разговора по сути не было",
    "unclear": "итог неясен",
}

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": OUTCOMES},
        "summary": {"type": "string"},
        "offers": {"type": "array", "items": {"type": "string"}},
        "objections": {"type": "array", "items": {"type": "string"}},
        "numbers": {"type": "array", "items": {"type": "string"}},
        "promises": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["outcome", "summary", "offers", "objections", "numbers", "promises"],
}

SUMMARY_SYSTEM = """Ты помощник менеджера сервисного центра. Тебе дают автоматическую
расшифровку ОДНОГО прошедшего телефонного разговора по заказу. Реплики помечены
«Голос 1» / «Голос 2» (отдельные дорожки записи) или «Участник» (дорожки не
разделены). Сам определи по смыслу, кто менеджер сервисного центра, а кто клиент.
Расшифровка может содержать ошибки распознавания. Заполни JSON строго по тексту,
ничего не выдумывай:
- outcome: agreed — клиент согласился на ремонт/условия; refused — отказался;
  thinking — взял время подумать/перезвонит; no_conversation — автоответчик, обрыв,
  пустой разговор; unclear — неясно. «agreed» ставь только если клиент явно согласился.
- summary: 1–3 коротких предложения о сути разговора.
- offers: что предлагал менеджер (цена, скидка, срок, гарантия, альтернативы) —
  короткими фразами.
- objections: возражения и причины сомнений клиента.
- numbers: названные суммы, сроки, гарантии (как прозвучали, например «15 тысяч за
  плату», «2–3 недели»).
- promises: что обещали сделать стороны (перезвонить, прислать, проверить).
Отвечай только JSON."""

_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class HistoryError(ValueError):
    """Raised when a call summary cannot be obtained or parsed."""


def safe_order_name(order_code: str) -> str:
    """Make a filesystem-safe directory name for an order code."""
    name = re.sub(r"[^\w\-]", "_", str(order_code), flags=re.UNICODE)[:60] or "order"
    if name.upper() in _RESERVED_WINDOWS_NAMES:
        name = "_" + name
    return name


def _string_list(value: Any) -> list[str]:
    """Coerce an optional model list into a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_summary(raw: Any) -> dict[str, Any]:
    """Coerce a summary answer to the stable six-key structure."""
    source = raw if isinstance(raw, Mapping) else {}
    outcome = source.get("outcome") if source.get("outcome") in OUTCOMES else "unclear"
    return {
        "outcome": outcome,
        "summary": str(source.get("summary") or "").strip(),
        "offers": _string_list(source.get("offers")),
        "objections": _string_list(source.get("objections")),
        "numbers": _string_list(source.get("numbers")),
        "promises": _string_list(source.get("promises")),
    }


def transcript_text(transcript: TranscriptInput) -> str:
    """Render a transcript string or an utterance list as ``speaker: text``."""
    if isinstance(transcript, str):
        return transcript
    lines: list[str] = []
    for item in transcript:
        if isinstance(item, str):
            lines.append(item)
        elif isinstance(item, Mapping):
            speaker = str(item.get("speaker") or item.get("who") or "").strip()
            text = str(item.get("text") or "").strip()
            lines.append(f"{speaker}: {text}" if speaker else text)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            if len(item) == 3:
                _, speaker, text = item
                lines.append(f"{speaker}: {text}")
            elif len(item) == 2:
                speaker, text = item
                lines.append(f"{speaker}: {text}")
            else:
                lines.append(str(item))
        else:
            lines.append(str(item))
    return "\n".join(line for line in lines if line.strip())


def summarize(llm: LLMCallable, transcript: TranscriptInput) -> dict[str, Any]:
    """Summarize one call with the LLM; short/empty calls bypass the model."""
    text = transcript_text(transcript).strip()
    if len(re.findall(r"\w+", text)) < 8:
        return normalize_summary(
            {"outcome": "no_conversation", "summary": "Разговора по сути не было."}
        )
    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": "Расшифровка разговора:\n" + text[:6000]},
    ]
    try:
        answer = llm(messages)
    except Exception as exc:  # noqa: BLE001 - any backend failure is a history error
        raise HistoryError(f"LLM summarization failed: {exc}") from exc
    try:
        raw = parse_json_loose(answer)
    except ExtractError as exc:
        raise HistoryError(str(exc)) from exc
    return normalize_summary(raw)


def _atomic_write(path: Path, payload: str) -> None:
    """Write ``payload`` to ``path`` atomically, cleaning up the temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


class CallHistory:
    """Text-only call history rooted at ``data/calls`` by convention."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _order_dir(self, order_code: str) -> Path:
        return self.root / safe_order_name(order_code)

    def save(
        self,
        order_code: str,
        text: str,
        summary: Mapping[str, Any] | str | None,
    ) -> dict[str, Any]:
        """Store one call transcript and summary, returning the written record."""
        body = str(text)
        now = datetime.now(UTC)
        if isinstance(summary, Mapping):
            normalized = normalize_summary(summary)
        else:
            normalized = normalize_summary({"summary": str(summary or "")})
        digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:8]
        filename = f"{now:%Y%m%dT%H%M%S%f}_{digest}.json"
        record = {
            "order_code": str(order_code),
            "saved_at": now.isoformat(),
            "time": now.strftime("%d.%m.%Y %H:%M"),
            "text": body,
            "summary": normalized,
        }
        payload = json.dumps(record, ensure_ascii=False, indent=1) + "\n"
        _atomic_write(self._order_dir(order_code) / filename, payload)
        return record

    def list(self, order_code: str) -> list[dict[str, Any]]:
        """Return all readable records for an order, oldest first."""
        folder = self._order_dir(order_code)
        if not folder.exists():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(folder.glob("*.json")):
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                log.warning("skipping unreadable call record %s", path)
                continue
            if isinstance(loaded, dict):
                records.append(loaded)
        return sorted(records, key=lambda record: str(record.get("saved_at") or ""))

    @staticmethod
    def _digest_line(record: Mapping[str, Any]) -> str:
        """Render one call record as one compact digest line."""
        when = str(record.get("time") or record.get("saved_at") or "?")
        summary = record.get("summary")
        if not isinstance(summary, Mapping) or not str(
            summary.get("summary") or ""
        ).strip():
            return f"• {when}: звонок без итога."
        outcome = OUTCOME_RU.get(str(summary.get("outcome")), OUTCOME_RU["unclear"])
        parts = [f"• {when} — {outcome}. {str(summary.get('summary')).strip()}".strip()]
        if summary.get("offers"):
            parts.append("Предлагали: " + "; ".join(_string_list(summary["offers"])) + ".")
        if summary.get("objections"):
            parts.append(
                "Возражения клиента: "
                + "; ".join(_string_list(summary["objections"]))
                + "."
            )
        if summary.get("numbers"):
            parts.append("Называлось: " + "; ".join(_string_list(summary["numbers"])) + ".")
        if summary.get("promises"):
            parts.append("Обещания: " + "; ".join(_string_list(summary["promises"])) + ".")
        return " ".join(parts)

    def digest(self, order_code: str, max_chars: int = 4000) -> str:
        """Return a compact call history for coaching, newest lines kept."""
        if max_chars <= 0:
            return ""
        lines = [self._digest_line(record) for record in self.list(order_code)]
        while lines and sum(len(line) + 1 for line in lines) > max_chars:
            lines.pop(0)
        return "\n".join(lines)
