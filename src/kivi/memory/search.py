"""Hybrid memory search: FTS5 and embeddings fused with reciprocal rank.

Neither signal is trusted alone. Keyword search is exact but misses paraphrase;
embeddings handle paraphrase but can be fuzzy. Reciprocal Rank Fusion combines
the two ranked lists without needing comparable scores, and the result keeps the
owner type and trust label so the context builder can wrap it correctly.

Embeddings are optional: without an embedder only the FTS list is returned.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from kivi.memory.store import MemoryStore

Embedder = Callable[[list[str]], list[list[float]]]


@dataclass(frozen=True, slots=True)
class SearchHit:
    owner_type: str
    owner_id: int
    role: str
    text: str
    trust: str
    score: float = 0.0
    sources: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, int]:
        return self.owner_type, self.owner_id


class HybridSearch:
    def __init__(self, store: MemoryStore, *, rrf_k: int = 60) -> None:
        if rrf_k < 1:
            raise ValueError("rrf_k must be positive")
        self.store = store
        self.rrf_k = rrf_k

    def _fts_hits(self, query: str, limit: int) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for fact in self.store.search_facts(query, limit):
            hits.append(
                SearchHit(
                    owner_type="fact",
                    owner_id=int(fact["id"]),
                    role="fact",
                    text=f"{fact['key']}: {fact['value']}",
                    trust=str(fact["trust"]),
                )
            )
        for message in self.store.search_messages(query, limit):
            hits.append(
                SearchHit(
                    owner_type="message",
                    owner_id=int(message["id"]),
                    role=str(message["role"]),
                    text=str(message["content"]),
                    trust=str(message["trust"]),
                )
            )
        return hits

    def _vector_hits(
        self, query_vector: Sequence[float], model: str, limit: int
    ) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for row in self.store.search_embeddings(list(query_vector), model, limit):
            owner_type = str(row["owner_type"])
            owner_id = int(row["owner_id"])
            if owner_type == "message":
                message = self.store.get_message(owner_id)
                if message is None:
                    continue
                hits.append(
                    SearchHit(
                        owner_type="message",
                        owner_id=owner_id,
                        role=str(message["role"]),
                        text=str(message["content"]),
                        trust=str(message["trust"]),
                        score=float(row["score"]),
                    )
                )
            elif owner_type == "fact":
                fact = self.store.get_fact_by_id(owner_id)
                if fact is None:
                    continue
                hits.append(
                    SearchHit(
                        owner_type="fact",
                        owner_id=owner_id,
                        role="fact",
                        text=f"{fact['key']}: {fact['value']}",
                        trust=str(fact["trust"]),
                        score=float(row["score"]),
                    )
                )
        return hits

    def search(
        self,
        query: str,
        *,
        embedder: Embedder | None = None,
        model: str = "",
        limit: int = 10,
    ) -> list[SearchHit]:
        if limit < 1:
            raise ValueError("limit must be positive")
        ranked: dict[tuple[str, int], dict] = {}

        def add(hits: list[SearchHit], source: str) -> None:
            for rank, hit in enumerate(hits, start=1):
                entry = ranked.setdefault(
                    hit.key,
                    {"hit": hit, "score": 0.0, "sources": set()},
                )
                entry["score"] += 1.0 / (self.rrf_k + rank)
                entry["sources"].add(source)

        add(self._fts_hits(query, limit), "fts")
        if embedder is not None and model.strip() and query.strip():
            vectors = embedder([query])
            if vectors:
                add(self._vector_hits(vectors[0], model, limit), "vector")

        ordered = sorted(
            ranked.values(),
            key=lambda item: (
                -float(item["score"]),
                item["hit"].owner_type,
                item["hit"].owner_id,
            ),
        )
        result: list[SearchHit] = []
        for entry in ordered[:limit]:
            hit: SearchHit = entry["hit"]
            result.append(
                SearchHit(
                    owner_type=hit.owner_type,
                    owner_id=hit.owner_id,
                    role=hit.role,
                    text=hit.text,
                    trust=hit.trust,
                    score=round(float(entry["score"]), 6),
                    sources=tuple(sorted(entry["sources"])),
                )
            )
        return result


__all__ = ["Embedder", "HybridSearch", "SearchHit"]
