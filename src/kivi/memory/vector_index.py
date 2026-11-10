"""Optional LanceDB ANN index for memory embeddings.

SQLite remains the source of truth: embeddings are still written to the
``embeddings`` table. This module adds an optional LanceDB table for fast
approximate search on large memories. The rest of Kivi must keep working when
LanceDB is not installed, so the imports are lazy and :func:`available` is cheap.

``export_from_store`` copies vectors from SQLite into LanceDB; callers keep both
in sync after indexing/deleting. The index is a cache, never the only copy.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path
from typing import Any

from kivi.memory.store import MemoryStore

DEFAULT_TABLE = "embeddings"


class VectorIndexError(RuntimeError):
    """Raised when the optional vector index is unavailable or invalid."""


def available() -> bool:
    """True when lancedb and pyarrow can be imported."""
    try:
        import lancedb  # noqa: F401
        import pyarrow  # noqa: F401
    except ImportError:
        return False
    return True


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _validate_vector(vector: Any) -> list[float]:
    if not isinstance(vector, (list, tuple)) or not vector:
        raise VectorIndexError("vector must be a non-empty list")
    values = [float(item) for item in vector]
    if not all(math.isfinite(item) for item in values):
        raise VectorIndexError("vector must contain finite numbers")
    return values


def _validate_owner(owner_type: str) -> None:
    if owner_type not in {"message", "fact"}:
        raise VectorIndexError(f"unknown owner type: {owner_type}")


class LanceVectorIndex:
    """A small wrapper over one LanceDB table of ``(owner, model, vector)`` rows."""

    def __init__(
        self,
        uri: Path,
        *,
        table: str = DEFAULT_TABLE,
        dim: int | None = None,
    ) -> None:
        if not available():
            raise VectorIndexError(
                "lancedb/pyarrow are not installed; install the 'vector' extra"
            )
        if dim is not None and dim < 1:
            raise VectorIndexError("dim must be positive")
        import lancedb

        self.uri = Path(uri)
        self.uri.mkdir(parents=True, exist_ok=True)
        self.table_name = table
        self.dim = dim
        self._db = lancedb.connect(str(self.uri))
        names = set(self._db.list_tables())
        self._table = self._db.open_table(table) if table in names else None
        if self._table is not None and dim is None:
            self.dim = self._infer_dim()

    def _infer_dim(self) -> int | None:
        assert self._table is not None
        try:
            arrow = self._table.to_arrow()
            if arrow.num_rows == 0:
                return None
            vector = arrow["vector"][0].as_py()
        except Exception:  # noqa: BLE001 - empty/an unusual table
            return None
        return len(vector) if vector else None

    @property
    def table(self) -> Any:
        return self._table

    def _ensure_table(self, rows: list[dict[str, Any]]) -> None:
        if self._table is not None:
            return
        self._table = self._db.create_table(self.table_name, data=rows, mode="overwrite")

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        """Insert or replace rows; returns the number of rows written."""
        if not rows:
            return 0
        cleaned: list[dict[str, Any]] = []
        for row in rows:
            owner_type = str(row.get("owner_type", ""))
            _validate_owner(owner_type)
            owner_id = int(row.get("owner_id", 0))
            model = str(row.get("model", "")).strip()
            if not model:
                raise VectorIndexError("model must not be empty")
            vector = _validate_vector(row.get("vector"))
            if self.dim is None:
                self.dim = len(vector)
            if len(vector) != self.dim:
                raise VectorIndexError(
                    f"vector dimension {len(vector)} does not match index dimension {self.dim}"
                )
            cleaned.append(
                {
                    "owner_type": owner_type,
                    "owner_id": owner_id,
                    "model": model,
                    "vector": vector,
                }
            )
        self._ensure_table(cleaned)
        assert self._table is not None
        for row in cleaned:
            self._table.delete(
                f"owner_type = {_sql_string(row['owner_type'])} AND "
                f"owner_id = {row['owner_id']} AND model = {_sql_string(row['model'])}"
            )
        self._table.add(cleaned)
        return len(cleaned)

    def search(self, vector: list[float], model: str, limit: int = 10) -> list[dict[str, Any]]:
        """Cosine search; returns higher-is-better ``score``."""
        if limit < 1:
            raise VectorIndexError("limit must be positive")
        query = _validate_vector(vector)
        if self.dim is not None and len(query) != self.dim:
            raise VectorIndexError(
                f"query dimension {len(query)} does not match index dimension {self.dim}"
            )
        if self._table is None:
            return []
        rows = (
            self._table.search(query)
            .metric("cosine")
            .where(f"model = {_sql_string(model.strip())}")
            .limit(limit)
            .to_list()
        )
        return [
            {
                "owner_type": str(row["owner_type"]),
                "owner_id": int(row["owner_id"]),
                "model": str(row["model"]),
                "score": 1.0 - float(row.get("_distance", 1.0)),
            }
            for row in rows
        ]

    def delete(self, owner_type: str, owner_id: int, model: str | None = None) -> int:
        """Delete matching rows; returns the number of matched rows."""
        _validate_owner(owner_type)
        if self._table is None:
            return 0
        import pyarrow.compute as pc

        arrow = self._table.to_arrow()
        mask = pc.and_(
            pc.equal(arrow["owner_type"], owner_type),
            pc.equal(arrow["owner_id"], owner_id),
        )
        if model is not None:
            mask = pc.and_(mask, pc.equal(arrow["model"], model))
        matched = int(pc.sum(mask).as_py() or 0)
        condition = f"owner_type = {_sql_string(owner_type)} AND owner_id = {owner_id}"
        if model is not None:
            condition += f" AND model = {_sql_string(model)}"
        self._table.delete(condition)
        return matched

    def count(self, model: str | None = None) -> int:
        """Count rows, optionally restricted to one model."""
        if self._table is None:
            return 0
        if model is None:
            return int(self._table.count_rows())
        import pyarrow.compute as pc

        arrow = self._table.to_arrow()
        return int(pc.sum(pc.equal(arrow["model"], model)).as_py() or 0)

    def drop(self) -> None:
        """Drop the LanceDB table (the SQLite embeddings are untouched)."""
        if self._table is not None:
            self._db.drop_table(self.table_name)
            self._table = None

    def close(self) -> None:
        self._table = None


def export_from_store(
    store: MemoryStore,
    index: LanceVectorIndex,
    model: str,
) -> int:
    """Copy every stored embedding for ``model`` into the ANN index."""
    rows = store.embeddings(model)
    payload = [
        {
            "owner_type": row["owner_type"],
            "owner_id": int(row["owner_id"]),
            "model": row["model"],
            "vector": row["vector"],
        }
        for row in rows
    ]
    return index.upsert(payload)


def rebuild_index(uri: Path, store: MemoryStore, model: str) -> LanceVectorIndex:
    """Drop and rebuild an index from SQLite; useful after schema changes."""
    target = Path(uri)
    if target.exists():
        shutil.rmtree(target)
    index = LanceVectorIndex(target)
    export_from_store(store, index, model)
    return index


__all__ = [
    "DEFAULT_TABLE",
    "LanceVectorIndex",
    "VectorIndexError",
    "available",
    "export_from_store",
    "rebuild_index",
]
