"""Coaching prompts for Work CRM: pre-call briefs and live hints.

The old coach read ``playbook.toml`` and depended on tkinter-era state. This
headless version takes an injected LLM callable and an optional playbook mapping;
nothing is loaded from disk. Prompts are adapted from the old Work mode, and all
inputs are truncated so a long order card cannot flood the model context.

Adapted from D:\\ai-agent\\modes\\work
"""

# Adapted from D:\ai-agent\modes\work

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

LLMCallable = Callable[[list[dict[str, str]]], str]

SYSTEM = """Ты опытный руководитель отдела продаж сервисного центра. Помогаешь менеджеру
вернуть клиента, который отказался от ремонта, и согласовать заказ. Пиши по-русски,
кратко и конкретно, без воды. Опирайся только на данные заказа, разговор и правила
компании. Если данных не хватает — так и скажи. Лимиты скидок, гарантий и альтернативы
бери ТОЛЬКО из правил компании. Если правило не задано — не придумывай число,
напиши «уточнить лимит у руководителя»."""

DEFAULT_ORDER_CHARS = 3500
DEFAULT_ORDER_CHARS_WITH_HISTORY = 2200
DEFAULT_HISTORY_CHARS = 4000
DEFAULT_CALLS_CHARS = 2500
DEFAULT_TRANSCRIPT_CHARS = 1800
DEFAULT_TIP_ORDER_CHARS = 1200
DEFAULT_TRANSCRIPT_LINES = 14


def playbook_text(playbook: Mapping[str, Any] | None) -> str:
    """Render compact company rules from a playbook mapping.

    The function deliberately does not read files: callers pass an already loaded
    mapping (for example from ``config/kivi.yaml`` or a module manifest).
    """
    if not isinstance(playbook, Mapping):
        return "Правила компании не заданы (лимиты неизвестны)."
    lines: list[str] = []
    limits = playbook.get("limits", {})
    if isinstance(limits, Mapping):
        if limits.get("max_discount_percent"):
            lines.append(
                "Максимальная скидка без согласования: "
                f"{limits['max_discount_percent']}%"
            )
        if limits.get("approval_discount_percent"):
            lines.append(
                "Скидка с согласования руководителя: до "
                f"{limits['approval_discount_percent']}%"
            )
    warranty = playbook.get("warranty", {})
    if isinstance(warranty, Mapping):
        if warranty.get("standard_days"):
            lines.append(f"Стандартная гарантия: {warranty['standard_days']} дн.")
        if warranty.get("extended_options"):
            options = ", ".join(str(item) for item in warranty["extended_options"])
            lines.append("Расширенная гарантия: " + options)
    alternatives = playbook.get("alternatives", {})
    if isinstance(alternatives, Mapping) and alternatives.get("options"):
        options = ", ".join(str(item) for item in alternatives["options"])
        lines.append("Альтернативные варианты ремонта: " + options)
    contact = playbook.get("contact", {})
    if isinstance(contact, Mapping) and contact.get("cadence"):
        lines.append("Частота обратной связи: " + str(contact["cadence"]))
    notes = playbook.get("notes", {})
    if isinstance(notes, Mapping) and notes.get("text"):
        lines.append("Заметки: " + str(notes["text"]))
    if not lines:
        return "Правила компании не заданы (лимиты неизвестны)."
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    """Truncate text to at most ``limit`` characters."""
    if limit <= 0:
        return ""
    return text[:limit]


class Coach:
    """Build coaching prompts and call the injected LLM."""

    def __init__(
        self,
        llm: LLMCallable,
        *,
        system_prompt: str = SYSTEM,
        playbook: Mapping[str, Any] | None = None,
        order_chars: int = DEFAULT_ORDER_CHARS,
        order_chars_with_history: int = DEFAULT_ORDER_CHARS_WITH_HISTORY,
        history_chars: int = DEFAULT_HISTORY_CHARS,
        calls_chars: int = DEFAULT_CALLS_CHARS,
        transcript_chars: int = DEFAULT_TRANSCRIPT_CHARS,
        tip_order_chars: int = DEFAULT_TIP_ORDER_CHARS,
    ) -> None:
        self.llm = llm
        self.system_prompt = system_prompt
        self.playbook = playbook if isinstance(playbook, Mapping) else {}
        self.order_chars = order_chars
        self.order_chars_with_history = order_chars_with_history
        self.history_chars = history_chars
        self.calls_chars = calls_chars
        self.transcript_chars = transcript_chars
        self.tip_order_chars = tip_order_chars

    def _messages(self, user: str) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user},
        ]

    def _ask(self, user: str) -> str:
        answer = self.llm(self._messages(user))
        return answer.strip() if isinstance(answer, str) else str(answer).strip()

    def brief(
        self, order_text: str, history_text: str = "", calls_text: str = ""
    ) -> str:
        """Build a pre-call brief from the order card and previous call history."""
        has_history = bool(history_text.strip() or calls_text.strip())
        order_limit = (
            self.order_chars_with_history if has_history else self.order_chars
        )
        blocks = ["Правила компании:", playbook_text(self.playbook)]
        if history_text.strip():
            blocks.extend(
                [
                    "",
                    "История прошлых звонков по заказу (расшифрованы автоматически, "
                    "могут содержать ошибки; «согласился» считай подтверждённым "
                    "только по этим итогам):",
                    _clip(history_text, self.history_chars),
                ]
            )
        if calls_text.strip():
            blocks.extend(
                [
                    "",
                    "Краткие итоги прошлых звонков (дайджест):",
                    _clip(calls_text, self.calls_chars),
                ]
            )
        blocks.extend(
            [
                "",
                "Данные заказа (текст карточки из CRM):",
                _clip(order_text, order_limit),
                "",
                "Составь бриф перед звонком клиенту, который, возможно, уже "
                "отказался от ремонта:",
                "1. Что видно по заказу (устройство, неисправность, диагностика, "
                "цена, прошлые комментарии, настроение клиента) — 3-4 пункта.",
                "2. Вероятная причина сомнений/отказа и на чём она основана "
                "(опирайся на возражения клиента из прошлых звонков; если по данным "
                "неясно — скажи). Что клиенту уже предлагали и что он отверг: "
                "не повторяй это, предложи новое.",
                "3. 3 варианта предложения по убыванию приоритета, строго в рамках "
                "правил компании.",
                "4. Что уточнить у клиента (2-3 вопроса).",
                "5. Первая фраза звонка.",
                "6. Когда и как вернуться к клиенту, если он не решит сразу.",
            ]
        )
        return self._ask("\n".join(blocks))

    def tip(
        self, order_text: str, transcript: str = "", history_text: str = ""
    ) -> str:
        """Build 2-3 short live tips from the latest dialogue."""
        lines = [line for line in transcript.splitlines() if line.strip()]
        recent = "\n".join(lines[-DEFAULT_TRANSCRIPT_LINES:])
        blocks = [
            "Правила компании:",
            playbook_text(self.playbook),
            "",
            "Заказ (кратко):",
            _clip(order_text, self.tip_order_chars),
        ]
        if history_text.strip():
            blocks.extend(
                [
                    "",
                    "История прошлых звонков:",
                    _clip(history_text, self.history_chars),
                ]
            )
        blocks.extend(
            [
                "",
                "Последние реплики разговора:",
                _clip(recent, self.transcript_chars),
                "",
                "Оцени по разговору текущее решение клиента и его возражения. "
                "Дай 2-3 коротких совета, что сказать или предложить прямо сейчас "
                "(каждый одной строкой, без вступлений).",
            ]
        )
        return self._ask("\n".join(blocks))
