# Kivi — локальный ИИ-ассистент для Windows

> **Статус:** этапы 0–9 закрыты, M5 ~99%, M6 100%, M7 ~80%, M8 100% (P0/P1/P2)
> в 0.21-кандидате. Железо
> перепроверено 12.11.2026: **RTX 4060 Ti 16 ГБ**, 8B Q4/14B/SDXL/Whisper
> стали реалистичны. Работают `kivi doctor`, `kivi models`, `kivi ask`,
> `kivi chat`, `kivi serve`, `kivi say`, `kivi voice design`, `kivi coder`,
> `kivi browse`, `kivi mail`, `kivi telegram`, `kivi listen`, `kivi crm`,
> `kivi embed`, `kivi cloud status/ask/scout/handoff`, `kivi tools list/run`,
> `kivi memory`, `kivi elevated`, `kivi killswitch`, `kivi hotkey`, `kivi zones`,
> реестр моделей, VRAM-бюджет, lease/eviction, watchdog, SQLite, Event Bus,
> аудит, Policy Engine, Protected Zones, risk-политика R0–R4, trust labels,
> санитайзер, kill switch, elevated broker, память с FTS5/hybrid/LanceDB,
> M1 planner/TTS/2D-аватар, M2 Coder, M3 Operator, M4 Communicator, M6 Work CRM
> и Qwen3-TTS VoiceDesign/Base голос Kivi; **Fish Audio S2.1 Pro Free + Astrea/s
> сейчас основной голос**. Есть импорт голосов из файлов/URL и `media.image`.
> Новый план: [`docs/PLAN-2026-11-12.md`](docs/PLAN-2026-11-12.md).
> Осталось: M7 настоящий text-to-video; M8 P0/P1/P2 закрыт, M6 — 100%.
> Подробный статус и backlog: [`docs/STATUS.md`](docs/STATUS.md).

## Запуск интерфейса

Дважды щёлкни **`Kivi UI.bat`** в корне проекта — откроется отдельное окно
Kivi (Edge/Chrome app mode) и иконка в трее. Повторный запуск откроет уже
работающий экземпляр; для второго экземпляра используй `--new-instance`.

Ярлык на рабочий стол:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\create_desktop_shortcut.ps1
```

Если в футере старая версия, запусти **`KiviRestart.bat`** (без пробела в имени):

```powershell
cd D:\AI-assistent
.\KiviRestart.bat
```

Он закроет процесс на `8765` и запустит текущий код.

Или вручную:

```powershell
cd D:\AI-assistent
.venv\Scripts\kivi.exe ui
```

3D-аватар отдельно:

```powershell
.venv\Scripts\kivi.exe avatar --port 8766
```

Или двойной клик по **`Kivi Avatar.bat`**.

Для Fish Audio нужен ключ в окружении:

```powershell
$env:FISH_AUDIO_API_KEY = "твой-ключ"
```

## Быстрый старт

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev,gpu,ui,voice,video]"
.venv\Scripts\kivi.exe doctor
.venv\Scripts\kivi.exe config check
.venv\Scripts\kivi.exe ask "привет, кто ты?" --keep-alive 0
.venv\Scripts\kivi.exe ask "объясни коротко" --stream --keep-alive 0
.venv\Scripts\kivi.exe embed "local assistant" --keep-alive 0
.venv\Scripts\kivi.exe cloud status
.venv\Scripts\kivi.exe cloud scout
.venv\Scripts\kivi.exe cloud handoff
.venv\Scripts\kivi.exe tools list
.venv\Scripts\kivi.exe tools run fs.read --args '{"path":"README.md"}'
.venv\Scripts\kivi.exe killswitch status
.venv\Scripts\kivi.exe elevated list
.venv\Scripts\kivi.exe memory remember city Новосибирск --source user
.venv\Scripts\kivi.exe memory context Новосибирск
.venv\Scripts\kivi.exe memory stats
.venv\Scripts\kivi.exe memory vector-sync -m bge-m3
.venv\Scripts\kivi.exe chat "прочитай README.md"
.venv\Scripts\kivi.exe coder "создай hello world с тестом"
.venv\Scripts\kivi.exe browse https://example.com
.venv\Scripts\kivi.exe mail status
.venv\Scripts\kivi.exe mail list -n 5
.venv\Scripts\kivi.exe telegram status
.venv\Scripts\kivi.exe listen --mic --push-to-talk
.venv\Scripts\kivi.exe crm read
.venv\Scripts\kivi.exe crm fill --field "Согласованные сроки ремонта=2 недели"
.venv\Scripts\kivi.exe tools run window.list --args '{"limit":10}'
.venv\Scripts\kivi.exe tools run system.volume.get
.venv\Scripts\kivi.exe say "Привет, это Киви." --play
.venv\Scripts\kivi.exe voice design --out assets\voice-samples\kivi_voice_warm.wav
.venv\Scripts\kivi.exe say --engine qwen "Привет! Я Киви." --play
.venv\Scripts\kivi.exe say --engine fish "Привет! Я Киви." --play
.venv\Scripts\kivi.exe chat "Привет! Как дела?" --speak
.venv\Scripts\kivi.exe audio devices
.venv\Scripts\kivi.exe audio test --device 6
.venv\Scripts\kivi.exe voice add --file my_voice.wav --name my-voice --consent --transcript "Привет, это Киви."
.venv\Scripts\kivi.exe voice list
.venv\Scripts\kivi.exe media image "kivi portrait, anime style" --width 1024
.venv\Scripts\kivi.exe media sdxl "kivi portrait, anime style" --width 1024
.venv\Scripts\kivi.exe serve --port 8765
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

## 2. Железо (перепроверено 12.11.2026)

| Компонент | Значение | Вывод |
|---|---|---|
| CPU | Intel Core i3-14100F, 4 ядра / 8 потоков | Хватит для ядра, TTS, эмбеддингов; CPU-инференс LLM — fallback |
| RAM | 31,8 ГБ | Достаточно; свободно ~18,5 ГБ |
| GPU | **NVIDIA RTX 4060 Ti, 16 380 МБ VRAM** | 8B Q4, 7B coder @16k, Whisper и 3D-аватар помещаются |
| Диск C | 475,9 ГБ, свободно 75,7 ГБ (SSD) | Не для моделей |
| Диск D | 465,7 ГБ, свободно 112,7 ГБ (SSD) | Модели и кэш; датасеты — позже |
| ОС | Windows 11 Pro 26200 | Windows Sandbox есть; WSL2 не установлен |
| Python | 3.12.10 в `.venv` | Рабочая версия |
| Ollama | 0.40.16, модели на `D:\ollama_models` | База для LLM уже есть |

В простое занято ~1,6 ГБ VRAM, свободно **~14,5 ГБ**. Kivi обязана считать
бюджет по фактически свободной VRAM и держать одну тяжёлую модель.

Новый baseline: [BASELINE.md](docs/BASELINE.md) §5. План после перепроверки:
[PLAN-2026-11-12.md](docs/PLAN-2026-11-12.md).

**Установлено 12.11.2026:** `Qwen3-14B Q4` (качественный чат), `Qwen2.5-Coder-14B Q4`
(код), `Qwen3-VL-8B Q4` (vision), `faster-whisper-large-v3-turbo` (ASR),
SDXL base 1.0 (image, нужен ComfyUI/SD WebUI).

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
| [`docs/STATUS.md`](docs/STATUS.md) | Текущий статус, сделанное, backlog и критический путь |
| [`docs/PLAN-2026-11-12.md`](docs/PLAN-2026-11-12.md) | Новый план после перепроверки 16 ГБ |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Принятые решения, A4-Bounded, admin, облако, приоритеты |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Модель угроз, prompt injection, egress, тесты |
| [`docs/TOOLS.md`](docs/TOOLS.md) | Контракт Tool, Permission Engine, Protected Zones, риски R0–R4 |
| [`docs/ELEVATED.md`](docs/ELEVATED.md) | Elevated broker: allowlist, UAC, request/result, аудит |
| [`docs/TRUST.md`](docs/TRUST.md) | Trust labels, envelope, санитайзер и injection-корпус |
| [`docs/MEMORY.md`](docs/MEMORY.md) | Память: сообщения, факты, артефакты, FTS5 и Context Builder |
| [`docs/LIFECYCLE.md`](docs/LIFECYCLE.md) | Retention, суммаризация, TTL и durable approvals |
| [`docs/VECTOR.md`](docs/VECTOR.md) | LanceDB-индекс, sync/rebuild и ANN-поиск |
| [`docs/PLANNER.md`](docs/PLANNER.md) | Planner DAG, executor, verifier, replan и fastpath |
| [`docs/VOICE-AVATAR.md`](docs/VOICE-AVATAR.md) | Silero/Piper TTS, WAV и 2D-аватар с липсинком |
| [`docs/API.md`](docs/API.md) | Локальный JSON API `/health` и `/chat` |
| [`docs/CODER.md`](docs/CODER.md) | Coder loop: генерация, тесты, исправления |
| [`docs/CODER-TOOLS.md`](docs/CODER-TOOLS.md) | `repo.index`, `code.patch`, git-инструменты |
| [`docs/SANDBOX.md`](docs/SANDBOX.md) | `code.run`: audit hook, Job Object, ограничения |
| [`docs/OPERATOR-WINDOWS.md`](docs/OPERATOR-WINDOWS.md) | Окна, приложения, фокус, закрытие |
| [`docs/OPERATOR-UI.md`](docs/OPERATOR-UI.md) | UI Automation: клики, ввод, горячие клавиши |
| [`docs/OPERATOR-SYSTEM.md`](docs/OPERATOR-SYSTEM.md) | Уведомления, громкость, питание |
| [`docs/BROWSER.md`](docs/BROWSER.md) | Playwright, профиль, SSRF и prompt injection |
| [`docs/MAIL.md`](docs/MAIL.md) | Gmail OAuth, чтение, поиск, черновики без отправки |
| [`docs/TELEGRAM.md`](docs/TELEGRAM.md) | Telegram-бот, allowlist, rate limits |
| [`docs/VOICE-INPUT.md`](docs/VOICE-INPUT.md) | Audio, VAD, faster-whisper, wake-word |
| [`docs/UI.md`](docs/UI.md) | Kivi UI: запуск, API, возможности |
| [`docs/AVATAR3D-STATUS.md`](docs/AVATAR3D-STATUS.md) | 3D-VRM: текущий статус и запуск |
| [`docs/ASSET-LICENSE.md`](docs/ASSET-LICENSE.md) | Лицензия VRM и three.js |
| [`docs/VOICE-PIPELINE.md`](docs/VOICE-PIPELINE.md) | VoiceSession, barge-in, подтверждения |
| [`docs/VOICE-QWEN.md`](docs/VOICE-QWEN.md) | Qwen3-TTS VoiceDesign/Base, CUDA и замеры |
| [`docs/VOICE-FISH.md`](docs/VOICE-FISH.md) | Fish Audio voice ID, API-ключ и права |
| [`docs/VOICE-LIBRARY.md`](docs/VOICE-LIBRARY.md) | Импорт голосов из файлов/URL, consent |
| [`docs/CRM-EXTENSION.md`](docs/CRM-EXTENSION.md) | AltCRM через расширение Яндекс.Браузера |
| [`docs/ECC.md`](docs/ECC.md) | ECC: установка, skills и рабочий цикл |
| [`docs/WAKE-TRAINING.md`](docs/WAKE-TRAINING.md) | обучение своей ONNX wake-модели |
| [`docs/WAKE-BACKGROUND.md`](docs/WAKE-BACKGROUND.md) | wake без окна браузера и автозапуск |
| [`docs/OCR-PDF.md`](docs/OCR-PDF.md) | PDF-текст и офлайн-OCR |
| [`docs/APPROVALS.md`](docs/APPROVALS.md) | durable approvals → PermissionEngine |
| [`docs/CRM-PARITY.md`](docs/CRM-PARITY.md) | паритет extension/CDP и rollback |
| [`docs/SHELL-P2.md`](docs/SHELL-P2.md) | shell namespace, undo, thumbnails |
| [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md) | матрица приёмки и acceptance-smoke |
| [`docs/RELEASE-0.20.md`](docs/RELEASE-0.20.md) | release notes первого прототипа |
| [`docs/MEDIA.md`](docs/MEDIA.md) | M7: бесплатная генерация изображений |
| [`docs/MEDIA-JOBS.md`](docs/MEDIA-JOBS.md) | M7: очередь медиа-заданий |
| [`docs/VIDEO.md`](docs/VIDEO.md) | M7: локальный видео-fallback |
| [`docs/SDXL.md`](docs/SDXL.md) | Локальный SDXL через SD WebUI API |
| [`docs/SKILLS-PLUGINS.md`](docs/SKILLS-PLUGINS.md) | Аудит скиллов и плагинов |
| [`docs/AVATAR3D.md`](docs/AVATAR3D.md) | 3D-VRM слой, виземы, состояния, fallback |
| [`docs/CRM-DOMAIN.md`](docs/CRM-DOMAIN.md) | Факты, цитаты, черновики, история, метрики |
| [`docs/CRM-ADAPTER.md`](docs/CRM-ADAPTER.md) | AltCRM CDP: чтение и заполнение без сохранения |
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

1. Установить и замерить **14B Q4**, **SDXL** и **faster-whisper large-v3-turbo**
   на 16 ГБ; обновить registry по фактическим цифрам.
2. **M7 Cloud Media:** локальный SDXL + бесплатные image/video-провайдеры,
   10 сценариев.
3. Закрыть M5 внешними ресурсами: `assets/wake/kivi.onnx`, `assets/avatar/kivi.vrm`,
   микрофон, замеры false wake/WER.
4. Закрыть M6 при доступе к реальному AltCRM: 10 заказов, графическая панель,
   паритет с extension.
5. Дальше — M7-видео; M8 P0/P1/P2, OCR/PDF и approvals уже в 0.21.

Полный график — [PLAN-2026-11-12.md](docs/PLAN-2026-11-12.md).

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
