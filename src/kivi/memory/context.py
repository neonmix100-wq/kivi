"""Context builder: profile, relevant memory and recent messages within budget.

Untrusted rows are wrapped in :class:`TrustEnvelope`; facts and messages keep
their provenance. The builder never spends more than the character budget and
marks the result as truncated instead of silently dropping the tail.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kivi.memory.search import Embedder, HybridSearch
from kivi.memory.store import MemoryStore
from kivi.safety.sanitize import sanitize_text
from kivi.safety.trust import TRUST_LEVELS, build_envelope


@dataclass(frozen=True, slots=True)
class Context:
    text: str
    sections: dict[str, int] = field(default_factory=dict)
    used_chars: int = 0
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not self.text


class ContextBuilder:
    def __init__(
        self,
        store: MemoryStore,
        *,
        max_chars: int = 6000,
        max_facts: int = 10,
        max_messages: int = 8,
        max_search: int = 5,
        hybrid: HybridSearch | None = None,
        embedder: Embedder | None = None,
        model: str = "",
    ) -> None:
        if max_chars < 200:
            raise ValueError("max_chars must be at least 200")
        self.store = store
        self.max_chars = max_chars
        self.max_facts = max_facts
        self.max_messages = max_messages
        self.max_search = max_search
        self.hybrid = hybrid
        self.embedder = embedder
        self.model = model

    def _safe_trust(self, trust: str) -> str:
        return trust if trust in TRUST_LEVELS else "untrusted:tool"

    def _envelope(self, content: str, trust: str, locator: str) -> str:
        sanitized = sanitize_text(content, html=False)
        text = sanitized.text
        if sanitized.suspicious:
            text += f"\n[flags: {', '.join(sanitized.suspicious)}]"
        return build_envelope(
            text,
            self._safe_trust(trust),  # type: ignore[arg-type]
            locator=locator,
        ).render()

    def _profile(self) -> str:
        lines: list[str] = []
        for key, value in self.store.preferences().items():
            lines.append(f"- {key}: {sanitize_text(value, html=False).text}")
        for fact in self.store.facts(self.max_facts):
            lines.append(
                f"- {fact['key']}: {sanitize_text(str(fact['value']), html=False).text}"
            )
        return "\n".join(lines)

    def _relevant(self, query: str) -> tuple[str, set[int], set[str]]:
        if not query.strip():
            return "", set(), set()
        if self.hybrid is not None:
            return self._relevant_hybrid(query)
        blocks: list[str] = []
        message_ids: set[int] = set()
        fact_keys: set[str] = set()
        for fact in self.store.search_facts(query, self.max_search):
            fact_keys.add(str(fact["key"]))
            blocks.append(
                self._envelope(
                    f"{fact['key']}: {fact['value']}",
                    str(fact["trust"]),
                    f"fact:{fact['id']}",
                )
            )
        for message in self.store.search_messages(query, self.max_search):
            message_ids.add(int(message["id"]))
            blocks.append(
                self._envelope(
                    f"{message['role']}: {message['content']}",
                    str(message["trust"]),
                    f"message:{message['id']}",
                )
            )
        return "\n".join(blocks), message_ids, fact_keys

    def _relevant_hybrid(self, query: str) -> tuple[str, set[int], set[str]]:
        blocks: list[str] = []
        message_ids: set[int] = set()
        fact_keys: set[str] = set()
        assert self.hybrid is not None
        hits = self.hybrid.search(
            query,
            embedder=self.embedder,
            model=self.model,
            limit=self.max_search,
        )
        for hit in hits:
            if hit.owner_type == "message":
                message_ids.add(hit.owner_id)
            else:
                fact_keys.add(hit.text.split(":", 1)[0].strip())
            blocks.append(
                self._envelope(
                    hit.text,
                    hit.trust,
                    f"{hit.owner_type}:{hit.owner_id}",
                )
            )
        return "\n".join(blocks), message_ids, fact_keys

    def _recent(self, task_id: str, skip: set[int]) -> str:
        lines: list[str] = []
        for message in reversed(self.store.recent_messages(self.max_messages, task_id=task_id)):
            if int(message["id"]) in skip:
                continue
            lines.append(
                self._envelope(
                    f"{message['role']}: {message['content']}",
                    str(message["trust"]),
                    f"message:{message['id']}",
                )
            )
        return "\n".join(lines)

    def build(self, query: str = "", *, task_id: str = "") -> Context:
        relevant, message_ids, fact_keys = self._relevant(query)
        profile_lines = self._profile()
        # Avoid repeating facts that already appeared in the relevant section.
        if fact_keys:
            filtered = [
                line
                for line in profile_lines.splitlines()
                if line.split(":", 1)[0].removeprefix("- ").strip() not in fact_keys
            ]
            profile_lines = "\n".join(filtered)
        recent = self._recent(task_id, message_ids)

        blocks = [
            ("Profile", profile_lines),
            ("Relevant memory", relevant),
            ("Recent messages", recent),
        ]
        rendered: list[str] = []
        used = 0
        truncated = False
        section_sizes: dict[str, int] = {}
        for title, body in blocks:
            if not body.strip():
                continue
            block = f"## {title}\n{body}\n"
            if used + len(block) <= self.max_chars:
                rendered.append(block)
                used += len(block)
                section_sizes[title] = len(block)
                continue
            remaining = self.max_chars - used
            if remaining > 80:
                cut = block[:remaining]
                rendered.append(cut)
                section_sizes[title] = len(cut)
                used += len(cut)
            truncated = True
            break
        text = "\n".join(rendered).strip()
        return Context(
            text=text,
            sections=section_sizes,
            used_chars=len(text),
            truncated=truncated,
        )


__all__ = ["Context", "ContextBuilder"]
