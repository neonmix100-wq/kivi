"""Global kill-switch hotkey.

The listener registers one system hotkey and calls a callback for each press.
Parsing and the message loop are separated from the Windows API, so tests can
drive a fake backend. The default backend uses ``RegisterHotKey`` with
``MOD_NOREPEAT`` and a ``GetMessageW`` loop; the CLI wraps it around the kill
switch.

The callback must never widen permissions: it can only engage the stop flag.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

_MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
}

_KEYS = {
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "pause": 0x13,
}


class HotkeyError(ValueError):
    """Raised for an invalid hotkey string."""


class HotkeyUnavailable(RuntimeError):
    """Raised when the OS refuses to register the hotkey."""


@dataclass(frozen=True, slots=True)
class HotkeySpec:
    text: str
    modifiers: int
    virtual_key: int


def _key_code(token: str) -> int:
    lowered = token.lower()
    if lowered in _KEYS:
        return _KEYS[lowered]
    if len(lowered) == 1 and (lowered.isalpha() or lowered.isdigit()):
        return ord(lowered.upper())
    if lowered.startswith("f") and lowered[1:].isdigit():
        number = int(lowered[1:])
        if 1 <= number <= 24:
            return 0x70 + number - 1
    raise HotkeyError(f"unknown hotkey key: {token!r}")


def parse_hotkey(text: str) -> HotkeySpec:
    """Parse ``Ctrl+Alt+J`` into modifiers and a virtual-key code."""
    parts = [part.strip() for part in text.split("+") if part.strip()]
    if len(parts) < 2:
        raise HotkeyError("a hotkey needs at least one modifier and one key")
    modifiers = 0
    names: list[str] = []
    key = ""
    for part in parts:
        lowered = part.lower()
        if lowered in _MODIFIERS:
            modifier = _MODIFIERS[lowered]
            if modifiers & modifier:
                raise HotkeyError(f"duplicate modifier: {part}")
            modifiers |= modifier
            names.append("Ctrl" if modifier == MOD_CONTROL else part)
            continue
        if key:
            raise HotkeyError("a hotkey must have exactly one key")
        key = part
    if not modifiers:
        raise HotkeyError("a hotkey needs at least one modifier")
    if not key:
        raise HotkeyError("a hotkey needs a key")
    return HotkeySpec(
        text="+".join([*names, key.upper()]),
        modifiers=modifiers | MOD_NOREPEAT,
        virtual_key=_key_code(key),
    )


class HotkeyBackend(Protocol):
    def register(self, modifiers: int, virtual_key: int) -> None: ...

    def wait(self) -> bool: ...

    def unregister(self) -> None: ...


class WindowsHotkey:
    """``RegisterHotKey`` + ``GetMessageW`` for the current thread."""

    def __init__(self, hotkey_id: int = 0x4B49) -> None:
        self.hotkey_id = hotkey_id
        self._registered = False

    def _api(self) -> Any:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.RegisterHotKey.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.GetMessageW.restype = ctypes.c_int
        return ctypes, wintypes, user32

    def register(self, modifiers: int, virtual_key: int) -> None:
        _ctypes, _wintypes, user32 = self._api()
        if not user32.RegisterHotKey(None, self.hotkey_id, modifiers, virtual_key):
            raise HotkeyUnavailable("the OS refused to register the hotkey")
        self._registered = True

    def wait(self) -> bool:
        if not self._registered:
            raise HotkeyUnavailable("hotkey is not registered")
        ctypes, wintypes, user32 = self._api()
        message = wintypes.MSG()
        result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
        if result <= 0:
            return False
        return message.message == WM_HOTKEY and message.wParam == self.hotkey_id

    def unregister(self) -> None:
        if not self._registered:
            return
        _ctypes, _wintypes, user32 = self._api()
        user32.UnregisterHotKey(None, self.hotkey_id)
        self._registered = False


class HotkeyListener:
    def __init__(
        self,
        spec: HotkeySpec,
        backend: HotkeyBackend,
        on_trigger: Callable[[], None],
    ) -> None:
        self.spec = spec
        self.backend = backend
        self.on_trigger = on_trigger

    def run(self, max_events: int | None = None) -> int:
        """Block on hotkey messages; return the number of handled presses."""
        if max_events is not None and max_events < 1:
            raise ValueError("max_events must be positive")
        self.backend.register(self.spec.modifiers, self.spec.virtual_key)
        handled = 0
        try:
            while max_events is None or handled < max_events:
                if not self.backend.wait():
                    break
                self.on_trigger()
                handled += 1
        finally:
            self.backend.unregister()
        return handled


__all__ = [
    "MOD_ALT",
    "MOD_CONTROL",
    "MOD_NOREPEAT",
    "MOD_SHIFT",
    "MOD_WIN",
    "HotkeyBackend",
    "HotkeyError",
    "HotkeyListener",
    "HotkeySpec",
    "HotkeyUnavailable",
    "WM_HOTKEY",
    "WindowsHotkey",
    "parse_hotkey",
]
