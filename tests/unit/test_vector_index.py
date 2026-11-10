from __future__ import annotations

from pathlib import Path

import pytest

from kivi.kernel.db import Database
from kivi.memory.store import MemoryStore
from kivi.memory.vector_index import (
    LanceVectorIndex,
    VectorIndexError,
    available,
    export_from_store,
    rebuild_index,
)

pytestmark = pytest.mark.skipif(not available(), reason="lancedb/pyarrow are not installed")


def make_index(tmp_path: Path) -> LanceVectorIndex:
    return LanceVectorIndex(tmp_path / "lance", dim=2)


def test_index_upsert_and_search_ordering(tmp_path: Path) -> None:
    index = make_index(tmp_path)
    index.upsert(
        [
            {"owner_type": "message", "owner_id": 1, "model": "m", "vector": [1.0, 0.0]},
            {"owner_type": "message", "owner_id": 2, "model": "m", "vector": [0.0, 1.0]},
        ]
    )
    hits = index.search([0.9, 0.1], "m", limit=2)
    assert hits[0]["owner_id"] == 1
    assert hits[0]["score"] > hits[1]["score"]


def test_model_filter_and_upsert_replaces(tmp_path: Path) -> None:
    index = make_index(tmp_path)
    index.upsert(
        [
            {"owner_type": "fact", "owner_id": 1, "model": "a", "vector": [1.0, 0.0]},
            {"owner_type": "fact", "owner_id": 1, "model": "b", "vector": [0.0, 1.0]},
        ]
    )
    index.upsert(
        [{"owner_type": "fact", "owner_id": 1, "model": "a", "vector": [0.5, 0.5]}]
    )
    assert index.count() == 2
    assert index.count("a") == 1
    assert index.search([1.0, 0.0], "b", 1)[0]["owner_id"] == 1


def test_delete_and_count(tmp_path: Path) -> None:
    index = make_index(tmp_path)
    index.upsert(
        [
            {"owner_type": "message", "owner_id": 1, "model": "m", "vector": [1.0, 0.0]},
            {"owner_type": "message", "owner_id": 2, "model": "m", "vector": [0.0, 1.0]},
        ]
    )
    assert index.delete("message", 1, "m") == 1
    assert index.count() == 1
    assert index.delete("message", 99, "m") == 0


def test_dimension_mismatch_and_invalid_vectors(tmp_path: Path) -> None:
    index = make_index(tmp_path)
    with pytest.raises(VectorIndexError, match="dimension"):
        index.search([1.0], "m")
    with pytest.raises(VectorIndexError, match="finite"):
        index.upsert(
            [{"owner_type": "message", "owner_id": 1, "model": "m", "vector": [float("nan"), 0.0]}]
        )
    with pytest.raises(VectorIndexError, match="owner"):
        index.upsert(
            [{"owner_type": "document", "owner_id": 1, "model": "m", "vector": [1.0, 0.0]}]
        )


def test_empty_index_returns_no_hits(tmp_path: Path) -> None:
    index = make_index(tmp_path)
    assert index.search([1.0, 0.0], "m") == []
    assert index.count() == 0


def test_export_from_store_and_rebuild(tmp_path: Path) -> None:
    db = Database(tmp_path / "kivi.db")
    store = MemoryStore(db, tmp_path / "artifacts")
    index = make_index(tmp_path)
    try:
        message_id = store.add_message("user", "alpha")
        store.put_embedding("message", message_id, "m", [1.0, 0.0])
        copied = export_from_store(store, index, "m")
        rebuilt = rebuild_index(tmp_path / "lance-rebuilt", store, "m")
    finally:
        db.close()
    assert copied == 1
    assert index.search([1.0, 0.0], "m", 1)[0]["owner_id"] == message_id
    assert rebuilt.count() == 1
