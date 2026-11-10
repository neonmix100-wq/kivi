from __future__ import annotations

from pathlib import Path

import pytest

from kivi.interfaces import cli as cli_module
from kivi.interfaces.cli import main
from kivi.paths import Paths
from kivi.safety.hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    HotkeyError,
    HotkeyListener,
    HotkeyUnavailable,
    parse_hotkey,
)


class FakeBackend:
    def __init__(self, events: list[bool]) -> None:
        self.events = list(events)
        self.registered: tuple[int, int] | None = None
        self.unregistered = False
        self.calls = 0

    def register(self, modifiers: int, virtual_key: int) -> None:
        self.registered = (modifiers, virtual_key)

    def wait(self) -> bool:
        self.calls += 1
        return self.events.pop(0) if self.events else False

    def unregister(self) -> None:
        self.unregistered = True


def test_parse_ctrl_alt_j() -> None:
    spec = parse_hotkey("Ctrl+Alt+J")
    assert spec.modifiers == MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
    assert spec.virtual_key == ord("J")
    assert spec.text == "Ctrl+Alt+J"


def test_parse_is_case_insensitive_and_supports_fkeys() -> None:
    spec = parse_hotkey("shift+f12")
    assert spec.virtual_key == 0x7B
    assert spec.modifiers & 0x0004


def test_parse_digits_and_named_keys() -> None:
    assert parse_hotkey("Ctrl+1").virtual_key == ord("1")
    assert parse_hotkey("Alt+Space").virtual_key == 0x20


@pytest.mark.parametrize(
    "value",
    ["J", "Ctrl", "Ctrl+Alt", "Ctrl+J+K", "Ctrl+NotAKey", "Ctrl+Ctrl+J"],
)
def test_invalid_hotkeys_are_rejected(value: str) -> None:
    with pytest.raises(HotkeyError):
        parse_hotkey(value)


def test_listener_triggers_and_unregisters() -> None:
    backend = FakeBackend([True, True, True])
    calls: list[int] = []
    listener = HotkeyListener(parse_hotkey("Ctrl+Alt+J"), backend, lambda: calls.append(1))
    handled = listener.run(max_events=2)
    assert handled == 2
    assert calls == [1, 1]
    assert backend.registered is not None
    assert backend.unregistered is True


def test_listener_stops_when_the_backend_ends() -> None:
    backend = FakeBackend([True])
    calls: list[int] = []
    handled = HotkeyListener(
        parse_hotkey("Ctrl+Alt+J"), backend, lambda: calls.append(1)
    ).run()
    assert handled == 1
    assert backend.unregistered is True


def test_listener_unregisters_on_callback_error() -> None:
    backend = FakeBackend([True])

    def boom() -> None:
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        HotkeyListener(parse_hotkey("Ctrl+Alt+J"), backend, boom).run(max_events=1)
    assert backend.unregistered is True


def test_listener_requires_positive_max_events() -> None:
    backend = FakeBackend([])
    with pytest.raises(ValueError, match="max_events"):
        HotkeyListener(parse_hotkey("Ctrl+Alt+J"), backend, lambda: None).run(max_events=0)


def test_register_failure_propagates() -> None:
    class Broken(FakeBackend):
        def register(self, modifiers: int, virtual_key: int) -> None:
            raise HotkeyUnavailable("denied")

    backend = Broken([])
    with pytest.raises(HotkeyUnavailable):
        HotkeyListener(parse_hotkey("Ctrl+Alt+J"), backend, lambda: None).run(max_events=1)
    assert backend.unregistered is False


def make_cli_profile(tmp_path: Path) -> Paths:
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "config" / "kivi.yaml").write_text("version: 1\n", encoding="utf-8")
    return Paths(root=root, home=tmp_path / "profile")


def test_cli_hotkey_check(tmp_path: Path, monkeypatch) -> None:
    paths = make_cli_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)
    assert main(["hotkey", "--check"]) == 0
    assert main(["hotkey", "--check", "--hotkey", "Ctrl+Shift+F5"]) == 0


def test_cli_hotkey_rejects_invalid_spec(tmp_path: Path, monkeypatch) -> None:
    paths = make_cli_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)
    assert main(["hotkey", "--check", "--hotkey", "J"]) == 1
