"""Sanitizer for untrusted content.

The cleaner is deterministic and conservative: it keeps visible text, removes
script/style blocks, HTML comments, hidden elements, zero-width and bidi
characters, control characters and secret-looking tokens, and flags common
instruction patterns. It does not "understand" the text; flags are hints for the
planner and for the audit, never a permission decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

MAX_CHARS = 32 * 1024

_HIDDEN_TAGS = frozenset({"script", "style", "noscript", "template"})
_HIDDEN_STYLES = ("display:none", "visibility:hidden", "font-size:0", "opacity:0")

_ZERO_WIDTH = {
    0x00AD,
    0x034F,
    0x061C,
    0x115F,
    0x1160,
    0x17B4,
    0x17B5,
    0x180E,
    0x200B,
    0x200C,
    0x200D,
    0x200E,
    0x200F,
    0x202A,
    0x202B,
    0x202C,
    0x202D,
    0x202E,
    0x2060,
    0x2061,
    0x2062,
    0x2063,
    0x2064,
    0x2066,
    0x2067,
    0x2068,
    0x2069,
    0xFEFF,
}

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_HINT = re.compile(
    r"<(?:!doctype|html|body|div|p|span|script|style|a|img|br|table|ul|ol|li)\b",
    re.IGNORECASE,
)

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gho_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)

_SUSPICIOUS: tuple[str, ...] = (
    "ignore previous instructions",
    "ignore all previous",
    "disregard previous",
    "forget your instructions",
    "reveal your system prompt",
    "show your instructions",
    "system override",
    "you are now",
    "execute command",
    "run the following",
    "игнорируй предыдущие инструкции",
    "покажи системный промпт",
    "выполни команду",
    "отправь данные",
)


class SanitizerError(ValueError):
    """Raised for an unreadable input, never for merely suspicious text."""


@dataclass(frozen=True, slots=True)
class Sanitized:
    """Visible text plus what was removed and what looks suspicious."""

    text: str
    removed: dict[str, int] = field(default_factory=dict)
    suspicious: tuple[str, ...] = ()
    truncated: bool = False
    original_chars: int = 0

    @property
    def clean(self) -> bool:
        return not self.suspicious

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "removed": dict(self.removed),
            "suspicious": list(self.suspicious),
            "truncated": self.truncated,
            "original_chars": self.original_chars,
        }


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden: list[str] = []

    @staticmethod
    def _is_hidden(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if tag in _HIDDEN_TAGS:
            return True
        values = {name.lower(): (value or "") for name, value in attrs}
        if "hidden" in values:
            return True
        style = values.get("style", "").replace(" ", "").lower()
        return any(marker in style for marker in _HIDDEN_STYLES)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._is_hidden(tag.lower(), attrs):
            self._hidden.append(tag.lower())

    def handle_endtag(self, tag: str) -> None:
        if self._hidden and self._hidden[-1] == tag.lower():
            self._hidden.pop()

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


def _strip_invisible(text: str) -> tuple[str, int, int]:
    kept: list[str] = []
    zero_width = 0
    control = 0
    for char in text:
        code = ord(char)
        if code in _ZERO_WIDTH:
            zero_width += 1
            continue
        if code < 32 and char not in "\n\t":
            control += 1
            continue
        if code == 127:
            control += 1
            continue
        kept.append(char)
    return "".join(kept), zero_width, control


def _visible_text(text: str) -> str:
    parser = _VisibleTextParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - malformed HTML must not crash us
        raise SanitizerError(f"cannot parse HTML: {exc}") from exc
    return " ".join(part.strip() for part in parser.parts if part.strip())


def redact_secrets(text: str) -> tuple[str, int]:
    """Replace token-looking strings with ``[REDACTED]``; return the count."""
    redacted = text
    count = 0
    for pattern in _SECRET_PATTERNS:
        redacted, replaced = pattern.subn("[REDACTED]", redacted)
        count += replaced
    return redacted, count


def _normalise_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _suspicious_phrases(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    return tuple(phrase for phrase in _SUSPICIOUS if phrase in lowered)


def sanitize_text(
    raw: str,
    *,
    html: bool | None = None,
    max_chars: int = MAX_CHARS,
    redact: bool = True,
) -> Sanitized:
    """Return visible, normalised text and a record of what was removed."""
    if not isinstance(raw, str):
        raise SanitizerError("sanitizer input must be str")
    if max_chars < 1:
        raise SanitizerError("max_chars must be positive")

    original_chars = len(raw)
    removed: dict[str, int] = {}
    if html is None:
        html = bool(_HTML_HINT.search(raw))

    if html:
        comments = len(_HTML_COMMENT.findall(raw))
        if comments:
            removed["html_comments"] = comments
        text = _visible_text(raw)
    else:
        text = raw

    text, zero_width, control = _strip_invisible(text)
    if zero_width:
        removed["zero_width"] = zero_width
    if control:
        removed["control_chars"] = control

    text = _normalise_whitespace(text)

    if redact:
        text, secrets = redact_secrets(text)
        if secrets:
            removed["secrets"] = secrets

    suspicious = _suspicious_phrases(text)

    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars].rstrip()

    return Sanitized(
        text=text,
        removed=removed,
        suspicious=suspicious,
        truncated=truncated,
        original_chars=original_chars,
    )


__all__ = [
    "MAX_CHARS",
    "Sanitized",
    "SanitizerError",
    "redact_secrets",
    "sanitize_text",
]
