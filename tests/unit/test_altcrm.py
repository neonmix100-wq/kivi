"""Unit tests for the AltCRM Playwright/CDP adapter.

Everything runs against a small in-memory fake page. No test starts Chrome,
opens a socket or touches a real CRM. The fake records every click and fill so
the central invariant is asserted directly: save, send, delete, pay, discount
and task-creation buttons are never pressed.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pytest

from kivi.modules.work_crm.altcrm import (
    CHECKLIST_LABELS,
    AltCrmClient,
    AltCrmConfig,
    AltCrmError,
    parse_when,
    resolve_selectors,
)

DEFAULT_URL = "https://altcrm.example/device/abc-123"
BODY_TEXT = (
    "Устройство #4321\n"
    "Клиент: Иван Петров\n"
    "Статус: В работе\n"
    "Сумма: 5000 руб.\n"
)
DANGEROUS_BUTTONS = ("Сохранить", "Отправить", "Удалить", "Оплатить", "Скидка")


def norm(value: object) -> str:
    return " ".join(str(value or "").split())


class FakeElement:
    """One node of the fake DOM; its own text, attributes and click handler."""

    def __init__(
        self,
        tag: str = "div",
        *,
        text: str = "",
        placeholder: str = "",
        classes: tuple[str, ...] = (),
        value: str = "",
        visible: bool = True,
        enabled: bool = True,
        aria_label: str = "",
    ) -> None:
        self.tag = tag
        self.text = text
        self.placeholder = placeholder
        self.classes = classes
        self.value = value
        self.visible = visible
        self.enabled = enabled
        self.aria_label = aria_label
        self.parent: FakeElement | None = None
        self.children: list[FakeElement] = []
        self.on_click: Any = None
        self.fill_error: Exception | None = None
        self.wait_error: Exception | None = None
        self.clicks = 0

    def add(self, child: FakeElement) -> FakeElement:
        child.parent = self
        self.children.append(child)
        return child

    def walk(self) -> list[FakeElement]:
        nodes = [self]
        for child in self.children:
            nodes.extend(child.walk())
        return nodes


class FakeLocator:
    """Playwright-like locator over a fixed list of fake elements."""

    def __init__(self, page: FakePage, nodes: list[FakeElement]) -> None:
        self.page = page
        self.nodes = nodes

    @property
    def first(self) -> FakeLocator:
        return FakeLocator(self.page, self.nodes[:1])

    def count(self) -> int:
        return len(self.nodes)

    def nth(self, index: int) -> FakeLocator:
        return FakeLocator(self.page, [self.nodes[index]])

    def is_visible(self) -> bool:
        return bool(self.nodes) and self.nodes[0].visible

    def is_enabled(self) -> bool:
        return bool(self.nodes) and self.nodes[0].enabled

    def input_value(self) -> str:
        if not self.nodes:
            raise TimeoutError("no element")
        return self.nodes[0].value

    def fill(self, value: str) -> None:
        if not self.nodes:
            raise TimeoutError("no element to fill")
        node = self.nodes[0]
        if node.fill_error is not None:
            raise node.fill_error
        node.value = str(value)
        label = node.placeholder or node.text or ".".join(node.classes)
        self.page.events.append(("fill", label, str(value)))

    def click(self) -> None:
        if not self.nodes:
            raise TimeoutError("no element to click")
        node = self.nodes[0]
        label = norm(node.text) or node.placeholder or ".".join(node.classes)
        self.page.events.append(("click", label))
        node.clicks += 1
        if node.on_click is not None:
            node.on_click()

    def press(self, key: str) -> None:
        if not self.nodes:
            raise TimeoutError("no element")
        node = self.nodes[0]
        label = node.placeholder or node.text or ".".join(node.classes)
        self.page.events.append(("press", label, key))

    def wait_for(self, timeout: int | None = None) -> None:
        if not self.nodes:
            raise TimeoutError("element did not appear")
        if self.nodes[0].wait_error is not None:
            raise self.nodes[0].wait_error
        self.page.events.append(("wait_for", timeout))

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self.page, self.page.query(self.nodes, selector))

    def get_by_text(self, text: str, exact: bool = True) -> FakeLocator:
        return FakeLocator(self.page, self.page.find_by_text(self.nodes, text, exact))

    def get_by_placeholder(self, text: str, exact: bool = True) -> FakeLocator:
        return FakeLocator(
            self.page, self.page.find_by_placeholder(self.nodes, text, exact)
        )

    def get_by_label(self, text: str, exact: bool = True) -> FakeLocator:
        return FakeLocator(self.page, self.page.find_by_label(self.nodes, text, exact))


class FakePage:
    """Minimal order page: URL, body text, fake DOM and an event log."""

    def __init__(self, *, url: str = DEFAULT_URL, body_text: str = BODY_TEXT) -> None:
        self.url = url
        self.body_text = body_text
        self.root = FakeElement("body")
        self.events: list[tuple[Any, ...]] = []
        self.closed = False
        self.default_timeout = 0
        self.inner_text_error: Exception | None = None
        self.act_block: FakeElement | None = None
        self.act_search: FakeElement | None = None
        self.task_dialog: FakeElement | None = None

    def inner_text(self, selector: str) -> str:
        self.events.append(("inner_text", selector))
        if self.inner_text_error is not None:
            raise self.inner_text_error
        if selector != "body":
            raise AssertionError(f"unexpected selector: {selector}")
        return self.body_text

    def get_by_text(self, text: str, exact: bool = True) -> FakeLocator:
        return FakeLocator(self, self.find_by_text([self.root], text, exact))

    def get_by_placeholder(self, text: str, exact: bool = True) -> FakeLocator:
        return FakeLocator(self, self.find_by_placeholder([self.root], text, exact))

    def get_by_label(self, text: str, exact: bool = True) -> FakeLocator:
        return FakeLocator(self, self.find_by_label([self.root], text, exact))

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, self.query([self.root], selector))

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.events.append(("wait", milliseconds))

    def set_default_timeout(self, milliseconds: int) -> None:
        self.default_timeout = milliseconds

    def is_closed(self) -> bool:
        return self.closed

    # -- queries ---------------------------------------------------------
    def find_by_text(
        self, roots: list[FakeElement], text: str, exact: bool
    ) -> list[FakeElement]:
        wanted = norm(text)
        found: list[FakeElement] = []
        for root in roots:
            for node in root.walk():
                own = norm(node.text)
                if not own:
                    continue
                if (own == wanted) if exact else (wanted in own):
                    found.append(node)
        return found

    def find_by_placeholder(
        self, roots: list[FakeElement], text: str, exact: bool
    ) -> list[FakeElement]:
        wanted = norm(text)
        found: list[FakeElement] = []
        for root in roots:
            for node in root.walk():
                if node.tag not in ("input", "textarea"):
                    continue
                own = norm(node.placeholder)
                if not own:
                    continue
                if (own == wanted) if exact else (wanted in own):
                    found.append(node)
        return found

    def find_by_label(
        self, roots: list[FakeElement], text: str, exact: bool
    ) -> list[FakeElement]:
        wanted = norm(text)
        found: list[FakeElement] = []
        for root in roots:
            for node in root.walk():
                if node.tag not in ("input", "textarea"):
                    continue
                own = norm(node.placeholder or node.aria_label)
                if not own:
                    continue
                if (own == wanted) if exact else (wanted in own):
                    found.append(node)
        return found

    def query(self, nodes: list[FakeElement], selector: str) -> list[FakeElement]:
        if selector == "xpath=..":
            return [node.parent for node in nodes if node.parent is not None]
        if selector.startswith("xpath=ancestor::"):
            return self._ancestors(nodes, selector)
        if selector.startswith("xpath=following::"):
            return self._following(nodes, selector)
        parts = _split_css(selector)
        if not parts:
            return []
        current = nodes
        for part in parts:
            matched: list[FakeElement] = []
            for root in current:
                for node in root.walk():
                    if node is root:
                        continue
                    if _matches(node, part) and node not in matched:
                        matched.append(node)
            current = matched
        return current

    def _ancestors(self, nodes: list[FakeElement], selector: str) -> list[FakeElement]:
        found: list[FakeElement] = []
        input_match = re.search(r'\.//input\[@placeholder="([^"]*)"\]', selector)
        button_match = re.search(
            r'\.//button\[contains\(normalize-space\(\.\), "([^"]*)"\)\]',
            selector,
        )
        for node in nodes:
            parent = node.parent
            while parent is not None:
                if input_match is not None:
                    needed = input_match.group(1)
                    hit = any(
                        child.tag in ("input", "textarea")
                        and child.placeholder == needed
                        for child in parent.walk()
                    )
                elif button_match is not None:
                    needed = button_match.group(1)
                    hit = any(
                        child.tag == "button" and needed in norm(child.text)
                        for child in parent.walk()
                    )
                else:
                    hit = False
                if hit:
                    found.append(parent)
                    break
                parent = parent.parent
        return found

    def _following(self, nodes: list[FakeElement], selector: str) -> list[FakeElement]:
        class_match = re.search(r'following::button\[contains\(@class, "([^"]*)"\)\]', selector)
        if class_match is None:
            return []
        wanted = class_match.group(1)
        order = self.root.walk()
        found: list[FakeElement] = []
        for node in nodes:
            if node not in order:
                continue
            start = order.index(node)
            for candidate in order[start + 1 :]:
                if candidate.tag == "button" and wanted in candidate.classes:
                    found.append(candidate)
                    break
        return found

    # -- convenience -----------------------------------------------------
    def find_element(
        self, *, placeholder: str = "", text: str = "", tag: str = ""
    ) -> FakeElement:
        for node in self.root.walk():
            if placeholder and node.placeholder == placeholder:
                return node
            if text and norm(node.text) == norm(text):
                return node
            if tag and node.tag == tag:
                return node
        raise AssertionError(f"fake element not found: {placeholder or text or tag}")

    def clicks(self) -> list[str]:
        return [str(event[1]) for event in self.events if event[0] == "click"]

    def fills(self) -> list[tuple[str, str]]:
        return [(str(event[1]), str(event[2])) for event in self.events if event[0] == "fill"]


def _split_css(selector: str) -> list[str]:
    parts: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    for char in selector:
        if quote is not None:
            buffer.append(char)
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
            buffer.append(char)
        elif char.isspace():
            if buffer:
                parts.append("".join(buffer))
                buffer = []
        else:
            buffer.append(char)
    if buffer:
        parts.append("".join(buffer))
    return parts


def _matches(node: FakeElement, part: str) -> bool:
    match = re.fullmatch(
        r'(?P<tag>[a-zA-Z0-9]*)(?:\.(?P<cls>[\w-]+))?'
        r'(?:\[(?P<attr>[\w-]+)="(?P<value>[^"]*)"\])?',
        part,
    )
    if match is None:
        return False
    tag = match.group("tag")
    classes = match.group("cls")
    attr = match.group("attr")
    value = match.group("value")
    if tag and node.tag != tag:
        return False
    if classes and classes not in node.classes:
        return False
    if attr == "placeholder" and node.placeholder != value:
        return False
    return attr in (None, "placeholder")


def add_act_row(block: FakeElement, name: str = "") -> FakeElement:
    row = block.add(FakeElement("div", classes=("table-row",)))
    row.add(FakeElement("input", classes=("name_input",), value=name))
    row.add(FakeElement("input", classes=("warranty-input",)))
    row.add(FakeElement("input", classes=("price-input",)))
    return row


def make_page(
    *,
    url: str = DEFAULT_URL,
    with_fields: bool = True,
    with_act: bool = True,
    with_task: bool = True,
) -> FakePage:
    page = FakePage(url=url)
    root = page.root
    edit = root.add(FakeElement("button", text="Редактировать"))
    edit.on_click = lambda: setattr(edit, "visible", False)
    for text in DANGEROUS_BUTTONS:
        root.add(FakeElement("button", text=text))
    if with_fields:
        for label in CHECKLIST_LABELS:
            root.add(FakeElement("input", placeholder=label))
        root.add(FakeElement("textarea", placeholder="Напишите сообщение..."))
    if with_act:
        block = root.add(FakeElement("div", classes=("act-block",)))
        block.add(FakeElement("div", text="Расчет стоимости запчастей"))
        search = block.add(
            FakeElement("input", classes=("name_input",), placeholder="Поиск")
        )
        add_button = block.add(
            FakeElement("button", classes=("add-form-btn",), text="+")
        )
        add_button.on_click = lambda: add_act_row(block)
        page.act_block = block
        page.act_search = search
    if with_task:
        root.add(FakeElement("button", text="Добавить задачу"))
        dialog = root.add(FakeElement("div", classes=("task-dialog",)))
        dialog.add(FakeElement("div", text="Добавление новой задачи"))
        dialog.add(FakeElement("button", text="Создать задачу"))
        dialog.add(FakeElement("input", placeholder="Срок выполнения"))
        dialog.add(FakeElement("textarea"))
        page.task_dialog = dialog
    return page


class FakeContext:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages
        self.new_page_calls = 0

    def new_page(self) -> FakePage:
        self.new_page_calls += 1
        raise AssertionError("the adapter must never create a new page")


class FakeBrowser:
    def __init__(self, pages: list[FakePage]) -> None:
        self.contexts = [FakeContext(pages)]


class FakeChromium:
    def __init__(self, manager: FakePlaywrightManager) -> None:
        self.manager = manager

    def connect_over_cdp(self, url: str) -> FakeBrowser:
        self.manager.connect_urls.append(url)
        if self.manager.connect_error is not None:
            raise self.manager.connect_error
        return self.manager.browser


class FakePlaywright:
    def __init__(self, manager: FakePlaywrightManager) -> None:
        self.manager = manager
        self.chromium = FakeChromium(manager)

    def stop(self) -> None:
        self.manager.stopped += 1


class FakePlaywrightManager:
    def __init__(
        self,
        pages: list[FakePage],
        *,
        connect_error: Exception | None = None,
    ) -> None:
        self.browser = FakeBrowser(pages)
        self.connect_error = connect_error
        self.connect_urls: list[str] = []
        self.started = 0
        self.stopped = 0

    def start(self) -> FakePlaywright:
        self.started += 1
        return FakePlaywright(self)

    def stop(self) -> None:
        self.stopped += 1


# -- config and selectors ----------------------------------------------------
def test_config_defaults() -> None:
    config = AltCrmConfig()
    assert config.cdp_url == "http://127.0.0.1:9222"
    assert config.order_url_contains == "altcrm"
    assert config.timeout_ms == 15000
    assert config.selectors is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cdp_url": "ftp://127.0.0.1:9222"},
        {"cdp_url": "http://user:pass@127.0.0.1:9222"},
        {"cdp_url": "http://127.0.0.1:9222/?token=secret"},
        {"timeout_ms": 0},
        {"timeout_ms": 120_001},
        {"selectors": []},
    ],
)
def test_config_rejects_unsafe_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        AltCrmConfig(**kwargs)


def test_resolve_selectors_merges_and_validates() -> None:
    resolved = resolve_selectors(
        {
            "order": {"edit_button": "Править"},
            "checklist": {"Согласованные риски": "Риски"},
        }
    )
    assert resolved["order"]["edit_button"] == "Править"
    assert resolved["order"]["title_regex"] == r"Устройство\s*#(\d+)"
    assert resolved["checklist"]["Согласованные риски"] == "Риски"
    assert resolved["checklist"]["Согласованные сроки ремонта"] == (
        "Согласованные сроки ремонта"
    )


def test_resolve_selectors_rejects_bad_css_class() -> None:
    with pytest.raises(ValueError):
        resolve_selectors({"act": {"row_class": "not a class"}})


# -- reading -----------------------------------------------------------------
def test_read_order_parses_visible_card() -> None:
    page = make_page()
    client = AltCrmClient(AltCrmConfig(order_url_contains="altcrm"), page=page)
    info = client.read_order()

    assert info.url == DEFAULT_URL
    assert info.order_code == "abc-123"
    assert info.device_id == "4321"
    assert "Иван Петров" in info.text
    assert info.fields["Клиент"] == "Иван Петров"
    assert info.fields["Статус"] == "В работе"
    assert page.fills() == []
    assert page.clicks() == []


def test_read_order_wraps_page_failure() -> None:
    page = make_page()
    page.inner_text_error = TimeoutError("slow page")
    client = AltCrmClient(AltCrmConfig(), page=page)
    with pytest.raises(AltCrmError):
        client.read_order()


def test_read_order_requires_matching_tab_without_launching() -> None:
    other = FakePage(url="https://example.test/other")
    manager = FakePlaywrightManager([other])
    config = AltCrmConfig(cdp_url="http://127.0.0.1:9222", order_url_contains="altcrm")
    client = AltCrmClient(config, playwright_factory=lambda: manager)
    with pytest.raises(AltCrmError):
        client.read_order()
    assert manager.connect_urls == ["http://127.0.0.1:9222"]
    assert manager.started == 1
    assert manager.stopped == 1
    assert manager.browser.contexts[0].new_page_calls == 0
    assert not hasattr(manager, "launch")


def test_connect_selects_existing_matching_tab() -> None:
    other = FakePage(url="https://example.test/other")
    wanted = make_page(url="https://altcrm.example/devices/order/77")
    manager = FakePlaywrightManager([other, wanted])
    client = AltCrmClient(
        AltCrmConfig(order_url_contains="devices/order"),
        playwright_factory=lambda: manager,
    )
    info = client.read_order()
    assert info.url == "https://altcrm.example/devices/order/77"
    assert manager.browser.contexts[0].new_page_calls == 0
    assert manager.stopped == 0


def test_connect_failure_is_typed_and_stops_playwright() -> None:
    manager = FakePlaywrightManager(
        [make_page()], connect_error=RuntimeError("connection refused")
    )
    client = AltCrmClient(AltCrmConfig(), playwright_factory=lambda: manager)
    with pytest.raises(AltCrmError) as caught:
        client.read_order()
    assert "connection refused" not in str(caught.value)
    assert manager.stopped == 1


def test_close_is_idempotent_and_does_not_close_the_page() -> None:
    page = make_page()
    manager = FakePlaywrightManager([page])
    client = AltCrmClient(AltCrmConfig(), playwright_factory=lambda: manager)
    assert client.read_order().device_id == "4321"
    client.close()
    client.close()
    assert client.is_closed
    assert manager.stopped == 1
    assert page.closed is False
    with pytest.raises(AltCrmError):
        client.read_order()


# -- filling -----------------------------------------------------------------
def test_fill_order_fills_fields_and_never_saves() -> None:
    page = make_page()
    client = AltCrmClient(AltCrmConfig(), page=page)
    results = client.fill_order(
        "Клиент ждёт звонка",
        {"Согласованные сроки ремонта": "до пятницы"},
    )
    assert all(result.ok for result in results)
    assert page.find_element(placeholder="Согласованные сроки ремонта").value == (
        "до пятницы"
    )
    assert page.find_element(placeholder="Напишите сообщение...").value == (
        "Клиент ждёт звонка"
    )
    assert any(result.note == "не отправлено" for result in results)
    assert page.clicks() == ["Редактировать"]
    assert not set(page.clicks()) & set(DANGEROUS_BUTTONS)
    assert not any("Создать" in click for click in page.clicks())


def test_fill_order_reports_missing_field() -> None:
    page = make_page(with_fields=False)
    client = AltCrmClient(AltCrmConfig(), page=page)
    results = client.fill_order("", {"Согласованные риски": "нет"})
    assert len(results) == 1
    assert results[0].ok is False
    assert "не найдено" in results[0].error


def test_fill_order_refuses_unknown_checklist_label() -> None:
    page = make_page()
    client = AltCrmClient(AltCrmConfig(), page=page)
    results = client.fill_order("", {"Неизвестное поле": "x"})
    assert results[0].ok is False
    assert "unknown" in results[0].error
    assert page.fills() == []


def test_fill_order_refuses_edit_button_that_saves() -> None:
    page = make_page()
    config = AltCrmConfig(selectors={"order": {"edit_button": "Сохранить"}})
    client = AltCrmClient(config, page=page)
    results = client.fill_order("", {"Согласованные риски": "нет"})
    assert any(result.field == "Сохранить" and not result.ok for result in results)
    assert "Сохранить" not in page.clicks()


@pytest.mark.parametrize(
    "button",
    ["Сохранить", "Отправить", "Удалить", "Оплатить", "Скидка", "Создать задачу"],
)
def test_click_safe_refuses_dangerous_text(button: str) -> None:
    page = make_page()
    client = AltCrmClient(AltCrmConfig(), page=page)
    with pytest.raises(AltCrmError):
        client._click_safe(button, page=page)
    assert page.clicks() == []


def test_fill_timeout_is_reported_as_field_error() -> None:
    page = make_page()
    page.find_element(placeholder="Согласованные риски").fill_error = TimeoutError(
        "fill timed out"
    )
    client = AltCrmClient(AltCrmConfig(), page=page)
    results = client.fill_order("", {"Согласованные риски": "нет"})
    assert results[0].ok is False
    assert "timed out" in results[0].error


# -- act rows ----------------------------------------------------------------
def test_add_work_rows_fills_row_and_never_saves() -> None:
    page = make_page()
    client = AltCrmClient(AltCrmConfig(), page=page)
    results = client.add_work_rows(
        [{"name": "Замена экрана", "price": 5000, "warranty": 90}]
    )
    assert all(result.ok for result in results)
    rows = [
        node
        for node in page.root.walk()
        if "table-row" in node.classes
    ]
    assert len(rows) == 1
    assert rows[0].children[0].value == "Замена экрана"
    assert rows[0].children[1].value == "90"
    assert rows[0].children[2].value == "5000"
    assert page.clicks() == ["Редактировать", "+"]
    assert not set(page.clicks()) & set(DANGEROUS_BUTTONS)


def test_add_work_rows_skips_existing_work() -> None:
    page = make_page()
    assert page.act_block is not None
    add_act_row(page.act_block, name="Диагностика")
    client = AltCrmClient(AltCrmConfig(), page=page)
    results = client.add_work_rows(
        [{"name": "Диагностика", "price": 0, "warranty": 0}]
    )
    assert any("пропущена" in result.note for result in results)
    assert "+" not in page.clicks()


def test_add_work_rows_reports_missing_block() -> None:
    page = make_page(with_act=False)
    client = AltCrmClient(AltCrmConfig(), page=page)
    results = client.add_work_rows([{"name": "Замена", "price": 1, "warranty": 1}])
    assert any(
        result.field.startswith("блок") and not result.ok for result in results
    )


def test_add_work_rows_rejects_empty_or_oversized_batches() -> None:
    client = AltCrmClient(AltCrmConfig(), page=make_page())
    with pytest.raises(AltCrmError):
        client.add_work_rows([])
    too_many = [{"name": f"work-{index}"} for index in range(21)]
    with pytest.raises(AltCrmError):
        client.add_work_rows(too_many)


# -- task form ---------------------------------------------------------------
def test_create_task_fills_form_but_does_not_create() -> None:
    page = make_page()
    client = AltCrmClient(AltCrmConfig(), page=page)
    results = client.create_task("Позвонить клиенту", "завтра")
    assert any(result.field == "окно задачи" and result.ok for result in results)
    assert any(result.field.startswith("срок") and result.ok for result in results)
    assert any(result.field == "комментарий задачи" and result.ok for result in results)
    manual = [result for result in results if result.field == "создание задачи"]
    assert manual and "Создать задачу" in manual[0].note
    assert page.task_dialog is not None
    dialog_nodes = page.task_dialog.walk()
    due = [node for node in dialog_nodes if node.placeholder == "Срок выполнения"]
    boxes = [node for node in dialog_nodes if node.tag == "textarea"]
    assert due and due[0].value
    assert boxes and boxes[0].value == "Позвонить клиенту"
    assert "Создать задачу" not in page.clicks()
    assert page.clicks() == ["Добавить задачу"]


def test_create_task_reports_unparsed_due_date() -> None:
    page = make_page()
    client = AltCrmClient(AltCrmConfig(), page=page)
    results = client.create_task("Перезвонить", "когда-нибудь")
    due = [result for result in results if result.field == "срок"]
    assert due and due[0].ok is False
    assert "не понял срок" in due[0].error
    assert "Создать задачу" not in page.clicks()


def test_create_task_reports_missing_dialog() -> None:
    page = make_page()
    dialog = page.task_dialog
    assert dialog is not None
    page.root.children = [child for child in page.root.children if child is not dialog]
    client = AltCrmClient(AltCrmConfig(), page=page)
    results = client.create_task("Перезвонить", "")
    assert results[0].field == "окно задачи"
    assert results[0].ok is False
    assert "Создать задачу" not in page.clicks()


def test_parse_when_supports_explicit_and_relative_dates() -> None:
    now = datetime(2026, 1, 1, 12, 30)
    assert parse_when("завтра", now) == datetime(2026, 1, 2, 10, 0)
    assert parse_when("через 2 дня", now) == datetime(2026, 1, 3, 10, 0)
    assert parse_when("01.02.2026") == datetime(2026, 2, 1)
    assert parse_when("01.02.2026 15:30") == datetime(2026, 2, 1, 15, 30)
    assert parse_when("как-нибудь потом") is None
