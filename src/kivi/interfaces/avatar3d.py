"""3D avatar (VRM) state, viseme lip-sync and a loopback web renderer.

The module is intentionally split in two:

* :class:`Avatar3DController` is pure logic. It owns the state machine and turns
  a normalized speech amplitude into VRM-style blend shapes.
* :class:`ThreeVrmRenderer` serves the static ``web/avatar3d`` page and a
  generated ``state.json`` from a loopback HTTP server. It never imports Tk,
  never creates a window, and opens a browser only when explicitly asked.

A real VRM asset is optional. When ``assets/avatar/kivi.vrm`` is missing, the
browser page draws a procedural placeholder head, so the integration can be
developed and tested before the character asset exists.
"""

from __future__ import annotations

import contextlib
import json
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import quote, unquote, urlsplit

from kivi.paths import project_root

DEFAULT_VRM_RELATIVE = Path("assets") / "avatar" / "kivi.vrm"
WEB_RELATIVE = Path("web") / "avatar3d"
STATE_FILENAME = "state.json"
STATE_SCHEMA_VERSION = 1
BLEND_SHAPE_NAMES: tuple[str, ...] = ("A", "I", "U", "E", "O")

_LOOPBACK = "127.0.0.1"
_MIN_LEVEL = 0.01

Avatar3DEventCallback = Callable[[dict[str, object]], None]


class Avatar3DState(StrEnum):
    """Visible states of the 3D avatar."""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    HAPPY = "happy"
    ERROR = "error"


class Viseme(StrEnum):
    """Mouth shapes used by the lip-sync model and the VRM page."""

    A = "A"
    I = "I"  # noqa: E741 - the IPA-style viseme names are part of the contract
    U = "U"
    E = "E"
    O = "O"  # noqa: E741 - the IPA-style viseme names are part of the contract
    CLOSED = "CLOSED"


class Avatar3DError(RuntimeError):
    """Raised for invalid 3D avatar input or an unavailable web root."""


def _coerce_state(state: Avatar3DState | str) -> Avatar3DState:
    if isinstance(state, Avatar3DState):
        return state
    try:
        return Avatar3DState(str(state).strip().lower())
    except ValueError as exc:
        raise Avatar3DError(f"unknown avatar state: {state!r}") from exc


def _coerce_viseme(viseme: Viseme | str | None) -> Viseme:
    if isinstance(viseme, Viseme):
        return viseme
    if viseme is None:
        return Viseme.CLOSED
    key = str(viseme).strip().upper()
    if not key:
        return Viseme.CLOSED
    for candidate in Viseme:
        if candidate.value == key:
            return candidate
    raise Avatar3DError(f"unknown viseme: {viseme!r}")


def _clamp01(value: float) -> float:
    number = float(value)
    if math.isnan(number):
        return 0.0
    return min(1.0, max(0.0, number))


# Vowel hints cover the three-letter VRM presets plus Russian and Latin
# phonemes. A consonant hint keeps the mouth closed even during loud audio.
_VOWEL_HINTS: dict[str, Viseme] = {
    "a": Viseme.A,
    "aa": Viseme.A,
    "а": Viseme.A,
    "я": Viseme.A,
    "i": Viseme.I,
    "ih": Viseme.I,
    "и": Viseme.I,
    "ы": Viseme.I,
    "й": Viseme.I,
    "u": Viseme.U,
    "ou": Viseme.U,
    "у": Viseme.U,
    "ю": Viseme.U,
    "e": Viseme.E,
    "ee": Viseme.E,
    "э": Viseme.E,
    "е": Viseme.E,
    "o": Viseme.O,
    "oh": Viseme.O,
    "о": Viseme.O,
    "ё": Viseme.O,
}
_CONSONANT_HINTS = frozenset(
    "бвгджзклмнпрстфхцчшщъь"
    "bcdfghjklmnpqrstvwxyz"
)


class VisemeMapper:
    """Map a normalized amplitude plus an optional phoneme hint to a viseme.

    The mouth level is smoothed with separate attack and decay coefficients::

        level += (target - level) * (attack if target > level else decay)

    Only the dominant viseme is returned by :meth:`blend_shapes`; all other
    shapes are exactly zero. ``CLOSED`` means "all shapes zero", so it is never
    a blend-shape key.
    """

    def __init__(
        self,
        *,
        attack: float = 0.6,
        decay: float = 0.25,
        silence_threshold: float = 0.05,
    ) -> None:
        if not 0.0 < attack <= 1.0:
            raise Avatar3DError("attack must be in (0, 1]")
        if not 0.0 < decay <= 1.0:
            raise Avatar3DError("decay must be in (0, 1]")
        if not 0.0 <= silence_threshold < 1.0:
            raise Avatar3DError("silence_threshold must be in [0, 1)")
        self.attack = attack
        self.decay = decay
        self.silence_threshold = silence_threshold
        self._level = 0.0
        self._viseme = Viseme.CLOSED

    @property
    def viseme(self) -> Viseme:
        """Viseme that currently owns the mouth, or ``CLOSED``."""
        return self._viseme

    @property
    def level(self) -> float:
        """Smoothed mouth openness in the closed interval ``[0, 1]``."""
        return self._level

    @property
    def blendshapes(self) -> dict[str, float]:
        """Current VRM-style weights; at most one value is non-zero."""
        if self._viseme is Viseme.CLOSED or self._level <= 0.0:
            return {name: 0.0 for name in BLEND_SHAPE_NAMES}
        return {
            name: (self._level if name == self._viseme.value else 0.0)
            for name in BLEND_SHAPE_NAMES
        }

    def reset(self) -> None:
        """Close the mouth immediately."""
        self._level = 0.0
        self._viseme = Viseme.CLOSED

    def _hint_viseme(self, hint: str | None) -> Viseme | None:
        if hint is None:
            return None
        key = hint.strip().lower()
        if not key:
            return None
        if key in _VOWEL_HINTS:
            return _VOWEL_HINTS[key]
        if key in _CONSONANT_HINTS or key[0] in _CONSONANT_HINTS:
            return Viseme.CLOSED
        return _VOWEL_HINTS.get(key[0])

    def _target_viseme(self, amplitude: float, hint: str | None) -> Viseme:
        if amplitude <= self.silence_threshold:
            return Viseme.CLOSED
        hinted = self._hint_viseme(hint)
        if hinted is Viseme.CLOSED:
            return Viseme.CLOSED
        if hinted is not None:
            return hinted
        # No hint: roughly map quiet/medium/loud speech to narrow/mid/wide.
        if amplitude < 0.35:
            return Viseme.U
        if amplitude < 0.7:
            return Viseme.E
        return Viseme.A

    def update(self, amplitude: float, hint: str | None = None) -> Viseme:
        """Advance the smoothed envelope by one frame and return the viseme."""
        value = _clamp01(amplitude)
        target = self._target_viseme(value, hint)
        target_level = 0.0 if target is Viseme.CLOSED else value
        if target is not Viseme.CLOSED:
            self._viseme = target
        coefficient = self.attack if target_level > self._level else self.decay
        self._level += (target_level - self._level) * coefficient
        if self._level < _MIN_LEVEL:
            self._level = 0.0
            self._viseme = Viseme.CLOSED
        return self._viseme

    def blend_shapes(self, amplitude: float, hint: str | None = None) -> dict[str, float]:
        """Update the envelope and return a fresh blend-shape mapping."""
        self.update(amplitude, hint)
        return self.blendshapes


class Avatar3DController:
    """Small, forgiving 3D avatar state machine with viseme lip-sync.

    Every state can transition to every other state. ``error`` is entered only
    by an explicit :meth:`set_state` call; speech methods never do it. The
    controller never touches a renderer, a browser or an audio device.
    """

    def __init__(
        self,
        mapper: VisemeMapper | None = None,
        *,
        on_event: Avatar3DEventCallback | None = None,
    ) -> None:
        self._mapper = mapper if mapper is not None else VisemeMapper()
        self._state = Avatar3DState.IDLE
        self._on_event = on_event

    @property
    def state(self) -> Avatar3DState:
        """Current avatar state."""
        return self._state

    @property
    def viseme(self) -> Viseme:
        """Current viseme from the lip-sync mapper."""
        return self._mapper.viseme

    @property
    def blendshapes(self) -> dict[str, float]:
        """Current blend-shape weights."""
        return self._mapper.blendshapes

    def snapshot(self) -> dict[str, object]:
        """Return a serializable state, viseme and blend-shape snapshot."""
        return {
            "state": self._state,
            "viseme": self._mapper.viseme,
            "blendshapes": self._mapper.blendshapes,
        }

    def _emit(self, event_type: str, previous: Avatar3DState) -> None:
        if self._on_event is None:
            return
        self._on_event(
            {
                "type": event_type,
                "state": self._state,
                "previous": previous,
                "viseme": self._mapper.viseme,
                "level": self._mapper.level,
                "blendshapes": self._mapper.blendshapes,
            }
        )

    def set_state(self, state: Avatar3DState | str) -> Avatar3DState:
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
        self._mapper.reset()
        self._state = Avatar3DState.SPEAKING
        self._emit("speech_start", previous)

    def on_speech_end(self) -> None:
        """Close the mouth and return to ``idle``."""
        previous = self._state
        self._mapper.reset()
        self._state = Avatar3DState.IDLE
        self._emit("speech_end", previous)

    def update_amplitude(self, amplitude: float, hint: str | None = None) -> dict[str, float]:
        """Feed one normalized amplitude frame and return blend shapes."""
        previous = self._state
        shapes = self._mapper.blend_shapes(amplitude, hint)
        self._emit("lip_sync", previous)
        return shapes


@dataclass(frozen=True, slots=True)
class VrmStatus:
    """Result of looking for the optional VRM character asset."""

    available: bool
    path: Path | None
    message: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly representation of the status."""
        return {
            "available": self.available,
            "path": str(self.path) if self.path is not None else None,
            "message": self.message,
        }


def _resolve_root(root: Path | str | None) -> Path:
    return (Path(root) if root is not None else project_root()).resolve()


def find_vrm(root: Path | str | None = None) -> Path | None:
    """Return ``<root>/assets/avatar/kivi.vrm`` when the file exists."""
    candidate = _resolve_root(root) / DEFAULT_VRM_RELATIVE
    return candidate if candidate.is_file() else None


def vrm_status(root: Path | str | None = None) -> VrmStatus:
    """Discover the VRM asset and describe the resulting fallback state."""
    base = _resolve_root(root)
    candidate = base / DEFAULT_VRM_RELATIVE
    if candidate.is_file():
        return VrmStatus(True, candidate, f"VRM asset found: {candidate}")
    return VrmStatus(
        False,
        None,
        f"VRM asset not found at {candidate}; procedural fallback is active",
    )


@runtime_checkable
class Avatar3DRenderer(Protocol):
    """Minimal renderer contract for the browser/VRM layer."""

    def start(self) -> None:
        """Start serving or drawing."""

    def update(self, snapshot: Mapping[str, object] | Avatar3DController) -> None:
        """Publish one controller snapshot."""

    def stop(self) -> None:
        """Stop serving or drawing."""


class ThreeVrmRenderer:
    """Loopback HTTP renderer for the static three.js VRM avatar page.

    The renderer serves ``web/avatar3d`` at ``/`` and the repository ``assets``
    directory at ``/assets``. ``update()`` both keeps the in-memory
    ``state.json`` payload and writes it under the web root, so the page can
    poll it every 100 ms. No browser is opened unless ``open_browser=True``;
    this keeps unit tests and headless hosts window-free.
    """

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        open_browser: bool = False,
        port: int = 0,
    ) -> None:
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise Avatar3DError("port must be an integer in [0, 65535]")
        self.root = _resolve_root(root)
        self.web_root = self.root / WEB_RELATIVE
        self.assets_root = self.root / "assets"
        self.open_browser = bool(open_browser)
        self.port = port
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._url: str | None = None
        self._state_bytes = self._encode(self._build_payload(self._default_snapshot()))

    @staticmethod
    def _default_snapshot() -> dict[str, object]:
        return {
            "state": Avatar3DState.IDLE,
            "viseme": Viseme.CLOSED,
            "blendshapes": {name: 0.0 for name in BLEND_SHAPE_NAMES},
        }

    @property
    def state_path(self) -> Path:
        """Generated state file polled by the page."""
        return self.web_root / STATE_FILENAME

    @property
    def is_running(self) -> bool:
        """Whether the loopback HTTP server is currently running."""
        return self._server is not None

    @property
    def url(self) -> str | None:
        """Loopback URL of the page, or ``None`` before ``start()``."""
        return self._url

    @property
    def bound_port(self) -> int | None:
        """Actual TCP port, useful when ``port=0`` chooses a free port."""
        server = self._server
        if server is None:
            return None
        return int(server.server_address[1])

    def start(self) -> None:
        """Bind the loopback server and optionally open the browser."""
        if self._server is not None:
            return
        index = self.web_root / "index.html"
        if not index.is_file():
            raise Avatar3DError(f"avatar3d page not found: {index}")
        with self._lock:
            self._write_state_file(self._state_bytes)
        handler = self._make_handler()
        try:
            server = ThreadingHTTPServer((_LOOPBACK, self.port), handler)
        except OSError as exc:
            raise Avatar3DError(f"failed to bind avatar3d server: {exc}") from exc
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="kivi-avatar3d-http",
            daemon=True,
        )
        self._server = server
        self._thread = thread
        self._url = f"http://{_LOOPBACK}:{server.server_address[1]}/"
        try:
            thread.start()
        except Exception as exc:
            self._server = None
            self._thread = None
            self._url = None
            with contextlib.suppress(Exception):
                server.server_close()
            raise Avatar3DError(f"failed to start avatar3d server: {exc}") from exc
        if self.open_browser:
            self._launch_browser()

    def _launch_browser(self) -> None:
        if self._url is None:
            return
        with contextlib.suppress(Exception):
            # Imported lazily so importing this module never registers a browser.
            import webbrowser

            webbrowser.open(self._url)

    def update(self, snapshot: Mapping[str, object] | Avatar3DController) -> None:
        """Publish a controller snapshot to ``state.json`` and the HTTP server."""
        if isinstance(snapshot, Avatar3DController):
            snapshot = snapshot.snapshot()
        if not isinstance(snapshot, Mapping):
            raise Avatar3DError("snapshot must be a mapping or Avatar3DController")
        data = self._encode(self._build_payload(snapshot))
        with self._lock:
            self._state_bytes = data
            self._write_state_file(data)

    def stop(self) -> None:
        """Stop the HTTP server; safe to call more than once."""
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        self._url = None
        if server is None:
            return
        with contextlib.suppress(Exception):
            server.shutdown()
        with contextlib.suppress(Exception):
            server.server_close()
        if thread is not None:
            thread.join(timeout=2.0)

    def _build_payload(self, snapshot: Mapping[str, object]) -> dict[str, object]:
        raw_state = snapshot.get("state", Avatar3DState.IDLE)
        try:
            state = _coerce_state(raw_state)  # type: ignore[arg-type]
        except Avatar3DError:
            state = Avatar3DState.IDLE
        raw_viseme = snapshot.get("viseme", Viseme.CLOSED)
        try:
            viseme = _coerce_viseme(raw_viseme)  # type: ignore[arg-type]
        except Avatar3DError:
            viseme = Viseme.CLOSED
        raw_shapes = snapshot.get("blendshapes", {})
        source: Mapping[object, object] = raw_shapes if isinstance(raw_shapes, Mapping) else {}
        shapes: dict[str, float] = {}
        for name in BLEND_SHAPE_NAMES:
            try:
                number = float(source.get(name, 0.0))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                number = 0.0
            shapes[name] = _clamp01(number)
        status = vrm_status(self.root)
        vrm_url: str | None = None
        if status.available and status.path is not None:
            try:
                relative = status.path.relative_to(self.root).as_posix()
            except ValueError:
                relative = DEFAULT_VRM_RELATIVE.as_posix()
            vrm_url = "/" + quote(relative)
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "updated_at": round(time.time(), 3),
            "state": state.value,
            "viseme": viseme.value,
            "blendshapes": shapes,
            "vrm": {
                "available": status.available,
                "url": vrm_url,
                "path": str(status.path) if status.path is not None else None,
                "message": status.message,
            },
            "vrm_available": status.available,
            "fallback": not status.available,
        }

    @staticmethod
    def _encode(payload: Mapping[str, object]) -> bytes:
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        return (text + "\n").encode("utf-8")

    def _write_state_file(self, data: bytes) -> None:
        if not self.web_root.is_dir():
            return
        target = self.state_path
        temporary = target.with_name(target.name + ".tmp")
        try:
            temporary.write_bytes(data)
            temporary.replace(target)
        except OSError as exc:
            raise Avatar3DError(f"failed to write avatar state: {exc}") from exc

    def _state_payload_bytes(self) -> bytes:
        with self._lock:
            return self._state_bytes

    def _make_handler(self) -> type[SimpleHTTPRequestHandler]:
        web_root = self.web_root
        assets_root = self.assets_root
        renderer = self

        class Avatar3DRequestHandler(SimpleHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            extensions_map = {
                **SimpleHTTPRequestHandler.extensions_map,
                ".js": "text/javascript; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".vrm": "model/gltf-binary",
            }

            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__(*args, directory=str(web_root), **kwargs)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                if urlsplit(self.path).path == f"/{STATE_FILENAME}":
                    payload = renderer._state_payload_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                super().do_GET()

            def translate_path(self, path: str) -> str:
                clean = unquote(urlsplit(path).path)
                if clean.startswith("/assets/"):
                    base = assets_root
                    relative = clean[len("/assets/") :]
                else:
                    base = web_root
                    relative = clean.lstrip("/")
                    if not relative:
                        relative = "index.html"
                try:
                    resolved_base = base.resolve()
                    candidate = (base / relative).resolve()
                    candidate.relative_to(resolved_base)
                except (OSError, ValueError):
                    return str(base / "__not_found__")
                return str(candidate)

            def list_directory(self, path: str) -> None:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return None

            def end_headers(self) -> None:
                self.send_header("Cache-Control", "no-store")
                super().end_headers()

            def log_message(self, _format: str, *args: object) -> None:
                return

        return Avatar3DRequestHandler

    def __enter__(self) -> ThreeVrmRenderer:
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()
