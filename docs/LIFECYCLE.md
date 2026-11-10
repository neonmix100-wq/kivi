# Жизненный цикл памяти

Этап 5 добавляет к памяти управляемый жизненный цикл: старые сообщения можно
свернуть в компактные сводки, TTL-уборка удаляет просроченные сообщения вместе
с их векторами, а таблица `approvals` из миграции 4 получает явный API.
Модуль — `src/kivi/memory/lifecycle.py`; тесты —
`tests/unit/test_lifecycle.py`.

Ключевой принцип: **ничего не удаляется само**. Политика хранения применяется
только явным вызовом; дефолты ограничивают объём одного прохода, а не запускают
фон.

## 1. Политика хранения (retention)

| Данные | Участвуют в TTL | Удаляются |
|---|---|---|
| `messages` старше `days` | да | сообщение и его эмбеддинги |
| `embeddings` сообщений | да | вместе с сообщением (`forget_message`) |
| `approvals` просроченные/отозванные | да | через `ApprovalStore.cleanup` |
| `facts`, `preferences`, `artifacts` | нет | никогда |

```python
from kivi.memory.lifecycle import apply_ttl

result = apply_ttl(store, days=90)          # default: 90 дней
print(result.messages_deleted, result.embeddings_deleted)
```

- `days=90` — дефолт; `days=0` означает «всё, что создано раньше текущего
  момента».
- Отрицательный `days` отклоняется (`ValueError`), чтобы опечатка не превратила
  уборку в удаление всей памяти.
- Эмбеддинги удаляются через публичный `MemoryStore.forget_message`, поэтому
  FTS-триггеры и таблица `embeddings` остаются согласованными.
- `TTLResult` содержит `messages_deleted`, `embeddings_deleted`,
  `approvals_deleted` и `cutoff` (ISO-время отсечки).
- Порядок: сначала считаются эмбеддинги старых сообщений, затем вызывается
  `forget_message`, затем `ApprovalStore.cleanup(now=...)`.

TTL не трогает `handoffs`, `tasks`, `steps`, `events`, `audit`: это журналы
исполнения, а не транскрипт общения.

## 2. Суммаризация

```python
from kivi.memory.lifecycle import extractive_summary, summarize_messages

result = summarize_messages(store, older_than_days=7, max_messages=200)
print(result.groups, result.summarized, result.deleted, result.summary_ids)
```

Поток:

1. Взять сообщения с `created_at < now - older_than_days`, не более
   `max_messages`, начиная с самых старых (`ORDER BY id`).
2. Строки с `task_id LIKE 'summary:%'` пропускаются: сводки — производные
   данные, их не суммируют повторно.
3. Сгруппировать по `task_id`; пустой/`NULL` task id образует отдельную группу.
4. Для каждой группы получить текст:
   - по умолчанию — `extractive_summary(messages, max_chars=1200)`:
     детерминированно, без модели, строки `role: content`, пробелы сжаты,
     обрезка по границе предложения (если она рядом) или по `max_chars`,
     в конце `…` при усечении;
   - если передан `summarizer: Callable[[list[dict]], str]`, вызывается он.
5. Сохранить результат как сообщение:
   `store.add_message("system", summary, trust="untrusted:tool", task_id="summary:<group>")`.
6. Только после успешной записи всех сводок и только при
   `delete_originals=True` удалить оригиналы (`forget_message`, вместе с
   векторами).

`SummaryResult` содержит `groups`, `summarized`, `deleted`, `summary_ids`.

Важно:

- Оригиналы по умолчанию **не удаляются** (`delete_originals=False`);
- сводка — **недоверенный текст** (`untrusted:tool`), потому что собрана из
  сообщений, которые могли прийти из web/file/tool. Context Builder обернёт её
  в `<untrusted ...>`; она не может давать инструкции;
- пустой результат суммаризатора — ошибка (`ValueError`), а не тихая запись
  пустого сообщения;
- `older_than_days < 0` и `max_messages < 1` отклоняются;
- повторный запуск **не идемпотентен**: пока оригиналы на месте, они будут
  суммированы снова (схема не хранит отметку «уже суммировано»). Запускайте
  проход с `delete_originals=True` или один раз на задачу.

## 3. Approvals: семантика

`ApprovalStore(store)` — долговременное хранилище подтверждений поверх таблицы
`approvals` (`id`, `task_id`, `action`, `scope`, `granted_at`, `expires_at`,
`revoked_at`).

```python
from kivi.memory.lifecycle import ApprovalStore

approvals = ApprovalStore(store)
row = approvals.grant("tsk_1", "shell.exec", scope="", ttl_s=300)
approvals.is_approved("tsk_1", "shell.exec")       # True
approvals.revoke(row["id"])                        # True
approvals.list("tsk_1")                            # все строки задачи
```

- `grant(task_id, action, *, scope="", ttl_s=None)` — создаёт строку и
  возвращает её dict. `ttl_s=None` — бессрочно; `ttl_s` обязан быть `> 0`.
  `task_id` и `action` непустые после `strip()`, иначе `ValueError`.
- `is_approved(task_id, action, *, scope="")` — `True`, если есть живая строка:
  `revoked_at IS NULL` и (`expires_at IS NULL` или `expires_at > now`).
  Просрочка строгая: в момент `expires_at` подтверждение уже не действует.
- Scope-матрица:
  - пустой запрошенный `scope` совпадает с любым сохранённым;
  - сохранённый `scope=""` покрывает любой запрос;
  - иначе требуется точное совпадение.
  То есть `scope=""` — широкий grant, непустой — сужение.
- `revoke(approval_id)` — ставит `revoked_at`; повторный отзыв или отсутствующий
  id дают `False` (идемпотентно по результату).
- `list(task_id=None)` — все строки (или только задачи), новые сверху; в dict
  добавляется вычисленный `active` (bool). Просроченные строки видны в `list`,
  пока их не удалил `cleanup`.
- `cleanup(now=None)` — удаляет строки, отозванные или просроченные **не менее
  суток назад**. Свежепросроченная строка остаётся на день, чтобы оператор мог
  увидеть причину отказа; `is_approved` её уже игнорирует. Возвращает число
  удалённых строк.
- Опциональный `clock` в конструкторе (`ApprovalStore(store, clock=...)`) нужен
  для детерминированных тестов; в проде используется UTC-время.

Approvals пока **не интегрированы** с `PermissionEngine`: это только надёжное
хранилище. Проверка grant'ов движком — следующий шаг.

## 4. Ограничения

- Сводка — производный недоверенный текст. Она не является инструкцией и не
  повышает trust; при попадании в контекст оборачивается как `untrusted`.
- Автоматического удаления нет: ни TTL, ни суммаризация не запускаются фоном.
  Планировщик/CLI должен вызывать их явно.
- Повторная суммаризация не идемпотентна (нет колонки-отметки); защита есть
  только от суммирования самих сводок (`summary:%`).
- TTL удаляет только сообщения и связанные эмбеддинги; факты, предпочтения и
  артефакты сохраняются всегда. Артефакты не удаляются даже при удалении
  сообщения, которое на них ссылалось.
- `cleanup` approvals — политика «сутки после истечения/отзыва», а не мгновенное
  удаление; до `cleanup` строки остаются в `list`.
- Нет интеграции с `PermissionEngine`, Warden и аудитом: `ApprovalStore` не
  пишет события сам. Подключение к policy engine — отдельная задача.
- Ограничение `max_messages` применяется к самым старым сообщениям; при большом
  бэклоге потребуется несколько проходов.

## 5. CLI (план интеграции)

Сейчас команды нет; `src/kivi/interfaces/cli.py` не менялся. Предлагаемые
подкоманды `kivi memory`:

- `kivi memory summarize --older-than-days 7 --max-messages 200 [--delete] [--json]`
  → `summarize_messages(...)`, печать `groups/summarized/deleted`;
- `kivi memory ttl --days 90 [--json]` → `apply_ttl(...)`, печать
  `messages_deleted/embeddings_deleted/approvals_deleted`;
- `kivi memory approvals list [--task-id ...]`,
  `kivi memory approvals grant TASK ACTION [--scope ...] [--ttl 300]`,
  `kivi memory approvals revoke ID`, `kivi memory approvals cleanup`.

Импорт: `from kivi.memory.lifecycle import ApprovalStore, apply_ttl, summarize_messages`
(в `kivi/memory/__init__.py` эти имена пока не реэкспортированы — это отдельная
правка).

См. также [MEMORY.md](MEMORY.md) — схема и `MemoryStore`.
