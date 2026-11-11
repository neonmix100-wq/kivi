"""Deterministic CRM drafts built only from verified call facts.

No LLM is involved here: the call comment, manager checklist, act rows and the
follow-up task are pure functions of the normalized facts structure. Only
sections with status ``agreed`` reach CRM fields; ``discussed`` values remain in
the free-form comment so the manager can see what was not settled.

Adapted from D:\\ai-agent\\modes\\work
"""

# Adapted from D:\ai-agent\modes\work

from __future__ import annotations

from datetime import datetime
from typing import Any

DECISION_RU = {
    "agreed": "клиент согласен",
    "refused": "клиент отказался",
    "thinking": "клиент думает",
    "unclear": "решение неясно",
}


def _work_label(item: dict[str, Any]) -> str:
    """Format one work item as a short human-readable label."""
    extra: list[str] = []
    if item["price_rub"]:
        extra.append(f"{item['price_rub']} руб.")
    if item["warranty_days"]:
        extra.append(f"гарантия {item['warranty_days']} дн.")
    return item["name"] + (f" ({', '.join(extra)})" if extra else "")


def _fmt(section: str, data: dict[str, Any]) -> str:
    """Format a fact section value for the call comment."""
    if section == "works":
        return "; ".join(_work_label(item) for item in data["items"])
    if section == "price":
        return f"{data['amount_rub']} руб." if data["amount_rub"] else ""
    if section == "old_parts":
        return {
            "yes": "клиент забирает старые запчасти",
            "no": "старые запчасти не нужны",
        }.get(data["answer"], "")
    key = "text" if "text" in data else "items"
    value = data[key]
    return "; ".join(value) if isinstance(value, list) else value


LABELS = {
    "price": "Стоимость",
    "works": "Работы",
    "parts": "Запчасти",
    "deadline": "Срок",
    "old_parts": "Старые запчасти",
    "warranty": "Гарантия",
    "risks": "Риски",
}


def build_comment(facts: dict[str, Any], now: datetime | None = None) -> str:
    """Build the manager comment for a call from normalized facts."""
    now = now or datetime.now()
    decision = DECISION_RU.get(facts["decision"]["value"], "решение неясно")
    lines = [f"Звонок {now:%d.%m.%Y %H:%M}: {decision}."]
    agreed: list[str] = []
    discussed: list[str] = []
    for section, label in LABELS.items():
        if section == "works":
            accepted = [
                _work_label(item)
                for item in facts["works"]["items"]
                if item["status"] == "agreed"
            ]
            deferred = [
                _work_label(item)
                for item in facts["works"]["items"]
                if item["status"] != "agreed"
            ]
            if accepted:
                agreed.append(f"{label}: " + "; ".join(accepted))
            if deferred:
                discussed.append(f"{label}: " + "; ".join(deferred))
            continue
        value = _fmt(section, facts[section])
        if not value:
            continue
        target = agreed if facts[section]["status"] == "agreed" else discussed
        target.append(f"{label}: {value}")
    if agreed:
        lines.append("Согласовано: " + "; ".join(agreed) + ".")
    if discussed:
        lines.append("Обсуждалось, не согласовано: " + "; ".join(discussed) + ".")
    if facts["objections"]:
        lines.append("Возражения: " + "; ".join(facts["objections"]) + ".")
    followup = facts["followup"]
    if followup["needed"]:
        reason = f" ({followup['reason']})" if followup["reason"] else ""
        when_text = followup["when_text"] or "срок не назван"
        lines.append(f"Следующий контакт: {when_text}{reason}.")
    return "\n".join(lines)


def build_checklist(facts: dict[str, Any]) -> dict[str, str]:
    """Build manager checklist fields; only client-agreed values are included."""

    def agreed(section: str) -> str:
        return _fmt(section, facts[section]) if facts[section]["status"] == "agreed" else ""

    works = "; ".join(
        item["name"] for item in facts["works"]["items"] if item["status"] == "agreed"
    )
    return {
        "Согласованные сроки ремонта": agreed("deadline"),
        "Согласованные зап. части": agreed("parts"),
        "Фактические выполняемые работы": works,
        "Согласованные риски": agreed("risks"),
    }


def build_act_rows(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Build act rows: only agreed works reach the act."""
    return [
        {
            "name": item["name"],
            "warranty_days": item["warranty_days"],
            "price_rub": item["price_rub"],
        }
        for item in facts["works"]["items"]
        if item["status"] == "agreed"
    ]


def act_rows_to_text(rows: list[dict[str, Any]]) -> str:
    """Serialize act rows as ``name | warranty days | price rub`` lines."""
    return "\n".join(
        f"{row['name']} | {row['warranty_days']} | {row['price_rub']}" for row in rows
    )


def _parse_number(parts: list[str], index: int) -> int:
    """Parse a non-negative integer from one act text field."""
    if len(parts) <= index:
        return 0
    try:
        cleaned = parts[index].replace(" ", "").replace(",", ".")
        return max(int(float(cleaned)), 0)
    except ValueError:
        return 0


def parse_act_text(text: str) -> list[dict[str, Any]]:
    """Parse act rows produced by :func:`act_rows_to_text`."""
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split("|")]
        name = parts[0]
        if name:
            rows.append(
                {
                    "name": name,
                    "warranty_days": _parse_number(parts, 1),
                    "price_rub": _parse_number(parts, 2),
                }
            )
    return rows


def price_check(facts: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    """Warn when act row prices do not add up to the agreed total price."""
    total = facts["price"]["amount_rub"] if facts["price"]["status"] == "agreed" else 0
    row_sum = sum(row["price_rub"] for row in rows)
    notes: list[str] = []
    if any(row["price_rub"] == 0 for row in rows):
        notes.append("у некоторых работ цена не определена (0) — укажите её вручную")
    if total and row_sum and total != row_sum:
        notes.append(
            f"сумма работ {row_sum} руб. не равна согласованной стоимости {total} руб. "
            f"(разница может быть за счёт запчастей — проверьте)"
        )
    return "; ".join(notes)


def build_task(facts: dict[str, Any]) -> dict[str, str] | None:
    """Build a follow-up task from facts, or ``None`` when none is needed."""
    followup = facts["followup"]
    if not followup["needed"]:
        return None
    return {
        "title": "Перезвонить клиенту / контроль заказа",
        "when_text": followup["when_text"],
        "reason": followup["reason"],
    }
