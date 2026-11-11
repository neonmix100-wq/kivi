from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from kivi.kernel.db import Database
from kivi.safety.permissions import Grant, PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools import ToolExecutor, ToolRegistry
from kivi.tools.ui import (
    FAILSAFE,
    PAUSE_S,
    PyAutoGuiBackend,
    PywinautoBackend,
    ScreenRect,
    UiController,
    UiElement,
    UiUnavailable,
    build_ui_tools,
    virtual_screen_rect,
)


class FakeBackend:
    """Complete in-memory backend; no real mouse, keyboard or window is touched."""

    def __init__(
        self,
        *,
        size: tuple[int, int] = (1920, 1080),
        elements: list[UiElement] | None = None,
    ) -> None:
        self.clicks: list[tuple[int, int, str, bool]] = []
        self.typed: list[tuple[str, float]] = []
        self.hotkeys: list[tuple[str, ...]] = []
        self.finds: list[tuple[str, str | None]] = []
        self.calls: list[str] = []
        self._size = size
        self._elements = list(elements or [])

    def click(
        self, x: int, y: int, button: str = "left", double: bool = False
    ) -> None:
        self.calls.append("click")
        self.clicks.append((x, y, button, double))

    def type_text(self, text: str, interval_s: float = 0.0) -> None:
        self.calls.append("type_text")
        self.typed.append((text, interval_s))

    def hotkey(self, keys: tuple[str, ...]) -> None:
        self.calls.append("hotkey")
        self.hotkeys.append(tuple(keys))

    def position(self) -> tuple[int, int]:
        return (0, 0)

    def screen_size(self) -> tuple[int, int]:
        return self._size

    def find(self, text: str, window_title: str | None = None) -> list[UiElement]:
        self.calls.append("find")
        self.finds.append((text, window_title))
        return self._elements


@dataclass
class Harness:
    executor: ToolExecutor
    db: Database
    backend: FakeBackend
    grant: Grant


@contextmanager
def ui_harness(
    tmp_path: Path,
    *,
    elements: list[UiElement] | None = None,
    screen_rect: ScreenRect | None = None,
    capabilities: tuple[str, ...] = ("ui.control", "ui.read"),
) -> Iterator[Harness]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = Database(tmp_path / "kivi.db")
    try:
        engine = PermissionEngine(db, workspace=str(workspace))
        grant = engine.issue("ui-task", capabilities)
        backend = FakeBackend(elements=elements)
        rect = screen_rect or ScreenRect(0, 0, 1920, 1080)
        controller = UiController(backend, screen_rect=rect)
        registry = ToolRegistry()
        for tool in build_ui_tools(controller):
            registry.register(tool)
        executor = ToolExecutor(
            registry,
            db,
            engine,
            ProtectedZones(workspace),
            workspace=workspace,
        )
        yield Harness(executor, db, backend, grant)
    finally:
        db.close()


def test_tool_contracts() -> None:
    tools = {tool.name: tool for tool in build_ui_tools()}
    assert set(tools) == {"ui.click", "ui.type", "ui.hotkey", "ui.find"}
    assert tools["ui.click"].risk == "R2"
    assert tools["ui.type"].risk == "R2"
    assert tools["ui.hotkey"].risk == "R2"
    assert tools["ui.find"].risk == "R1"
    assert tools["ui.click"].capabilities == ("ui.control",)
    assert tools["ui.type"].capabilities == ("ui.control",)
    assert tools["ui.hotkey"].capabilities == ("ui.control",)
    assert tools["ui.find"].capabilities == ("ui.read",)
    for tool in tools.values():
        assert tool.args_model.model_config["extra"] == "forbid"


def test_click_requires_confirmation(tmp_path: Path) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run("ui.click", {"x": 10, "y": 20}, grant=h.grant)
    assert not result.ok
    assert "confirmation required" in result.error
    assert h.backend.clicks == []


def test_click_dry_run_shows_coordinates_without_clicking(tmp_path: Path) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run(
            "ui.click",
            {"x": 10, "y": 20, "button": "right", "double": True},
            grant=h.grant,
            dry_run=True,
        )
    assert result.ok
    assert h.backend.clicks == []
    preview = json.loads(result.output)
    assert preview["action"] == "ui-click"
    assert preview["details"]["x"] == 10
    assert preview["details"]["y"] == 20
    assert preview["details"]["button"] == "right"
    assert preview["details"]["double"] is True
    assert preview["details"]["screen"]["width"] == 1920


def test_click_runs_when_approved(tmp_path: Path) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run(
            "ui.click", {"x": 10, "y": 20}, grant=h.grant, approved=True
        )
    assert result.ok
    assert h.backend.clicks == [(10, 20, "left", False)]
    assert result.meta == {"x": 10, "y": 20, "button": "left", "double": False}


def test_click_supports_right_and_double(tmp_path: Path) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run(
            "ui.click",
            {"x": 10, "y": 20, "button": "right", "double": True},
            grant=h.grant,
            approved=True,
        )
    assert result.ok
    assert h.backend.clicks == [(10, 20, "right", True)]


def test_click_rejects_out_of_bounds(tmp_path: Path) -> None:
    with ui_harness(tmp_path) as h:
        for x, y in ((1920, 0), (0, 1080), (-1, 0), (0, -1)):
            result = h.executor.run(
                "ui.click", {"x": x, "y": y}, grant=h.grant, approved=True
            )
            assert not result.ok
            assert "outside the virtual screen" in result.error
    assert h.backend.clicks == []


def test_click_accepts_negative_virtual_origin(tmp_path: Path) -> None:
    rect = ScreenRect(-1920, 0, 3840, 1080)
    with ui_harness(tmp_path, screen_rect=rect) as h:
        inside = h.executor.run(
            "ui.click", {"x": -100, "y": 500}, grant=h.grant, approved=True
        )
        outside = h.executor.run(
            "ui.click", {"x": 3000, "y": 500}, grant=h.grant, approved=True
        )
    assert inside.ok
    assert not outside.ok
    assert h.backend.clicks == [(-100, 500, "left", False)]


def test_click_rejects_unknown_button(tmp_path: Path) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run(
            "ui.click",
            {"x": 1, "y": 2, "button": "side"},
            grant=h.grant,
            approved=True,
        )
    assert not result.ok
    assert "invalid arguments" in result.error
    assert h.backend.clicks == []


def test_type_requires_confirmation(tmp_path: Path) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run(
            "ui.type", {"text": "hello"}, grant=h.grant
        )
    assert not result.ok
    assert h.backend.typed == []


def test_type_runs_and_does_not_echo_text(tmp_path: Path) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run(
            "ui.type",
            {"text": "hello world", "interval_s": 0.01},
            grant=h.grant,
            approved=True,
        )
        audit = json.dumps(h.db.recent_audit(), ensure_ascii=False)
    assert result.ok
    assert h.backend.typed == [("hello world", 0.01)]
    assert result.output == "typed 11 characters"
    assert "hello world" not in result.output
    assert "hello world" not in audit
    assert result.meta["chars"] == 11
    assert result.meta["redact_secrets"] == 0


def test_type_rejects_secret_text(tmp_path: Path) -> None:
    secret = "token sk-canary12345678901234567890"
    with ui_harness(tmp_path) as h:
        result = h.executor.run(
            "ui.type", {"text": secret}, grant=h.grant, approved=True
        )
        dry = h.executor.run(
            "ui.type", {"text": secret}, grant=h.grant, dry_run=True
        )
    assert not result.ok
    assert "looks like a secret" in result.error
    assert not dry.ok
    assert h.backend.typed == []


@pytest.mark.parametrize(
    "payload",
    [
        {"text": ""},
        {"text": "x" * 2001},
        {"text": "x", "interval_s": 1.5},
        {"text": "x", "interval_s": -0.1},
    ],
)
def test_type_rejects_bad_arguments(tmp_path: Path, payload: dict[str, object]) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run("ui.type", payload, grant=h.grant, approved=True)
    assert not result.ok
    assert "invalid arguments" in result.error
    assert h.backend.typed == []


def test_type_dry_run_does_not_type(tmp_path: Path) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run(
            "ui.type", {"text": "hello world"}, grant=h.grant, dry_run=True
        )
    assert result.ok
    assert h.backend.typed == []
    assert "hello world" not in result.output
    assert json.loads(result.output)["details"]["chars"] == 11


def test_hotkey_requires_confirmation(tmp_path: Path) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run("ui.hotkey", {"keys": ["ctrl", "s"]}, grant=h.grant)
    assert not result.ok
    assert h.backend.hotkeys == []


def test_hotkey_runs_with_normalized_keys(tmp_path: Path) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run(
            "ui.hotkey", {"keys": ["CTRL", "S"]}, grant=h.grant, approved=True
        )
    assert result.ok
    assert h.backend.hotkeys == [("ctrl", "s")]
    assert result.output == "pressed ctrl+s"


def test_hotkey_allows_win_l(tmp_path: Path) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run(
            "ui.hotkey", {"keys": ["win", "l"]}, grant=h.grant, approved=True
        )
    assert result.ok
    assert h.backend.hotkeys == [("win", "l")]


def test_hotkey_rejects_non_whitelisted_keys(tmp_path: Path) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run(
            "ui.hotkey", {"keys": ["ctrl", "rm"]}, grant=h.grant, approved=True
        )
    assert not result.ok
    assert "unsupported hotkey keys" in result.error
    assert h.backend.hotkeys == []


@pytest.mark.parametrize(
    "keys",
    [
        ["ctrl"],
        ["ctrl", "alt", "shift", "tab", "enter"],
    ],
)
def test_hotkey_rejects_wrong_key_count(tmp_path: Path, keys: list[str]) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run(
            "ui.hotkey", {"keys": keys}, grant=h.grant, approved=True
        )
    assert not result.ok
    assert "invalid arguments" in result.error
    assert h.backend.hotkeys == []


def test_hotkey_rejects_ctrl_alt_delete(tmp_path: Path) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run(
            "ui.hotkey",
            {"keys": ["CTRL", "ALT", "DELETE"]},
            grant=h.grant,
            approved=True,
        )
    assert not result.ok
    assert "ctrl+alt+delete" in result.error
    assert h.backend.hotkeys == []


def test_hotkey_dry_run_does_not_press(tmp_path: Path) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run(
            "ui.hotkey", {"keys": ["ctrl", "s"]}, grant=h.grant, dry_run=True
        )
    assert result.ok
    assert h.backend.hotkeys == []
    assert json.loads(result.output)["details"]["keys"] == ["ctrl", "s"]


def test_find_returns_sanitized_elements(tmp_path: Path) -> None:
    elements = [
        UiElement(
            "Editor - sk-canary12345678901234567890",
            "Edit",
            (10, 20, 100, 40),
        )
    ]
    with ui_harness(tmp_path, elements=elements) as h:
        result = h.executor.run("ui.find", {"text": "Save"}, grant=h.grant)
    assert result.ok
    assert h.backend.finds == [("Save", None)]
    payload = json.loads(result.output)
    assert len(payload) == 1
    assert "sk-canary" not in payload[0]["title"]
    assert payload[0]["control_type"] == "Edit"
    assert payload[0]["rect"] == [10, 20, 100, 40]
    assert result.trust == "untrusted:ui"


def test_find_passes_window_title_filter(tmp_path: Path) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run(
            "ui.find",
            {"text": "Save", "window_title": "Notepad"},
            grant=h.grant,
        )
    assert result.ok
    assert h.backend.finds == [("Save", "Notepad")]


def test_find_limits_results_and_marks_truncation(tmp_path: Path) -> None:
    elements = [
        UiElement(f"Window {index}", "Text", (0, index, 10, index + 1))
        for index in range(25)
    ]
    with ui_harness(tmp_path, elements=elements) as h:
        limited = h.executor.run(
            "ui.find", {"text": "Window", "max_results": 5}, grant=h.grant
        )
        default = h.executor.run("ui.find", {"text": "Window"}, grant=h.grant)
    assert limited.ok
    assert len(json.loads(limited.output)) == 5
    assert limited.truncated is True
    assert limited.meta == {"count": 5, "max_results": 5}
    assert len(json.loads(default.output)) == 20
    assert default.truncated is True


def test_find_dry_run_does_not_query(tmp_path: Path) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run(
            "ui.find", {"text": "Save"}, grant=h.grant, dry_run=True
        )
    assert result.ok
    assert h.backend.finds == []


def test_find_rejects_blank_text(tmp_path: Path) -> None:
    with ui_harness(tmp_path) as h:
        result = h.executor.run("ui.find", {"text": "   "}, grant=h.grant)
    assert not result.ok
    assert "invalid arguments" in result.error
    assert h.backend.finds == []


def test_controller_uses_a_separate_finder() -> None:
    input_backend = FakeBackend()
    finder = FakeBackend(elements=[UiElement("Notepad", "Edit", (1, 2, 3, 4))])
    controller = UiController(
        input_backend,
        finder=finder,
        screen_rect=ScreenRect(0, 0, 100, 100),
    )
    controller.click(5, 6)
    found = controller.find("Save")
    assert input_backend.clicks == [(5, 6, "left", False)]
    assert input_backend.finds == []
    assert finder.finds == [("Save", None)]
    assert found == [UiElement("Notepad", "Edit", (1, 2, 3, 4))]
    assert controller.screen_rect() == ScreenRect(0, 0, 100, 100)


def test_pyautogui_backend_cannot_find() -> None:
    with pytest.raises(UiUnavailable, match="cannot find"):
        PyAutoGuiBackend().find("Save")


def test_pywinauto_backend_is_find_only() -> None:
    backend = PywinautoBackend()
    with pytest.raises(UiUnavailable, match="find-only"):
        backend.click(1, 2)


def test_failsafe_is_enabled() -> None:
    assert FAILSAFE is True
    assert PAUSE_S == 0.05


def test_pyautogui_backend_configures_failsafe(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SimpleNamespace(FAILSAFE=False, PAUSE=0.0)
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    backend = PyAutoGuiBackend()
    assert backend._gui() is fake
    assert fake.FAILSAFE is True
    assert fake.PAUSE == PAUSE_S


def test_virtual_screen_rect_refuses_non_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    with pytest.raises(UiUnavailable, match="Windows-only"):
        virtual_screen_rect()
