# Векторный индекс LanceDB

SQLite остаётся источником истины: эмбеддинги пишутся в таблицу `embeddings`.
LanceDB — опциональный ANN-кэш для больших баз. Без него Kivi работает на
SQLite + cosine-переборе.

## Установка

```powershell
.venv\Scripts\python.exe -m pip install -e ".[vector]"
# или
.venv\Scripts\python.exe -m pip install "lancedb>=0.41" "pyarrow>=26.0"
```

## Синхронизация

```powershell
kivi memory index -m bge-m3
kivi memory vector-sync -m bge-m3 --path data/vector
kivi memory vector-sync -m bge-m3 --rebuild
kivi memory similar "тёплый город" -m bge-m3 --ann data/vector
```

- `vector-sync` копирует все векторы модели из SQLite в LanceDB;
- `--rebuild` удаляет каталог индекса и строит его заново;
- `memory similar --ann PATH` использует ANN вместо cosine-перебора.

## API

```python
from kivi.memory.vector_index import LanceVectorIndex, export_from_store

index = LanceVectorIndex(Path("data/vector"), dim=1024)
export_from_store(store, index, "bge-m3")
hits = index.search(query_vector, "bge-m3", limit=10)
index.delete("message", 42, "bge-m3")
index.count("bge-m3")
```

Метрика — cosine, `score = 1 - distance` (больше = ближе). `upsert` заменяет
строку по `(owner_type, owner_id, model)`.

## Ограничения

- Синхронизация ручная: после `memory index`/удаления вызывайте `vector-sync`
  или `--rebuild`.
- ANN приблизительный; для маленьких баз SQLite точнее.
- LanceDB-каталог — кэш, его можно удалить и перестроить.
- `HybridSearch(store, ann=index)` использует ANN, если он передан; FTS5 и
  recency остаются прежними.
