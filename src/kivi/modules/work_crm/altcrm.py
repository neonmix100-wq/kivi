"""AltCRM adapter over Playwright/CDP with a strict no-save, no-send policy.

The adapter connects to an already running, already authenticated Chrome
through the DevTools protocol. It never launches a browser or a profile, never
stores credentials and never evaluates arbitrary JavaScript. Only these actions
exist: read the visible card, fill named checklist fields and the order comment,
append rows to the act block and fill the task form.

The invariant is simple: Kivi fills, the human saves. Every click goes through
:meth:`AltCrmClient._click_safe`, which refuses any text containing "Сохранить",
"Отправить", "Удалить", "Оплатить", "Скидка" or "Создать". There is no code
path that submits a form, sends a message to a client or closes an order.

Selectors live in :data:`DEFAULT_SELECTORS` and mirror ``selectors.toml`` from
the original work-mode prototype. They can be overridden through
:class:`AltCrmConfig`; keys of the ``checklist`` mapping are the canonical
labels from :data:`CHECKLIST_LABELS` and values are their placeholders.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from kivi.tools.base import ToolError

log = logging.getLogger(__name__)

MAX_COMMENT_CHARS = 5000
MAX_CHECKLIST_CHARS = 2000
MAX_ROWS = 20
MAX_ROW_NAME_CHARS = 200
MAX_TEXT_CHARS = 6000
MAX_FIELD_CHARS = 300
MAX_ORDER_URL_CHARS = 2048
MAX_DUE_CHARS = 100

# Buttons the adapter must never press, anywhere, under any selector config.
FORBIDDEN_CLICK_MARKERS: tuple[str, ...] = (
    "сохранить",
    "отправить",
    "удалить",
    "оплатить",
    "скидка",
)
# Plus the task-creation button: the human creates the task, not the adapter.
NEVER_CLICK_MARKERS: tuple[str, ...] = (*FORBIDDEN_CLICK_MARKERS, "создать")

DEFAULT_SELECTORS: dict[str, Any] = {
    "order": {
        "title_regex": r"Устройство\s*#(\d+)",
        "edit_button": "Редактировать",
        "add_task_button": "Добавить задачу",
    },
    "comment": {"placeholder": "Напишите сообщение..."},
    "checklist": {
        "Согласованные сроки ремонта": "Согласованные сроки ремонта",
        "Согласованные зап. части": "Согласованные зап. части",
        "Фактические выполняемые работы": "Фактические выполняемые работы",
        "Согласованные риски": "Согласованные риски",
    },
    "task": {
        "dialog_title": "Добавление новой задачи",
        "create_button": "Создать задачу",
        "comment_placeholder": "Комментарий",
        "due_placeholder": "Срок выполнения",
    },
    "act": {
        "block_title": "Расчет стоимости запчастей",
        "search_placeholder": "Поиск",
        "add_button_class": "add-form-btn",
        "name_class": "name_input",
        "row_class": "table-row",
        "warranty_class": "warranty-input",
        "price_class": "price-input",
    },
}
CHECKLIST_LABELS: tuple[str, ...] = tuple(DEFAULT_SELECTORS["checklist"])

_COMMENT_FIELD = "Комментарий в чат заказа"
_CLASS_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_FIELD_LINE = re.compile(r"^\s*(?P<key>[^:\n]{2,60}?)\s*:\s*(?P<value>\S.*?)\s*$")
_ORDER_CODE = re.compile(r"/device/([\w-]+)")
_QUICK_NUMBERS = {
    "один": 1,
    "одну": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "пару": 2,
    "пара": 2,
    "несколько": 3,
}
_DUE_FORMATS: tuple[str, ...] = (
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)
_DEFAULT_HOUR = 10


class AltCrmError(ToolError):
    """An expected AltCRM failure; the tool layer turns it into a value."""


@dataclass(frozen=True, slots=True)
class AltCrmConfig:
    """Connection and selector settings for :class:`AltCrmClient`.

    ``order_url_contains`` is a case-insensitive substring of the order tab URL
    (for example ``"altcrm"`` or ``"/devices/order/"``). An empty string means
    "any existing tab", which is handy for tests but not recommended in use.
    """

    cdp_url: str = "http://127.0.0.1:9222"
    order_url_contains: str = "altcrm"
    timeout_ms: int = 15000
    selectors: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        parts = urlsplit(self.cdp_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("cdp_url must be an http(s) URL with a host")
        if parts.username or parts.password:
            raise ValueError("cdp_url must not contain credentials")
        if parts.query or parts.fragment:
            raise ValueError("cdp_url must not contain a query string or fragment")
        if not isinstance(self.timeout_ms, int) or isinstance(self.timeout_ms, bool):
            raise ValueError("timeout_ms must be an integer")
        if not 1 <= self.timeout_ms <= 120_000:
            raise ValueError("timeout_ms must be between 1 and 120000")
        needle = self.order_url_contains
        if not isinstance(needle, str) or len(needle) > 200:
            raise ValueError("order_url_contains must be a string of at most 200 chars")
        if any(ord(char) < 32 or ord(char) == 127 for char in needle):
            raise ValueError("order_url_contains must not contain control characters")
        if self.selectors is not None and not isinstance(self.selectors, dict):
            raise ValueError("selectors must be a mapping or None")


@dataclass(frozen=True, slots=True)
class OrderInfo:
    """Visible order-card data; ``text`` is untrusted page content."""

    url: str
    order_code: str
    device_id: str
    text: str
    fields: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "order_code": self.order_code,
            "device_id": self.device_id,
            "text": self.text,
            "fields": dict(self.fields),
        }


@dataclass(frozen=True, slots=True)
class FieldResult:
    """Outcome of one field, row or form action; errors are values."""

    field: str
    ok: bool
    error: str = ""
    note: str = ""
    strategy: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "ok": self.ok,
            "error": self.error,
            "note": self.note,
            "strategy": self.strategy,
        }


def resolve_selectors(override: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Merge ``override`` into the defaults and validate required selectors."""
    merged: dict[str, Any] = {key: dict(value) for key, value in DEFAULT_SELECTORS.items()}
    for section, values in (override or {}).items():
        current = merged.get(section)
        if isinstance(values, Mapping) and isinstance(current, dict):
            current.update(values)
        else:
            merged[section] = values

    for section in ("order", "comment", "checklist", "task", "act"):
        if not isinstance(merged.get(section), dict):
            raise ValueError(f"selectors.{section} must be a mapping")

    order = merged["order"]
    for key in ("title_regex", "edit_button", "add_task_button"):
        if not _text_value(order.get(key)):
            raise ValueError(f"selectors.order.{key} must be a non-empty string")
    try:
        re.compile(order["title_regex"])
    except re.error as exc:
        raise ValueError(f"selectors.order.title_regex is invalid: {exc}") from exc

    if not _text_value(merged["comment"].get("placeholder")):
        raise ValueError("selectors.comment.placeholder must be a non-empty string")

    checklist = merged["checklist"]
    if not checklist:
        raise ValueError("selectors.checklist must not be empty")
    for label, placeholder in checklist.items():
        if not _text_value(label) or not _text_value(placeholder):
            raise ValueError("selectors.checklist keys and values must be non-empty strings")

    task = merged["task"]
    for key in ("dialog_title", "create_button", "comment_placeholder", "due_placeholder"):
        if not _text_value(task.get(key)):
            raise ValueError(f"selectors.task.{key} must be a non-empty string")

    act = merged["act"]
    for key in ("block_title", "search_placeholder"):
        if not _text_value(act.get(key)):
            raise ValueError(f"selectors.act.{key} must be a non-empty string")
    for key in ("add_button_class", "name_class", "row_class", "warranty_class", "price_class"):
        value = act.get(key)
        if not isinstance(value, str) or not _CLASS_NAME.fullmatch(value):
            raise ValueError(f"selectors.act.{key} must be a plain CSS class name")
    return merged


def _text_value(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _page_url(page: Any) -> str:
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:  # noqa: BLE001 - a detached page has no URL
        return ""


def _is_page_closed(page: Any) -> bool:
    checker = getattr(page, "is_closed", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:  # noqa: BLE001 - treat an unreadable state as closed
        return True


def _default_playwright_factory() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise AltCrmError(
            "playwright is not installed; install the browser extra first"
        ) from exc
    return sync_playwright()


def _xpath_literal(value: str) -> str:
    """Return an XPath string literal, preferring double quotes."""
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    raise AltCrmError("selector contains both quote kinds and cannot be used")


def _css_attribute(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    raw = str(value).strip().replace(" ", "").replace(",", ".")
    if not raw:
        return 0
    try:
        return max(int(float(raw)), 0)
    except ValueError:
        return 0


def parse_when(text: str, now: datetime | None = None) -> datetime | None:
    """Parse an explicit date or a short Russian phrase into a due datetime."""
    source = (text or "").strip()
    if not source:
        return None
    for fmt in _DUE_FORMATS:
        try:
            return datetime.strptime(source, fmt)
        except ValueError:
            continue
    current = now or datetime.now()
    lowered = source.lower().replace("ё", "е")
    if "послезавтра" in lowered:
        return _day_after(current, 2)
    if "завтра" in lowered:
        return _day_after(current, 1)
    match = re.search(
        r"через\s+(?:(\d+|[а-я]+)\s+)?(мин\w*|час\w*|дн\w*|день|сут\w*|недел\w*)",
        lowered,
    )
    if not match:
        return None
    raw, unit_word = match.group(1), match.group(2)
    if raw is None:
        count = 1
    elif raw.isdigit():
        count = int(raw)
    elif raw in _QUICK_NUMBERS:
        count = _QUICK_NUMBERS[raw]
    else:
        return None
    if unit_word.startswith("мин"):
        return current + timedelta(minutes=count)
    if unit_word.startswith("час"):
        return current + timedelta(hours=count)
    weeks = unit_word.startswith("недел")
    return _day_after(current, count * (7 if weeks else 1))


def _day_after(now: datetime, days: int) -> datetime:
    target = now + timedelta(days=days)
    return target.replace(hour=_DEFAULT_HOUR, minute=0, second=0, microsecond=0)


def format_due(value: datetime) -> str:
    """Format a due datetime the way the AltCRM task dialog expects it."""
    return value.strftime("%d.%m.%Y %H:%M")


def _parse_fields(text: str) -> dict[str, str]:
    """Collect ``Label: value`` lines from the visible card text, capped."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        match = _FIELD_LINE.match(line)
        if match is None:
            continue
        key = match.group("key").strip()
        value = match.group("value").strip()
        if not key or not value:
            continue
        fields[key] = value[:MAX_FIELD_CHARS]
        if len(fields) >= 40:
            break
    return fields


class AltCrmClient:
    """Lazy CDP connection to one existing AltCRM order tab.

    ``page`` injects a page object for unit tests. ``playwright_factory`` must
    return a ``sync_playwright()``-like manager; the default imports Playwright
    on first use. The browser itself is never launched and never closed.
    """

    def __init__(
        self,
        config: AltCrmConfig,
        *,
        page: Any = None,
        playwright_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not isinstance(config, AltCrmConfig):
            raise TypeError("config must be an AltCrmConfig")
        self.config = config
        self.selectors = resolve_selectors(config.selectors)
        self._page = page
        self._playwright_factory = playwright_factory
        self._manager: Any = None
        self._playwright: Any = None
        self._browser: Any = None
        self._closed = False

    @property
    def is_connected(self) -> bool:
        return self._browser is not None and not self._closed

    @property
    def is_closed(self) -> bool:
        return self._closed

    # -- lifecycle -------------------------------------------------------
    def _ensure_page(self) -> Any:
        if self._closed:
            raise AltCrmError("AltCRM client is closed")
        if self._page is not None and not _is_page_closed(self._page):
            return self._page
        self._page = None
        if self._browser is None:
            self._connect()
        try:
            self._page = self._select_order_page()
        except AltCrmError:
            self._browser = None
            self._stop_playwright()
            raise
        set_default_timeout = getattr(self._page, "set_default_timeout", None)
        if callable(set_default_timeout):
            try:
                set_default_timeout(self.config.timeout_ms)
            except Exception:  # noqa: BLE001 - a default timeout is best effort
                log.debug("could not set the AltCRM page timeout")
        return self._page

    def _connect(self) -> None:
        if self._playwright is not None:
            return
        factory = self._playwright_factory or _default_playwright_factory
        try:
            manager = factory()
            playwright = manager.start()
        except AltCrmError:
            raise
        except Exception as exc:  # noqa: BLE001 - startup failure is typed
            raise AltCrmError("cannot start Playwright for AltCRM") from exc
        self._manager = manager
        self._playwright = playwright
        try:
            self._browser = playwright.chromium.connect_over_cdp(self.config.cdp_url)
        except Exception as exc:  # noqa: BLE001 - connect failure is typed
            self._stop_playwright()
            raise AltCrmError(
                "cannot connect to Chrome over CDP; start Chrome with "
                "--remote-debugging-port=9222 and keep the AltCRM order tab open"
            ) from exc
        if self._browser is None:
            self._stop_playwright()
            raise AltCrmError("CDP connection returned no browser")

    def _select_order_page(self) -> Any:
        if self._browser is None:
            raise AltCrmError("no CDP connection")
        needle = self.config.order_url_contains.lower()
        matches: list[Any] = []
        for context in getattr(self._browser, "contexts", ()) or ():
            for page in getattr(context, "pages", ()) or ():
                if needle in _page_url(page).lower():
                    matches.append(page)
        if not matches:
            raise AltCrmError(
                "no open tab matches the configured AltCRM URL; open the order card "
                "in the Chrome instance started with --remote-debugging-port=9222"
            )
        return matches[-1]

    def _stop_playwright(self) -> None:
        playwright, manager = self._playwright, self._manager
        self._playwright = None
        self._manager = None
        try:
            if playwright is not None and callable(getattr(playwright, "stop", None)):
                playwright.stop()
            elif manager is not None and callable(getattr(manager, "stop", None)):
                manager.stop()
        except Exception:  # noqa: BLE001 - shutdown is best effort
            log.debug("error while stopping Playwright for AltCRM")

    def close(self) -> None:
        """Detach from CDP; idempotent and never closes Chrome or the page."""
        if self._closed:
            return
        self._closed = True
        self._page = None
        self._browser = None
        self._stop_playwright()

    def __enter__(self) -> AltCrmClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- reading ---------------------------------------------------------
    def read_order(self) -> OrderInfo:
        """Read the visible order card; performs no clicks and no writes."""
        page = self._ensure_page()
        try:
            raw = page.inner_text("body")
        except Exception as exc:  # noqa: BLE001 - a page read failure is typed
            raise AltCrmError("cannot read the AltCRM order card") from exc
        text = re.sub(r"\n{2,}", "\n", str(raw or ""))
        url = _page_url(page)
        title = re.search(str(self.selectors["order"]["title_regex"]), text)
        code = _ORDER_CODE.search(url)
        if title is None:
            device_id = ""
        elif title.groups():
            device_id = title.group(1)
        else:
            device_id = title.group(0)
        return OrderInfo(
            url=url,
            order_code=code.group(1) if code else "",
            device_id=device_id,
            text=text[:MAX_TEXT_CHARS],
            fields=_parse_fields(text),
        )

    # -- filling ---------------------------------------------------------
    def fill_order(
        self,
        comment: str,
        checklist: Mapping[str, str],
    ) -> list[FieldResult]:
        """Fill named checklist fields and the comment; never saves or sends."""
        if not isinstance(comment, str):
            raise AltCrmError("comment must be a string")
        if len(comment) > MAX_COMMENT_CHARS:
            raise AltCrmError(f"comment is longer than {MAX_COMMENT_CHARS} characters")
        if not isinstance(checklist, Mapping):
            raise AltCrmError("checklist must be a mapping")

        results: list[FieldResult] = []
        todo: dict[str, str] = {}
        for label, value in checklist.items():
            if value is None or value == "":
                continue
            if not isinstance(label, str) or not isinstance(value, str):
                results.append(
                    FieldResult(str(label), False, error="label and value must be strings")
                )
                continue
            if label not in self.selectors["checklist"]:
                results.append(
                    FieldResult(label, False, error="unknown checklist field")
                )
                continue
            if len(value) > MAX_CHECKLIST_CHARS:
                results.append(
                    FieldResult(
                        label,
                        False,
                        error=f"value is longer than {MAX_CHECKLIST_CHARS} characters",
                    )
                )
                continue
            todo[label] = value

        if not todo and not comment:
            return results

        page = self._ensure_page()
        if todo:
            results.extend(self._enter_edit_mode(page))
        for label, value in todo.items():
            placeholder = str(self.selectors["checklist"][label])
            results.append(
                self._fill_field(page, label, value, placeholder=placeholder)
            )
        if comment:
            results.append(self._fill_comment(page, comment))
        return results

    def _enter_edit_mode(self, page: Any) -> list[FieldResult]:
        label = str(self.selectors["order"]["edit_button"])
        try:
            clicked = self._click_safe(label, page=page)
        except AltCrmError as exc:
            return [FieldResult(label, False, error=str(exc)[:200])]
        if not clicked:
            return []
        with suppress(Exception):
            page.wait_for_timeout(600)
        return []

    def _click_safe(self, text: str, *, page: Any = None, scope: Any = None) -> bool:
        """Click the first visible exact-text element, refusing dangerous text."""
        if not isinstance(text, str) or not text.strip():
            return False
        lowered = text.strip().lower()
        for marker in NEVER_CLICK_MARKERS:
            if marker in lowered:
                raise AltCrmError(
                    f"refusing to click {text!r}: the adapter never saves, sends or creates"
                )
        root = scope if scope is not None else page
        if root is None:
            root = self._ensure_page()
        try:
            locator = root.get_by_text(text, exact=True)
            count = int(locator.count())
        except Exception as exc:  # noqa: BLE001 - locator failure is typed
            raise AltCrmError(f"cannot locate {text!r} on the page") from exc
        for index in range(count):
            try:
                element = locator.nth(index)
                if element.is_visible():
                    element.click()
                    return True
            except Exception:  # noqa: BLE001 - try the next candidate
                continue
        return False

    def _fill_field(
        self,
        page: Any,
        label: str,
        value: str,
        *,
        placeholder: str | None = None,
    ) -> FieldResult:
        return self._fill_within(
            page,
            label,
            value,
            placeholder=placeholder or label,
            field_label=label,
        )

    def _fill_comment(self, page: Any, text: str) -> FieldResult:
        placeholder = str(self.selectors["comment"]["placeholder"])
        result = self._fill_within(
            page,
            _COMMENT_FIELD,
            text,
            placeholder=placeholder,
            field_label=_COMMENT_FIELD,
        )
        if result.ok:
            return FieldResult(
                field=result.field,
                ok=True,
                note="не отправлено",
                strategy=result.strategy,
            )
        return result

    def _fill_within(
        self,
        root: Any,
        label: str,
        value: str,
        *,
        placeholder: str,
        field_label: str,
        press_tab: bool = False,
    ) -> FieldResult:
        strategies: tuple[tuple[str, Callable[[], Any]], ...] = (
            ("placeholder", lambda: root.get_by_placeholder(placeholder, exact=True)),
            ("placeholder~", lambda: root.get_by_placeholder(placeholder, exact=False)),
            ("label", lambda: root.get_by_label(placeholder, exact=False)),
            (
                "css",
                lambda: root.locator(
                    f'input[placeholder="{_css_attribute(placeholder)}"], '
                    f'textarea[placeholder="{_css_attribute(placeholder)}"]'
                ),
            ),
        )
        last_problem = "поле не найдено (проверьте, что включён режим правки)"
        for name, make in strategies:
            try:
                locator = make()
                count = int(locator.count())
            except Exception as exc:  # noqa: BLE001 - a strategy may be unsupported
                last_problem = str(exc)[:200]
                continue
            if count == 0:
                continue
            if count > 1:
                last_problem = (
                    f"найдено {count} полей с названием «{label}», не знаю, какое выбрать"
                )
                continue
            element = locator.first
            try:
                if not element.is_visible():
                    last_problem = "поле скрыто"
                    continue
                if not element.is_enabled():
                    last_problem = "поле недоступно для правки (нажмите «Редактировать»)"
                    continue
                element.fill(value)
                actual = self._read_value(element)
                if actual is not None and actual != value:
                    last_problem = "значение не принято полем"
                    continue
                if press_tab:
                    self._press_tab(element)
                return FieldResult(field=field_label, ok=True, strategy=name)
            except Exception as exc:  # noqa: BLE001 - a field failure is a value
                last_problem = str(exc)[:200]
        return FieldResult(field=field_label, ok=False, error=last_problem)

    @staticmethod
    def _read_value(element: Any) -> str | None:
        reader = getattr(element, "input_value", None)
        if not callable(reader):
            return None
        try:
            return str(reader())
        except Exception:  # noqa: BLE001 - verification is best effort
            return None

    # -- act rows --------------------------------------------------------
    def add_work_rows(self, rows: list[Mapping[str, Any]]) -> list[FieldResult]:
        """Append act rows to "Расчет стоимости запчастей"; never saves."""
        normalized = self._normalize_rows(rows)
        page = self._ensure_page()
        results: list[FieldResult] = []
        results.extend(self._enter_edit_mode(page))
        act = self.selectors["act"]
        block = self._find_act_block(page)
        if block is None:
            results.append(
                FieldResult(
                    f"блок «{act['block_title']}»",
                    False,
                    error="не найден",
                )
            )
            return results

        row_selector = f".{act['row_class']}"
        name_selector = f"input.{act['name_class']}"
        search = self._act_search_input(block)
        if search is None:
            results.append(
                FieldResult(
                    f"блок «{act['block_title']}»",
                    False,
                    error="не нашёл поле ввода названия в блоке",
                )
            )
            return results

        existing = self._row_names(block, row_selector, name_selector)
        for row in normalized:
            name = str(row["name"])
            label = f"работа «{name}»"
            try:
                wanted = name.strip().lower()
                if wanted in existing:
                    results.append(
                        FieldResult(
                            label,
                            True,
                            note="уже есть в блоке, пропущена",
                        )
                    )
                    continue
                before = self._row_count(block, row_selector)
                search.fill(name)
                button = self._add_row_button(search)
                if button is None:
                    results.append(
                        FieldResult(label, False, error="кнопка «+» не найдена")
                    )
                    continue
                button.click()
                page.wait_for_timeout(700)
                if self._row_count(block, row_selector) <= before:
                    results.append(
                        FieldResult(
                            label,
                            False,
                            error="после «+» строка не появилась "
                            "(возможно, нужно выбрать вариант из подсказок)",
                        )
                    )
                    continue
                search.fill("")
                new_row = self._find_new_row(block, row_selector, name_selector, wanted)
                if new_row is None:
                    results.append(
                        FieldResult(
                            label,
                            False,
                            error="не смог определить, какая строка новая — "
                            "заполните вручную",
                        )
                    )
                    continue
                outcome = self._fill_act_row(new_row, row, name_selector)
                results.append(outcome)
                if outcome.ok:
                    existing.append(wanted)
            except Exception as exc:  # noqa: BLE001 - a row failure is a value
                results.append(FieldResult(label, False, error=str(exc)[:200]))
        return results

    def _normalize_rows(
        self, rows: list[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        if not isinstance(rows, list) or not rows:
            raise AltCrmError("rows must be a non-empty list")
        if len(rows) > MAX_ROWS:
            raise AltCrmError(f"at most {MAX_ROWS} rows are allowed")
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise AltCrmError("every row must be a mapping")
            name = str(row.get("name") or "").strip()
            if not name:
                raise AltCrmError("every row needs a non-empty name")
            if len(name) > MAX_ROW_NAME_CHARS:
                raise AltCrmError(
                    f"row name is longer than {MAX_ROW_NAME_CHARS} characters"
                )
            price = _as_int(row.get("price", row.get("price_rub", 0)))
            warranty = _as_int(row.get("warranty", row.get("warranty_days", 0)))
            normalized.append({"name": name, "price": price, "warranty": warranty})
        return normalized

    def _find_act_block(self, page: Any) -> Any:
        act = self.selectors["act"]
        placeholder = _xpath_literal(str(act["search_placeholder"]))
        try:
            titles = page.get_by_text(str(act["block_title"]), exact=True)
            if int(titles.count()) == 0:
                return None
            block = titles.first.locator(
                f"xpath=ancestor::*[.//input[@placeholder={placeholder}]][1]"
            )
            if int(block.count()) == 0:
                return None
            return block
        except Exception:  # noqa: BLE001 - a missing block is a value
            return None

    def _act_search_input(self, block: Any) -> Any:
        act = self.selectors["act"]
        candidates = (
            f'input[placeholder="{_css_attribute(str(act["search_placeholder"]))}"]',
            f"input.{act['name_class']}",
        )
        for selector in candidates:
            try:
                locator = block.locator(selector)
                if int(locator.count()) >= 1:
                    return locator.first
            except Exception:  # noqa: BLE001 - try the next strategy
                continue
        return None

    def _add_row_button(self, search: Any) -> Any:
        add_class = str(self.selectors["act"]["add_button_class"])
        try:
            button = search.locator("xpath=..").locator(f"button.{add_class}")
            if int(button.count()) == 1:
                return button.first
        except Exception:  # noqa: BLE001 - fall back to document order
            pass
        try:
            button = search.locator(
                f'xpath=following::button[contains(@class, "{add_class}")][1]'
            )
            if int(button.count()) >= 1:
                return button.first
        except Exception:  # noqa: BLE001 - a missing button is a value
            return None
        return None

    def _row_names(self, block: Any, row_selector: str, name_selector: str) -> list[str]:
        names: list[str] = []
        try:
            inputs = block.locator(f"{row_selector} {name_selector}")
            for index in range(int(inputs.count())):
                value = self._read_value(inputs.nth(index)) or ""
                names.append(value.strip().lower())
        except Exception:  # noqa: BLE001 - an unreadable block yields no names
            return names
        return names

    @staticmethod
    def _row_count(block: Any, row_selector: str) -> int:
        try:
            return int(block.locator(row_selector).count())
        except Exception:  # noqa: BLE001 - treat unreadable as unchanged
            return 0

    def _find_new_row(
        self,
        block: Any,
        row_selector: str,
        name_selector: str,
        wanted: str,
    ) -> Any:
        rows = block.locator(row_selector)
        empty = None
        for index in range(int(rows.count())):
            row = rows.nth(index)
            inputs = row.locator(name_selector)
            value = ""
            if int(inputs.count()) >= 1:
                value = (self._read_value(inputs.first) or "").strip().lower()
            if value == wanted:
                return row
            if value == "":
                empty = row
        return empty

    def _fill_act_row(
        self,
        row: Any,
        values: Mapping[str, Any],
        name_selector: str,
    ) -> FieldResult:
        act = self.selectors["act"]
        name = str(values["name"])
        label = f"работа «{name}»"
        warranty = row.locator(f"input.{act['warranty_class']}")
        price = row.locator(f"input.{act['price_class']}")
        if int(warranty.count()) != 1 or int(price.count()) != 1:
            return FieldResult(
                label,
                False,
                error=(
                    f"в строке полей гарантии: {int(warranty.count())}, "
                    f"цены: {int(price.count())} (ожидалось по 1). Заполните вручную"
                ),
            )
        name_input = row.locator(name_selector)
        if int(name_input.count()) == 1:
            current = (self._read_value(name_input.first) or "").strip()
            if not current:
                name_input.first.fill(name)
        warranty_input = warranty.first
        warranty_input.fill(str(values["warranty"]))
        self._press_tab(warranty_input)
        price_value = int(values["price"])
        if price_value:
            price_input = price.first
            price_input.fill(str(price_value))
            self._press_tab(price_input)
            return FieldResult(label, True)
        return FieldResult(label, True, note="цена не указана — введите вручную")

    @staticmethod
    def _press_tab(element: Any) -> None:
        press = getattr(element, "press", None)
        if not callable(press):
            return
        with suppress(Exception):
            press("Tab")

    # -- task form -------------------------------------------------------
    def create_task(self, comment: str, when_text: str = "") -> list[FieldResult]:
        """Fill the task dialog; never clicks "Создать задачу" or saves."""
        if not isinstance(comment, str):
            raise AltCrmError("comment must be a string")
        if not comment.strip():
            raise AltCrmError("task comment must not be empty")
        if len(comment) > 500:
            raise AltCrmError("task comment is longer than 500 characters")
        if not isinstance(when_text, str):
            raise AltCrmError("when_text must be a string")
        if len(when_text) > MAX_DUE_CHARS:
            raise AltCrmError(f"when_text is longer than {MAX_DUE_CHARS} characters")

        task = self.selectors["task"]
        add_task_button = str(self.selectors["order"]["add_task_button"])
        page = self._ensure_page()
        try:
            opened = self._click_safe(add_task_button, page=page)
        except AltCrmError as exc:
            return [FieldResult("окно задачи", False, error=str(exc)[:200])]
        if not opened:
            return [
                FieldResult(
                    "окно задачи",
                    False,
                    error=f"кнопка «{add_task_button}» не найдена",
                )
            ]
        try:
            title = page.get_by_text(str(task["dialog_title"]), exact=True).first
            title.wait_for(timeout=min(5000, self.config.timeout_ms))
            dialog = self._task_dialog(title)
            if dialog is None:
                raise AltCrmError("не удалось определить границы окна")
        except Exception as exc:  # noqa: BLE001 - a missing dialog is a value
            return [
                FieldResult(
                    "окно задачи",
                    False,
                    error=f"не открылось: {str(exc)[:160]}",
                )
            ]

        results: list[FieldResult] = [FieldResult("окно задачи", True)]
        due_result = self._fill_due(dialog, when_text)
        if due_result is not None:
            results.append(due_result)
        results.append(self._fill_task_comment(dialog, comment))
        results.append(
            FieldResult(
                "создание задачи",
                True,
                note=f"не нажато: нажмите «{task['create_button']}» вручную",
            )
        )
        return results

    def _task_dialog(self, title: Any) -> Any:
        create_button = _xpath_literal(str(self.selectors["task"]["create_button"]))
        try:
            dialog = title.locator(
                "xpath=ancestor::*[.//button[contains(normalize-space(.), "
                f"{create_button})]][1]"
            )
            if int(dialog.count()) == 0:
                return None
            return dialog
        except Exception:  # noqa: BLE001 - a missing dialog is a value
            return None

    def _fill_due(self, dialog: Any, when_text: str) -> FieldResult | None:
        task = self.selectors["task"]
        if not when_text.strip():
            return FieldResult(
                "срок",
                True,
                note="не задан — оставлен срок CRM по умолчанию",
            )
        target = parse_when(when_text, datetime.now())
        if target is None:
            return FieldResult(
                "срок",
                False,
                error=(
                    f"не понял срок «{when_text}» — оставлен срок CRM "
                    "по умолчанию, поставьте вручную"
                ),
            )
        formatted = format_due(target)
        result = self._fill_within(
            dialog,
            f"срок ({formatted})",
            formatted,
            placeholder=str(task["due_placeholder"]),
            field_label=f"срок ({formatted})",
            press_tab=True,
        )
        return result

    def _fill_task_comment(self, dialog: Any, comment: str) -> FieldResult:
        try:
            boxes = dialog.locator("textarea")
            if int(boxes.count()) == 0:
                return FieldResult(
                    "комментарий задачи",
                    False,
                    error="поле комментария не найдено",
                )
            box = boxes.first
            box.fill(comment)
            actual = self._read_value(box)
            if actual is not None and actual != comment:
                return FieldResult(
                    "комментарий задачи",
                    False,
                    error="значение не принято полем",
                )
            return FieldResult("комментарий задачи", True)
        except Exception as exc:  # noqa: BLE001 - a field failure is a value
            return FieldResult("комментарий задачи", False, error=str(exc)[:200])


__all__ = [
    "CHECKLIST_LABELS",
    "DEFAULT_SELECTORS",
    "FORBIDDEN_CLICK_MARKERS",
    "MAX_CHECKLIST_CHARS",
    "MAX_COMMENT_CHARS",
    "MAX_ROWS",
    "NEVER_CLICK_MARKERS",
    "AltCrmClient",
    "AltCrmConfig",
    "AltCrmError",
    "FieldResult",
    "OrderInfo",
    "format_due",
    "parse_when",
    "resolve_selectors",
]
