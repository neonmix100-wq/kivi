# Kivi — инструкции для агента

## Что это

Kivi — локальный ИИ-ассистент для Windows 11 (Python 3.12, `src`-layout). Цель —
локальный first, облако только по явному разрешению и бюджету. Текущий этап —
Model Gateway (этап 2), ядро и CLI уже работают.

Актуальный статус и backlog: `docs/STATUS.md`; план — `docs/ROADMAP.md`.

## Сначала контекст, потом код

Не читай `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, `docs/DECISIONS.md`,
`docs/VOICE-CLONING.md` целиком. Это 100+ КБ. Используй:

```powershell
.venv\Scripts\python.exe scripts\kivi_context.py stage
.venv\Scripts\python.exe scripts\kivi_context.py search "lease manager"
.venv\Scripts\python.exe scripts\kivi_context.py section ARCHITECTURE "Model Gateway"
.venv\Scripts\python.exe scripts\kivi_context.py map
.venv\Scripts\python.exe scripts\kivi_context.py index
```

`docs/_INDEX.md` — карта всей документации, `docs/_CODE-MAP.md` — карта кода.
Оба файла генерируются: после правок docs/docstring'ов запусти
`python scripts/build_knowledge_index.py`.

## Команды

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest -q tests\unit\test_gateway.py
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe scripts\kivi_context.py verify --path tests\unit\test_gateway.py
.venv\Scripts\python.exe scripts\bench_gateway.py --switches 20
.venv\Scripts\python.exe scripts\bench_gateway.py --fake --switches 20 --no-write
.venv\Scripts\python.exe scripts\measure_footprint.py qwen2.5-coder:7b --context 8192
.venv\Scripts\kivi.exe doctor
.venv\Scripts\kivi.exe models
.venv\Scripts\kivi.exe ask "вопрос" --keep-alive 0
.venv\Scripts\kivi.exe embed "text" --keep-alive 0
.venv\Scripts\kivi.exe cloud status
.venv\Scripts\kivi.exe tools list
.venv\Scripts\kivi.exe tools run fs.read --args '{"path":"README.md"}'
.venv\Scripts\kivi.exe tools run shell.exec --args '{"argv":["python","-V"]}' --dry-run
.venv\Scripts\kivi.exe killswitch status
.venv\Scripts\kivi.exe elevated list
.venv\Scripts\kivi.exe hotkey --check
.venv\Scripts\kivi.exe memory stats
.venv\Scripts\kivi.exe memory index -m bge-m3
.venv\Scripts\kivi.exe memory similar "тёплый город" -m bge-m3
.venv\Scripts\kivi.exe memory vector-sync -m bge-m3
.venv\Scripts\kivi.exe chat "прочитай README.md"
.venv\Scripts\kivi.exe coder "создай hello world с тестом"
.venv\Scripts\kivi.exe browse https://example.com
.venv\Scripts\kivi.exe mail status
.venv\Scripts\kivi.exe telegram status
.venv\Scripts\kivi.exe tools run window.list --args '{"limit":10}'
.venv\Scripts\kivi.exe tools run system.volume.get
.venv\Scripts\kivi.exe tools run code.run --args '{"code":"print(2+2)"}' --approve
.venv\Scripts\kivi.exe tools run repo.index --args '{"action":"summary"}'
.venv\Scripts\kivi.exe say "Привет, это Киви."
.venv\Scripts\kivi.exe serve --port 8765
.venv\Scripts\kivi.exe zones status
```

`cloud mode auto_free`: только бесплатные провайдеры и общедоступные данные.

`cpu_fallback: true` в `config/kivi.yaml` разрешает CPU-загрузку, когда VRAM не хватает.

Для проверки используй `verify` (он пишет полный лог в `data/logs/`), а не
полные прогоны на каждый шаг. Перед коммитом — полный `pytest -q` и `ruff`.

## Карта кода

- `src/kivi/config.py` — Pydantic-конфиг, `extra="forbid"`, безопасные дефолты.
- `src/kivi/kernel/` — SQLite, Event Bus, JSONL-логи, аудит.
- `src/kivi/safety/` — Policy Engine, зоны, approvals; единственный источник
  разрешений на опасные действия. `zones.py` — Protected Zones, `permissions.py` —
  grant'ы, TTL и риски R0–R4, `trust.py` — метки и envelope, `sanitize.py` —
  очистка недоверенного текста и redaction секретов, `zone_manifest.py` —
  подписанный HMAC-манифест дополнительных зон, `elevated.py` — allowlist
  admin-операций через UAC.
- `src/kivi/tools/` — контракт `Tool`, реестр, `ToolExecutor` (Capability Broker),
  `fs.*`, `shell.exec`, `http.get`, `proc.*`, `clipboard.*`, `screen.capture`,
  `code.run` (audit-песочница), `repo.index`, `code.patch`, `git.*`,
  `window.*`, `app.*`, `ui.*`, `system.*`, `browser.*`, `mail.*`, `telegram.*`.
  Каждый вызов идёт через executor и пишется в аудит.
- `src/kivi/integrations/` — Gmail OAuth и Telegram Bot API; секреты только
  в env/приватных файлах, без логов.
- `src/kivi/models/` — registry, Ollama backend, VRAM budget, lease/eviction.
- `src/kivi/memory/` — SQLite-память (messages/facts/preferences/artifacts,
  FTS5), эмбеддинги + hybrid-search (RRF), LanceDB-индекс, retention/суммаризация/TTL,
  approvals, content-addressed артефакты и Context Builder с бюджетом.
- `src/kivi/brain/` — planner DAG, validator, executor с approvals/replan,
  verifier, fastpath, `KiviBrain` и `Coder` (тест-цикл).
- `src/kivi/senses/` — Silero/Piper TTS и WAV; `src/kivi/interfaces/` — CLI,
  локальный API и 2D-аватар.
- `tests/unit/` — быстрые тесты без Ollama и сети.

## Инварианты

1. **Local-first.** Облако не используется без явного разрешения и бюджета.
2. **Одна тяжёлая модель.** Загрузка идёт только через `ModelGateway`.
3. **Safety не обходим.** Не ослабляй `PolicyEngine`, Warden, approval-правила
   и запреты на секреты. Не добавляй обходные пути «для теста».
4. **Аудит обязателен.** Значимые действия пишут событие/запись аудита.
5. **Секреты не читаем.** `.env`, ключи, токены — не читать, не печатать,
   не коммитить. Для конфигов используй `*.example`.
6. **Почта только черновики.** `auto_send=false`, отправка запрещена.
7. **Конфиг строгий.** Новые поля — с дефолтами и валидацией; неизвестные ключи
   должны падать, а не игнорироваться.

## Стиль

- Python 3.12, type hints, `from __future__ import annotations`.
- Строки ≤ 100, `ruff` обязателен.
- Публичные функции — с коротким docstring; сложные решения — комментарий.
- Тесты: pytest, без сети; для Ollama/GPU — fake-объекты.
- Коммиты: `feat:`, `fix:`, `docs:`, `test:`; один логический шаг на коммит.

## Obsidian

Vault — сама папка `docs/`. Открой её в Obsidian: **Open folder as vault** →
`D:\AI-assistent\docs`. Начни с `_INDEX.md`; граф и обратные ссылки работают
без копирования файлов. Подробнее — skill `kivi-obsidian`.

## GitHub

Репозиторий: приватный, создаётся скриптом `scripts/publish_github.ps1`.

- После каждой масштабной доработки и по прямой просьбе пользователя:
  сделать коммит и запустить публикацию.
- Перед push проверить `git status`: секреты, `.env`, `data/`, `private/`,
  `voice-dataset/` и модели не должны попадать в коммит.
- Команда:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\publish_github.ps1
```

## Skills

`kivi-core`, `kivi-verify`, `kivi-gateway`, `kivi-cloud`, `kivi-security`,
`kivi-docs`, `kivi-obsidian`, `kivi-module`, `kivi-context`.
