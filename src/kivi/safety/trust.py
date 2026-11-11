"""Trust labels and envelope rendering for external content.

Every piece of text that reaches a planner carries provenance. Files, web
pages, messages, tool output, vision and generated code are *untrusted*: they
can describe facts, but they cannot issue instructions. Only the system, the
user and a signed module may instruct.

``TrustEnvelope.render`` puts untrusted text into an explicit envelope whose
closing tag and role markers are neutralised, so a payload cannot pretend to
end the envelope and start a system message.
"""

from __future__ import annotations

import hashlib
import html
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

TrustLevel = Literal[
    "trusted:system",
    "trusted:user",
    "trusted:module",
    "untrusted:file",
    "untrusted:web",
    "untrusted:message",
    "untrusted:tool",
    "untrusted:vision",
    "untrusted:code",
    "untrusted:os",
    "untrusted:ui",
    "untrusted:email",
    "tainted:memory",
]

TRUST_LEVELS: frozenset[str] = frozenset(
    {
        "trusted:system",
        "trusted:user",
        "trusted:module",
        "untrusted:file",
        "untrusted:web",
        "untrusted:message",
        "untrusted:tool",
        "untrusted:vision",
        "untrusted:code",
        "untrusted:os",
        "untrusted:ui",
        "untrusted:email",
        "tainted:memory",
    }
)

# Only these may give the planner instructions or widen permissions.
INSTRUCTION_LEVELS: frozenset[str] = frozenset(
    {"trusted:system", "trusted:user", "trusted:module"}
)

SOURCE_LEVELS: dict[str, TrustLevel] = {
    "system": "trusted:system",
    "user": "trusted:user",
    "module": "trusted:module",
    "file": "untrusted:file",
    "web": "untrusted:web",
    "message": "untrusted:message",
    "tool": "untrusted:tool",
    "vision": "untrusted:vision",
    "code": "untrusted:code",
    "os": "untrusted:os",
    "ui": "untrusted:ui",
    "email": "untrusted:email",
    "memory": "tainted:memory",
}

# Markers that could be parsed as a role change by a chat template.
_ROLE_MARKERS = (
    "system:",
    "assistant:",
    "tool:",
    "<|system|>",
    "<|assistant|>",
    "<|user|>",
    "[inst]",
    "[/inst]",
    "<<sys>>",
    "<</sys>>",
    "<untrusted",
    "</untrusted",
    "<trusted",
    "</trusted",
    "<tainted",
    "</tainted",
)


class TrustError(ValueError):
    """Raised for an unknown trust level or an invalid envelope."""


def is_trusted(level: str) -> bool:
    return level in INSTRUCTION_LEVELS


def can_issue_instructions(level: str) -> bool:
    """Untrusted and tainted content may never become an instruction."""
    if level not in TRUST_LEVELS:
        raise TrustError(f"unknown trust level: {level}")
    return level in INSTRUCTION_LEVELS


def classify_source(kind: str) -> TrustLevel:
    """Map a source name (``web``, ``file``, ...) to its trust label."""
    level = SOURCE_LEVELS.get(kind.strip().lower())
    if level is None:
        raise TrustError(f"unknown source kind: {kind}")
    return level


def _escape_markers(text: str) -> str:
    """Neutralise role markers and envelope tags without hiding the content."""
    result = text
    for marker in _ROLE_MARKERS:
        escaped = marker.replace(":", "&#58;").replace("<", "&lt;").replace(">", "&gt;")
        result = _replace_casefold(result, marker, escaped)
    return result


def _replace_casefold(text: str, needle: str, replacement: str) -> str:
    lowered = text.lower()
    needle = needle.lower()
    if needle not in lowered:
        return text
    parts: list[str] = []
    start = 0
    while True:
        index = lowered.find(needle, start)
        if index < 0:
            parts.append(text[start:])
            break
        parts.append(text[start:index])
        parts.append(replacement)
        start = index + len(needle)
    return "".join(parts)


@dataclass(frozen=True, slots=True)
class TrustEnvelope:
    """Sanitised content plus the provenance a planner is allowed to see."""

    content: str
    trust: TrustLevel
    source: str = ""
    locator: str = ""
    digest: str = ""
    collected_at: str = ""

    def __post_init__(self) -> None:
        if self.trust not in TRUST_LEVELS:
            raise TrustError(f"unknown trust level: {self.trust}")

    @property
    def can_instruct(self) -> bool:
        return self.can_issue_instructions()

    def can_issue_instructions(self) -> bool:
        return is_trusted(self.trust)

    def render(self) -> str:
        """Render a delimited envelope; markers inside are neutralised."""
        tag = self.trust.split(":", 1)[0]
        attributes = [f'source="{html.escape(self.trust.split(":", 1)[1])}"']
        if self.locator:
            attributes.append(f'locator="{html.escape(self.locator, quote=True)}"')
        if self.digest:
            attributes.append(f'hash="{self.digest}"')
        body = _escape_markers(self.content)
        return f"<{tag} {' '.join(attributes)}>\n{body}\n</{tag}>"

    def as_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "trust": self.trust,
            "source": self.source,
            "locator": self.locator,
            "digest": self.digest,
            "collected_at": self.collected_at,
            "can_instruct": self.can_issue_instructions(),
        }


def build_envelope(
    content: str,
    trust: TrustLevel,
    *,
    source: str = "",
    locator: str = "",
    clock: Any = None,
) -> TrustEnvelope:
    """Hash and date the content before it enters a prompt."""
    clock = clock or (lambda: datetime.now(UTC))
    return TrustEnvelope(
        content=content,
        trust=trust,
        source=source,
        locator=locator,
        digest=hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
        collected_at=clock().isoformat(timespec="seconds"),
    )


__all__ = [
    "INSTRUCTION_LEVELS",
    "SOURCE_LEVELS",
    "TRUST_LEVELS",
    "TrustEnvelope",
    "TrustError",
    "TrustLevel",
    "build_envelope",
    "can_issue_instructions",
    "classify_source",
    "is_trusted",
]
