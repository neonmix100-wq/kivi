"""Headless facts extraction from call transcripts using an injected LLM callable.

The extractor owns the prompt, tolerant JSON parsing and transcript helpers but
never imports Ollama, tkinter or the old ``core``/``modes`` packages. The LLM is
any ``Callable[[list[dict[str, str]]], str]``; tests use a fake, production code
can pass a gateway adapter.

Transcripts may use human labels (``Менеджер:`` / ``Клиент:``), stereo track
labels (``Голос 1`` is the manager microphone, ``Голос 2`` is the client) or a
single ``Участник`` track. Only verified client lines can support an ``agreed``
status, so a mono track without speaker separation cannot produce agreements.

Adapted from D:\\ai-agent\\modes\\work
"""

# Adapted from D:\ai-agent\modes\work

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kivi.modules.work_crm.schema import FACTS_SCHEMA, normalize

LLMCallable = Callable[[list[dict[str, str]]], str]

CLIENT = "Клиент"
MANAGER = "Менеджер"
UNKNOWN = "?"

SYSTEM = """Ты помощник менеджера сервисного центра. Тебе дают расшифровку телефонного
разговора менеджера с клиентом (строки «Менеджер: ...» и «Клиент: ...»; если запись
разделена на дорожки — «Голос 1» это менеджер, «Голос 2» это клиент; «Участник»
означает, что дорожки не разделены). Расшифровка автоматическая и может содержать ошибки.
Заполни JSON по схеме. Строгие правила:
1. Статус «agreed» ставь ТОЛЬКО если КЛИЕНТ явно согласился именно с этим пунктом
   («да», «ок», «делайте», «устраивает»). Если менеджер предложил, а клиент не ответил,
   сомневается, торгуется или говорит «подумаю» — статус «discussed».
   Если пункт не упоминался — «not_mentioned» и пустое значение.
2. В поле evidence для «agreed» дай ДОСЛОВНУЮ цитату слов клиента, где он согласился;
   для «discussed» — дословную цитату, где пункт обсуждался. Цитата копируется из
   расшифровки без изменений. Нет цитаты — не ставь «agreed».
3. Ничего не выдумывай и не додумывай. Суммы и сроки бери только из разговора.
   Если сумма неясна — 0.
4. decision.value: agreed — клиент согласен на ремонт; refused — отказывается;
   thinking — думает/перезвонит; unclear — неясно.
5. followup.needed = true, если нужно перезвонить или проконтролировать заказ;
   when_text — когда (как сказали в разговоре), reason — зачем.
5a. works.items — КАЖДАЯ работа/услуга отдельным элементом (например:
   «Техническое обслуживание», «Восстановление силовой линии питания»,
   «Замена контроллера питания»). name — название работы по-русски, коротко и
   нейтрально, как её называли в разговоре. price_rub — цена именно этой работы,
   о которой договорились (0, если для неё цена отдельно не называлась).
   warranty_days — гарантия на эту работу в днях (месяц=30, 3 месяца=90,
   полгода=180, год=365; «без гарантии» или не называли = 0).
   status и evidence — для каждой работы отдельно, по правилам 1 и 2.
   Запчасти и доставку в works не включай, они идут в parts.
6. old_parts.answer: yes — клиент хочет забрать старые запчасти, no — не нужны,
   not_discussed — не обсуждали.
7. objections — возражения клиента (цена, срок, недоверие, «дорого» и т.п.)
   короткими фразами.
8. summary — 1–2 предложения по сути разговора.
Отвечай только JSON."""


class ExtractError(ValueError):
    """Raised when the LLM answer is missing, not a string or not valid JSON."""


@dataclass(frozen=True, slots=True)
class Utterance:
    """One transcript line with a normalized speaker label."""

    ts: float | None
    speaker: str
    text: str


_TIMESTAMP_RE = re.compile(r"^\[?(\d{1,2}):(\d{2})(?::(\d{2}))?\]?\s+")
_LABEL_RE = re.compile(r"^([^:]{1,40}):\s*(.*)$")
_VOICE_RE = re.compile(r"^(?:голос|voice|speaker)\s*(\d+)$")
_CYRILLIC_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


def _norm_label(label: str) -> str:
    """Normalize a speaker label for matching without changing its meaning."""
    return re.sub(r"\s+", " ", label.strip().lower().replace("ё", "е"))


def _speaker_for(label: str) -> str | None:
    """Map a known transcript label to a canonical speaker, or return ``None``."""
    key = _norm_label(label)
    if not key or not _CYRILLIC_LETTER_RE.search(key):
        return None
    if key.startswith("клиент") or key.startswith("client"):
        return CLIENT
    if (
        key.startswith("менеджер")
        or key.startswith("manager")
        or key.startswith("оператор")
    ):
        return MANAGER
    if key.startswith("участник") or key.startswith("speaker"):
        return UNKNOWN
    voice = _VOICE_RE.match(key)
    if voice is not None:
        return MANAGER if voice.group(1) == "1" else CLIENT
    return label.strip()


def _split_timestamp(line: str) -> tuple[float | None, str]:
    """Strip a leading ``[mm:ss]`` / ``hh:mm:ss`` timestamp and return its seconds."""
    match = _TIMESTAMP_RE.match(line)
    if match is None:
        return None, line
    first, second, third = match.groups()
    if third is None:
        seconds = int(first) * 60 + int(second)
    else:
        seconds = int(first) * 3600 + int(second) * 60 + int(third)
    return float(seconds), line[match.end() :]


def parse_transcript(raw: str) -> list[Utterance]:
    """Parse labelled transcript text into utterances with canonical speakers.

    Supported labels: ``Клиент``/``Client``, ``Менеджер``/``Manager``/``Оператор``,
    ``Голос 1``/``Голос 2`` (manager/client) and ``Участник`` (unknown). An
    unrecognized label is preserved as-is; an unlabelled line gets ``?``.
    """
    utterances: list[Utterance] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        ts, rest = _split_timestamp(stripped)
        match = _LABEL_RE.match(rest)
        if match is not None:
            speaker = _speaker_for(match.group(1))
            if speaker is not None:
                text = match.group(2).strip()
                if text:
                    utterances.append(Utterance(ts, speaker, text))
                continue
        utterances.append(Utterance(ts, UNKNOWN, rest))
    return utterances


def transcript_as_text(utterances: list[Utterance], last_n: int | None = None) -> str:
    """Render parsed utterances back as ``speaker: text`` lines."""
    selected = utterances[-last_n:] if last_n else utterances
    return "\n".join(f"{item.speaker}: {item.text}" for item in selected)


def speaker_only_text(raw: str, speaker: str) -> str:
    """Return only the text lines spoken by ``speaker``."""
    return "\n".join(
        item.text for item in parse_transcript(raw) if item.speaker == speaker
    )


def client_only_text(raw: str) -> str:
    """Return only client lines, used for quote verification of agreements."""
    return speaker_only_text(raw, CLIENT)


def manager_only_text(raw: str) -> str:
    """Return only manager lines (useful for diagnostics and tests)."""
    return speaker_only_text(raw, MANAGER)


def parse_json_loose(text: str) -> dict[str, Any]:
    """Extract one JSON object from an LLM answer, tolerating fences and prose."""
    if not isinstance(text, str):
        raise ExtractError("LLM answer must be a string")
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    start, end = candidate.find("{"), candidate.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    raise ExtractError(f"LLM answer is not a JSON object: {text[:200]!r}")


class FactExtractor:
    """Extract verified Work CRM facts from one call transcript."""

    def __init__(
        self,
        llm: LLMCallable,
        *,
        system_prompt: str = SYSTEM,
        max_order_chars: int = 1500,
    ) -> None:
        self.llm = llm
        self.system_prompt = system_prompt
        self.max_order_chars = max_order_chars

    def build_messages(
        self, transcript_text: str, order_context: str = ""
    ) -> list[dict[str, str]]:
        """Build the system/user prompt, including the facts JSON schema."""
        parts: list[str] = []
        if order_context:
            context = order_context[: self.max_order_chars]
            parts.append(
                "Контекст заказа (для справки, не источник согласований):\n" + context
            )
        parts.append("Расшифровка разговора:\n" + transcript_text)
        schema_text = json.dumps(FACTS_SCHEMA, ensure_ascii=False)
        parts.append("Схема ответа (JSON Schema):\n" + schema_text)
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": "\n\n".join(parts)},
        ]

    def extract(
        self,
        transcript_text: str,
        order_context: str = "",
        client_text: str = "",
    ) -> dict[str, Any]:
        """Extract facts from transcript text.

        ``client_text`` may be supplied by a caller that already split the
        speakers; otherwise it is derived from the transcript labels. An empty
        transcript returns empty facts without calling the model.
        """
        full_text = transcript_text.strip()
        if not full_text:
            return normalize({}, "", "")
        client = client_text.strip() or client_only_text(full_text)
        messages = self.build_messages(full_text, order_context)
        try:
            answer = self.llm(messages)
        except ExtractError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the domain API predictable
            raise ExtractError(f"LLM call failed: {exc}") from exc
        raw = parse_json_loose(answer)
        return normalize(raw, full_text, client)
