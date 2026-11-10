"""Pure 2D avatar state and lip-sync logic plus an optional Tk renderer.

The core of this module has no GUI or audio dependencies. ``TkAvatarRenderer``
is deliberately opt-in: constructing it never imports tkinter or opens a
window, and only :meth:`TkAvatarRenderer.start` does so.
"""

from __future__ import annotations

import contextlib
import math
import struct
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from kivi.paths import project_root

DEFAULT_PORTRAIT = project_root() / "assets" / "avatar" / "concept" / "kivi_portrait.png"

Pcm = bytes | bytearray | memoryview | Sequence[int]
AvatarEventCallback = Callable[[dict[str, object]], None]


class AvatarState(StrEnum):
    """Visible states of the Kivi avatar."""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ERROR = "error"


class AvatarError(RuntimeError):
    """Raised for invalid avatar input or an unavailable Tk display."""


def _coerce_state(state: AvatarState | str) -> AvatarState:
    if isinstance(state, AvatarState):
        return state
    try:
        return AvatarState(str(state).strip().lower())
    except ValueError as exc:
        raise AvatarError(f"unknown avatar state: {state!r}") from exc


def _pcm_to_samples(pcm: Pcm) -> list[int]:
    """Decode little-endian PCM16 bytes or clamp a sequence of int16 values."""
    if isinstance(pcm, (bytes, bytearray, memoryview)):
        data = bytes(pcm)
        if len(data) % 2:
            raise AvatarError("PCM16 data must contain an even number of bytes")
        return [sample for (sample,) in struct.iter_unpack("<h", data)]
    samples: list[int] = []
    for value in pcm:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise AvatarError(f"invalid PCM sample: {value!r}") from exc
        samples.append(max(-32768, min(32767, number)))
    return samples


class LipSync:
    """Convert PCM16 mono amplitude into a smoothed mouth-openness value.

    For every analysis window the target is ``min(1, RMS / 32768 * gain)``.
    The returned value moves toward the target with an attack coefficient on
    louder input and a decay coefficient on quieter input::

        openness += (target - openness) * (attack if target > openness else decay)

    Processing happens in ``window_ms`` windows, so a long buffer produces a
    natural envelope instead of a single value for the whole utterance.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 48000,
        window_ms: int = 20,
        attack: float = 0.6,
        decay: float = 0.2,
        gain: float = 3.0,
        noise_floor: float = 0.01,
    ) -> None:
        if sample_rate <= 0:
            raise AvatarError("sample_rate must be positive")
        if window_ms <= 0:
            raise AvatarError("window_ms must be positive")
        if not 0.0 < attack <= 1.0:
            raise AvatarError("attack must be in (0, 1]")
        if not 0.0 < decay <= 1.0:
            raise AvatarError("decay must be in (0, 1]")
        if gain <= 0:
            raise AvatarError("gain must be positive")
        if not 0.0 <= noise_floor < 1.0:
            raise AvatarError("noise_floor must be in [0, 1)")
        self.sample_rate = sample_rate
        self.window_ms = window_ms
        self.attack = attack
        self.decay = decay
        self.gain = gain
        self.noise_floor = noise_floor
        self._openness = 0.0

    @property
    def openness(self) -> float:
        """Current mouth openness in the closed interval ``[0, 1]``."""
        return self._openness

    def reset(self) -> None:
        """Close the mouth immediately."""
        self._openness = 0.0

    def update(self, pcm: Pcm) -> float:
        """Consume one PCM16 buffer and return the updated mouth openness."""
        samples = _pcm_to_samples(pcm)
        if not samples:
            return self._openness
        window = max(1, int(self.sample_rate * self.window_ms / 1000))
        for start in range(0, len(samples), window):
            chunk = samples[start : start + window]
            if not chunk:
                continue
            mean_square = sum(sample * sample for sample in chunk) / len(chunk)
            rms = math.sqrt(mean_square) / 32768.0
            target = min(1.0, rms * self.gain)
            if target < self.noise_floor:
                target = 0.0
            coeff = self.attack if target >= self._openness else self.decay
            self._openness += (target - self._openness) * coeff
            self._openness = max(0.0, min(1.0, self._openness))
        return self._openness

    def update_pcm(self, pcm: Pcm) -> float:
        """Alias kept for callers that name the input after its format."""
        return self.update(pcm)


class AvatarController:
    """Small, forgiving avatar state machine with an optional event callback.

    Every state can transition to every other state; ``error`` is entered only
    by an explicit :meth:`set_state` call. The controller never touches Tk or an
    audio device, which keeps it unit-testable and safe to drive from any loop.
    """

    def __init__(
        self,
        lip_sync: LipSync | None = None,
        *,
        on_event: AvatarEventCallback | None = None,
    ) -> None:
        self._lip_sync = lip_sync if lip_sync is not None else LipSync()
        self._state = AvatarState.IDLE
        self._on_event = on_event

    @property
    def state(self) -> AvatarState:
        """Current avatar state."""
        return self._state

    @property
    def openness(self) -> float:
        """Current mouth openness from the lip-sync model."""
        return self._lip_sync.openness

    def snapshot(self) -> dict[str, object]:
        """Return a serializable view of the current state and mouth openness."""
        return {"state": self._state, "openness": self._lip_sync.openness}

    def _emit(self, event_type: str, previous: AvatarState) -> None:
        if self._on_event is None:
            return
        self._on_event(
            {
                "type": event_type,
                "state": self._state,
                "previous": previous,
                "openness": self._lip_sync.openness,
            }
        )

    def set_state(self, state: AvatarState | str) -> AvatarState:
        """Switch to any valid state and notify the observer when it changes."""
        target = _coerce_state(state)
        previous = self._state
        self._state = target
        if previous is not target:
            self._emit("state_changed", previous)
        return target

    def on_speech_start(self) -> None:
        """Enter ``speaking`` and reset the mouth envelope."""
        previous = self._state
        self._lip_sync.reset()
        self._state = AvatarState.SPEAKING
        self._emit("speech_start", previous)

    def on_speech_end(self) -> None:
        """Close the mouth and return to ``idle``."""
        previous = self._state
        self._lip_sync.reset()
        self._state = AvatarState.IDLE
        self._emit("speech_end", previous)

    def update_pcm(self, pcm: Pcm) -> float:
        """Feed a PCM16 buffer to lip sync and return mouth openness."""
        previous = self._state
        openness = self._lip_sync.update(pcm)
        self._emit("lip_sync", previous)
        return openness


@runtime_checkable
class AvatarRenderer(Protocol):
    """Minimal renderer contract used by a future window or web shell."""

    def start(self) -> None:
        """Create the rendering surface if the backend needs one."""

    def render(self, state: AvatarState, openness: float) -> None:
        """Draw one frame for the given state and mouth openness."""

    def stop(self) -> None:
        """Release the rendering surface."""


class TkAvatarRenderer:
    """Optional tkinter renderer for the 2D portrait and a simple mouth.

    The constructor only stores configuration. Call :meth:`start` before
    drawing; on a headless host it raises :class:`AvatarError` instead of
    crashing with a raw ``TclError``. This is a development stub, not a
    production transparent always-on-top window.
    """

    def __init__(
        self,
        portrait: Path | str | None = None,
        *,
        title: str = "Kivi",
        width: int = 512,
        height: int = 512,
        always_on_top: bool = True,
        show: bool = True,
    ) -> None:
        self.portrait = Path(portrait) if portrait is not None else DEFAULT_PORTRAIT
        self.title = title
        self.width = width
        self.height = height
        self.always_on_top = always_on_top
        self.show = show
        self._root: Any | None = None
        self._canvas: Any | None = None
        self._image: Any | None = None
        self._mouth: int | None = None
        self._state = AvatarState.IDLE
        self._openness = 0.0
        self._width = width
        self._height = height

    @property
    def is_running(self) -> bool:
        """Whether :meth:`start` has created a Tk root."""
        return self._root is not None

    def _load_portrait(self, tk: Any) -> None:
        if not self.portrait.is_file() or self._canvas is None:
            return
        try:
            image = tk.PhotoImage(file=str(self.portrait))
        except Exception:
            return
        self._image = image
        self._width = image.width()
        self._height = image.height()
        self._canvas.configure(width=self._width, height=self._height)
        self._canvas.create_image(
            self._width / 2,
            self._height / 2,
            image=self._image,
            anchor="center",
        )

    def _draw_mouth(self) -> None:
        if self._canvas is None:
            return
        center_x = self._width * 0.53
        center_y = self._height * 0.56
        half_width = max(6.0, self._width * 0.05)
        half_height = max(1.5, self._height * 0.006 + self._openness * self._height * 0.035)
        if self._mouth is None:
            self._mouth = self._canvas.create_oval(
                center_x - half_width,
                center_y - half_height,
                center_x + half_width,
                center_y + half_height,
                fill="#7a3542",
                outline="#4f1f29",
                width=1,
            )
        else:
            self._canvas.coords(
                self._mouth,
                center_x - half_width,
                center_y - half_height,
                center_x + half_width,
                center_y + half_height,
            )

    def start(self) -> None:
        """Create the Tk window and portrait canvas (main thread only)."""
        if self._root is not None:
            return
        try:
            import tkinter as tk

            root = tk.Tk()
        except Exception as exc:
            raise AvatarError(
                "tkinter is unavailable or there is no display; use a headless renderer"
            ) from exc
        try:
            root.title(self.title)
            root.resizable(False, False)
            if self.always_on_top:
                root.attributes("-topmost", True)
            canvas = tk.Canvas(
                root,
                width=self.width,
                height=self.height,
                background="#101418",
                highlightthickness=0,
            )
            canvas.pack(fill="both", expand=True)
            root.geometry(f"{self.width}x{self.height}")
            self._root = root
            self._canvas = canvas
            self._load_portrait(tk)
            if self._image is not None:
                root.geometry(f"{self._width}x{self._height}")
            self._draw_mouth()
            if not self.show:
                root.withdraw()
        except Exception as exc:
            self._root = None
            self._canvas = None
            self._image = None
            self._mouth = None
            with contextlib.suppress(Exception):
                root.destroy()
            raise AvatarError(f"failed to start Tk avatar: {exc}") from exc

    def render(self, state: AvatarState, openness: float) -> None:
        """Store the frame and update the mouth if the window is running."""
        self._state = _coerce_state(state)
        self._openness = max(0.0, min(1.0, float(openness)))
        if self._root is None:
            return
        self._draw_mouth()

    def stop(self) -> None:
        """Destroy the Tk window; safe to call when it was never started."""
        root = self._root
        self._root = None
        self._canvas = None
        self._image = None
        self._mouth = None
        if root is None:
            return
        with contextlib.suppress(Exception):
            root.destroy()

    def __enter__(self) -> TkAvatarRenderer:
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()
