"""Tests for the headless Work CRM domain (no network, no Ollama, no tkinter)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from time import sleep

import pytest

import kivi.modules.work_crm.history as history_module
from kivi.modules.work_crm import (
    CLIENT,
    MANAGER,
    UNKNOWN,
    CallHistory,
    Coach,
    CrmMetrics,
    ExtractError,
    FactExtractor,
    HistoryError,
    act_rows_to_text,
    build_act_rows,
    build_checklist,
    build_comment,
    build_task,
    client_only_text,
    empty_facts,
    manager_only_text,
    normalize,
    normalize_summary,
    parse_act_text,
    parse_transcript,
    playbook_text,
    price_check,
    quote_found,
    safe_order_name,
    summarize,
    transcript_text,
)

CALL = (
    "Менеджер: Добрый день, это сервисный центр, по вашему принтеру. "
    "Мы провели диагностику, нужна замена материнской платы, "
    "пятнадцать тысяч, и работы десять тысяч.\n"
    "Клиент: Ого, дорого. А можно подешевле?\n"
    "Менеджер: Могу сделать скидку десять процентов на работы. "
    "Итого получится двадцать четыре тысячи.\n"
    "Клиент: Ну ладно, с десятью процентами согласен, делайте.\n"
    "Менеджер: Срок два-три недели, плату заказываем под заказ. "
    "Старые запчасти вам нужны?\n"
    "Клиент: Нет, старую плату можете выбросить.\n"
    "Менеджер: Гарантию на залитые принтеры мы не даём, вас это устраивает?\n"
    "Клиент: Да, понимаю риск.\n"
    "Менеджер: Тогда я позвоню вам через неделю, расскажу, как идёт заказ.\n"
    "Клиент: Хорошо, звоните."
)


def section(value_key: str, value: object, status: str, evidence: str = "") -> dict:
    """Build one raw fact section as the model would return it."""
    return {value_key: value, "status": status, "evidence": evidence}


def facts_from(raw: dict, text: str = CALL) -> dict:
    """Normalize raw facts against a transcript, deriving client lines."""
    return normalize(raw, text, client_only_text(text))


# ------------------------------------------------------------------ quotes and schema
def test_quote_exact_and_fuzzy() -> None:
    hay = "Ну ладно, с десятью процентами согласен, делайте."
    assert quote_found("с десятью процентами согласен", hay)
    assert quote_found("с десятью процентами согласен делайте", hay)
    assert quote_found("с 10 процентами согласен, делайте", hay, 0.7)
    assert not quote_found("да, оформляйте заказ на ремонт платы", hay)
    assert not quote_found("", hay)


def test_fabricated_agreement_is_downgraded() -> None:
    raw = {
        "price": section(
            "amount_rub", 24000, "agreed", "да, согласен на двадцать четыре тысячи"
        )
    }
    facts = facts_from(raw)
    assert facts["price"]["status"] == "discussed"
    assert facts["price"]["verified"] is False


def test_real_client_agreement_is_kept() -> None:
    raw = {
        "price": section(
            "amount_rub", 24000, "agreed", "с десятью процентами согласен, делайте"
        )
    }
    facts = facts_from(raw)
    assert facts["price"]["status"] == "agreed"
    assert facts["price"]["verified"] is True


def test_manager_quote_cannot_make_agreed() -> None:
    raw = {
        "deadline": section(
            "text", "2-3 недели", "agreed", "Срок два-три недели, плату заказываем под заказ"
        )
    }
    facts = facts_from(raw)
    assert facts["deadline"]["status"] == "discussed"
    assert facts["deadline"]["verified"] is True


def test_empty_value_becomes_not_mentioned() -> None:
    facts = facts_from({"price": section("amount_rub", 0, "agreed", "x")})
    assert facts["price"]["status"] == "not_mentioned"


def test_decision_requires_client_quote() -> None:
    facts = facts_from(
        {"decision": {"value": "refused", "evidence": "не буду ничего делать"}}
    )
    assert facts["decision"]["value"] == "unclear"


def test_garbage_input_is_safe() -> None:
    facts = normalize(
        {"price": "oops", "works": {"items": "no", "status": 5}, "objections": "x"},
        "",
        "",
    )
    assert facts["price"]["amount_rub"] == 0
    assert facts["works"]["items"] == []
    assert facts["objections"] == []


def test_empty_facts_shape() -> None:
    facts = empty_facts()
    assert facts["decision"]["value"] == "unclear"
    assert facts["price"]["status"] == "not_mentioned"
    assert facts["works"]["items"] == []


# ------------------------------------------------------------------ works and drafts
def test_works_deduplicated_and_section_agreed() -> None:
    evidence = "да, согласен на все три работы"
    text = (
        "Клиент: да, согласен на все три работы, гарантия три месяца на питание\n"
        "Менеджер: хорошо"
    )
    items = [
        {
            "name": "Техническое обслуживание",
            "price_rub": 3000,
            "warranty_days": 0,
            "status": "agreed",
            "evidence": evidence,
        },
        {
            "name": "Восстановление силовой линии питания",
            "price_rub": 4500,
            "warranty_days": 90,
            "status": "agreed",
            "evidence": evidence,
        },
        {
            "name": "Замена контроллера питания",
            "price_rub": 6000,
            "warranty_days": 90,
            "status": "agreed",
            "evidence": evidence,
        },
        {
            "name": "техническое обслуживание",
            "price_rub": 1,
            "warranty_days": 0,
            "status": "agreed",
            "evidence": evidence,
        },
    ]
    facts = facts_from({"works": {"items": items}}, text)
    assert [item["name"] for item in facts["works"]["items"]] == [
        "Техническое обслуживание",
        "Восстановление силовой линии питания",
        "Замена контроллера питания",
    ]
    assert facts["works"]["status"] == "agreed"


def test_unagreed_work_not_in_act() -> None:
    text = (
        "Клиент: да, согласен на все три работы, гарантия три месяца на питание\n"
        "Менеджер: хорошо"
    )
    raw = {
        "works": {
            "items": [
                {
                    "name": "Замена экрана",
                    "price_rub": 9000,
                    "warranty_days": 30,
                    "status": "discussed",
                    "evidence": "гарантия три месяца на питание",
                }
            ]
        }
    }
    facts = facts_from(raw, text)
    assert build_act_rows(facts) == []
    assert facts["works"]["status"] == "discussed"


def test_comment_contains_sections() -> None:
    raw = {
        "price": section("amount_rub", 24000, "agreed", "с десятью процентами согласен, делайте"),
        "deadline": section("text", "2-3 недели", "discussed", "Срок два-три недели"),
        "decision": {"value": "agreed", "evidence": "с десятью процентами согласен, делайте"},
        "objections": ["дорого"],
        "followup": {"needed": True, "when_text": "через неделю", "reason": "статус заказа"},
    }
    comment = build_comment(facts_from(raw), now=datetime(2026, 10, 22, 14, 30))
    assert comment.startswith("Звонок 22.10.2026 14:30: клиент согласен.")
    assert "Согласовано:" in comment
    assert "Стоимость: 24000 руб." in comment
    assert "Обсуждалось, не согласовано:" in comment
    assert "Срок: 2-3 недели" in comment
    assert "Возражения: дорого." in comment
    assert "Следующий контакт: через неделю (статус заказа)." in comment


def test_comment_for_refused_decision() -> None:
    text = "Клиент: нет, не буду ремонтировать\nМенеджер: хорошо"
    raw = {"decision": {"value": "refused", "evidence": "нет, не буду ремонтировать"}}
    comment = build_comment(facts_from(raw, text), now=datetime(2026, 10, 22, 9, 5))
    assert "клиент отказался" in comment


def test_checklist_only_agreed() -> None:
    text = (
        "Клиент: да, срок две недели устраивает и работы делайте, гарантия три месяца\n"
        "Менеджер: запчасти закажем отдельно"
    )
    raw = {
        "works": {
            "items": [
                {
                    "name": "Замена платы",
                    "price_rub": 1000,
                    "warranty_days": 90,
                    "status": "agreed",
                    "evidence": "работы делайте",
                }
            ]
        },
        "deadline": section("text", "две недели", "agreed", "срок две недели устраивает"),
        "parts": section("items", ["плата"], "discussed", "запчасти закажем отдельно"),
        "warranty": section("text", "три месяца", "agreed", "гарантия три месяца"),
    }
    checklist = build_checklist(facts_from(raw, text))
    assert checklist["Согласованные сроки ремонта"] == "две недели"
    assert checklist["Согласованные зап. части"] == ""
    assert checklist["Фактические выполняемые работы"] == "Замена платы"
    assert checklist["Согласованные риски"] == ""


def test_act_rows_only_agreed() -> None:
    text = (
        "Клиент: да, согласен на все три работы, гарантия три месяца на питание\n"
        "Менеджер: хорошо"
    )
    raw = {
        "works": {
            "items": [
                {
                    "name": "Техническое обслуживание",
                    "price_rub": 3000,
                    "warranty_days": 0,
                    "status": "agreed",
                    "evidence": "да, согласен на все три работы",
                },
                {
                    "name": "Замена экрана",
                    "price_rub": 9000,
                    "warranty_days": 30,
                    "status": "discussed",
                    "evidence": "гарантия три месяца на питание",
                },
            ]
        }
    }
    rows = build_act_rows(facts_from(raw, text))
    assert rows == [{"name": "Техническое обслуживание", "warranty_days": 0, "price_rub": 3000}]


def test_act_text_roundtrip() -> None:
    rows = [
        {"name": "ТО", "warranty_days": 0, "price_rub": 3000},
        {"name": "Ремонт линии", "warranty_days": 90, "price_rub": 0},
    ]
    assert parse_act_text(act_rows_to_text(rows)) == rows


def test_parse_act_text_handles_invalid_numbers() -> None:
    assert parse_act_text("Работа | abc | 1 500,5\n\n | 1 | 2") == [
        {"name": "Работа", "warranty_days": 0, "price_rub": 1500}
    ]


def test_price_check_reports_mismatch_and_zero() -> None:
    facts = facts_from(
        {"price": section("amount_rub", 24000, "agreed", "с десятью процентами согласен, делайте")}
    )
    rows = [
        {"name": "A", "warranty_days": 0, "price_rub": 3000},
        {"name": "B", "warranty_days": 0, "price_rub": 0},
    ]
    warning = price_check(facts, rows)
    assert "цена не определена" in warning
    assert "не равна согласованной стоимости 24000 руб." in warning


def test_price_check_ignores_unagreed_total() -> None:
    facts = facts_from({"price": section("amount_rub", 24000, "discussed", "дорого")})
    rows = [{"name": "A", "warranty_days": 0, "price_rub": 3000}]
    assert price_check(facts, rows) == ""


def test_price_check_passes_when_equal() -> None:
    facts = facts_from(
        {"price": section("amount_rub", 3000, "agreed", "с десятью процентами согласен, делайте")}
    )
    rows = [{"name": "A", "warranty_days": 0, "price_rub": 3000}]
    assert price_check(facts, rows) == ""


def test_build_task_for_followup() -> None:
    facts = facts_from(
        {
            "followup": {
                "needed": True,
                "when_text": "через неделю",
                "reason": "статус заказа",
            }
        }
    )
    assert build_task(facts) == {
        "title": "Перезвонить клиенту / контроль заказа",
        "when_text": "через неделю",
        "reason": "статус заказа",
    }


def test_build_task_without_followup() -> None:
    assert build_task(empty_facts()) is None


# ------------------------------------------------------------------ transcripts and extractor
def test_parse_transcript_labels_and_timestamp() -> None:
    raw = (
        "Голос 1: Добрый день\n"
        "Голос 2: Это дорого\n"
        "Участник: Алло\n"
        "[00:05] Клиент: да\n"
        "Оператор: слушаю"
    )
    items = parse_transcript(raw)
    assert [item.speaker for item in items] == [
        MANAGER,
        CLIENT,
        UNKNOWN,
        CLIENT,
        MANAGER,
    ]
    assert items[3].ts == 5.0
    assert items[0].text == "Добрый день"


def test_client_only_text_excludes_manager_and_unknown() -> None:
    raw = "Менеджер: предложение\nКлиент: согласен\nУчастник: шум"
    assert client_only_text(raw) == "согласен"
    assert manager_only_text(raw) == "предложение"


def test_extractor_end_to_end_with_fake_llm() -> None:
    answer = {
        "price": section("amount_rub", 24000, "agreed", "с десятью процентами согласен, делайте"),
        "works": {
            "items": [
                {
                    "name": "Замена материнской платы",
                    "price_rub": 24000,
                    "warranty_days": 0,
                    "status": "agreed",
                    "evidence": "с десятью процентами согласен, делайте",
                },
                {
                    "name": "Чистка драйвера лампы",
                    "price_rub": 5000,
                    "warranty_days": 30,
                    "status": "agreed",
                    "evidence": "да, чистите, хорошо",
                },
            ]
        },
        "parts": section("items", ["материнская плата"], "discussed", "плату заказываем под заказ"),
        "deadline": section("text", "2-3 недели", "discussed", "Срок два-три недели"),
        "old_parts": section("answer", "no", "agreed", "старую плату можете выбросить"),
        "warranty": section(
            "text", "на залитые принтеры гарантии нет", "agreed", "Да, понимаю риск"
        ),
        "risks": section(
            "items", ["гарантии нет на залитые принтеры"], "agreed", "Да, понимаю риск"
        ),
        "decision": {"value": "agreed", "evidence": "с десятью процентами согласен, делайте"},
        "objections": ["дорого"],
        "followup": {"needed": True, "when_text": "через неделю", "reason": "статус заказа"},
        "summary": "Клиент согласился на ремонт со скидкой.",
    }

    def fake_llm(messages: list[dict[str, str]]) -> str:
        return json.dumps(answer, ensure_ascii=False)

    facts = FactExtractor(fake_llm).extract(CALL)
    assert facts["decision"]["value"] == "agreed"
    comment = build_comment(facts)
    assert "24000 руб." in comment
    assert "Обсуждалось, не согласовано" in comment
    checklist = build_checklist(facts)
    assert checklist["Согласованные сроки ремонта"] == ""
    assert checklist["Фактические выполняемые работы"] == "Замена материнской платы"
    assert build_act_rows(facts) == [
        {"name": "Замена материнской платы", "warranty_days": 0, "price_rub": 24000}
    ]
    assert build_task(facts) is not None
    assert build_task(facts)["when_text"] == "через неделю"


def test_extractor_prompt_contains_system_schema_and_order_context() -> None:
    seen: list[list[dict[str, str]]] = []

    def fake_llm(messages: list[dict[str, str]]) -> str:
        seen.append(messages)
        return "{}"

    facts = FactExtractor(fake_llm).extract(CALL, order_context="Заказ 42, принтер")
    assert facts["decision"]["value"] == "unclear"
    system, user = seen[0]
    assert "agreed" in system["content"]
    assert "amount_rub" in user["content"]
    assert "Схема ответа" in user["content"]
    assert "Заказ 42, принтер" in user["content"]
    assert "Расшифровка разговора" in user["content"]


def test_extractor_rejects_malformed_json() -> None:
    with pytest.raises(ExtractError, match="JSON"):
        FactExtractor(lambda messages: "не json").extract(CALL)


def test_extractor_wraps_llm_failure() -> None:
    def boom(messages: list[dict[str, str]]) -> str:
        raise RuntimeError("gateway down")

    with pytest.raises(ExtractError, match="gateway down"):
        FactExtractor(boom).extract(CALL)


def test_extractor_empty_transcript_skips_llm() -> None:
    def boom(messages: list[dict[str, str]]) -> str:
        raise AssertionError("LLM must not be called for an empty transcript")

    facts = FactExtractor(boom).extract("   ")
    assert facts["decision"]["value"] == "unclear"


def test_extractor_derives_client_text_from_labels() -> None:
    answer = {
        "price": section(
            "amount_rub", 24000, "agreed", "Итого получится двадцать четыре тысячи"
        )
    }

    def fake_llm(messages: list[dict[str, str]]) -> str:
        return json.dumps(answer, ensure_ascii=False)

    facts = FactExtractor(fake_llm).extract(CALL)
    # The quote belongs to the manager, so the agreement is downgraded.
    assert facts["price"]["status"] == "discussed"
    assert facts["price"]["verified"] is True


# ------------------------------------------------------------------ coach
def test_playbook_text_fallback_and_limits() -> None:
    assert "не заданы" in playbook_text(None)
    text = playbook_text(
        {
            "limits": {"max_discount_percent": 10},
            "warranty": {"standard_days": 90},
        }
    )
    assert "10%" in text
    assert "90 дн." in text


def test_coach_brief_prompt_contains_order_and_history() -> None:
    seen: list[list[dict[str, str]]] = []

    def fake_llm(messages: list[dict[str, str]]) -> str:
        seen.append(messages)
        return "бриф"

    coach = Coach(fake_llm, playbook={"limits": {"max_discount_percent": 10}})
    answer = coach.brief(
        "Принтер, плата 15000",
        history_text="Клиент отказался из-за цены",
        calls_text="20.10 — дорого",
    )
    assert answer == "бриф"
    user = seen[0][1]["content"]
    assert "Принтер, плата 15000" in user
    assert "Клиент отказался из-за цены" in user
    assert "20.10 — дорого" in user
    assert "10%" in user
    assert "3 варианта предложения" in user


def test_coach_tip_keeps_recent_lines_and_truncates_order() -> None:
    seen: list[list[dict[str, str]]] = []

    def fake_llm(messages: list[dict[str, str]]) -> str:
        seen.append(messages)
        return "совет"

    coach = Coach(fake_llm, tip_order_chars=50)
    transcript = "\n".join(f"Клиент: линия {index}" for index in range(1, 21))
    assert coach.tip("З" * 100, transcript=transcript) == "совет"
    user = seen[0][1]["content"]
    assert "З" * 50 in user
    assert "З" * 51 not in user
    assert "линия 20" in user
    assert "линия 7" in user
    assert "линия 6" not in user


def test_coach_injectable_system_prompt_and_order_limit() -> None:
    seen: list[list[dict[str, str]]] = []

    def fake_llm(messages: list[dict[str, str]]) -> str:
        seen.append(messages)
        return "ок"

    coach = Coach(fake_llm, system_prompt="МОЙ ПРОМПТ", order_chars=10)
    coach.brief("abcdefghijklmno")
    assert seen[0][0]["content"] == "МОЙ ПРОМПТ"
    assert "abcdefghij" in seen[0][1]["content"]
    assert "abcdefghijk" not in seen[0][1]["content"]


# ------------------------------------------------------------------ history
def test_history_save_and_list_roundtrip(tmp_path: Path) -> None:
    history = CallHistory(tmp_path / "calls")
    summary = {
        "outcome": "thinking",
        "summary": "Клиент думает",
        "offers": ["скидка 10%"],
        "objections": ["дорого"],
        "numbers": ["15 тысяч"],
        "promises": ["перезвонить"],
    }
    record = history.save("202610-1913", CALL, summary)
    assert record["summary"]["outcome"] == "thinking"
    folder = tmp_path / "calls" / safe_order_name("202610-1913")
    assert len(list(folder.glob("*.json"))) == 1
    records = history.list("202610-1913")
    assert len(records) == 1
    assert records[0]["text"] == CALL
    assert records[0]["summary"] == normalize_summary(summary)
    assert records[0]["saved_at"]


def test_history_safe_order_name_blocks_traversal(tmp_path: Path) -> None:
    safe = safe_order_name("../../evil")
    assert "/" not in safe
    assert ".." not in safe
    history = CallHistory(tmp_path / "calls")
    history.save("../evil", "текст", "итог")
    files = list((tmp_path / "calls").rglob("*.json"))
    assert len(files) == 1
    assert files[0].resolve().is_relative_to((tmp_path / "calls").resolve())
    assert len(history.list("../evil")) == 1


def test_history_atomic_write_cleans_up_on_failure(tmp_path: Path, monkeypatch) -> None:
    history = CallHistory(tmp_path / "calls")

    def broken_replace(source: object, target: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(history_module.os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        history.save("A", "текст", "итог")
    assert history.list("A") == []
    assert list((tmp_path / "calls").rglob("*.tmp")) == []


def test_history_digest_keeps_newest_and_trims_oldest(tmp_path: Path) -> None:
    history = CallHistory(tmp_path / "calls")
    history.save("A", "первый", {"outcome": "refused", "summary": "старый итог"})
    sleep(0.002)
    history.save("A", "второй", {"outcome": "agreed", "summary": "новый итог"})
    full = history.digest("A")
    lines = full.splitlines()
    assert len(lines) == 2
    assert "старый итог" in full
    assert "новый итог" in full
    short = history.digest("A", max_chars=len(lines[-1]) + 1)
    assert lines[-1] in short
    assert "старый итог" not in short


def test_history_digest_missing_order_is_empty(tmp_path: Path) -> None:
    assert CallHistory(tmp_path / "calls").digest("missing") == ""


def test_history_skips_corrupt_records(tmp_path: Path) -> None:
    folder = tmp_path / "calls" / safe_order_name("A")
    folder.mkdir(parents=True)
    (folder / "bad.json").write_text("not json", encoding="utf-8")
    assert CallHistory(tmp_path / "calls").list("A") == []


def test_history_stores_no_audio(tmp_path: Path) -> None:
    history = CallHistory(tmp_path / "calls")
    history.save("A", "расшифровка", "итог")
    assert list((tmp_path / "calls").rglob("*.wav")) == []
    assert list((tmp_path / "calls").rglob("*.mp3")) == []
    assert list((tmp_path / "calls").rglob("*.ogg")) == []


def test_summarize_normalizes_fake_llm() -> None:
    calls = 0

    def fake_llm(messages: list[dict[str, str]]) -> str:
        nonlocal calls
        calls += 1
        return json.dumps(
            {
                "outcome": "refused",
                "summary": "Клиент отказался из-за цены.",
                "offers": ["скидка 10%"],
                "objections": ["дорого"],
                "numbers": ["15 тысяч за плату"],
                "promises": [],
            },
            ensure_ascii=False,
        )

    summary = summarize(fake_llm, CALL)
    assert calls == 1
    assert summary["outcome"] == "refused"
    assert summary["summary"] == "Клиент отказался из-за цены."
    assert summary["offers"] == ["скидка 10%"]
    assert summary["promises"] == []


def test_summarize_short_call_skips_llm() -> None:
    def boom(messages: list[dict[str, str]]) -> str:
        raise AssertionError("LLM must not be called for a short call")

    summary = summarize(boom, "Алло")
    assert summary["outcome"] == "no_conversation"
    assert "по сути не было" in summary["summary"]


def test_summarize_accepts_utterance_tuples() -> None:
    seen: list[list[dict[str, str]]] = []

    def fake_llm(messages: list[dict[str, str]]) -> str:
        seen.append(messages)
        return json.dumps(
            {"outcome": "thinking", "summary": "Клиент думает."}, ensure_ascii=False
        )

    transcript = [
        (0.0, "Голос 1", "Добрый день, это сервисный центр, звоню по вашему принтеру."),
        (6.2, "Голос 2", "Сколько это будет стоить, пятнадцать тысяч это дорого."),
    ]
    summary = summarize(fake_llm, transcript)
    assert summary["outcome"] == "thinking"
    assert "Голос 1" in seen[0][1]["content"]
    assert "Голос 2" in seen[0][1]["content"]
    assert transcript_text(transcript).startswith("Голос 1: Добрый день")


def test_summarize_malformed_answer_raises_history_error() -> None:
    with pytest.raises(HistoryError, match="JSON"):
        summarize(lambda messages: "не json", CALL)


# ------------------------------------------------------------------ metrics
def test_metrics_counts_and_rates() -> None:
    metrics = CrmMetrics()
    metrics.record_order(10, prepared=True, filled=True)
    metrics.record_order(20, prepared=True, filled=False, error=True)
    assert metrics.orders_total == 2
    assert metrics.prepared == 2
    assert metrics.filled == 1
    assert metrics.errors == 1
    assert metrics.seconds_total == 30.0
    assert metrics.automation_rate == 0.5
    assert metrics.average_seconds == 15.0
    assert metrics.as_dict() == {
        "orders_total": 2,
        "prepared": 2,
        "filled": 1,
        "errors": 1,
        "seconds_total": 30.0,
        "automation_rate": 0.5,
        "average_seconds": 15.0,
    }


def test_metrics_zero_state_is_safe() -> None:
    data = CrmMetrics().as_dict()
    assert data["orders_total"] == 0
    assert data["automation_rate"] == 0.0
    assert data["average_seconds"] == 0.0


def test_metrics_negative_seconds_are_clamped() -> None:
    metrics = CrmMetrics()
    metrics.record_order(-5, error=True)
    assert metrics.seconds_total == 0.0
    assert metrics.errors == 1
    assert metrics.average_seconds == 0.0


def test_metrics_reset() -> None:
    metrics = CrmMetrics()
    metrics.record_order(5, prepared=True, filled=True)
    metrics.reset()
    data = metrics.as_dict()
    assert data["orders_total"] == 0
    assert data["prepared"] == 0
    assert data["seconds_total"] == 0.0


def test_metrics_are_thread_safe() -> None:
    metrics = CrmMetrics()

    def record(_: int) -> None:
        metrics.record_order(1.0, prepared=True, filled=True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(record, range(1600)))
    assert metrics.orders_total == 1600
    assert metrics.filled == 1600
    assert metrics.seconds_total == 1600.0
