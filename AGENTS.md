# Kivi — инструкции для агента

## Что это

Kivi — локальный ИИ-ассистент для Windows 11 (Python 3.12, `src`-layout). Цель —
локальный first, облако только по явному разрешению и бюджету. Текущий этап —
Model Gateway (этап 2), ядро и CLI уже работают.

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
```

Для проверки используй `verify` (он пишет полный лог в `data/logs/`), а не
полные прогоны на каждый шаг. Перед коммитом — полный `pytest -q` и `ruff`.

## Карта кода

- `src/kivi/config.py` — Pydantic-конфиг, `extra="forbid"`, безопасные дефолты.
- `src/kivi/kernel/` — SQLite, Event Bus, JSONL-логи, аудит.
- `src/kivi/safety/` — Policy Engine, зоны, approvals; единственный источник
  разрешений на опасные действия.
- `src/kivi/models/` — registry, Ollama backend, VRAM budget, lease/eviction.
- `src/kivi/brain/` — планировщик и исполнение (этапы 4–5).
- `src/kivi/senses/`, `tools/`, `memory/`, `modules/`, `interfaces/`.
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

## Skills

`kivi-core`, `kivi-verify`, `kivi-gateway`, `kivi-security`, `kivi-docs`,
`kivi-obsidian`, `kivi-module`, `kivi-context`.
