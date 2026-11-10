# Kivi — локальный ИИ-ассистент для Windows

> **Статус:** этап 3 — Cloud Broker, второй срез (v0.9). Работают `kivi doctor`,
> `kivi models`, `kivi ask`, `kivi embed`, `kivi cloud status`, `kivi cloud ask`,
> `kivi cloud scout`, `kivi cloud handoff`, реестр моделей, VRAM-бюджет
> (NVML + nvidia-smi fallback), lease/eviction, watchdog, SQLite, Event Bus,
> аудит и Policy Engine.
> Осталось: живой вызов с ключом, denylist/redaction перед облаком (этап 4).

## Быстрый старт

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev,gpu]"
.venv\Scripts\kivi.exe doctor
.venv\Scripts\kivi.exe config check
.venv\Scripts\kivi.exe ask "привет, кто ты?" --keep-alive 0
.venv\Scripts\kivi.exe ask "объясни коротко" --stream --keep-alive 0
.venv\Scripts\kivi.exe embed "local assistant" --keep-alive 0
.venv\Scripts\kivi.exe cloud status
.venv\Scripts\kivi.exe cloud scout
.venv\Scripts\kivi.exe cloud handoff
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests
```

## 1. Цель

Персональный ассистент уровня «Джарвис», но со своим характером:

- имя **Kivi**, женский русский голос, живая анимация при речи;
- понимает текст и голос, отвечает кратко и предлагает подробности;
- выполняет задачи на ПК: код, управление системой, браузер, почта/Telegram, файлы;
- пишет и запускает код в песочнице;
- генерирует изображения; видео — через бесплатные облачные сервисы;
- помнит контекст, проекты, предпочтения и историю задач;
- **не занимает всю VRAM**: модели загружаются послойно, по одной, и выгружаются;
- локальные модели в приоритете; облако — только бесплатные лимиты;
- в перспективе — псевдо-ОС (Kivi Shell) поверх Windows, позже — Linux.

## 2. Железо (замерено 08.11.2026)

| Компонент | Значение | Вывод |
|---|---|---|
| CPU | Intel Core i3-14100F, 4 ядра / 8 потоков | Хватит для ядра, TTS, эмбеддингов; CPU-инференс LLM — fallback |
| RAM | 31,8 ГБ | Достаточно; резерв 10 ГБ под ОС/браузер |
| GPU | NVIDIA RTX 3060 Ti, **8 ГБ VRAM** | Главное ограничение; планируется RTX 4070 12 ГБ |
| Диск C | 95,5 ГБ свободно | Не для моделей |
| Диск D | 126,6 ГБ свободно (на 08.11) | Планируется внутренний M.2 NVMe 1 ТБ |
| ОС | Windows 11 Pro 26200 | Windows Sandbox есть; WSL2 не установлен |
| Python | 3.10 (default) и 3.12 через `py` | Брать **3.12** |
| Ollama | установлена, модели на `D:\ollama_models` | База для LLM уже есть |

На момент замера **5,8 из 8 ГБ VRAM заняты** (Chrome, Telegram, NVIDIA Broadcast, Ollama).
Свободно ~1,8 ГБ. Kivi обязана считать бюджет по фактически свободной VRAM.

**Планируемый апгрейд:** GPU **RTX 4070 12 ГБ** (выбрана, ещё не куплена) + внутренний SSD M.2 1 ТБ.

**Важно:** ранее рассматривалась MSI RTX 3070, но у неё **8 ГБ VRAM**, а не 12 ГБ.
Пользователь отказался от неё в пользу RTX 4070 12 ГБ. Рекомендации — в `docs/BASELINE.md`, раздел 8.

## 3. Ключевая идея — «послойное развёртывание»

1. **Сервисы** — ядро и wake-word всегда живы; тяжёлые воркеры по требованию.
2. **Модели** — tier 0 (роутер 4B) → tier 1 (чат 8B) → tier 2 (кодер/VLM) → tier 3 (ASR/TTS/эмбеддинги) → tier 4 (SDXL/видео). Одна тяжёлая модель за раз.
3. **Тензоры** — GGUF Q4/Q5, частичный offload (`-ngl`), квантование KV-кэша.
4. **Конвейер** — LLM выгружена → SDXL загружена → генерация → выгрузка.

Model Gateway: lease, приоритеты, LRU-вытеснение, проверка освобождения VRAM через NVML,
fallback GPU → CPU → облако, watchdog. В простое — 0 МБ VRAM.

## 4. Безопасность: A4-Bounded

Пользователь выбрал **A4 — полную автономию**, но с жёсткими запретами. Реализуем так:

- внутри разрешённых областей Kivi действует без подтверждений и строит длинные цепочки;
- за пределами — **hard deny**, а не «спросить»;
- системные файлы, диски, загрузчик, реестр HKLM, драйверы, секреты, safety-ядро — запрещены всегда;
- список запретных мест редактирует только пользователь через UI; агент не может его изменить;
- ядро работает **без прав администратора**; для admin-задач — отдельный elevated broker с UAC;
- удаление — в корзину, перезапись — с бэкапом, всё — в аудит, есть kill switch;
- **защита от prompt injection**: текст не является полномочием, недоверенный контент
  проходит карантинного читателя, Policy Engine и egress-контроль.

Подробности: `docs/DECISIONS.md`, `docs/SECURITY.md`.

## 4.1. Голос и аватар

- Голос: **Silero `baya`** — локально, тёплый женский тембр. Улучшение: `v5_ru` 48 кГц,
  постобработка, паузы и ударения. Сэмплы: `assets/voice-samples/`
  (включая `silero_v5_baya_48k.wav` и `silero_v5_baya_48k_clean.wav`).
- Клонирование голоса жены — только после письменного согласия; план и запреты —
  `docs/VOICE-CLONING.md`.
- Аватар: 18 лет, 156 см, бирюзовые волосы и глаза, реалистичный аниме, без аксессуаров.
- Одежда: **вариант №5 Summer casual** — светло-мятный сарафан, короткий кардиган, сандалии.
- Делаем сами: VRoid Studio → Blender → VRM 1.0.
- Всегда на рабочем столе, отдельное окно, второй монитор, скрытие по запросу.

## 5. Приоритеты

1. Ядро, память, файлы/документы, TTS + анимация.
2. Код и песочница.
3. Управление ПК.
4. Браузер.
5. Почта/Telegram.
6. Голосовой ввод + 3D-аватар.
7. Модуль работы (CRM из `ai-agent`).
8. Облачные картинки/видео.
9. Kivi Shell (псевдо-ОС) + v1.0.

## 6. Документы

| Файл | Содержание |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Слои, Model Gateway, Cloud Broker, память, безопасность, структура |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Этапы, milestone'ы, критерии приёмки, первые 14 дней |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Принятые решения, A4-Bounded, admin, облако, приоритеты |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Модель угроз, prompt injection, egress, тесты |
| [`docs/SHELL.md`](docs/SHELL.md) | Kivi Shell: замена Explorer, паритет, fail-safe |
| [`docs/PERSONA.md`](docs/PERSONA.md) | Kivi: характер, голос, аватар, одежда, липсинк |
| [`docs/VOICE-CLONING.md`](docs/VOICE-CLONING.md) | Улучшение Silero, согласие, датасет и пайплайн клонирования голоса |
| [`docs/MODULES.md`](docs/MODULES.md) | Plugin API и миграция `ai-agent` в модуль `work_crm` |
| [`docs/REUSE-AUDIT.md`](docs/REUSE-AUDIT.md) | Что взять из существующих проектов |
| [`docs/BASELINE.md`](docs/BASELINE.md) | Замеры железа, моделей и idle-бюджеты |
| [`docs/OPEN-QUESTIONS.md`](docs/OPEN-QUESTIONS.md) | Открытые вопросы и шаблон ответа |

## 7. Принципы

1. **Local-first.** Локальные модели в приоритете; облако — бесплатный бонус.
2. **Одна тяжёлая модель за раз.** Leases, бюджеты, очередь, выгрузка.
3. **Безопасность вне модели.** Ограничения в ядре и ОС, а не в промпте.
4. **Всё логируется.** Действия, инструменты, модели, подтверждения.
5. **Обратимость.** Корзина, бэкапы, git, точки восстановления.
6. **Модульность.** Инструмент/модуль = контракт + права + версия + тесты.
7. **Наблюдаемость.** VRAM, RAM, латентность, токены/с, ошибки, стоимость.
8. **Сначала вертикальный срез.** Один сценарий от голоса до результата, потом расширение.

## 8. Границы v1

**Входит:** текст, TTS + анимация, файлы/документы, shell, код в песочнице, управление ПК,
браузер, почта/Telegram, память, SDXL, облачные бесплатные сервисы, web-UI, аудит, модуль работы.

**Не входит:** реальная замена ОС, полная автономия без hard stops, локальное видео 1080p,
мультипользовательский режим, автоматическое изменение safety-ядра.

## 9. Следующий шаг

1. ~~Выбрать голос и одежду.~~ Выбрано: Silero `baya`, одежда №5, Shell A, RTX 4070 12 ГБ,
   Gmail-черновики.
2. Сравнить `v5_ru` 48 кГц и очищенный вариант с `v4`; зафиксировать настройки TTS.
3. Подготовить концепт-арт Kivi (turnaround + портрет), затем VRM в VRoid/Blender.
4. ~~Зафиксировать baseline и собрать скелет.~~ Скелет собран (этап 1), baseline — частично.
5. ~~Реализовать Model Gateway и первый срез.~~ Сделано: `config/models.yaml`, registry,
   `kivi ask`, nvidia-smi-бюджет, lease/eviction, события `model.*`.
6. ~~Watchdog и метрики.~~ Сделано: `GatewayWatchdog`, сверка с `/api/ps`,
   `gateway.status()`, benchmark-стенд `scripts/bench_gateway.py`.
7. ~~Cloud Broker (этап 3): реестр, квоты, Scout, handoff.~~ Сделано: `config/cloud.yaml`,
   `config/cloud-candidates.yaml`, `QuotaExhausted` + SQLite-чекпойнты,
   `kivi cloud ask/scout/handoff`.
8. Этап 4: контракт `Tool`, Permission Engine, Protected Zones, Trust labels,
   Capability Broker, denylist/redaction перед облаком.
9. Живой облачный вызов с ключом из env и проверка провайдера.

## 10. Разработка и база знаний

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe scripts\kivi_context.py stage
.venv\Scripts\python.exe scripts\kivi_context.py search "Model Gateway"
.venv\Scripts\python.exe scripts\bench_gateway.py --fake --switches 20 --no-write
.venv\Scripts\python.exe scripts\build_knowledge_index.py
```

Документация открывается в Obsidian как vault: **Open folder as vault** →
`D:\AI-assistent\docs`, стартовая карта — `_INDEX.md`. Карта кода —
`_CODE-MAP.md`. Обе генерируются из docs/docstring'ов, не правь их вручную.

Агентные инструкции и skills лежат в `AGENTS.md` и `.opencode/skills/`.
