# Память и контекст

Память Kivi — это SQLite плюс файлы артефактов. Первый срез даёт сообщения,
факты, предпочтения, content-addressed артефакты, FTS5-поиск и Context Builder.
Векторная база и эмбеддинги подключаются следующим срезом.

## 1. Схема

Миграция 4 добавляет:

| Таблица | Назначение |
|---|---|
| `messages` | сообщения с `role`, `trust`, `task_id` и временем |
| `messages_fts` | FTS5-индекс сообщений (external content + триггеры) |
| `facts` | факты `key → value` с источником, trust и confidence |
| `facts_fts` | FTS5-индекс фактов |
| `preferences` | настройки профиля `key → value` |
| `artifacts` | `sha256 → path`, mime и размер |
| `approvals` | заготовка для TTL-подтверждений |

FTS5 работает без внешних зависимостей. Запрос экранируется как фраза:
пользовательский текст не может сломать синтаксис `MATCH`.

## 2. MemoryStore

```python
from kivi.memory import MemoryStore

store = MemoryStore(db, paths.artifacts)
store.add_message("user", "привет", task_id="tsk_1")
store.remember("city", "Новосибирск", source="user")
store.set_preference("language", "ru")
store.search_facts("Новосибирск")
store.search_messages("привет")
store.forget("city")
artifact = store.write_artifact(b"bytes", mime="image/bmp")
```

- `remember` — upsert по ключу, FTS обновляется триггером;
- `forget` удаляет факт и его индекс;
- `write_artifact` считает SHA-256, кладёт файл в
  `data/artifacts/<2 символа>/<sha256>` и не дублирует одинаковые байты;
- `export()` возвращает JSON-совместимый снимок;
- все записи проверяют trust-метку и роль.

## 3. Context Builder

```python
from kivi.memory import ContextBuilder

context = ContextBuilder(store, max_chars=6000).build("Новосибирск", task_id="tsk_1")
print(context.text)
```

Порядок секций:

1. **Profile** — предпочтения и последние факты;
2. **Relevant memory** — FTS-совпадения по запросу (факты и сообщения);
3. **Recent messages** — последние сообщения задачи/всех задач.

Каждый элемент проходит санитайзер и оборачивается в `TrustEnvelope`
(`trusted:user`, `untrusted:web`, ...). Подозрительные фразы помечаются
`[flags: ...]`. Бюджет символов соблюдается; при нехватке места контекст
помечается `truncated`, а не обрезается молча в середине структуры.

## 4. CLI

```powershell
kivi memory add "текст" --role user --task-id tsk_1
kivi memory remember city Новосибирск --source user
kivi memory search Новосибирск
kivi memory recent -n 20
kivi memory forget city
kivi memory context Новосибирск --max-chars 6000
kivi memory stats
kivi memory export --out data/memory.json
kivi memory import D:\AI-base\AI-base\docs --limit 50
```

`memory import` читает только `.md/.markdown/.txt/.rst`, пропускает `.git`,
`.obsidian`, `private`, `voice-dataset`, symlink'и, бинарные и слишком большие
файлы; каждый файл чанкуется по абзацам и сохраняется как `untrusted:file`.
Повторный импорт того же пути пропускается (duplicates), исходные файлы не
меняются.

`kivi chat` теперь сохраняет сообщение пользователя в память; ответы модели
появятся при интеграции Gateway/планировщика.

## 5. Ограничения

- Векторный поиск (LanceDB + bge-m3) ещё не подключён; сейчас только FTS5.
- Нет суммаризации старых сообщений и TTL-политики; бюджет решает Context
  Builder, но хранилище не чистится автоматически.
- `approvals` — только схема, API подтверждений появится вместе с планером.
