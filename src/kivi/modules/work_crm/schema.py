"""Work CRM facts schema and anti-hallucination quote checks.

The model may report a section as agreed only together with a verbatim quote
from the client. This module normalizes the model answer, verifies every quote
against the transcript and downgrades unsupported agreements to ``discussed``.
A quote found only in the manager's lines can never make a section agreed.

Adapted from D:\\ai-agent\\modes\\work
"""

# Adapted from D:\ai-agent\modes\work

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

STATUSES = ["agreed", "discussed", "not_mentioned"]

# Section id -> human-readable Russian label used in drafts and CRM fields.
SECTIONS = {
    "price": "Стоимость ремонта",
    "works": "Согласованные работы",
    "parts": "Запчасти",
    "deadline": "Сроки",
    "old_parts": "Старые запчасти",
    "warranty": "Гарантия",
    "risks": "Риски",
}


def _section_schema(value_schema: dict[str, Any], value_key: str) -> dict[str, Any]:
    """Build the JSON schema for one fact section: value, status and evidence."""
    return {
        "type": "object",
        "properties": {
            value_key: value_schema,
            "status": {"type": "string", "enum": STATUSES},
            "evidence": {"type": "string"},
        },
        "required": [value_key, "status", "evidence"],
    }


_STR = {"type": "string"}
_LIST = {"type": "array", "items": {"type": "string"}}

_WORK_ITEM = {
    "type": "object",
    "properties": {
        "name": _STR,
        "price_rub": {"type": "integer"},
        "warranty_days": {"type": "integer"},
        "status": {"type": "string", "enum": STATUSES},
        "evidence": _STR,
    },
    "required": ["name", "price_rub", "warranty_days", "status", "evidence"],
}

FACTS_SCHEMA = {
    "type": "object",
    "properties": {
        "price": _section_schema({"type": "integer"}, "amount_rub"),
        "works": {
            "type": "object",
            "properties": {"items": {"type": "array", "items": _WORK_ITEM}},
            "required": ["items"],
        },
        "parts": _section_schema(_LIST, "items"),
        "deadline": _section_schema(_STR, "text"),
        "old_parts": _section_schema(
            {"type": "string", "enum": ["yes", "no", "not_discussed"]}, "answer"
        ),
        "warranty": _section_schema(_STR, "text"),
        "risks": _section_schema(_LIST, "items"),
        "decision": {
            "type": "object",
            "properties": {
                "value": {
                    "type": "string",
                    "enum": ["agreed", "refused", "thinking", "unclear"],
                },
                "evidence": _STR,
            },
            "required": ["value", "evidence"],
        },
        "objections": _LIST,
        "followup": {
            "type": "object",
            "properties": {"needed": {"type": "boolean"}, "when_text": _STR, "reason": _STR},
            "required": ["needed", "when_text", "reason"],
        },
        "summary": _STR,
    },
    "required": [
        "price",
        "works",
        "parts",
        "deadline",
        "old_parts",
        "warranty",
        "risks",
        "decision",
        "objections",
        "followup",
        "summary",
    ],
}

VALUE_KEY = {
    "price": "amount_rub",
    "works": "items",
    "parts": "items",
    "deadline": "text",
    "old_parts": "answer",
    "warranty": "text",
    "risks": "items",
}


def _norm(text: str) -> str:
    """Normalize Russian text for fuzzy quote comparison."""
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def quote_found(evidence: str, haystack: str, min_ratio: float = 0.8) -> bool:
    """Return True when the quote is found in the text, allowing ASR noise.

    ``min_ratio`` is the share of the normalized evidence that must match as one
    contiguous block. The default tolerates small recognition errors while still
    rejecting invented phrases.
    """
    ev, hs = _norm(evidence), _norm(haystack)
    if len(ev) < 4 or not hs:
        return False
    if ev in hs:
        return True
    match = SequenceMatcher(None, ev, hs, autojunk=False).find_longest_match(
        0, len(ev), 0, len(hs)
    )
    return match.size / len(ev) >= min_ratio


def _empty_value(key: str) -> Any:
    return {"amount_rub": 0, "items": [], "text": "", "answer": "not_discussed"}[key]


def _to_int(value: Any) -> int:
    """Convert a model-provided price/warranty value to a non-negative integer."""
    try:
        cleaned = str(value).replace(" ", "").replace(",", ".")
        return max(int(float(cleaned)), 0)
    except (TypeError, ValueError):
        return 0


def _normalize_works(src: Any, full_text: str, client_text: str) -> dict[str, Any]:
    """Normalize work items; every item keeps its own status and citation."""
    items_raw = (
        src.get("items")
        if isinstance(src, dict) and isinstance(src.get("items"), list)
        else []
    )
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_item in items_raw:
        if not isinstance(raw_item, dict):
            raw_item = {"name": str(raw_item)}
        name = str(raw_item.get("name") or "").strip()
        key = _norm(name)
        if not name or key in seen:
            continue
        seen.add(key)
        raw_status = raw_item.get("status")
        status = raw_status if raw_status in ("agreed", "discussed") else "discussed"
        evidence = str(raw_item.get("evidence") or "").strip()
        verified = False
        if status == "agreed":
            verified = quote_found(evidence, client_text)
            if not verified:
                status = "discussed"
                verified = quote_found(evidence, full_text)
        else:
            verified = quote_found(evidence, full_text)
        items.append(
            {
                "name": name,
                "price_rub": _to_int(raw_item.get("price_rub")),
                "warranty_days": _to_int(raw_item.get("warranty_days")),
                "status": status,
                "evidence": evidence,
                "verified": verified,
            }
        )
    if not items:
        section_status = "not_mentioned"
    elif all(item["status"] == "agreed" for item in items):
        section_status = "agreed"
    else:
        section_status = "discussed"
    return {
        "items": items,
        "status": section_status,
        "evidence": "; ".join(
            item["evidence"] for item in items if item["evidence"]
        )[:200],
        "verified": bool(items) and all(item["verified"] for item in items),
    }


def normalize(raw: Any, full_text: str, client_text: str) -> dict[str, Any]:
    """Coerce a model answer to the strict facts shape and verify all quotes.

    ``full_text`` is used to verify discussion quotes; ``client_text`` contains
    only the client's lines and is required for an ``agreed`` status. Unknown or
    malformed input is converted to empty facts instead of raising.
    """
    source = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    out["works"] = _normalize_works(source.get("works"), full_text, client_text)
    for section, value_key in VALUE_KEY.items():
        if section == "works":
            continue
        src = source.get(section) if isinstance(source.get(section), dict) else {}
        status = src.get("status") if src.get("status") in STATUSES else "not_mentioned"
        value = src.get(value_key, _empty_value(value_key))
        if value_key == "amount_rub":
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = 0
        elif value_key == "items":
            value = [str(item) for item in value] if isinstance(value, list) else []
        elif value_key == "answer":
            value = value if value in ("yes", "no", "not_discussed") else "not_discussed"
        else:
            value = str(value or "")
        evidence = str(src.get("evidence") or "").strip()
        verified = False
        if status == "agreed":
            verified = quote_found(evidence, client_text)
            if not verified:
                # No client confirmation -> the point was discussed at most.
                status = "discussed"
                verified = quote_found(evidence, full_text)
        elif status == "discussed":
            verified = quote_found(evidence, full_text)
        empty = value in (0, [], "", "not_discussed")
        if empty and status != "not_mentioned":
            status = "not_mentioned"
        out[section] = {
            value_key: value,
            "status": status,
            "evidence": evidence,
            "verified": verified,
        }

    decision_src = source.get("decision") if isinstance(source.get("decision"), dict) else {}
    decision_value = (
        decision_src.get("value")
        if decision_src.get("value") in ("agreed", "refused", "thinking", "unclear")
        else "unclear"
    )
    decision_evidence = str(decision_src.get("evidence") or "")
    decision_verified = quote_found(decision_evidence, client_text)
    if decision_value in ("agreed", "refused") and not decision_verified:
        # A firm client decision is trusted only with a verifiable client quote.
        decision_value = "unclear"
    out["decision"] = {
        "value": decision_value,
        "evidence": decision_evidence,
        "verified": decision_verified,
    }
    objections = source.get("objections")
    out["objections"] = (
        [str(item) for item in objections if item] if isinstance(objections, list) else []
    )
    followup = source.get("followup") if isinstance(source.get("followup"), dict) else {}
    out["followup"] = {
        "needed": bool(followup.get("needed")),
        "when_text": str(followup.get("when_text") or ""),
        "reason": str(followup.get("reason") or ""),
    }
    out["summary"] = str(source.get("summary") or "")
    return out


def empty_facts() -> dict[str, Any]:
    """Return the canonical empty facts object."""
    return normalize({}, "", "")
