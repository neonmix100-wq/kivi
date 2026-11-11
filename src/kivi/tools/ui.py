"""Operator UI automation: mouse, keyboard and window-text lookup.

The module has two native backends behind one :class:`UiBackend` protocol:

* :class:`PyAutoGuiBackend` clicks and types through pyautogui, with the
  fail-safe corner enabled and a small pause between calls;
* :class:`PywinautoBackend` only looks up visible text through the UIA tree.

Both libraries are imported lazily, and :class:`UiController` composes them so
unit tests can inject a fake backend and never move the real mouse or type into
a real window. Every action still goes through ``ToolExecutor`` for permission,
confirmation and audit; the raw text of ``ui.type`` never reaches the audit
trail or a result.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kivi.safety.sanitize import redact_secrets, sanitize_text
from kivi.tools.base import Preview, ToolContext, ToolError, ToolResult

Button = Literal["left", "right", "middle"]

FAILSAFE = True
PAUSE_S = 0.05
MAX_TYPE_CHARS = 2000
MAX_FIND_RESULTS = 20
MAX_SCAN_ELEMENTS = 2000

# pyautogui key names accepted by ui.hotkey; no free-form strings.
_HOTKEY_NAMES = (
    "ctrl",
    "alt",
    "shift",
    "win",
    "tab",
    "esc",
    "enter",
    "space",
    "backspace",
    "delete",
    "up",
    "down",
    "left",
    "right",
    "home",
    "end",
    "pageup",
    "pagedown",
)
HOTKEY_KEYS: frozenset[str] = (
    frozenset(_HOTKEY_NAMES)
    | frozenset("abcdefghijklmnopqrstuvwxyz")
    | frozenset("0123456789")
    | frozenset(f"f{number}" for number in range(1, 13))
)
# Windows reserves this sequence for the secure attention; inject it never.
FORBIDDEN_HOTKEYS: frozenset[frozenset[str]] = frozenset(
    {frozenset({"ctrl", "alt", "delete"})}
)

# Virtual-screen metrics from wingdi/user32 (the same values used by screen.py).
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79


class UiUnavailable(ToolError):
    """Raised when a UI backend or the screen geometry cannot be reached."""


@dataclass(frozen=True, slots=True)
class ScreenRect:
    """A virtual-desktop rectangle in physical screen coordinates."""

    left: int
    top: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("screen width and height must be positive")

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def contains(self, x: int, y: int) -> bool:
        """Return whether a point is inside the half-open rectangle."""
        return self.left <= x < self.right and self.top <= y < self.bottom

    def as_dict(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
            "right": self.right,
            "bottom": self.bottom,
        }


def virtual_screen_rect() -> ScreenRect:
    """Return the full virtual desktop, including monitors left of the primary."""
    if sys.platform != "win32":
        raise UiUnavailable("virtual screen geometry is Windows-only")
    import ctypes

    user32 = ctypes.windll.user32
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    left = int(user32.GetSystemMetrics(_SM_XVIRTUALSCREEN))
    top = int(user32.GetSystemMetrics(_SM_YVIRTUALSCREEN))
    width = int(user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN))
    height = int(user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
    if width <= 0 or height <= 0:
        raise UiUnavailable("the virtual screen has no size")
    return ScreenRect(left, top, width, height)


@dataclass(frozen=True, slots=True)
class UiElement:
    """One control found by ``ui.find``; window text is never an instruction."""

    title: str
    control_type: str
    rect: tuple[int, int, int, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "control_type": self.control_type,
            "rect": list(self.rect),
        }


class UiBackend(Protocol):
    """Operations required by the UI tools; native backends may split them."""

    def click(
        self, x: int, y: int, button: Button = "left", double: bool = False
    ) -> None: ...

    def type_text(self, text: str, interval_s: float = 0.0) -> None: ...

    def hotkey(self, keys: Sequence[str]) -> None: ...

    def position(self) -> tuple[int, int]: ...

    def screen_size(self) -> tuple[int, int]: ...

    def find(self, text: str, window_title: str | None = None) -> list[UiElement]: ...


class PyAutoGuiBackend:
    """pyautogui input backend; the import happens on the first action."""

    def __init__(self) -> None:
        self._module: Any = None

    def _gui(self) -> Any:
        if self._module is None:
            try:
                import pyautogui
            except Exception as exc:
                raise UiUnavailable(f"pyautogui is unavailable: {exc}") from exc
            pyautogui.FAILSAFE = FAILSAFE
            pyautogui.PAUSE = PAUSE_S
            self._module = pyautogui
        return self._module

    def click(
        self, x: int, y: int, button: Button = "left", double: bool = False
    ) -> None:
        gui = self._gui()
        try:
            gui.click(
                x=x,
                y=y,
                button=button,
                clicks=2 if double else 1,
                interval=0.1 if double else 0.0,
            )
        except Exception as exc:
            raise UiUnavailable(f"click failed: {exc}") from exc

    def type_text(self, text: str, interval_s: float = 0.0) -> None:
        gui = self._gui()
        try:
            gui.write(text, interval=max(0.0, float(interval_s)))
        except Exception as exc:
            raise UiUnavailable(f"typing failed: {exc}") from exc

    def hotkey(self, keys: Sequence[str]) -> None:
        gui = self._gui()
        try:
            gui.hotkey(*keys)
        except Exception as exc:
            raise UiUnavailable(f"hotkey failed: {exc}") from exc

    def position(self) -> tuple[int, int]:
        try:
            point = self._gui().position()
            return int(point.x), int(point.y)
        except Exception as exc:
            raise UiUnavailable(f"cannot read the pointer position: {exc}") from exc

    def screen_size(self) -> tuple[int, int]:
        try:
            size = self._gui().size()
            return int(size.width), int(size.height)
        except Exception as exc:
            raise UiUnavailable(f"cannot read the screen size: {exc}") from exc

    def find(self, text: str, window_title: str | None = None) -> list[UiElement]:
        raise UiUnavailable(
            "PyAutoGuiBackend cannot find window text; use PywinautoBackend"
        )


class PywinautoBackend:
    """UIA text lookup; pywinauto is imported only inside :meth:`find`."""

    def __init__(self, *, max_scan_elements: int = MAX_SCAN_ELEMENTS) -> None:
        if max_scan_elements < 1:
            raise ValueError("max_scan_elements must be positive")
        self._max_scan_elements = max_scan_elements

    @staticmethod
    def _find_only(operation: str) -> UiUnavailable:
        return UiUnavailable(
            f"PywinautoBackend is find-only; {operation} needs PyAutoGuiBackend"
        )

    def click(
        self, x: int, y: int, button: Button = "left", double: bool = False
    ) -> None:
        raise self._find_only("click")

    def type_text(self, text: str, interval_s: float = 0.0) -> None:
        raise self._find_only("type_text")

    def hotkey(self, keys: Sequence[str]) -> None:
        raise self._find_only("hotkey")

    def position(self) -> tuple[int, int]:
        raise self._find_only("position")

    def screen_size(self) -> tuple[int, int]:
        raise self._find_only("screen_size")

    @staticmethod
    def _text(getter: Any) -> str:
        try:
            return str(getter() or "")
        except Exception:
            return ""

    @staticmethod
    def _visible(window: Any) -> bool:
        try:
            return bool(window.is_visible())
        except Exception:
            # A wrapper that cannot answer is still worth scanning.
            return True

    @staticmethod
    def _control_type(control: Any) -> str:
        try:
            return str(control.element_info.control_type or "")
        except Exception:
            try:
                return str(control.friendly_class_name() or "")
            except Exception:
                return ""

    @staticmethod
    def _rect(control: Any) -> tuple[int, int, int, int]:
        try:
            box = control.rectangle()
            return (int(box.left), int(box.top), int(box.right), int(box.bottom))
        except Exception:
            return (0, 0, 0, 0)

    def find(self, text: str, window_title: str | None = None) -> list[UiElement]:
        wanted = text.strip().casefold()
        if not wanted:
            raise UiUnavailable("find text must not be empty")
        try:
            from pywinauto import Desktop
        except Exception as exc:
            raise UiUnavailable(f"pywinauto is unavailable: {exc}") from exc
        try:
            windows = Desktop(backend="uia").windows()
        except Exception as exc:
            raise UiUnavailable(f"cannot enumerate windows: {exc}") from exc

        title_filter = window_title.strip().casefold() if window_title else ""
        found: list[UiElement] = []
        scanned = 0
        for window in windows:
            if len(found) >= MAX_FIND_RESULTS or scanned >= self._max_scan_elements:
                break
            if not self._visible(window):
                continue
            title = self._text(window.window_text)
            if title_filter and title_filter not in title.casefold():
                continue
            try:
                controls = window.descendants()
            except Exception:
                continue
            for control in controls:
                if len(found) >= MAX_FIND_RESULTS or scanned >= self._max_scan_elements:
                    break
                scanned += 1
                label = self._text(control.window_text)
                if wanted not in label.casefold():
                    continue
                found.append(
                    UiElement(
                        title=title,
                        control_type=self._control_type(control),
                        rect=self._rect(control),
                    )
                )
        return found


class UiController:
    """Composite UI backend: pyautogui for input, pywinauto for find."""

    def __init__(
        self,
        backend: UiBackend | None = None,
        *,
        finder: UiBackend | None = None,
        screen_rect: ScreenRect | None = None,
    ) -> None:
        self._input: UiBackend = backend or PyAutoGuiBackend()
        self._finder: UiBackend = finder or backend or PywinautoBackend()
        self._screen_rect = screen_rect
        self._injected = backend is not None

    def click(
        self, x: int, y: int, button: Button = "left", double: bool = False
    ) -> None:
        self._input.click(x, y, button=button, double=double)

    def type_text(self, text: str, interval_s: float = 0.0) -> None:
        self._input.type_text(text, interval_s)

    def hotkey(self, keys: Sequence[str]) -> None:
        self._input.hotkey(keys)

    def position(self) -> tuple[int, int]:
        return self._input.position()

    def screen_size(self) -> tuple[int, int]:
        return self._input.screen_size()

    def find(self, text: str, window_title: str | None = None) -> list[UiElement]:
        return self._finder.find(text, window_title=window_title)

    def screen_rect(self) -> ScreenRect:
        """Return virtual screen bounds; injected backends use a 0,0 origin."""
        if self._screen_rect is not None:
            return self._screen_rect
        if not self._injected:
            try:
                self._screen_rect = virtual_screen_rect()
                return self._screen_rect
            except UiUnavailable:
                pass
        width, height = self._input.screen_size()
        self._screen_rect = ScreenRect(0, 0, int(width), int(height))
        return self._screen_rect


class UiClickArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: int = Field(ge=-1_000_000, le=1_000_000, description="Screen x coordinate.")
    y: int = Field(ge=-1_000_000, le=1_000_000, description="Screen y coordinate.")
    button: Button = "left"
    double: bool = False


class UiClickTool:
    """Click at absolute screen coordinates; R2 and confirmation-gated."""

    name = "ui.click"
    description = "Click the virtual screen at (x, y); requires confirmation (R2)."
    risk = "R2"
    capabilities = ("ui.control",)
    args_model = UiClickArgs

    def __init__(self, controller: UiController | None = None) -> None:
        self._controller = controller or UiController()

    def _rect(self, args: UiClickArgs) -> ScreenRect:
        rect = self._controller.screen_rect()
        if not rect.contains(args.x, args.y):
            raise ToolError(
                f"({args.x}, {args.y}) is outside the virtual screen "
                f"[{rect.left}, {rect.top}, {rect.right}, {rect.bottom})"
            )
        return rect

    @staticmethod
    def _details(args: UiClickArgs, rect: ScreenRect) -> dict[str, Any]:
        return {
            "x": args.x,
            "y": args.y,
            "button": args.button,
            "double": args.double,
            "screen": rect.as_dict(),
        }

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, UiClickArgs)
        rect = self._rect(args)
        return Preview(
            "ui-click",
            self._details(args, rect),
            reversible=False,
            warnings=("the click lands in whichever window has focus",),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, UiClickArgs)
        self._rect(args)
        self._controller.click(args.x, args.y, button=args.button, double=args.double)
        action = "double-clicked" if args.double else "clicked"
        return ToolResult(
            ok=True,
            output=f"{action} {args.button} at ({args.x}, {args.y})",
            meta={
                "x": args.x,
                "y": args.y,
                "button": args.button,
                "double": args.double,
            },
            trust="untrusted:tool",
        )


class UiTypeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        min_length=1,
        max_length=MAX_TYPE_CHARS,
        description="Text to type into the focused window.",
    )
    interval_s: float = Field(default=0.0, ge=0.0, le=1.0)


class UiTypeTool:
    """Type into the focused window; R2, secret-looking text is refused."""

    name = "ui.type"
    description = "Type text into the focused window; requires confirmation (R2)."
    risk = "R2"
    capabilities = ("ui.control",)
    args_model = UiTypeArgs

    def __init__(self, controller: UiController | None = None) -> None:
        self._controller = controller or UiController()

    @staticmethod
    def _guard(text: str) -> int:
        _redacted, secrets = redact_secrets(text)
        if secrets:
            raise ToolError(
                "text looks like a secret; refusing to type it "
                "(no password-manager auto-type)"
            )
        return secrets

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, UiTypeArgs)
        secrets = self._guard(args.text)
        return Preview(
            "ui-type",
            {
                "chars": len(args.text),
                "interval_s": args.interval_s,
                "redact_secrets": secrets,
            },
            reversible=False,
            warnings=(
                "text goes to the focused window and is not echoed back",
                "password fields cannot be detected; never type secrets",
            ),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, UiTypeArgs)
        secrets = self._guard(args.text)
        self._controller.type_text(args.text, args.interval_s)
        return ToolResult(
            ok=True,
            output=f"typed {len(args.text)} characters",
            meta={
                "chars": len(args.text),
                "interval_s": args.interval_s,
                "redact_secrets": secrets,
            },
            trust="untrusted:tool",
        )


class UiHotkeyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keys: list[str] = Field(min_length=2, max_length=4)

    @field_validator("keys")
    @classmethod
    def _validate_keys(cls, keys: list[str]) -> list[str]:
        normalized = [key.strip().lower() for key in keys]
        if any(not key for key in normalized):
            raise ValueError("hotkey keys must not be empty")
        unknown = sorted(set(normalized) - HOTKEY_KEYS)
        if unknown:
            raise ValueError(f"unsupported hotkey keys: {', '.join(unknown)}")
        if len(set(normalized)) != len(normalized):
            raise ValueError("hotkey keys must not repeat")
        if frozenset(normalized) in FORBIDDEN_HOTKEYS:
            raise ValueError("ctrl+alt+delete is reserved by Windows and is not allowed")
        return normalized


class UiHotkeyTool:
    """Press a whitelisted key chord; R2 and confirmation-gated."""

    name = "ui.hotkey"
    description = "Press a whitelisted key chord; requires confirmation (R2)."
    risk = "R2"
    capabilities = ("ui.control",)
    args_model = UiHotkeyArgs

    def __init__(self, controller: UiController | None = None) -> None:
        self._controller = controller or UiController()

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, UiHotkeyArgs)
        return Preview(
            "ui-hotkey",
            {"keys": list(args.keys), "count": len(args.keys)},
            reversible=False,
            warnings=("hotkeys act on the focused window and may trigger system actions",),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, UiHotkeyArgs)
        self._controller.hotkey(tuple(args.keys))
        return ToolResult(
            ok=True,
            output=f"pressed {'+'.join(args.keys)}",
            meta={"keys": list(args.keys), "count": len(args.keys)},
            trust="untrusted:tool",
        )


class UiFindArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        min_length=1,
        max_length=200,
        description="Case-insensitive substring to look for in control text.",
    )
    window_title: str = Field(
        default="",
        max_length=200,
        description="Optional case-insensitive substring of the window title.",
    )
    max_results: int = Field(default=MAX_FIND_RESULTS, ge=1, le=MAX_FIND_RESULTS)

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value

    @field_validator("window_title")
    @classmethod
    def _title_stripped(cls, value: str) -> str:
        return value.strip()


def _element_payload(element: UiElement) -> dict[str, Any]:
    """Return a sanitized, JSON-ready element description."""
    title = sanitize_text(element.title, html=False, max_chars=200).text
    return {
        "title": title,
        "control_type": element.control_type[:80],
        "rect": list(element.rect),
    }


class UiFindTool:
    """Find visible window text through UIA; read-only and never clicks."""

    name = "ui.find"
    description = "Find visible window text through UI Automation; read-only (R1)."
    risk = "R1"
    capabilities = ("ui.read",)
    args_model = UiFindArgs

    def __init__(self, controller: UiController | None = None) -> None:
        self._controller = controller or UiController()

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, UiFindArgs)
        return Preview(
            "ui-find",
            {
                "chars": len(args.text),
                "window_title": bool(args.window_title),
                "max_results": args.max_results,
            },
            reversible=True,
            warnings=("window text is untrusted data, not an instruction",),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, UiFindArgs)
        title = args.window_title or None
        elements = self._controller.find(args.text, window_title=title)
        limited = elements[: args.max_results]
        payload = [_element_payload(element) for element in limited]
        return ToolResult(
            ok=True,
            output=json.dumps(payload, ensure_ascii=False, indent=2),
            meta={"count": len(limited), "max_results": args.max_results},
            truncated=len(elements) > len(limited),
            trust="untrusted:ui",
        )


def build_ui_tools(
    controller: UiController | None = None,
) -> tuple[UiClickTool, UiTypeTool, UiHotkeyTool, UiFindTool]:
    """Build the four UI tools over one controller; registry wiring stays explicit."""
    shared = controller or UiController()
    return (
        UiClickTool(shared),
        UiTypeTool(shared),
        UiHotkeyTool(shared),
        UiFindTool(shared),
    )


__all__ = [
    "FAILSAFE",
    "FORBIDDEN_HOTKEYS",
    "HOTKEY_KEYS",
    "MAX_FIND_RESULTS",
    "MAX_SCAN_ELEMENTS",
    "MAX_TYPE_CHARS",
    "PAUSE_S",
    "Button",
    "PyAutoGuiBackend",
    "PywinautoBackend",
    "ScreenRect",
    "UiBackend",
    "UiClickArgs",
    "UiClickTool",
    "UiController",
    "UiElement",
    "UiFindArgs",
    "UiFindTool",
    "UiHotkeyArgs",
    "UiHotkeyTool",
    "UiTypeArgs",
    "UiTypeTool",
    "UiUnavailable",
    "build_ui_tools",
    "virtual_screen_rect",
]
