"""Headless Work CRM domain extracted from the old ``ai-agent`` Work mode.

This package has no tkinter, Ollama or ``core``/``modes`` imports. It contains
the facts schema with quote verification, deterministic CRM drafts, transcript
parsing, a callback-based fact extractor, coaching prompts, per-order call
history and automation metrics. See ``docs/CRM-DOMAIN.md`` for the model and
migration notes.
"""

from kivi.modules.work_crm.coach import Coach, playbook_text
from kivi.modules.work_crm.drafts import (
    DECISION_RU,
    act_rows_to_text,
    build_act_rows,
    build_checklist,
    build_comment,
    build_task,
    parse_act_text,
    price_check,
)
from kivi.modules.work_crm.extractor import (
    CLIENT,
    MANAGER,
    UNKNOWN,
    ExtractError,
    FactExtractor,
    Utterance,
    client_only_text,
    manager_only_text,
    parse_json_loose,
    parse_transcript,
    speaker_only_text,
    transcript_as_text,
)
from kivi.modules.work_crm.history import (
    OUTCOME_RU,
    OUTCOMES,
    CallHistory,
    HistoryError,
    normalize_summary,
    safe_order_name,
    summarize,
    transcript_text,
)
from kivi.modules.work_crm.metrics import CrmMetrics
from kivi.modules.work_crm.schema import (
    FACTS_SCHEMA,
    SECTIONS,
    STATUSES,
    VALUE_KEY,
    empty_facts,
    normalize,
    quote_found,
)

__all__ = [
    "CLIENT",
    "DECISION_RU",
    "FACTS_SCHEMA",
    "MANAGER",
    "OUTCOMES",
    "OUTCOME_RU",
    "SECTIONS",
    "STATUSES",
    "UNKNOWN",
    "VALUE_KEY",
    "CallHistory",
    "Coach",
    "CrmMetrics",
    "ExtractError",
    "FactExtractor",
    "HistoryError",
    "Utterance",
    "act_rows_to_text",
    "build_act_rows",
    "build_checklist",
    "build_comment",
    "build_task",
    "client_only_text",
    "empty_facts",
    "manager_only_text",
    "normalize",
    "normalize_summary",
    "parse_act_text",
    "parse_json_loose",
    "parse_transcript",
    "playbook_text",
    "price_check",
    "quote_found",
    "safe_order_name",
    "speaker_only_text",
    "summarize",
    "transcript_as_text",
    "transcript_text",
]
