"""Voice session orchestration: wake gate, barge-in, confirmations and metrics.

The module is deliberately independent of concrete audio hardware and of
``kivi.senses.audio``. The audio source, VAD segmenter and transcriber are
injected duck-typed objects, so importing this module never pulls in
``sounddevice``, ``torch`` or a speech model::

    source -> frames -> segmenter.feed(frame) -> segment.pcm
           -> transcriber.transcribe(pcm) -> text
           -> answer(text) -> speak(answer)

The session is synchronous. Barge-in is cooperative: :meth:`VoiceSession.stop`
flips a :class:`threading.Event` that is checked between the stages of a turn.
"""

from __future__ import annotations

import inspect
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

log = logging.getLogger(__name__)

STOP_PHRASES: tuple[str, ...] = (
    "киви, стоп",
    "киви стоп",
    "стоп",
    "kivi stop",
    "kivi, stop",
    "stop",
)
BARE_STOP_WORDS = frozenset({"стоп", "stop"})
MAX_BARE_STOP_CHARS = 40
STOP_TAILS = frozenset(
    {
        "пожалуйста",
        "пжлст",
        "please",
        "сейчас",
        "хватит",
        "достаточно",
        "now",
    }
)

WAKE_WORDS: tuple[str, ...] = ("киви", "kivi")

YES_WORDS = frozenset({"да", "yes", "конечно", "подтверждаю", "ага"})
NO_WORDS = frozenset({"нет", "no", "отмена", "неа"})
NO_PHRASES: tuple[tuple[str, ...], ...] = (("не", "надо"),)

MAX_FRAMES_PER_TURN = 100_000

_PUNCTUATION = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACES = re.compile(r"\s+")
_WORD = re.compile(r"[a-zа-яё]+", re.IGNORECASE)
_WAKE_PREFIX = re.compile(
    r"^\s*(?:киви|kivi)\b(?=\s|$|[,.:;!?—–])[\s,.:;!?—–-]*",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    """Lowercase text, unify ``ё`` and collapse punctuation and whitespace."""
    lowered = str(text).casefold().replace("ё", "е")
    return _SPACES.sub(" ", _PUNCTUATION.sub(" ", lowered)).strip()


def is_stop_phrase(text: str, phrases: Sequence[str] | None = None) -> bool:
    """Return whether ``text`` is a barge-in stop command.

    Matching is exact after normalizing case, punctuation and whitespace.
    A phrase may additionally be followed by one short politeness/filler word
    from :data:`STOP_TAILS` (``"стоп, пожалуйста"``, ``"kivi stop now"``).
    A bare ``"стоп"``/``"stop"`` is only accepted as a short utterance: text
    longer than :data:`MAX_BARE_STOP_CHARS` never matches by prefix, so
    ``"стоп-кран"`` or a long sentence containing ``"стоп"`` is ignored.
    """
    normalized = _normalize(text)
    if not normalized:
        return False
    candidates = STOP_PHRASES if phrases is None else tuple(phrases)
    normalized_phrases = {_normalize(phrase) for phrase in candidates}
    normalized_phrases.discard("")
    if normalized in normalized_phrases:
        return True
    for phrase in normalized_phrases:
        prefix = f"{phrase} "
        if not normalized.startswith(prefix):
            continue
        tail = normalized[len(prefix) :].strip()
        if tail not in STOP_TAILS:
            continue
        if phrase in BARE_STOP_WORDS and len(normalized) > MAX_BARE_STOP_CHARS:
            continue
        return True
    return False


def strip_wake_word(text: str) -> str:
    """Remove a leading ``"киви"``/``"kivi"`` wake word from a transcript.

    Only the start of the utterance is touched; punctuation and spacing around
    the wake word are removed as well. A word such as ``"киви-та"`` or a
    mention in the middle of a sentence is left intact.
    """
    return _WAKE_PREFIX.sub("", str(text), count=1).strip()


def has_wake_word(text: str) -> bool:
    """Return whether ``text`` starts with a wake word."""
    return bool(_WAKE_PREFIX.match(str(text)))


class BargeInController:
    """Thread-safe cooperative stop flag for speech barge-in.

    ``request_stop`` is idempotent while stopped: callbacks run once per stop
    transition. Callback exceptions are logged and swallowed so a broken
    listener cannot break the audio loop.
    """

    def __init__(
        self,
        phrases: Sequence[str] | None = None,
        *,
        on_stop: Callable[[], None] | None = None,
    ) -> None:
        self._phrases = tuple(phrases) if phrases is not None else STOP_PHRASES
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []
        if on_stop is not None:
            self._callbacks.append(on_stop)

    @property
    def phrases(self) -> tuple[str, ...]:
        """Stop phrases configured for this controller."""
        return self._phrases

    @property
    def stop_event(self) -> threading.Event:
        """The underlying event, suitable for passing to a speak worker."""
        return self._event

    @property
    def is_stopped(self) -> bool:
        """Whether a stop has been requested and not reset yet."""
        return self._event.is_set()

    def on_stop(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a no-argument callback fired after a stop transition."""
        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._lock:
            self._callbacks.append(callback)
        return callback

    def request_stop(self) -> bool:
        """Request a stop, return whether this call changed the state."""
        with self._lock:
            if self._event.is_set():
                return False
            self._event.set()
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001 - a callback must not break audio
                log.exception("barge-in stop callback failed")
        return True

    def reset(self) -> None:
        """Clear the stop flag for a new listening turn."""
        with self._lock:
            self._event.clear()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until a stop is requested or ``timeout`` seconds pass."""
        return self._event.wait(timeout)

    def is_stop_phrase(self, text: str) -> bool:
        """Check ``text`` against this controller's phrase list."""
        return is_stop_phrase(text, self._phrases)

    def check(self, text: str) -> bool:
        """Check ``text`` and request a stop when it is a stop command."""
        if self.is_stop_phrase(text):
            self.request_stop()
            return True
        return False


class VoiceConfirmation:
    """Parse spoken yes/no answers without ever defaulting to "yes"."""

    @staticmethod
    def parse(text: str | None) -> bool | None:
        """Return ``True`` for yes, ``False`` for no, or ``None`` if unclear.

        Ambiguous utterances that contain both a yes and a no token (for
        example ``"да нет"`` or ``"конечно нет"``) return ``None``. Negated
        confirmations such as ``"не подтверждаю"`` count as no.
        """
        if text is None:
            return None
        tokens = _WORD.findall(_normalize(text))
        if not tokens:
            return None
        negated_yes = any(
            tokens[index] == "не" and tokens[index + 1] in YES_WORDS
            for index in range(len(tokens) - 1)
        )
        no_hit = any(token in NO_WORDS for token in tokens) or any(
            _contains_phrase(tokens, phrase) for phrase in NO_PHRASES
        )
        yes_hit = any(token in YES_WORDS for token in tokens)
        if negated_yes:
            yes_hit = False
            no_hit = True
        if yes_hit and no_hit:
            return None
        if yes_hit:
            return True
        if no_hit:
            return False
        return None

    def ask(self, text: str, listener: Callable[[str], Any]) -> bool | None:
        """Ask a question through ``listener`` and parse its spoken answer.

        ``listener`` may return a string, a ``bool`` or an object with a
        ``.text`` attribute (for example a transcript). Anything else and any
        listener exception yields ``None``; this method never returns ``True``
        unless the answer was actually affirmative.
        """
        try:
            response = listener(text)
        except Exception:  # noqa: BLE001 - confirmation must not crash a session
            log.exception("voice confirmation listener failed")
            return None
        if isinstance(response, bool):
            return response
        if response is None:
            return None
        value: Any = response
        if not isinstance(value, str):
            value = getattr(value, "text", None)
        if value is None:
            return None
        return self.parse(str(value))


def _contains_phrase(tokens: Sequence[str], phrase: Sequence[str]) -> bool:
    size = len(phrase)
    if size == 0 or size > len(tokens):
        return False
    return any(
        tuple(tokens[index : index + size]) == tuple(phrase)
        for index in range(len(tokens) - size + 1)
    )


@dataclass(frozen=True, slots=True)
class VoiceMetrics:
    """Stage latencies of one voice turn, in milliseconds."""

    wake_ms: float = 0.0
    stt_ms: float = 0.0
    answer_ms: float = 0.0
    tts_ms: float = 0.0
    total_ms: float = 0.0

    def as_dict(self) -> dict[str, float]:
        """Return the metrics as an insertion-ordered JSON-friendly mapping."""
        return {
            "wake_ms": self.wake_ms,
            "stt_ms": self.stt_ms,
            "answer_ms": self.answer_ms,
            "tts_ms": self.tts_ms,
            "total_ms": self.total_ms,
        }


def format_metrics(metrics: VoiceMetrics) -> str:
    """Format metrics as a compact one-line human-readable string."""
    return " | ".join(
        f"{name.removesuffix('_ms')}={value:.1f} ms"
        for name, value in metrics.as_dict().items()
    )


@dataclass(frozen=True, slots=True)
class VoiceTurn:
    """One utterance/answer turn plus its stop state and latencies.

    ``addressed`` is false when the wake gate rejected the utterance, so the
    caller can distinguish "not for Kivi" from an empty transcription.
    """

    text: str
    answer: str
    stopped: bool = False
    metrics: VoiceMetrics = field(default_factory=VoiceMetrics)
    addressed: bool = True

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view of the turn."""
        return {
            "text": self.text,
            "answer": self.answer,
            "stopped": self.stopped,
            "addressed": self.addressed,
            "metrics": self.metrics.as_dict(),
        }


class VoiceSession:
    """Synchronous voice loop around injected audio, VAD and STT ports.

    ``source`` may be an iterable of frames, expose ``frames()`` (callable or
    iterable), expose an iterable ``stream``, or expose ``read_frame()`` /
    ``read()`` / ``read(frames)`` returning ``None`` or an empty container at the
    end. ``segmenter`` needs ``feed(frame) -> segment | None`` where a segment
    has ``.pcm`` (and normally ``.duration_s``). ``transcriber`` needs
    ``transcribe(pcm) -> object`` with a ``.text`` attribute (or a plain
    string). None of these types are imported from :mod:`kivi.senses.audio`.
    """

    def __init__(
        self,
        source: Any,
        segmenter: Any,
        transcriber: Any,
        *,
        answer: Callable[[str], str],
        speak: Callable[[str], object] | None = None,
        wake: Callable[[str], bool] | None = None,
        frame_wake: Callable[[Any], bool] | None = None,
        barge_in: BargeInController | None = None,
        max_turns: int = 1,
    ) -> None:
        if not callable(getattr(transcriber, "transcribe", None)):
            raise TypeError("transcriber must expose transcribe(pcm)")
        if not callable(getattr(segmenter, "feed", None)):
            raise TypeError("segmenter must expose feed(frame)")
        if not callable(answer):
            raise TypeError("answer must be callable")
        if speak is not None and not callable(speak):
            raise TypeError("speak must be callable or None")
        if wake is not None and not callable(wake):
            raise TypeError("wake must be callable or None")
        if frame_wake is not None and not callable(frame_wake):
            raise TypeError("frame_wake must be callable or None")
        if max_turns < 0:
            raise ValueError("max_turns must be non-negative")
        self.source = source
        self.segmenter = segmenter
        self.transcriber = transcriber
        self.answer = answer
        self.speak = speak
        self.wake = wake
        self.frame_wake = frame_wake
        self._frame_wake_hit = False
        self.barge_in = barge_in if barge_in is not None else BargeInController()
        self.max_turns = max_turns
        self._last_turn: VoiceTurn | None = None

    @property
    def last_turn(self) -> VoiceTurn | None:
        """The most recent turn produced by this session, if any."""
        return self._last_turn

    @property
    def is_stopped(self) -> bool:
        """Whether barge-in has requested a stop."""
        return self.barge_in.is_stopped

    def stop(self) -> None:
        """Request a cooperative stop between the stages of the current turn."""
        self.barge_in.request_stop()

    def reset(self) -> None:
        """Clear a previous barge-in stop so the session can listen again."""
        self.barge_in.reset()

    def _iter_frames(self) -> Iterator[Any]:
        """Yield frames from the injected source in any supported shape."""
        source = self.source
        frames = getattr(source, "frames", None)
        if callable(frames):
            yield from frames()
            return
        if frames is not None and hasattr(frames, "__iter__"):
            yield from frames
            return
        stream = getattr(source, "stream", None)
        if stream is not None and hasattr(stream, "__iter__"):
            yield from stream
            return
        if isinstance(source, Iterable):
            yield from source
            return
        for attribute in ("read_frame", "read"):
            reader = getattr(source, attribute, None)
            if callable(reader):
                yield from self._read_until_end(
                    self._frame_reader(source, reader)
                )
                return
        raise TypeError(
            "source must be iterable or expose frames(), stream, "
            "read_frame() or read()"
        )

    @staticmethod
    def _source_frame_size(source: Any) -> int:
        """Best-effort frame count for a block-style ``read(frames)`` API."""
        size = getattr(source, "frames_per_chunk", None)
        if callable(size):
            size = size()
        if isinstance(size, int) and size > 0:
            return size
        samplerate = getattr(source, "samplerate", 16000)
        chunk_ms = getattr(source, "chunk_ms", 30)
        try:
            computed = round(float(samplerate) * float(chunk_ms) / 1000.0)
        except (TypeError, ValueError):
            return 480
        return max(1, computed)

    @classmethod
    def _frame_reader(
        cls,
        source: Any,
        reader: Callable[..., Any],
    ) -> Callable[[], Any]:
        """Adapt a zero-argument or ``read(frames)`` reader to zero arguments."""
        try:
            parameters = inspect.signature(reader).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        required = [
            parameter
            for parameter in parameters
            if parameter.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
            and parameter.default is inspect.Parameter.empty
        ]
        if not required:
            return reader
        frame_size = cls._source_frame_size(source)
        return lambda: reader(frame_size)

    @staticmethod
    def _read_until_end(reader: Callable[[], Any]) -> Iterator[Any]:
        """Call a block-style reader until it reports end of stream."""
        while True:
            frame = reader()
            if frame is None:
                return
            try:
                empty = len(frame) == 0
            except TypeError:
                empty = False
            if empty:
                return
            yield frame

    def _next_segment(self) -> Any | None:
        """Feed frames to the segmenter until it returns a speech segment."""
        for index, frame in enumerate(self._iter_frames()):
            if self.barge_in.is_stopped:
                return None
            if index >= MAX_FRAMES_PER_TURN:
                log.warning("frame budget exceeded while waiting for a segment")
                return None
            if self.frame_wake is not None:
                try:
                    if self.frame_wake(frame):
                        self._frame_wake_hit = True
                except Exception:  # noqa: BLE001 - a broken frame gate is a miss
                    log.exception("frame wake gate failed")
            segment = self.segmenter.feed(frame)
            if segment is not None:
                return segment
        return None

    @staticmethod
    def _segment_pcm(segment: Any) -> Any:
        """Extract PCM from a VAD segment or accept raw bytes as a fallback."""
        pcm = getattr(segment, "pcm", None)
        if pcm is not None:
            return pcm
        if isinstance(segment, (bytes, bytearray, memoryview)):
            return bytes(segment)
        return b""

    @staticmethod
    def _transcript_text(transcript: Any) -> str:
        """Extract text from a transcript object or a plain string."""
        if transcript is None:
            return ""
        text = getattr(transcript, "text", transcript)
        return "" if text is None else str(text)

    def _make_turn(
        self,
        started: float,
        metrics: VoiceMetrics,
        *,
        text: str = "",
        answer: str = "",
        stopped: bool = False,
        addressed: bool = True,
    ) -> VoiceTurn:
        """Build a turn with a final total latency and remember it."""
        completed = replace(
            metrics,
            total_ms=(time.perf_counter() - started) * 1000.0,
        )
        turn = VoiceTurn(
            text=text,
            answer=answer,
            stopped=stopped,
            metrics=completed,
            addressed=addressed,
        )
        self._last_turn = turn
        return turn

    def run_once(self, push_to_talk: bool = False) -> VoiceTurn:
        """Read one utterance, answer it and optionally speak the answer.

        A pre-existing stop makes the turn ``stopped`` before any audio is
        read. A stop signalled by the transcript, by the answer callback or by
        the speak callback is honoured between stages; ``speak`` is skipped
        whenever the answer stage was itself interrupted.
        """
        started = time.perf_counter()
        metrics = VoiceMetrics()
        self._frame_wake_hit = False
        if self.barge_in.is_stopped:
            return self._make_turn(started, metrics, stopped=True)

        segment = self._next_segment()
        if self.barge_in.is_stopped:
            return self._make_turn(started, metrics, stopped=True)
        if segment is None:
            return self._make_turn(started, metrics)

        pcm = self._segment_pcm(segment)
        stt_started = time.perf_counter()
        try:
            transcript = self.transcriber.transcribe(pcm)
        except Exception:  # noqa: BLE001 - a failed segment must not kill the loop
            log.exception("voice transcription failed")
            metrics = replace(
                metrics,
                stt_ms=(time.perf_counter() - stt_started) * 1000.0,
            )
            return self._make_turn(
                started,
                metrics,
                stopped=self.barge_in.is_stopped,
            )
        metrics = replace(
            metrics,
            stt_ms=(time.perf_counter() - stt_started) * 1000.0,
        )

        raw_text = self._transcript_text(transcript).strip()
        if not raw_text:
            return self._make_turn(
                started,
                metrics,
                stopped=self.barge_in.is_stopped,
            )
        text = strip_wake_word(raw_text)
        if not text:
            return self._make_turn(
                started,
                metrics,
                stopped=self.barge_in.is_stopped,
                addressed=False,
            )
        if self.barge_in.check(text):
            return self._make_turn(started, metrics, text=text, stopped=True)

        addressed = self.wake is None and self.frame_wake is None
        if not push_to_talk and self.frame_wake is not None:
            addressed = self._frame_wake_hit
        if not push_to_talk and self.wake is not None:
            wake_started = time.perf_counter()
            try:
                text_addressed = bool(self.wake(raw_text))
            except Exception:  # noqa: BLE001 - a broken wake gate is a miss
                log.exception("wake gate failed; ignoring the utterance")
                text_addressed = False
            addressed = addressed or text_addressed
            metrics = replace(
                metrics,
                wake_ms=(time.perf_counter() - wake_started) * 1000.0,
            )
        if not push_to_talk and (self.wake is not None or self.frame_wake is not None):
            if self.barge_in.is_stopped:
                return self._make_turn(
                    started,
                    metrics,
                    text=text,
                    stopped=True,
                )
            if not addressed:
                return self._make_turn(
                    started,
                    metrics,
                    text=text,
                    addressed=False,
                )

        if self.barge_in.is_stopped:
            return self._make_turn(started, metrics, text=text, stopped=True)

        answer_started = time.perf_counter()
        reply = self.answer(text)
        reply_text = "" if reply is None else str(reply)
        metrics = replace(
            metrics,
            answer_ms=(time.perf_counter() - answer_started) * 1000.0,
        )
        if self.barge_in.is_stopped:
            return self._make_turn(
                started,
                metrics,
                text=text,
                answer=reply_text,
                stopped=True,
            )

        if self.speak is not None:
            tts_started = time.perf_counter()
            self.speak(reply_text)
            metrics = replace(
                metrics,
                tts_ms=(time.perf_counter() - tts_started) * 1000.0,
            )
            if self.barge_in.is_stopped:
                return self._make_turn(
                    started,
                    metrics,
                    text=text,
                    answer=reply_text,
                    stopped=True,
                )

        return self._make_turn(
            started,
            metrics,
            text=text,
            answer=reply_text,
        )

    def run_loop(
        self,
        max_turns: int | None = None,
        push_to_talk: bool = False,
        on_turn: Callable[[VoiceTurn], object] | None = None,
    ) -> list[VoiceTurn]:
        """Run at most ``max_turns`` turns and stop on barge-in or silence.

        The limit defaults to the constructor value. Empty turns and turns
        rejected by the wake gate are not appended to the result, but a wake
        gate miss does not discard the rest of the stream. A bounded number of
        wake misses is allowed so the loop cannot spin forever. ``on_turn``
        errors are logged and swallowed.
        """
        limit = self.max_turns if max_turns is None else max_turns
        if limit < 0:
            raise ValueError("max_turns must be non-negative")
        if limit == 0:
            return []
        turns: list[VoiceTurn] = []
        attempts = 0
        max_attempts = max(limit * 10, 10)
        while len(turns) < limit and attempts < max_attempts:
            attempts += 1
            if self.barge_in.is_stopped:
                break
            turn = self.run_once(push_to_talk=push_to_talk)
            if turn.stopped:
                turns.append(turn)
                self._notify(on_turn, turn)
                break
            if not turn.text and not turn.answer:
                break
            if not turn.addressed:
                continue
            turns.append(turn)
            self._notify(on_turn, turn)
        return turns

    @staticmethod
    def _notify(
        on_turn: Callable[[VoiceTurn], object] | None,
        turn: VoiceTurn,
    ) -> None:
        """Call an optional turn observer without letting it break the loop."""
        if on_turn is None:
            return
        try:
            on_turn(turn)
        except Exception:  # noqa: BLE001 - observers are optional
            log.exception("on_turn callback failed")


__all__ = [
    "BARE_STOP_WORDS",
    "BargeInController",
    "MAX_BARE_STOP_CHARS",
    "MAX_FRAMES_PER_TURN",
    "NO_PHRASES",
    "NO_WORDS",
    "STOP_PHRASES",
    "STOP_TAILS",
    "VoiceConfirmation",
    "VoiceMetrics",
    "VoiceSession",
    "VoiceTurn",
    "WAKE_WORDS",
    "YES_WORDS",
    "format_metrics",
    "has_wake_word",
    "is_stop_phrase",
    "strip_wake_word",
]
