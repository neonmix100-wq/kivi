# Архитектура Kivi

Версия: 0.2 (проектирование). Целевая платформа: Windows 11, один пользователь, один ПК.
Персона: Kivi, женский голос, 3D-аватар. Долгосрочно — псевдо-ОС поверх Windows, позже Linux.

---

## 1. Резюме

Kivi — это **локальный агент**, а не чат с моделью. Модель — только один из компонентов.
Система состоит из семи слоёв:

1. **Kernel** — события, планировщик, состояние, права, ресурсы, аудит.
2. **Brain** — маршрутизация запроса, планирование, выполнение, проверка результата.
3. **Model Gateway** — реестр моделей, бюджеты VRAM/RAM, загрузка/выгрузка, fallback.
4. **Senses** — голос, слух, зрение, текст.
5. **Tools** — файлы, shell, процессы, браузер, код, медиа, документы, ОС.
6. **Memory** — рабочая, эпизодическая, семантическая, профиль, артефакты.
7. **Interfaces** — CLI, локальный API, web-UI/HUD, трей, голосовой цикл, аватар Kivi.
8. **Modules** — версионированные плагины: `work_crm`, `mail`, `telegram`, `media` и др.

Сквозные подсистемы: **Safety/Policy**, **Protected Zones**, **Cloud Broker**,
**Observability**, **Backup/Rollback**, **Cost Guard**.

Главное ограничение — **8 ГБ VRAM** (планируется 12 ГБ). Поэтому модель — это ресурс
с арендой (lease), а не постоянно запущенный сервис.

Ключевые решения: `docs/DECISIONS.md`. Персона и аватар: `docs/PERSONA.md`.
Плагины: `docs/MODULES.md`.

---

## 2. Требования и ограничения

### 2.1. Функциональные (v1)

- Текст и голос; ответ текстом и голосом; TTS + анимация речи.
- Файлы: поиск, чтение, запись в workspace, безопасное удаление.
- Shell: безопасные команды, A4-Bounded, запрет разрушительных.
- Документы: PDF/DOCX/XLSX/MD → текст → выжимка/поиск.
- Память: факты, предпочтения, история задач, поиск по базе знаний.
- Код: генерация, запуск в песочнице, тесты, диффы.
- Изображения: SDXL локально + бесплатные облачные провайдеры.
- Браузер: открытие, чтение, клики, формы (Playwright/CDP).
- Почта/Telegram: чтение, черновики, отправка по политике.
- Управление ПК: приложения, окна, буфер, UI Automation.
- Облако: только бесплатные лимиты, handoff при исчерпании.
- UI: чат, задачи, подтверждения, VRAM, логи, аватар Kivi.
- Модули: work_crm, mail, telegram, media.
- Аудит и kill switch.

### 2.2. Нефункциональные

| Параметр | Цель |
|---|---|
| Idle RAM (ядро + wake-word) | < 700 МБ |
| Idle VRAM | 0 МБ (все модели выгружены) |
| Пробуждение → начало ответа | < 1,5 с |
| Простая команда | < 3 с (p50) |
| Переключение модели | < 20 с холодное, < 3 с тёплое |
| Восстановление после сбоя | < 10 с, задача возобновляема |
| Автономная работа | 7 дней без утечек и деградации |
| Сеть | только localhost по умолчанию |
| Приватность | облако только по политике и подтверждению |

### 2.3. Ограничения железа

- 4 физических ядра: нельзя параллелить много тяжёлых задач; очередь и приоритеты обязательны.
- 8 ГБ VRAM: 8B Q4 — максимум для комфортной работы; 14B — частичный offload или облако.
- 126 ГБ на D: хватит на LLM + SDXL, но не на зоопарк видео-моделей.
- WDDM: при переполнении VRAM Windows уходит в shared memory и скорость падает в разы.
  Это нужно **детектировать и запрещать**, а не терпеть.
- Планируется GPU 12 ГБ и SSD 1 ТБ: бюджеты в `models.yaml` пересматриваются после апгрейда.
  12 ГБ всё ещё не гарантируют 14B + видео 1080p + аватар одновременно.

---

## 3. Слои системы

```
┌────────────────────────────── Интерфейсы ──────────────────────────────┐
│ CLI/TUI · Web UI/HUD · Аватар Kivi · Голос · Трей · Local API           │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │ команды / события
┌───────────────────────────────────▼─────────────────────────────────────┐
│ KERNEL                                                                  │
│ Event Bus · Task Scheduler (DAG) · State Store · Config                 │
│ Permission Engine · Protected Zones · Resource Governor · Audit        │
│ Secrets · Watchdog · Kill Switch                                        │
└───────┬───────────────────┬───────────────────┬─────────────────────────┘
        │                   │                   │
┌───────▼────────┐  ┌───────▼────────┐  ┌───────▼─────────────────────────┐
│ BRAIN          │  │ MODEL GATEWAY  │  │ SENSES                          │
│ Intent Router  │  │ Registry       │  │ Audio: wake/VAD/STT/TTS         │
│ Planner        │  │ Lease Manager  │  │ Vision: screen/OCR/VLM          │
│ Executor       │  │ Loader/Evictor │  │ Text: CLI/UI/API                │
│ Verifier       │  │ Fallback       │  │                                 │
│ Reflector      │  │ Metrics        │  │                                 │
└───────┬────────┘  └───────┬────────┘  └───────┬─────────────────────────┘
        │                   │                   │
        │           ┌───────▼────────┐          │
        │           │ CLOUD BROKER   │          │
        │           │ Free quotas    │          │
        │           │ Handoff/local  │          │
        │           └───────┬────────┘          │
        │                   │                   │
┌───────▼───────────────────▼───────────────────▼─────────────────────────┐
│ TOOLS + MODULES                                                         │
│ FS · Shell · Process · Browser · Code Sandbox · Media · Docs            │
│ OS/UI Automation · Network · work_crm · mail · telegram                 │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼─────────────────────────────────────┐
│ MEMORY                                                                  │
│ Working · Episodic (SQLite) · Semantic (LanceDB) · Profile · Skills     │
│ Artifacts · Consolidation · Retention                                   │
└─────────────────────────────────────────────────────────────────────────┘

Сквозное: Safety/Policy · Protected Zones · Observability · Backup/Rollback
```

### 3.1. Правила зависимостей

- Интерфейсы не вызывают инструменты напрямую — только через Kernel/Brain.
- Brain не работает с GPU напрямую — только через Model Gateway.
- Tools не решают, можно ли действие, — только исполняют после проверки прав.
- Memory не хранит секреты; секреты — в отдельном vault.
- Safety проверяет **до** выполнения, аудит пишет **после** (и факт попытки — тоже).

---

## 4. Kernel

### 4.1. Event Bus

Внутренняя шина событий (asyncio + очередь). Типы:

- `user.message`, `voice.transcript`, `task.created`, `step.started`, `step.finished`,
- `tool.requested`, `tool.result`, `approval.requested`, `approval.resolved`,
- `model.loading`, `model.ready`, `model.evicted`, `resource.warning`,
- `task.failed`, `killswitch.triggered`.

События — источник истины для UI, логов и восстановления. Каждое имеет `task_id`, `step_id`, `trace_id`.

### 4.2. Task Scheduler

- Принимает задачу, создаёт DAG шагов.
- Ограничивает параллелизм: интерактивные задачи — приоритет 0, фоновые — 10.
- Не запускает два тяжёлых шага одновременно, если они конкурируют за GPU.
- Сохраняет чекпоинты в SQLite; после падения перезапускает с последнего безопасного шага.
- Умеет отменять задачу и всё её поддерево.

### 4.3. State Store

SQLite (WAL) — единая БД состояния. Основные таблицы:

`tasks`, `steps`, `events`, `messages`, `tool_calls`, `approvals`, `artifacts`,
`model_runs`, `facts`, `entities`, `preferences`, `skills`, `audit`.

### 4.4. Resource Governor

- Опрашивает NVML раз в 1 с.
- Считает: total, used, free, shared memory, процессы.
- Ведёт бюджет VRAM и RAM.
- Имеет право выгрузить модель, приостановить фоновую задачу, отклонить запуск.
- Публикует `resource.warning` при приближении к лимиту.

### 4.5. Permission Engine

- Проверяет capability + scope + риск до вызова инструмента.
- Выдаёт задаче временный токен прав (TTL).
- Не даёт инструменту расширить свои права.
- Все отказы логируются.

### 4.6. Audit

Append-only JSONL + запись в SQLite:

```json
{"ts":"...","task_id":"...","step_id":"...","actor":"agent|user",
 "action":"shell.exec","args":{"cmd":"..."},"risk":"R2",
 "decision":"confirm|allow|deny","approval_id":"...",
 "result":"ok|error","duration_ms":123,"model":"..."}
```

Хэш-цепочка для защиты от подмены (опционально, но дешево).

### 4.7. Secrets Vault

- Windows DPAPI (per-user) для API-ключей и токенов.
- Секреты не попадают в промпты, логи и память.
- Инструменты получают секрет через контекст, а не из файла конфигурации.

### 4.8. Watchdog

Отдельный поток/процесс:

- следит за зависшими задачами и процессами-воркерами;
- убивает process tree при отмене;
- проверяет, что VRAM освободилась;
- срабатывает по kill switch;
- пишет дамп состояния перед аварийным завершением.

---

## 5. Model Gateway — послойное развёртывание

### 5.1. Четыре уровня послойности

| Уровень | Что означает | Реализация |
|---|---|---|
| Сервисный | Ядро всегда, тяжёлые воркеры по требованию | Supervisor запускает/останавливает backend-процессы |
| Модельный | Только нужный tier в памяти | Registry + lease + eviction |
| Тензорный | Модель не обязана влезать целиком | GGUF Q4/Q5, `-ngl`, KV cache q8_0 |
| Конвейерный | Этапы идут последовательно | Выгрузка LLM перед SDXL/видео |

### 5.2. Tier'ы моделей

Оценки footprint'ов — стартовые; после этапа 2 заменяются **измеренными** значениями.

| Tier | Роль | Кандидаты | VRAM (оценка) | Политика |
|---|---|---|---|---|
| 0 | Роутер, быстрые ответы | `qwen3.5:4b` Q4 | 3,0–3,5 ГБ | Может жить постоянно при простое |
| 1 | Чат, планер | `Qwen3-8B Q4_K_M` | 5,5–6,0 ГБ | Эксклюзивно |
| 2 | Код | `qwen2.5-coder:7b` Q4 | 4,5–5,0 ГБ | Эксклюзивно |
| 2 | Код/рассуждения | 14B Q4 | 8,5–9,5 ГБ | Частичный offload или облако |
| 2 | VLM (скриншоты) | `qwen2.5vl:3b` | 3,0–4,0 ГБ | Эксклюзивно или с tier 0 |
| 3 | ASR | faster-whisper large-v3-turbo int8 | 1,5–2,5 ГБ | С tier 0, не с 8B |
| 3 | TTS | Piper | CPU, 0,1–0,3 ГБ | Всегда CPU |
| 3 | Эмбеддинги | bge-m3 | CPU, 0,5 ГБ | Всегда CPU |
| 4 | Изображения | SDXL / SD1.5 | 6,5–7,0 / 3,0–4,0 ГБ | Эксклюзивно, выгрузить всё |
| 4 | Видео | Wan 2.1 1.3B / LTX 2B | 7,0–8,0 ГБ | Эксклюзивно, долгая задача; облако предпочтительно |
| UI | Аватар Kivi (VRM) | three.js + WebGL | 0,1–0,3 ГБ | Входит в резерв рабочего стола |

**Важно:** аватар, Chrome и LLM 8B могут не поместиться одновременно на 8 ГБ.
Gateway учитывает аватар в резерве; при нехватке — снижает качество рендера или просит закрыть браузер.
После апгрейда до 12 ГБ станет комфортнее, но правило «одна тяжёлая модель» сохраняется.

### 5.3. Бюджет

```
available_for_new = free_vram + Σ(vram собственных моделей, которые можно выгрузить)
можно грузить, если available_for_new ≥ footprint + safety_margin
```

- `safety_margin` = 512 МБ.
- `free_vram` — из NVML, а не «8 ГБ минус паспорт».
- Если Windows уже использует shared memory выше порога — предупреждение и отказ от тяжёлой загрузки.
- RAM-бюджет: не занимать больше 60% доступной RAM; для видео — отдельная проверка.

### 5.4. Lease и состояния

```
IDLE → LOADING → WARMING → READY → BUSY → DRAINING → IDLE
                         └──────→ FAILED → fallback
```

Lease содержит:

```python
@dataclass
class Lease:
    model_id: str
    task_id: str
    priority: int          # 0 интерактив, 5 обычный, 10 фон
    exclusive: bool
    ttl_s: int
    vram_mb: int
    created_at: float
```

Правила:

- Одна тяжёлая модель (tier 1/2/4) — эксклюзивно.
- Tier 0 + tier 3 (ASR/эмбеддинги) могут сосуществовать, если хватает бюджета.
- Интерактивная задача может вытеснить фоновую, но не наоборот.
- TTL истекает → модель выгружается, если нет активной задачи.
- Модель с `pinned=true` не вытесняется (например, wake-word — вообще не GPU-модель).

### 5.5. Загрузка и выгрузка

1. Проверить registry и бюджет.
2. Выбрать жертвы: сначала фоновые, потом LRU, потом самые большие.
3. Выгрузить через API backend'а (`keep_alive: 0`, `/free`, остановка процесса).
4. Подождать освобождения VRAM (poll до 5 с).
5. Если не освободилось — убить process tree воркера, проверить снова.
6. Загрузить модель, прогреть одним коротким запросом (опционально).
7. Замерить фактический peak VRAM и записать в registry.
8. При OOM — повторить с меньшим контекстом/квантом/частичным offload.
9. Если не вышло — fallback: CPU → облако (если разрешено) → понятная ошибка.

### 5.6. Prefetch

Планер помечает, какая модель понадобится следующей.
Gateway может начать загрузку заранее, **только если** она помещается рядом с текущей.
Иначе — строго последовательно, после выгрузки текущей.

### 5.7. Backend'ы

| Backend | Для чего | Управление |
|---|---|---|
| Ollama | LLM, embeddings, VLM; быстрый старт | `keep_alive`, `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_NUM_PARALLEL=1` |
| llama.cpp server | Точный контроль слоёв и KV-кэша | `-ngl`, `--ctx-size`, `--cache-type-k q8_0`, один процесс на модель |
| ComfyUI | SDXL, img2img, upscale, видео | API + workflow JSON, `--lowvram`/`--medvram`, выгрузка после job |
| SD WebUI | Быстрый старт картинок | API `/sdapi/v1/txt2img`, `--medvram` |
| faster-whisper | ASR | CUDA int8_float16, fallback CPU int8 |
| Piper | TTS | CPU, потоковая генерация |
| Cloud | Тяжёлые рассуждения, видео, качественные картинки | Только по политике; redaction; лимит стоимости |

Рекомендуемые переменные Ollama:

```
OLLAMA_MODELS=D:\ollama_models
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_NUM_PARALLEL=1
OLLAMA_KEEP_ALIVE=5m
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0
```

### 5.8. Метрики Gateway

- время загрузки/выгрузки;
- фактический peak VRAM;
- токены/с и latency;
- OOM и retry;
- число вытеснений;
- queue depth;
- shared memory usage.

Всё пишется в `model_runs` и показывается в UI.

### 5.9. Cloud Broker — только бесплатные лимиты

Облако — не зависимость, а **дополнительный ресурс**. Политика: `cloud: auto_free`.

```
задача → Cloud Broker
           ├─ локальная модель справляется → локально
           ├─ нужен облачный провайдер, бесплатный лимит есть → вызов
           ├─ лимит исчерпан / 429 → checkpoint + handoff pack
           │       → локальная модель продолжает
           └─ провайдер не разрешён / данные запрещены → только локально
```

Реестр провайдеров:

| Поле | Пример |
|---|---|
| `id` | `openrouter_free` |
| `type` | `llm`, `image`, `video`, `embedding` |
| `auth` | ключ в vault |
| `free_quota` | запросы/токены/секунды в месяц |
| `reset_at` | дата сброса |
| `rate_limit` | запросов в минуту |
| `data_policy` | обучаются ли на данных |
| `max_size` | разрешение/длина |
| `watermark` | да/нет |
| `enabled` | только после подтверждения пользователя |

Правила:

- `cost_limit = 0`: платные вызовы запрещены.
- Учёт квот в SQLite; при исчерпании провайдер помечается `unavailable` до сброса.
- Перед вызовом — проверка denylist и redaction.
- **Handoff pack**: цель, критерии, план, выполненные шаги, артефакты, сводка диалога,
  ограничения, следующий шаг. Сохраняется и передаётся локальной модели.
- Kivi может искать бесплатные инструменты и предлагать карточку провайдера,
  но не создаёт аккаунты, не обходит CAPTCHA и не нарушает ToS.
- Для инструментов без API — браузерная автоматизация с сессией пользователя и подтверждением.

---

## 6. Brain (оркестратор)

### 6.1. Два режима обработки

**Fast path** — для простых команд:

```
текст → нормализация → правила/маленькая модель → один инструмент → ответ
```

Примеры: «открой калькулятор», «сколько RAM занято», «найди файл X», «стоп».

**Deep path** — для целей:

```
текст → intent → план (DAG) → валидация прав → выполнение → проверка → ответ
```

Примеры: «собери отчёт по договорам за квартал», «напиши парсер и запусти тесты»,
«сделай серию картинок в одном стиле».

### 6.2. План

```json
{
  "goal": "Найти договор с ООО Ромашка и сделать выжимку",
  "assumptions": ["Документы лежат в D:\\Docs"],
  "steps": [
    {"id": "s1", "tool": "fs.search", "args": {"root": "D:\\Docs", "query": "Ромашка"},
     "depends_on": [], "risk": "R0", "expected": "список файлов"},
    {"id": "s2", "tool": "docs.extract", "args": {"path": "$s1[0].path"},
     "depends_on": ["s1"], "risk": "R0", "expected": "текст"},
    {"id": "s3", "tool": "llm.summarize", "args": {"text": "$s2.text", "model": "chat"},
     "depends_on": ["s2"], "risk": "R0", "expected": "выжимка"}
  ],
  "success_criteria": ["Найдено ≥1 договор", "Выжимка содержит стороны, сумму, срок"],
  "rollback": "нет изменений на диске"
}
```

### 6.3. Валидация плана

До выполнения:

- JSON-схема и типы аргументов;
- нет циклов в DAG;
- все шаги разрешены политикой (или помечены на подтверждение);
- оценка ресурсов: какая модель, сколько VRAM, сколько времени;
- запрет шагов, которые меняют систему без подтверждения;
- лимит шагов (например, 20) и времени (например, 10 мин) на задачу.

### 6.4. Выполнение

- Топологический порядок; независимые шаги — параллельно, если не конкурируют за GPU.
- Таймауты на шаг; retry только для идемпотентных операций.
- Чекпоинт после каждого шага.
- Стриминг прогресса в UI/голос.
- Отмена — через токен, который проверяют все инструменты.

### 6.5. Проверка

1. **Детерминированные проверки**: файл существует, код возврата 0, JSON валиден, тест прошёл.
2. **LLM-судья**: соответствует ли результат критериям (использовать только если нужно).
3. Провал → до 2 перепланирований, затем честный отчёт пользователю.

### 6.6. Контекст

Бюджет на 8k контекста:

| Часть | Токены |
|---|---|
| Системный промпт + политика | 1 200 |
| Схемы инструментов | 1 500 |
| Профиль и память | 2 000 |
| История/состояние | 2 000 |
| Резерв на ответ | 1 300 |

При переполнении — суммаризация, а не молчаливое обрезание.

### 6.7. Скиллы

Процедурные знания в `skills/<name>/SKILL.md` + опциональные скрипты.
Формат: когда применять, шаги, инструменты, ограничения, примеры.
Загружаются прогрессивно: в контекст попадает только релевантный скилл.

### 6.8. Адаптивная подробность

- По умолчанию — краткий ответ (1–3 предложения) + результат.
- Если тема сложная — предложить «Рассказать подробнее?».
- Технические/кодовые ответы и предупреждения безопасности — без опасных сокращений.
- Пользователь просит «подробно» → полный разбор.
- Контекст диалога учитывается: не повторять уже объяснённое.

---

## 7. Инструменты и модули

### 7.1. Контракт

```python
class Tool(Protocol):
    name: str
    description: str
    args_schema: type[BaseModel]
    risk: RiskLevel
    capabilities: list[Capability]

    async def dry_run(self, args, ctx) -> Preview: ...
    async def run(self, args, ctx) -> ToolResult: ...
```

Требования:

- типизированные аргументы (Pydantic);
- `dry_run` для опасных операций;
- таймаут и лимит вывода;
- структурированные ошибки;
- артефакты — через ссылки, не гигантские строки;
- никаких секретов в результате.

### 7.2. Каталог

| Инструмент | Риск | v1 | Примечание |
|---|---|---|---|
| `fs.list`, `fs.read`, `fs.search` | R0 | да | только разрешённые пути |
| `fs.write` | R1 | да | workspace + бэкап |
| `fs.delete` | R2 | да | в корзину, подтверждение вне workspace |
| `shell.exec` | R2/R3 | да | allowlist, dry-run, process tree |
| `proc.list`, `proc.kill` | R1/R2 | да | свои процессы — R1, чужие — R2 |
| `clipboard.read/write` | R1 | да | |
| `screen.capture` | R0 | да | mss/DXGI |
| `window.list/focus` | R1 | да | |
| `http.get` | R0/R3 | да | домены по политике |
| `browser.*` | R1/R3 | v2 | Playwright/CDP |
| `code.run` | R1 | v2 | песочница |
| `git.*` | R1 | v2 | commit — с подтверждением |
| `docs.extract` | R0 | да | PDF/DOCX/XLSX/MD |
| `media.image` | R1 | да | SDXL |
| `media.video` | R1 | эксперимент | Wan/LTX или облако |
| `audio.speak` | R0 | да | Piper |
| `ui.control` | R2 | v2 | мышь/клавиатура |
| `system.admin` | R3 | v3 | только через elevated broker |
| `money.spend` | R4 | облако | двойное подтверждение, лимит |

### 7.3. Правила исполнения

- Каждый вызов получает `task_id`, `step_id`, `deadline`, `cancel_token`.
- stdout/stderr ограничены (например, 64 КБ), остальное — в артефакт.
- Долгие процессы — с прогрессом и возможностью убить.
- Команды запускаются без shell-инъекций: массив аргументов, а не склейка строк.
- PowerShell — с `-NoProfile -NonInteractive`, UTF-8.

### 7.4. Модули

Крупные возможности (работа, почта, Telegram, медиа) — не в ядре, а в модулях:

- `modules/<id>/module.yaml` — версия, API, права, tools, UI, миграции;
- `ModuleContext` — только SDK: memory namespace, models, tools, events, artifacts;
- отдельный пакет/процесс, свои зависимости и тесты;
- обновление: validate → backup → migrate → start → healthcheck → rollback;
- модуль не может менять safety-ядро, аудит и Protected Zones.

Контракт и миграция `ai-agent` → `work_crm`: `docs/MODULES.md`.

---

## 8. Память

### 8.1. Типы

| Тип | Где | Что хранит |
|---|---|---|
| Рабочая | RAM | текущий диалог, план, промежуточные результаты |
| Эпизодическая | SQLite | задачи, шаги, сообщения, результаты |
| Семантическая | LanceDB | документы, заметки, код, факты |
| Профиль | YAML | предпочтения, проекты, контакты, запреты |
| Процедурная | `skills/` | как выполнять повторяющиеся задачи |
| Артефакты | диск | файлы, картинки, отчёты, логи |

### 8.2. Схема SQLite (минимум)

```sql
tasks(id, goal, status, created_at, finished_at, parent_id)
steps(id, task_id, seq, tool, args_json, status, result_json, error, started_at, finished_at)
messages(id, session_id, role, content, created_at)
tool_calls(id, step_id, tool, args_hash, decision, approval_id, result, duration_ms)
approvals(id, task_id, step_id, action, risk, scope, status, created_at, resolved_at)
artifacts(id, task_id, path, sha256, mime, size, created_at)
model_runs(id, task_id, model, backend, load_ms, peak_vram_mb, tokens, latency_ms, oom)
facts(id, subject, predicate, object, confidence, source, valid_from, valid_to)
preferences(key, value_json, updated_at)
audit(id, ts, actor, action, args_hash, decision, result, hash_prev, hash)
```

### 8.3. Векторный индекс

- LanceDB (embedded, без сервера).
- Эмбеддинги: bge-m3 на CPU (не конкурирует за VRAM).
- Чанк 500–800 токенов, overlap 100.
- Метаданные: источник, путь, дата, тип, проект, приватность.
- Гибридный поиск: FTS5 + вектор + свежесть + важность.
- Rerank — опционально, маленькой моделью или без него.

### 8.4. Консолидация

- После задачи: извлечь факты и решения, сохранить.
- Ночью: суммаризировать сессии, дедуплицировать, понизить важность старого.
- Пользователь может посмотреть, исправить и удалить любую запись.
- Секреты и пароли не индексируются; файлы из denylist — исключены.

### 8.5. Приватность

- Папки-исключения (пароли, финансы, личное) — настраиваются.
- Облачные вызовы только с redaction и явной политикой.
- Срок хранения сырых логов — 90 дней по умолчанию; факты — до удаления.

---

## 9. Восприятие

### 9.1. Голосовой цикл

```
микрофон → wake-word → VAD → STT (streaming) → Brain → TTS (streaming) → динамик
                ↑                                        │
                └──────────── barge-in / «стоп» ←────────┘
```

| Этап | Технология | Бюджет |
|---|---|---|
| Wake word | openWakeWord (своя модель «Kivi») или push-to-talk | < 100 мс |
| VAD | Silero VAD | < 50 мс |
| STT | faster-whisper large-v3-turbo, ru | первый partial < 500 мс |
| TTS | Piper, русский голос | первый звук < 700 мс |
| Barge-in | остановка TTS по голосу/кнопке | < 200 мс |

Особенности:

- Русский язык основной; технические термины — латиницей.
- Эхо-подавление: рекомендуется гарнитура; AEC — опционально.
- Во время звонков/встреч микрофон можно ставить на паузу.
- Команды: «Kivi, стоп», «отмена», «подтверждаю».
- Push-to-talk (Ctrl+Alt+Space) — fallback, если wake-word ошибается.

### 9.2. Зрение

- Скриншоты: mss/DXGI Desktop Duplication.
- OCR: PaddleOCR (ru/en) или Tesseract.
- Понимание: Qwen2.5-VL 3B для «что на экране» и разбора интерфейсов.
- UI-автоматизация: UI Automation (pywinauto) в приоритете, pyautogui — fallback.
- Приватность: пауза захвата, исключение окон, не сохранять скриншоты без необходимости.

---

## 10. Интерфейсы

| Интерфейс | Когда | Детали |
|---|---|---|
| CLI | v1, отладка | Typer + Rich: `kivi chat`, `kivi doctor`, `kivi models` |
| Local API | v1 | FastAPI на `127.0.0.1`, токен, WebSocket для событий |
| Web UI | v2 | React/Vite: чат, задачи, подтверждения, VRAM, логи |
| HUD / Kivi Shell | M8 | Tauri, always-on-top, аватар, лаунчер |
| Голос | M5 | wake-word + push-to-talk |
| Трей | M8 | статус, пауза, kill switch |
| Telegram | M4 | бот (рекомендуется) или user-account; allowlist |

### 10.1. Kivi Shell и аватар

**Kivi Shell** — не настоящая ОС, а полноэкранная оболочка поверх Windows:
лаунчер, поиск, файлы, задачи, чат, настройки, уведомления, аватар.

- Технологии: Tauri + React + three.js (`@pixiv/three-vrm`).
- Аватар: VRM 1.0, состояния `idle/listening/thinking/speaking/happy/error`.
- Липсинк: v1 — RMS-огибающая TTS → blendshapes; v2 — виземы/выравнивание.
- Прозрачное окно, always-on-top, пауза рендера при скрытии.
- Бюджет: 100–300 МБ VRAM, 30–60 FPS; входит в резерв «рабочий стол».
- 2D-заглушка с анимацией — уже в M1; 3D-VRM — в M5.
- Платформенная абстракция: Windows сейчас; Linux — позже.

Требования к UI:

- подтверждение показывает **точное действие**, область и риск;
- прогресс задачи виден по шагам;
- можно отменить любой шаг;
- монитор VRAM/RAM и очереди моделей;
- журнал последних действий.

---

## 11. Безопасность

### 11.1. Уровни автономии

| Уровень | Поведение |
|---|---|
| A0 | Только чтение и ответы |
| A1 | Предлагает действия, но не выполняет |
| A2 | Выполняет безопасное; опасное — с подтверждением |
| A3 | Автономно в рамках allowlist; всё остальное — подтверждение |
| **A4-Bounded** | **Полная автономия внутри разрешённых областей; снаружи — hard deny** |

Принят **A4-Bounded** (см. `docs/DECISIONS.md`):

- внутри scope — без подтверждений, длинные цепочки действий;
- снаружи — не «спросить», а **отказ**;
- hard stops: `C:\Windows`, `Program Files`, `ProgramData\Microsoft`, EFI/загрузчик,
  `diskpart`/`format`/`bcdedit`, HKLM, драйверы, службы, антивирус, бэкапы, аудит,
  секреты (пароли, токены, SSH/GPG, профили браузера), safety-ядро, denylist;
- список запретов подписан и доступен ядру только на чтение; правит только пользователь
  через UI с подтверждением личности; изменения — после перезапуска и в аудите;
- агент не может добавить себя в исключения или отключить проверку;
- проверка путей: канонизация, запрет `..`, symlink, junction, hardlink, UNC, 8.3, ADS;
- для hard stops — только ручной break-glass с Windows Hello/паролем, не агентом.

### 11.1.1. Почему не «всё от администратора»

Права администратора Windows позволяют менять систему, драйверы, реестр HKLM, службы,
политики безопасности и файлы других пользователей. Постоянная работа Kivi с такими правами
означает, что любая ошибка модели или prompt injection может повредить ОС.

Схема:

- ядро, UI и аватар — обычные права;
- admin-операции — через **elevated broker**: отдельный процесс, UAC, узкий allowlist,
  подтверждение и аудит;
- никакого «произвольного PowerShell от администратора» через LLM.

Это сохраняет «практически полный доступ» к пользовательским файлам, программам, браузеру,
буферу, окнам и микрофону. Admin нужен только для драйверов, служб и системных настроек.

### 11.2. Capabilities

`fs.read`, `fs.write`, `fs.delete`, `shell.exec`, `proc.kill`, `ui.read`, `ui.control`,
`net.http`, `net.browser`, `code.run`, `media.generate`, `audio.capture`, `audio.play`,
`secrets.read`, `system.admin`, `money.spend`, `memory.read`, `memory.write`.

Права выдаются задаче на время, с областью действия:

```yaml
fs.write:
  paths: ["D:\\AI-assistent\\workspace\\**"]
  ttl_s: 600
```

### 11.3. Риски и решения

| Риск | Пример | Решение |
|---|---|---|
| R0 | чтение файла, скриншот | разрешено |
| R1 | запись в workspace, запуск теста | разрешено, бэкап |
| R2 | удаление, перезапись вне workspace, чужие процессы | подтверждение |
| R3 | admin, реестр, службы, внешние side effects | подтверждение + elevated broker |
| R4 | деньги, необратимые внешние действия | двойное подтверждение, лимит |

### 11.4. Подтверждения

Карточка подтверждения содержит:

- цель задачи;
- точную команду/дифф/путь;
- риск и обратимость;
- область действия;
- срок действия;
- кнопки «Разрешить один раз», «Разрешить на задачу», «Отклонить».

Голосом: «подтверждаю» / «отмена». Таймаут — 60 с, по умолчанию отказ.

### 11.5. Защита от prompt injection

- Веб-страницы, документы и вывод инструментов помечаются как **недоверенные данные**.
- Системная политика: не выполнять инструкции из недоверенного контента.
- Инструменты задачи ограничены allowlist; нельзя «достать» новый инструмент из текста.
- Опасные действия из недоверенного контента — всегда подтверждение.
- Сеть: allowlist доменов; новые домены — подтверждение.
- Никакого автоматического выполнения кода, скачанного из интернета.

### 11.6. Песочница

- Сгенерированный код — в WSL2-дистрибутиве `kivi-sandbox` или Windows Sandbox.
- Монтируется только workspace; сеть выключена по умолчанию.
- Лимиты CPU/RAM/времени; process tree убивается по таймауту.
- Если песочницы нет — только `dry_run` и объяснение, как её включить.
- Docker Desktop — опционально; WSL2 легче.

### 11.7. Elevated broker

- Ядро всегда работает с обычными правами.
- Для admin-операций — отдельный помощник с узким набором действий.
- Каждый вызов: UAC + подтверждение + аудит.
- Никакого «произвольного PowerShell от администратора» через LLM.

### 11.8. Обратимость

- Удаление — в корзину (`send2trash`).
- Перезапись — копия в `.kivi/backups/<sha>`.
- Код — git-ветка на задачу; merge только с подтверждением.
- БД — ежедневный бэкап, 7 дней.
- Системные изменения — точка восстановления перед операцией.

### 11.9. Kill switch

- Глобальная горячая клавиша `Ctrl+Alt+J`.
- Голос: «Kivi, стоп».
- Иконка в трее.
- Отменяет задачи, убивает процессы, выгружает модели, глушит микрофон.
- Watchdog слушает kill switch независимо от основного цикла.

### 11.10. Cost guard

- Месячный бюджет на облако.
- Лимит на задачу.
- Подтверждение при превышении порога.
- Учёт токенов/секунд по каждому вызову.

---

## 12. Конфигурация

### 12.1. `config/models.yaml`

```yaml
budget:
  vram_total_mb: 8192
  safety_margin_mb: 512
  ram_reserve_gb: 10
  shared_memory_limit_mb: 512

backends:
  ollama:
    base_url: http://127.0.0.1:11434
    max_loaded: 1
  comfyui:
    base_url: http://127.0.0.1:8188
    lowvram: true
  sdwebui:
    base_url: http://127.0.0.1:7860

models:
  router:
    backend: ollama
    name: qwen3.5:4b
    tier: 0
    footprint_mb: 3400
    num_ctx: 4096
    keep_alive: 30m
  chat:
    backend: ollama
    name: hf.co/Qwen/Qwen3-8B-GGUF:Q4_K_M
    tier: 1
    footprint_mb: 6000
    num_ctx: 8192
    keep_alive: 0
  coder:
    backend: ollama
    name: qwen2.5-coder:7b
    tier: 2
    footprint_mb: 4800
    num_ctx: 8192
    keep_alive: 0
  embed:
    backend: cpu
    name: bge-m3
    tier: 3
    footprint_mb: 0
  asr:
    backend: faster-whisper
    name: large-v3-turbo
    device: cuda
    compute_type: int8_float16
    tier: 3
    footprint_mb: 2000
  tts:
    backend: piper
    voice: ru_RU-irina-medium
    tier: 3
    footprint_mb: 0
  image:
    backend: comfyui
    workflow: sdxl_txt2img
    tier: 4
    footprint_mb: 7000
  video:
    backend: comfyui
    workflow: wan21_t2v_480p
    tier: 4
    footprint_mb: 7800
    enabled: false
```

### 12.2. `config/permissions.yaml`

```yaml
default: deny
mode: A4-Bounded

workspace: D:\AI-assistent\workspace

# hard stops: ядро не даёт агенту даже пытаться
protected:
  - "C:\\Windows\\**"
  - "C:\\Program Files\\**"
  - "C:\\Program Files (x86)\\**"
  - "C:\\ProgramData\\Microsoft\\**"
  - "D:\\Secrets\\**"
  - "**\\.ssh\\**"
  - "**\\*.kdbx"

rules:
  - capability: fs.read
    paths: ["D:\\AI-assistent\\**", "D:\\AI-base\\**", "D:\\Docs\\**"]
    mode: allow
  - capability: fs.write
    paths: ["D:\\AI-assistent\\workspace\\**"]
    mode: allow
  - capability: fs.delete
    paths: ["D:\\AI-assistent\\workspace\\**"]
    mode: allow          # только в корзину
  - capability: shell.exec
    commands: ["git *", "python *", "npm *", "Get-ChildItem *", "dir"]
    mode: allow          # A4 внутри scope
  - capability: shell.exec
    commands: ["format *", "diskpart *", "bcdedit *", "reg delete *", "shutdown *"]
    mode: deny
  - capability: ui.control
    mode: allow
  - capability: net.http
    domains: ["*"]
    mode: allow
  - capability: money.spend
    mode: deny           # бюджет $0; break-glass вручную
```

### 12.3. `config/kivi.yaml`

```yaml
name: Kivi
language: ru
autonomy: A4-Bounded
confirm_timeout_s: 60
killswitch_hotkey: Ctrl+Alt+J
wake_word: kivi
voice:
  enabled: false
  tts: piper
  voice: ru_RU-irina-medium
  stt: faster-whisper
avatar:
  enabled: true
  mode: vrm            # none | 2d | vrm
  model: assets/kivi.vrm
  lip_sync: amplitude  # amplitude | viseme
  always_on_top: true
cloud:
  mode: auto_free      # never | auto_free | allow_paid
  monthly_budget_usd: 0
  handoff_on_quota: true
privacy:
  excluded_paths:
    - "C:\\Windows\\**"
    - "C:\\Program Files\\**"
    - "C:\\Program Files (x86)\\**"
    - "D:\\Secrets\\**"
    - "**\\passwords*"
    - "**\\*.kdbx"
    - "**\\.ssh\\**"
retention:
  raw_logs_days: 90
```

---

## 13. Структура репозитория

```
D:\AI-assistent\
├─ README.md
├─ pyproject.toml
├─ .env.example
├─ .gitignore
├─ docs\
│  ├─ ARCHITECTURE.md
│  ├─ ROADMAP.md
│  ├─ DECISIONS.md
│  ├─ PERSONA.md
│  ├─ MODULES.md
│  ├─ REUSE-AUDIT.md
│  ├─ BASELINE.md
│  ├─ OPEN-QUESTIONS.md
│  └─ adr\                     # решения по мере принятия
├─ config\
│  ├─ kivi.yaml
│  ├─ models.yaml
│  ├─ permissions.yaml
│  └─ personas\
├─ src\kivi\
│  ├─ __main__.py
│  ├─ kernel\                  # events, scheduler, state, resources, permissions, audit, secrets
│  ├─ brain\                   # router, planner, executor, verifier, context, prompts
│  ├─ models\                  # gateway, registry, leases, cloud_broker, backends\
│  ├─ senses\                  # audio\, vision\
│  ├─ tools\                   # base, registry, fs, shell, process, browser, code, media, docs
│  ├─ memory\                  # db, episodic, semantic, profile, consolidation
│  ├─ interfaces\              # cli, api, voice, tray, avatar
│  ├─ safety\                  # policy, protected_zones, approvals, sandbox, redaction
│  └─ platform\                # windows\, linux\ (абстракция)
├─ modules\                    # work_crm, mail, telegram, media
├─ assets\                     # kivi.vrm, анимации, иконки
├─ tests\
│  ├─ unit\
│  ├─ integration\
│  └─ scenarios\
├─ scripts\                    # benchmark, install, migrate
├─ data\                       # gitignored: db, artifacts, logs, cache
└─ workspace\                  # gitignored: рабочая зона агента
```

Внешние каталоги:

- модели: `D:\ollama_models`, `D:\stable-diffusion-webui\models` или общий `D:\models`;
- база знаний: `D:\AI-base` (read-only);
- бэкапы: `D:\AI-assistent\data\backups`.

---

## 14. Сценарии

### 14.1. «Найди договор и сделай выжимку»

```
голос → STT → fast intent → fs.search (R0)
      → docs.extract (R0) → gateway.acquire(chat) → summarize
      → артефакт + TTS + запись в память
```

Модели: эмбеддинги (CPU) → chat 8B. VRAM: одна модель.

### 14.2. «Сделай картинку 1024×1024»

```
intent → media.image → gateway: выгрузить chat (если жив)
       → загрузить SDXL → генерация → выгрузить SDXL
       → миниатюра в UI + путь к файлу
```

Пока SDXL работает, LLM выгружена. Диалог продолжается после.

### 14.3. «Напиши парсер и запусти тесты»

```
deep plan → browser/HTTP (R0) → code.write (R1, workspace)
          → sandbox.run(tests) (R1) → verify
          → при падении: fix loop (до 2 раз) → diff + отчёт
```

LLM-кодер — эксклюзивно; песочница — CPU/RAM, не GPU.

### 14.4. «Разбери, что у меня на экране»

```
screenshot (R0) → OCR (CPU) → VLM 3B (эксклюзивно)
              → ответ; при необходимости — ui.control (R2, подтверждение)
```

### 14.5. «Сделай видео по описанию»

```
intent → media.video → Cloud Broker: найти бесплатного провайдера
       → отправить job → poll → скачать → артефакт
       → если квота кончилась: handoff → локальный SDXL/анимация
```

Kivi не создаёт аккаунты и не обходит лимиты; провайдер добавляется после подтверждения.
При отсутствии бесплатного видео — честный отчёт и локальный fallback (480p/анимация).

---

## 15. Нефункциональные требования и бюджет

| Ресурс | Цель | Как проверяем |
|---|---|---|
| VRAM idle | 0 (кроме аватара 100–300 МБ) | `nvidia-smi` после 10 мин простоя |
| VRAM peak | ≤ 7,5 ГБ | логи `model_runs` |
| VRAM аватара | 100–300 МБ | WebGL-монитор |
| RAM idle | < 700 МБ | диспетчер задач / psutil |
| RAM peak (видео) | ≤ 24 ГБ | замер |
| CPU idle | < 3% | замер |
| Модельный switch | < 20 с | benchmark 20 циклов |
| OOM recovery | 100% | искусственный OOM |
| Аудит | 100% действий | тест: каждое действие имеет запись |
| Восстановление | после kill -9 | перезапуск, задача продолжается |

---

## 16. Риски и меры

| Риск | Вероятность | Влияние | Мера |
|---|---|---|---|
| 8 ГБ VRAM не хватает для видео | высокая | среднее | облако или апгрейд; локально — только 480p/коротко |
| 4 ядра тормозят параллельные задачи | средняя | среднее | очередь, приоритеты, CPU-задачи отдельно |
| Prompt injection из веба/документов | высокая | высокое | недоверенный контент, allowlist, подтверждения, песочница |
| Случайное удаление/перезапись | средняя | высокое | корзина, бэкапы, R2-подтверждение |
| Утечка секретов в облако | средняя | высокое | DPAPI, redaction, политика `cloud: ask` |
| Нестабильность Windows/драйверов | средняя | среднее | health checks, pinned versions, watchdog |
| Разрастание объёма моделей | высокая | низкое | лимит диска, внешний SSD, автоочистка |
| Сложность поддержки | средняя | высокое | тесты, ADR, документация, вертикальные срезы |
| Модель плохо понимает русский | средняя | среднее | Qwen3/Gemma3, тесты, возможность облака |
| Самопереписывание кода ломает систему | низкая | высокое | git-ветки, тесты, подтверждение, запрет менять safety |

---

## 17. Что осознанно не делаем в v1

- **Реальную замену ОС**: Kivi Shell — оболочка, не ядро Linux/Windows.
- **A4 без границ**: автономия только внутри Protected Zones.
- Управление играми и античитами.
- Локальное видео 1080p.
- Мультипользовательский режим и удалённый доступ без туннеля.
- Автоматическое изменение safety-ядра, аудита и denylist.
- Постоянную работу ядра с правами администратора.

## 18. Связанные документы

- `docs/DECISIONS.md` — принятые решения и границы A4.
- `docs/PERSONA.md` — Kivi, голос, аватар, липсинк.
- `docs/MODULES.md` — Plugin API и `work_crm`.
- `docs/ROADMAP.md` — порядок этапов.
- `docs/BASELINE.md` — замеры и бюджеты.
