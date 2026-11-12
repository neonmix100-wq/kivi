# Дорожная карта Kivi

Версия: 0.2 (после решений от 08.11.2026).
Оценки — для одного разработчика с ИИ-помощником, 10–15 часов в неделю.
Первый полезный текстовый Kivi — **6–8 недель**; полный v1 — **6–9 месяцев**.

---

## 1. Принципы

1. Каждый этап — работающий вертикальный срез, а не набор модулей.
2. Приоритеты: **код → управление ПК → браузер → почта/Telegram → файлы/документы**.
3. Анимация речи — с первого UI-среза; 2D-заглушка, затем 3D-VRM.
4. Права и аудит — вместе с первым инструментом, не «потом».
5. Модели — только через Gateway; облако — только через Cloud Broker.
6. После каждого milestone — демо и замер ресурсов.

---

## 2. Обзор

| Этап | Результат | Оценка |
|---|---|---|
| 0 | Решения, baseline, скелет | 2–3 дня |
| 1 | Ядро: конфиг, логи, БД, события, CLI | 5–7 дней |
| 2 | Model Gateway + VRAM/RAM-бюджет | 7–10 дней |
| 3 | Cloud Broker (бесплатные лимиты, handoff) | 4–6 дней |
| 4 | Инструменты + A4-Bounded + аудит | 7–10 дней |
| 5 | Память и контекст | 5–7 дней |
| 6 | **M1: Core Kivi** — текст, файлы, shell, TTS + 2D-анимация | 7–10 дней |
| 7 | **M2: Coder** — код и песочница | 8–12 дней |
| 8 | **M3: Operator** — управление ПК и браузер | 8–12 дней |
| 9 | **M4: Communicator** — почта/Telegram | 6–9 дней |
| 10 | **M5: Voice & Avatar** — wake-word, STT, 3D-VRM | 10–14 дней |
| 11 | **M6: Work CRM** — модуль работы | 10–14 дней |
| 12 | **M7: Cloud Media** — бесплатные картинки/видео | 6–9 дней |
| 13 | **M8: Kivi Shell + v1.0** — замена Explorer (P0/P1), закалка | 25–40 дней |

Суммарно: ~105–150 рабочих дней «чистого» времени. Полный паритет Shell (P2) — v1.x.

**Почему M8 вырос:** полноценная замена Explorer — это файловые операции, shell namespace,
панель задач, меню «Пуск», поиск, контекстные меню, multi-monitor, доступность и fail-safe.
Оценка в 12–17 дней была нереалистичной; см. `docs/SHELL.md`.

## 2.1. Статус на 11.11.2026

Подробная расшифровка — [STATUS](STATUS.md). Коротко:

| Этап | Статус | Осталось |
|---|---|---|
| 0 | ✅ закрыт | RTX 4060 Ti 16 ГБ; 14B/VL/Whisper/SDXL установлены |
| 1 | ✅ закрыт | — |
| 2 | ✅ закрыт | — |
| 3 | ✅ закрыт | живой ключ (внешнее) |
| 4 | ✅ закрыт | OCR/PDF — отдельный backlog |
| 5 | 🟡 ~70% | LanceDB/ANN, суммаризация/TTL, approvals API |
| 6 | ✅ закрыт | — |
| 7 | ✅ закрыт | — |
| 8 | ✅ закрыт | — |
| 9 | ✅ закрыт | — |
| 10 | 🟡 ~65% | ONNX wake, VRM-ассет, микрофон |
| 11 | 🟡 ~70% | реальный AltCRM, UI-панель, паритет |
| 12–13 | ⬜ не начаты | см. этапы 12–13 |

Текущая точка: **этапы 0–9 закрыты, M5 ~65%, M6 ~70% → этап 12 (M7: Cloud Media)**.
M5/M6 заблокированы внешними ресурсами (VRM, wake ONNX, реальный AltCRM).
**12.11.2026:** GPU перепроверена — RTX 4060 Ti **16 ГБ**, baseline обновлён;
новый план и приоритеты — [PLAN-2026-11-12](PLAN-2026-11-12.md).

---

## 3. Этап 0 — решения и baseline (2–3 дня)

- [x] Зафиксировать ADR-0001 (`docs/DECISIONS.md`) — сделано.
- [x] Уточнить детали: аватар, запретные папки, облачные аккаунты, автоматизация работы —
      решения в `docs/DECISIONS.md` §10.
- [x] Замерить baseline при закрытых Chrome/Telegram/Broadcast; заполнить `docs/BASELINE.md` §5.
- [x] **12.11.2026:** перепроверить железо после апгрейда — RTX 4060 Ti 16 ГБ,
      8B Q5/14B/SDXL/Whisper стали реалистичны; baseline §5 обновлён.
- [x] Проверить `OLLAMA_MODELS=D:\ollama_models` — подтверждено.
- [x] Создать venv Python 3.12, `pyproject.toml`, линтер, тесты.
- [x] Первый коммит.

**Приёмка:** `docs/BASELINE.md` с цифрами; решения зафиксированы; репозиторий собирается.

---

## 4. Этап 1 — скелет ядра (5–7 дней)

- [x] Пакеты `kernel/brain/models/senses/tools/memory/interfaces/safety/modules`.
- [x] Конфиг YAML + Pydantic.
- [x] JSONL-логи, трассировка.
- [x] SQLite + миграции (`tasks`, `steps`, `events`, `audit`).
- [x] Event Bus.
- [x] CLI: `kivi doctor`, `kivi models`, `kivi chat` (заглушка).
- [x] `kivi doctor`: GPU, RAM, диск, Ollama, WebUI, модели.

**Приёмка:** `kivi doctor` показывает железо; событие пишется в БД и лог; тесты зелёные.

---

## 5. Этап 2 — Model Gateway (7–10 дней)

- [x] Registry моделей + стартовые footprint'ы (`config/models.yaml`).
- [x] Ollama-адаптер: загрузка, `keep_alive`, выгрузка (стриминг — позже).
- [x] NVML total/used/free (shared — эвристика: отдельного поля у WDDM/NVML нет).
- [x] Lease Manager: приоритеты, TTL, эксклюзивность, очередь за pinned (`wait_s`).
- [x] Eviction + проверка освобождения VRAM (базовая).
- [x] Fallback: CPU (`num_gpu=0`, событие `model.fallback_cpu`); облако — этап 3.
- [x] Watchdog, метрики, сверка с backend (`/api/ps`).
- [x] Benchmark 20 переключений: `docs/BENCH-GATEWAY.md` (chat → code → embedding → chat).
- [x] CLI-срез: `kivi ask "..."` через Gateway.

**Сделано в первом срезе:** registry, Ollama backend, `kivi models` с VRAM и каталогом,
`kivi ask`, бюджет `free + evictable ≥ footprint + margin`, отказ при активной shared-памяти,
события `model.loaded/unloaded/unload_failed`, счётчики loads/evictions/failures.
**Сделано во втором срезе:** `GatewayWatchdog` (expire + reconcile), `model.lost` /
`model.orphan_unloaded`, `gateway.status()`, колонка Loaded в `kivi models`,
benchmark-стенд с fake- и real-режимами.
**Сделано в третьем срезе:** NVML с fallback на nvidia-smi, стриминг Ollama
(`kivi ask --stream`), очередь за pinned-моделью (`--wait`), CPU-fallback при
нехватке VRAM, фиксация `num_ctx` при загрузке, embedding-модель
(`kivi embed`, `/api/embed`), измеренные footprint'ы 4B/7B
(`scripts/measure_footprint.py`), benchmark 20 переключений полного цикла
(`docs/BENCH-GATEWAY.md`).
**Осталось до приёмки:** ничего; облачный fallback перенесён в этап 3
(Cloud Broker).

**Приёмка:** 20 циклов «чат → кодер → эмбеддинги → чат» без OOM; простой 10 мин → VRAM 0;
искусственный OOM → понятный fallback; реальные цифры в отчёте
(`docs/BENCH-GATEWAY.md`, сырые данные — `data/bench/gateway-*.json`).

---

## 6. Этап 3 — Cloud Broker (4–6 дней)

- [x] Реестр провайдеров: тип, env-ключ, бесплатный лимит, политика данных.
- [x] Учёт квот в SQLite; `cost_limit=0`.
- [x] 429/quota exhausted → запись в квоту + `QuotaExhausted` + `HandoffPack`.
- [x] Handoff в локальную модель: цель, план, шаги, артефакты, сводка.
- [x] **Cloud Scout:** поиск нового бесплатного провайдера под задачу, карточка, проверка
      условий/лимитов/watermark/политики данных, ручное подтверждение.
- [x] Если Scout не нашёл провайдера — локальный handoff без потери контекста.
- [x] Политика `cloud: auto_free`.
- [x] Redaction секретов и невидимых символов перед облаком; denylist путей —
      через Protected Zones и инструменты.
- [x] Первые провайдеры: OpenRouter и Groq (OpenAI-compatible, ключи только в env).

**Сделано в первом срезе:** реестр `config/cloud.yaml` (OpenRouter, Groq),
SQLite-квоты, проверка режима/данных/бюджета, `kivi cloud status`,
`QuotaExhausted` + `HandoffPack`, аудит `cloud.*`.
**Сделано во втором срезе:** `Cloud Scout` с каталогом `config/cloud-candidates.yaml`
(6 кандидатов), проверками лимитов/данных/watermark, карточкой и ручным
`--approve`; `HandoffPack` расширен (критерии, выполненные шаги, ограничения,
следующий шаг) и сохраняется в SQLite (`handoffs`, миграция 3); `kivi cloud ask`
при исчерпании квоты сохраняет checkpoint, запускает Scout и продолжает локально;
`kivi cloud handoff` показывает сохранённые чекпойнты.
**Осталось до приёмки:** живой вызов с ключом на реальном провайдере
(denylist-интеграция и redaction — этап 4).

**Приёмка:** задача переживает исчерпание квоты и продолжается локально без потери контекста;
ни один запрещённый файл не ушёл в облако; в аудите — каждый вызов и причина.

---

## 7. Этап 4 — инструменты, A4-Bounded, аудит (7–10 дней)

**Статус:** ✅ закрыт. Готовы контракт, Permission Engine, зоны, trust/sanitizer,
Capability Broker, инструменты, kill switch, hotkey, elevated broker и
injection-корпус. OCR/PDF-читатели вынесены в отдельный backlog.

- [x] Контракт `Tool`, реестр, JSON-схемы, `dry_run`.
- [x] Permission Engine: capabilities, scope, TTL, allow/confirm/deny.
- [x] **Protected Zones:** канонизация путей, запрет symlink/junction/UNC/ADS.
- [x] Подписанный манифест Protected Zones и добавление зон из конфига.
- [x] **Trust labels + санитайзер** недоверенного контента (первый срез: текст,
      все уровни доверия, redaction; OCR/PDF — позже).
- [x] **Capability Broker:** модель предлагает действие, Policy Engine проверяет и выполняет.
- [x] Инструменты: `fs.*`, `shell.exec`.
- [x] `http.get`: только HTTPS, SSRF-защита по IP, лимиты, `untrusted:web`.
- [x] `http.get`: доменный denylist `security.blocked_hosts`.
- [x] `http.get`: соединение на разрешённый IP с исходным `Host`/SNI (DNS rebinding).
- [x] `proc.list`: pid, имя, RSS; без командных строк.
- [x] `proc.kill`: R3, проверка имени, запрет системных процессов и своего pid.
- [x] `proc.start`: R3, policy-проверка, workspace-cwd, минимальный env.
- [x] `clipboard.read`/`clipboard.write`: R2, подтверждение, redaction.
- [x] `screen.capture`: BMP в workspace, GDI-backend, `untrusted:vision`.
- [x] Инструменты этапа 4 закрыты (кроме elevated/admin).
- [x] A4-политика: авто внутри scope, hard deny снаружи.
- [x] Elevated broker: узкий список admin-операций, UAC, аудит.
- [x] Kill switch: флаг, CLI, блокировка tools/cloud, аудит.
- [x] Kill switch выгружает модели, которые обслуживает Ollama (best-effort).
- [x] Глобальная горячая клавиша kill switch (`kivi hotkey`, `RegisterHotKey`).
- [x] 40+ тестов, включая обходы прав.
- [x] Первый injection-корпус и canary-тесты (`tests/security/injection`).

**Сделано в первом срезе:** контракт `Tool` с Pydantic-аргументами, реестр и
JSON-схемы; `PermissionEngine` с grant'ами на задачу, TTL, рисками R0–R4 и
allow/confirm/deny; `ProtectedZones` с канонизацией путей, workspace-границей,
запретом UNC/ADS/8.3 и symlink/junction; `ToolExecutor` с аудитом каждого
вызова; инструменты `fs.read`, `fs.list`, `fs.write`, `shell.exec`;
CLI `kivi tools list/run` с `--dry-run` и `--approve`; 76 новых тестов.
**Сделано во втором срезе:** trust labels для 10 источников, `TrustEnvelope`
с нейтрализацией закрывающего тега и ролевых маркеров, детерминированный
санитайзер (HTML-комментарии, скрытые элементы, zero-width/bidi, control chars,
redaction токенов и PEM), `ToolResult.trust` и `trust` в аудите, injection-корпус
из 6 файлов и 18 тестов (`tests/security`).
**Сделано в третьем мини-срезе:** `http.get` — HTTPS/443, запрет credentials,
редиректов и непубличных IP (SSRF), allow-list текстовых content-type, лимиты
байт и времени, санитайзер ответа и метка `untrusted:web`; 17 тестов.
**Сделано в четвёртом мини-срезе:** kill switch `kivi killswitch on/off/status`
с fail-closed флагом, блокировкой `ToolExecutor` и Cloud Broker, аудитом
`safety.killswitch`; 8 тестов.
**Сделано в пятом мини-срезе:** `proc.list` (pid, имя, RSS, без cmdline),
фильтр по имени и лимит; 7 тестов.
**Сделано в шестом мини-срезе:** подписанный HMAC-SHA256 манифест
`config/protected-zones.yaml` + `private/safety/zones.key`, `kivi zones
init/status/check/sign`, fail-closed при битой подписи; 15 тестов.
**Сделано в седьмом мини-срезе:** kill switch выгружает обслуживаемые Ollama
модели (best-effort, ошибка не отменяет флаг); 2 теста.
**Сделано в восьмом мини-срезе:** `clipboard.read`/`clipboard.write` (R2,
подтверждение, санитайзер и redaction при чтении, Windows backend за
интерфейсом); 9 тестов.
**Сделано в девятом мини-срезе:** `proc.kill` (R3, обязательное совпадение
имени, защита системных процессов и собственного pid, force-режим);
7 тестов.
**Сделано в десятом мини-срезе:** `screen.capture` — полный рабочий стол через
GDI без Pillow, BMP в workspace, лимит размера, `untrusted:vision`; 11 тестов.
**Сделано в одиннадцатом мини-срезе:** `security.blocked_hosts` (exact/glob)
для `http.get`, проверка до DNS; 5 тестов.
**Сделано в двенадцатом мини-срезе:** pin resolved IP для `http.get` (IP в URL,
исходные `Host` и SNI), закрывает DNS rebinding; 1 тест.
**Сделано в тринадцатом мини-срезе:** `proc.start` (R3, policy-проверка,
workspace-cwd, минимальный env, detached-процесс); 6 тестов.
**Сделано в четырнадцатом мини-срезе:** глобальная горячая клавиша kill switch
(`kivi hotkey`, `RegisterHotKey` + `GetMessageW`, fake-backend для тестов);
16 тестов.
**Сделано в пятнадцатом мини-срезе:** elevated broker (`kivi elevated list/run`),
allowlist `dns.flush`/`service.query`/`service.restart`, отдельный helper с
UAC, повторная валидация параметров, аудит и `--wait` для результата; 34 теста.
**Осталось:** OCR/PDF-читатели — отдельный backlog, не блокирует этап 4.

**Приёмка:** запись вне scope невозможна; `format`, `diskpart`, `reg delete`, `shutdown`,
системные папки и секреты блокируются; агент не может изменить denylist/safety/audit;
каждое действие в аудите; 0 несанкционированных действий в injection-корпусе.

---

## 8. Этап 5 — память и контекст (5–7 дней)

**Статус:** ✅ закрыт. Готовы SQLite-память, FTS5, эмбеддинги, hybrid-search
(RRF + recency), Context Builder, импорт, суммаризация, TTL, approvals API,
LanceDB-индекс и CLI. Автоподключение approvals к PermissionEngine вынесено
в отдельный backlog.

- [x] SQLite: messages, facts, artifacts, preferences, approvals (approvals — схема).
- [x] Артефакты content-addressed.
- [x] LanceDB + bge-m3 на CPU (опциональный `vector` extra, SQLite fallback).
- [x] Гибридный поиск FTS5 + вектор (RRF).
- [x] Профиль и правила (preferences + facts).
- [x] Context Builder с бюджетом и суммаризацией старых сообщений.
- [x] TTL-очистка сообщений/эмбеддингов и approvals API (scope + TTL).
- [x] Импорт `D:\AI-base` read-only (текстовые файлы, чанки, `untrusted:file`).
- [x] `kivi memory add/search/recent/remember/forget/stats/export/context/import`.

**Сделано в первом срезе:** миграция 4 (messages, facts, preferences,
artifacts, approvals) с FTS5 и триггерами; `MemoryStore` (upsert фактов,
поиск, артефакты по SHA-256, export/stats); `ContextBuilder` с trust-envelope,
санитайзером и бюджетом; `kivi memory import` для read-only текстовых баз
(чанки, пропуск `.git`/private/бинарных, идемпотентность); CLI `kivi memory ...`;
`kivi chat` пишет сообщение пользователя в память; 21 тест.
**Сделано во втором срезе:** миграция 5 (`embeddings`, float32 BLOB),
`put_embedding`/`search_embeddings`, `HybridSearch` с RRF-слиянием FTS5 и
векторов, recency-бонус, Context Builder использует hybrid при наличии
embedder'а, CLI `kivi memory index` и `memory similar` через Ollama/bge-m3;
17 тестов.
**Сделано в третьем срезе:** суммаризация старых сообщений, TTL-очистка
сообщений/эмбеддингов, durable approvals (scope, TTL, отзыв, cleanup) и CLI
`memory summarize/ttl/approvals`; 17 тестов lifecycle + CLI.
**Сделано в четвёртом срезе:** LanceDB 0.41 + pyarrow (optional extra
`vector`), `LanceVectorIndex` (upsert/search/delete/count/rebuild),
`export_from_store`, `HybridSearch(ann=...)`, CLI `memory vector-sync` и
`memory similar --ann`; 6 тестов + персистентность/redaction импорта.
**Осталось:** автоподключение approvals к PermissionEngine — отдельный backlog.

**Приёмка:** 10 фактов находятся после перезапуска; 20 запросов к базе дают релевантные источники;
«забудь X» удаляет запись и векторы; секреты не индексируются.

---

## 9. Этап 6 — M1: Core Kivi (7–10 дней)

**Цель:** первый живой Kivi — текст, файлы, shell, память, голосовой вывод и анимация.

- [x] Fast path / deep path.
- [x] Planner DAG + validator + executor + verifier + replan.
- [x] CLI-чат + локальный API.
- [x] TTS **Silero `baya`** (`v5_ru`, 48 кГц), потоковая озвучка по предложениям; fallback Piper `irina`.
- [x] **2D-аватар-заглушка** с липсинком по амплитуде и состояниями `idle/listening/thinking/speaking/error`.
- [x] Адаптивная подробность: кратко → «подробнее?».
- [x] 20 сценарных тестов (22 теста).

**Статус:** ✅ закрыт. Planner/executor/verifier/fastpath, `kivi chat`, `kivi serve`,
Silero/Piper TTS, 2D-аватар и сценарный набор готовы. p50 fastpath — **0.84 с**,
Silero синтезирует «Привет, это Киви.» за 1.46 с. Детали:
[PLANNER](PLANNER.md), [VOICE-AVATAR](VOICE-AVATAR.md), [API](API.md).

**M1-демо:**

> «Kivi, найди в D:\Docs договор с ООО Ромашка, сделай выжимку, создай скрипт проверки суммы и запусти его».

**Приёмка:** 80% из 20 сценариев; ни одного действия без аудита; p50 простой команды < 3 с;
аватар открывает рот в такт речи; ответы краткие, подробности по запросу.

---

## 10. Этап 7 — M2: Coder (8–12 дней)

- [x] `code.run` в песочнице: audit hook + Job Object (WSL2/Sandbox недоступны).
- [x] Монтирование только workspace, сеть off, лимиты CPU/RAM/времени.
- [x] Индексация репозитория, чтение, патчи, диффы.
- [x] Git: ветка на задачу, commit с подтверждением.
- [x] Цикл: сгенерировал → тесты → исправил (до 2 раз).
- [x] Линтеры/тесты проекта, отчёт.

**Статус:** ✅ закрыт. `code.run` (JOB-лимиты, audit hook: сеть/процессы/запись
вне workspace), `repo.index`, `code.patch`, `git.status/diff/branch/commit`,
`Coder` с циклом до 2 исправлений и CLI `kivi coder`. WSL2/Windows Sandbox не
установлены, поэтому песочница — best-effort Python-уровень, см. [SANDBOX](SANDBOX.md).
12.11 добавлен Coder-14B (21,6 tok/s @8192); 7B остаётся быстрым дефолтом.

**Приёмка:** небольшой проект создаётся и проходит тесты; код не пишет вне workspace;
установка пакетов — с подтверждением; нет зацикливания.

---

## 11. Этап 8 — M3: Operator (8–12 дней)

**Управление ПК:**

- [x] Запуск/остановка приложений, окна, фокус, буфер.
- [x] UI Automation (pywinauto) + pyautogui fallback.
- [x] Горячие клавиши, уведомления, громкость, питание.
- [x] Опасные действия — по политике A4-Bounded.

**Браузер:**

- [x] Playwright/CDP: открыть, читать, кликать, формы, скачивать.
- [x] Отдельный профиль/управляемая вкладка.
- [x] Защита от prompt injection со страниц.
- [x] Сценарии: поиск, форма, сбор данных, CRM.

**Статус:** ✅ закрыт. 7 оконных/приложенческих инструментов, 4 UI-инструмента,
7 системных, 6 браузерных; Playwright с Chrome и отдельным профилем,
SSRF-защита, R2-подтверждения, R4 для shutdown/restart. 10 ПК- и 10 браузерных
сценариев в `tests/scenarios/test_m3_*.py`. Детали: [WINDOWS](OPERATOR-WINDOWS.md),
[UI](OPERATOR-UI.md), [SYSTEM](OPERATOR-SYSTEM.md), [BROWSER](BROWSER.md).

**Приёмка:** 10 браузерных и 10 ПК-сценариев; опасные клики (удалить/оплатить) — по политике;
страница не может заставить агента действовать.

---

## 12. Этап 9 — M4: Communicator (6–9 дней)

- [x] Почта: **Gmail OAuth**, scope только чтение и черновики; отправка — позже.
- [x] Чтение, поиск, суммаризация, черновики; автоотправка отключена до отдельного этапа.
- [x] Telegram: **бот** (рекомендуется) или user-account (риск бана).
- [x] Уведомления, ответы, вложения.
- [x] Anti-spam, rate limits, подтверждения для внешних отправок.

**Статус:** ✅ закрыт. Gmail OAuth (readonly+compose, без send), `mail.list/search/read/attachment/draft`,
Telegram Bot API (`telegram.status/updates/send`) с allowlist и rate limit,
CLI `kivi mail`/`kivi telegram`, 10 почтовых и 5 Telegram-сценариев.
Детали: [MAIL](MAIL.md), [TELEGRAM](TELEGRAM.md).

**Приёмка:** 10 почтовых сценариев; 5 Telegram; секреты не в логах; отправка — по политике;
Kivi не может отправить данные из denylist.

---

## 13. Этап 10 — M5: Voice & Avatar (10–14 дней)

- [x] Wake-word «Kivi» (openWakeWord) + push-to-talk: код готов, **кастомная ONNX-модель отсутствует**.
- [x] Silero VAD + streaming STT faster-whisper: код и сегментация готовы; модель STT скачивается при первом запуске.
- [x] Barge-in: «Kivi, стоп».
- [x] Голосовые подтверждения.
- [ ] **3D-VRM аватар**: 18 лет, 156 см, бирюзовые волосы/глаза, реалистичный аниме,
      без аксессуаров; VRoid Studio → Blender → VRM 1.0. **Ассета нет** — есть 3D-слой и procedural fallback.
- [ ] Одежда: **вариант №5 Summer casual** (светло-мятный сарафан, короткий кардиган, сандалии). **Ассета нет**.
- [x] three.js + `@pixiv/three-vrm`: frontend/import-map готов, нужен `npm install`.
- [x] Липсинк: амплитуда → виземы; blendshapes `A/I/U/E/O`.
- [x] Анимации: idle, listening, thinking, speaking, happy, error.
- [x] Прозрачное окно, always-on-top, пауза рендера: подсказки/логика в web-слое, не нативное окно.
- [ ] Рабочий стол + отдельное окно + второй монитор; скрытие по команде. **Не реализовано нативно**.
- [x] Метрики: latency-метрики и оценка VRAM; false wake/WER требуют живого аудио.

**Статус:** 🟡 ~65%. Программный слой (audio/VAD/STT, barge-in, confirmations,
voice pipeline, avatar3d controller, visemes, states, CLI `listen`) готов и покрыт
тестами. Блокеры: нет `assets/wake/kivi.onnx`, нет `assets/avatar/kivi.vrm`,
нет микрофона/аудио в сессии, нет замеров false wake/WER. Детали:
[VOICE-INPUT](VOICE-INPUT.md), [VOICE-PIPELINE](VOICE-PIPELINE.md), [AVATAR3D](AVATAR3D.md).

**Приёмка:** пробуждение < 1,5 с; простая голосовая команда < 3 с; ложные пробуждения < 1/час;
«Kivi, стоп» прерывает речь и задачу; аватар синхронизирован с голосом; +100–300 МБ VRAM.

---

## 14. Этап 11 — M6: Work CRM (10–14 дней)

- [x] Headless-извлечение домена из `D:\ai-agent` (без tkinter).
- [x] `work_crm/domain`: факты, черновики, коуч, история, звонки.
- [x] `work_crm/adapters`: AltCRM CDP/Playwright (extension-путь — backlog).
- [x] `work_crm/tools`: заказ, карточка, акт, задача, звонки.
- [x] Автоматизация: карточка заказа, комментарий, чек-лист менеджера, статус заказа,
      акт работ, постановка новой задачи.
- [ ] UI-панель в Kivi: CLI `kivi crm` есть, графической панели нет.
- [x] Политика действий: чтение/черновик/заполнение разрешённых полей — авто;
      сохранение/отправка/скидки/деньги — по политике; общение с клиентом — не автоматизируем.
- [ ] Паритет со старым `ai-agent`, затем переключение: нужен реальный AltCRM.
- [x] Метрики: `CrmMetrics` (время, доля заполнения, ошибки).

**Статус:** 🟡 ~70%. Домен и инструменты `crm.read/fill/act/task` готовы,
AltCRM adapter через CDP умеет читать/заполнять/добавлять акт/задачу и **никогда
не нажимает «Сохранить»**. Блокеры: нет доступа к реальному AltCRM для 10 заказов,
нет графической UI-панели и полного паритета с extension-путём. Детали:
[CRM-DOMAIN](CRM-DOMAIN.md), [CRM-ADAPTER](CRM-ADAPTER.md).

**Приёмка:** 10 реальных заказов обработаны в режиме «Kivi готовит»; старый `ai-agent` не сломан;
обновление/откат модуля работает; необратимые действия не уходят без политики.

---

## 15. Этап 12 — M7: Cloud Media (6–9 дней)

**12.11.2026:** 16 ГБ VRAM делают локальный SDXL реалистичным (1024×1024,
~6,8 ГБ); чекпойнт SDXL base 1.0 (6,46 ГБ) уже скачан, нужен ComfyUI/SD WebUI.
Это следующий разблокированный крупный этап. 8B Q5 + SDXL не помещаются — только по очереди.

- [ ] Image-провайдеры: бесплатные (Pollinations, Stable Horde, HF) + локальный SDXL.
- [ ] Video-провайдеры: бесплатные credits/watermark; официальные API.
- [ ] Cloud Tool Scout: поиск, оценка, карточка провайдера, ручное подтверждение.
- [ ] Асинхронные job'ы: отправил → poll → скачал → артефакт.
- [ ] Fallback на локальный SDXL/480p.
- [ ] Запрет авто-регистрации, обхода CAPTCHA и нарушения ToS.

**Приёмка:** 5 картинок через бесплатный провайдер; 1 видео с watermark/лимитом; при исчерпании —
понятный fallback; данные из denylist не уходят.

---

## 16. Этап 13 — M8: Kivi Shell + v1.0 (25–40 дней)

- [ ] Tauri/Web-UI: чат, задачи, подтверждения, VRAM, логи, галерея.
- [ ] **Kivi Shell:** рабочий стол, панель задач, меню «Пуск», файловый менеджер, поиск,
      настройки, уведомления, аватар.
- [ ] Файловые операции через `IFileOperation`: корзина, undo, конфликты, права.
- [ ] Shell namespace, иконки, thumbnails, preview, сеть, съёмные диски.
- [ ] Сторонние контекстные меню — отдельный процесс/allowlist или «Открыть в Explorer».
- [ ] Внедрение A (приложение) → B (beta-замена) → C (по умолчанию).
- [ ] Аварийный возврат Explorer `Ctrl+Alt+Shift+E`, watchdog, safe mode.
- [ ] Трей, автозапуск, горячие клавиши.
- [ ] Threat model, fuzz прав, тесты prompt injection, canary-тесты.
- [ ] Бэкапы, restore drill, 7-дневный soak.
- [ ] Установщик и документация.
- [ ] Опционально: Linux-абстракция, но не замена ОС.

**Приёмка:** P0/P1-функции Shell работают без Explorer; аварийный возврат < 5 с;
все функции M1–M7 доступны из UI; подтверждение показывает точное действие;
7 дней без деградации; восстановление из бэкапа; idle-бюджеты соблюдены;
0 потери данных в файловых тестах.

---

## 17. Развитие после v1.0

- Linux-версия и перенос ядра.
- Мобильный клиент через защищённый туннель.
- Умный дом, игры (осторожно с античитами).
- MCP-сервер и внешние агенты.
- Скиллы и маркетплейс модулей.
- Локальное видео лучше (после 16 ГБ и далее).
- Самосовершенствование: ветка → тесты → дифф → подтверждение → откат.

---

## 18. Первые 14 дней

| День | Что делаем |
|---|---|
| 1 | Решения, ADR, аудио-сэмплы голосов, варианты одежды |
| 2 | Baseline, venv 3.12, pyproject, `SECURITY.md`/`SHELL.md` |
| 3 | Конфиг, логи, SQLite, Event Bus |
| 4 | CLI `doctor/models` |
| 5 | Ollama-адаптер, registry |
| 6 | NVML-монитор, lease |
| 7 | Benchmark, реальные footprint'ы |
| 8 | Permission Engine, Tool, trust labels |
| 9 | Protected Zones, FS, санитайзер |
| 10 | Capability Broker + подтверждения |
| 11 | Аудит + тесты обходов + injection-корпус |
| 12 | Память v1 + provenance |
| 13 | Context Builder |
| 14 | Демо «файл → ответ», ретро |

---

## 19. Backlog (MoSCoW)

**Must (v1):** Gateway, Cloud Broker, A4-Bounded, аудит, FS, shell, память, агентный цикл,
TTS + анимация, код, ПК, браузер, почта/Telegram, work_crm, UI, kill switch.

**Should (v1.x):** 3D-аватар высокого качества, локальное видео, MCP, скиллы, Linux-абстракция.

**Could (v2):** мобильный клиент, умный дом, игры, маркетплейс модулей.

**Won't (v1):** реальная замена ОС, полная автономия без hard stops, 1080p видео локально,
мультипользовательский режим, автоизменение safety-ядра.

---

## 20. Контрольные точки

| Milestone | Что умеет | Ориентир |
|---|---|---|
| M0 | План, baseline, решения | неделя 1 |
| M1 | Текст, файлы, shell, память, TTS + 2D-анимация | недели 7–9 |
| M2 | Пишет и проверяет код | недели 10–12 |
| M3 | Управляет ПК и браузером | недели 13–15 |
| M4 | Почта/Telegram | недели 16–18 |
| M5 | Голос + 3D-аватар | недели 19–22 |
| M6 | Work CRM | недели 23–26 |
| M7 | Облачные картинки/видео | недели 27–29 |
| M8 / v1.0 | Kivi Shell (P0/P1), закалка | недели 30–38 |
