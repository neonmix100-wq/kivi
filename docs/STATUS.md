# Статус проекта Kivi

Дата среза: **12.11.2026**. Дорожная карта: [ROADMAP](ROADMAP.md).
Новый план после перепроверки железа: [PLAN-2026-11-12](PLAN-2026-11-12.md).
Тесты: **1691**, `ruff` чистый. `kivi doctor` и `kivi models` работают.

## 0. Железо на 12.11.2026

| Компонент | Значение |
|---|---|
| CPU | Intel Core i3-14100F, 4/8 |
| RAM | 31,8 ГБ |
| GPU | **NVIDIA RTX 4060 Ti, 16 380 МБ VRAM** |
| Свободно VRAM | ~14,5 ГБ в простое |
| Диски | C: 75,7 ГБ свободно; D: 100,5 ГБ свободно (оба SSD) |
| ОС / Python / Ollama | Windows 11 26200 / 3.12.10 / 0.40.16 |

Установлены 12.11.2026:

| Модель | Размер | Замер |
|---|---:|---|
| `Qwen3-14B Q4_K_M` | 9,0 ГБ | 14,6 tok/s @8192, 14 295 МБ |
| `Qwen2.5-Coder-14B Q4_K_M` | 9,0 ГБ | 21,6 tok/s @8192, 13 919 МБ |
| `Qwen3-VL-8B Q4_K_M` | 5,8 ГБ | vision, 5 575 МБ |
| `faster-whisper-large-v3-turbo` | 1,54 ГБ | CPU int8, 11,7 с речи |
| SDXL base 1.0 | 6,46 ГБ | чекпойнт, нужен ComfyUI/SD WebUI |
| Qwen3-TTS VoiceDesign 1.7B | 4,21 ГБ | голос по описанию, peak 4,1 ГБ |
| Qwen3-TTS Base 1.7B | 4,23 ГБ | клон reference, 6–8 с на фразу |

Удалены как устаревшие/дублирующие: `llama3:8b`, `qwen2.5:7b-instruct`,
`deepseek-coder:6.7b`, `Qwen3-8B Q5`, `supergemma4` — освободилось **22,7 ГБ**.
Осталось 7 Ollama-моделей + Qwen3-TTS + ASR + SDXL.

Главное изменение: **+8 ГБ VRAM**. 8B Q4, 7B coder @16k, Whisper и 3D-аватар
теперь помещаются; SDXL можно запускать отдельным этапом. 14B — качественный
чат/код, но 14–15 ГБ: без ASR/аватара. Подробно:
[BASELINE](BASELINE.md), [PLAN](PLAN-2026-11-12.md).

## 1. Краткий ответ

- **Этап 0 закрыт:** baseline 11.11.2026 обновлён 12.11.2026 на 16 ГБ;
  SDXL/Whisper/14B/VL установлены и замерены (см. `BASELINE.md` §5).
- **Этапы 1–3 закрыты:** ядро, Model Gateway и Cloud Broker.
- **Этап 4 закрыт:** инструменты, Permission Engine, Protected Zones,
  trust labels, санитайзер, kill switch, hotkey, elevated broker и
  injection-корпус готовы. OCR/PDF-читатели — отдельный backlog.
- **Этап 5 закрыт:** память, FTS5, эмбеддинги, hybrid-search, Context Builder,
  импорт, суммаризация, TTL, approvals API и LanceDB-индекс готовы.
  Автоподключение approvals к PermissionEngine — отдельный backlog.
- **Этап 6 (M1) закрыт:** planner DAG, fast/deep path, `kivi chat`, `kivi serve`,
  Silero TTS, 2D-аватар и 22 сценарных теста. p50 fastpath — 0.84 с.
- **Этап 7 (M2 Coder) закрыт:** `code.run` в Python-песочнице, `repo.index`,
  `code.patch`, git-инструменты, `Coder` с циклом до 2 исправлений и CLI
  `kivi coder`. WSL2/Sandbox недоступны — песочница best-effort.
- **Этап 8 (M3 Operator) закрыт:** окна/приложения, UI Automation + pyautogui,
  системные уведомления/громкость/питание, Playwright-браузер с отдельным
  профилем и SSRF/prompt-injection защитой; 10 ПК- и 10 браузерных сценариев.
- **Этап 9 (M4 Communicator) закрыт:** Gmail OAuth readonly+compose без send,
  `mail.list/search/read/attachment/draft`, Telegram-бот с allowlist и rate limit,
  CLI `kivi mail`/`kivi telegram`; 10+5 сценариев.
- **Этап 10 (M5 Voice & Avatar) ~99%:** audio/VAD/STT, barge-in, подтверждения,
  voice pipeline, avatar3d/visemes/states, CLI `listen`, **Qwen3-TTS
  VoiceDesign/Base (3 взрослых тембра)**, **Fish Audio S2.1 Pro Free / Astrea/s
  как основной голос**, библиотека голосов, **`chat --speak`**, `audio devices/test`,
  голосовой ввод в UI, **VRM-аватар, `kivi avatar`, 3D lip-sync через `chat --avatar`**.
  ONNX wake обучен на голосе пользователя (17/20 recall, 0/20 false wakes),
  подключён к `listen` и к фоновой кнопке UI через `/wake`.
- **Этап 11 (M6 Work CRM) ~99%:** headless-домен, AltCRM CDP adapter,
  `crm.read/fill/act/task`, расширение AltCRM через WebSocket-мост,
  read-only карточка, формы и счётчик; **13 уникальных чтений**;
  live fill 5/5, акт 1/1, задача 3/3 без сохранения/создания. Остались
  паритет и rollback-проверка.
- **Этап 12 (M7 Cloud Media) ~80%:** `media.image`, `media.sdxl`,
  `media.video` (локальный MP4 из кадра), `MediaJobQueue`, медиа-панель;
  локальный видео-fallback **принят для релиза**; настоящий text-to-video —
  после релиза.
- **Этап 13 (M8 Kivi Shell) ~95%:** Kivi UI, app-окно, трей, чат, история,
  голоса, настройки, аватар, голосовой ввод, медиа, файлы (вкладки, поиск,
  сортировка, back/forward, path input, copy/move, mkdir, rename, корзина,
  preview, context menu, drag-and-drop), CRM; Explorer-замена P2 — v1.x.

## 2. Этапы

| Этап | Статус | Что готово | Что осталось |
|---|---|---|---|
| 0. Baseline | ✅ закрыт | ADR, решения, `OLLAMA_MODELS`, baseline 5 моделей; **12.11 перепроверен на 16 ГБ** | — |
| 1. Скелет ядра | ✅ готово | конфиг, SQLite/миграции, Event Bus, JSONL, CLI, `doctor` | — |
| 2. Model Gateway | ✅ готово | registry, Ollama, NVML, lease/eviction, watchdog, CPU fallback, benchmark 20 переключений | — |
| 3. Cloud Broker | ✅ готово | провайдеры, квоты, `QuotaExhausted`, handoff, Scout, `cloud ask/scout/handoff` | живой ключ (внешнее) |
| 4. Tools + safety | ✅ готово | Tool contract, разрешения R0–R4, зоны, MAC-манифест, trust/sanitizer, fs/shell/http/proc/clipboard/screen, kill switch, hotkey, elevated broker, injection-корпус | OCR/PDF — backlog |
| 5. Память и контекст | ✅ готово | messages/facts/preferences/artifacts, FTS5, embeddings, RRF hybrid, Context Builder, импорт, суммаризация, TTL, approvals, LanceDB, CLI | автоподключение approvals — backlog |
| 6. M1: Core Kivi | ✅ готово | planner DAG, fastpath, chat, API, Silero/TTS, 2D-аватар, 22 сценария | — |
| 7. M2: Coder | ✅ готово | code.run sandbox, repo.index, code.patch, git, Coder loop, CLI | WSL2/Sandbox — апгрейд |
| 8. M3: Operator | ✅ готово | окна/приложения, UI, system, browser, 20 сценариев | — |
| 9. M4: Communicator | ✅ готово | Gmail drafts, Telegram bot, CLI, 15 сценариев | — |
| 10. M5: Voice & Avatar | 🟡 ~95% | audio/VAD/STT, Qwen3-TTS, Fish, voice library, UI mic, VRM + `kivi avatar`, lip-sync, text wake | ONNX wake после релиза |
| 11. M6: Work CRM | 🟡 ~99% | домен, AltCRM CDP, extension bridge, tools, CLI, CRM read/fill/act/task UI, live 5/5+1/1+3/3 | паритет, rollback |
| 12. M7: Cloud Media | 🟡 ~80% | `media.image`, `media.sdxl`, `media.video`, `MediaJobQueue`, UI image/video; fallback принят | real text-to-video после релиза |
| 13. M8: Kivi Shell | 🟡 ~95% | Kivi UI, app-окно, трей, голос, медиа, файлы (tabs/search/sort/back-forward/path/copy/move/mkdir/rename/trash/image preview/context menu/drag-drop), CRM, settings | Explorer |

## 3. Что уже сделано

**Ядро и аудит** — `config/kivi.yaml` (строгий Pydantic), SQLite с миграциями
1–5, Event Bus, JSONL-логи, `audit` для значимых действий.

**Model Gateway** — `config/models.yaml`, Ollama backend, VRAM-бюджет
(NVML + nvidia-smi), lease/eviction, watchdog, CPU fallback, `kivi ask/embed`,
benchmark полного цикла.

**Cloud Broker** — OpenRouter/Groq, дневные квоты в SQLite, проверка режима и
политики данных, `QuotaExhausted` + handoff, Cloud Scout с каталогом
кандидатов, `cloud ask/scout/handoff`.

**Безопасность** — Policy Engine, Protected Zones (UNC/ADS/8.3/symlink),
HMAC-манифест дополнительных зон, Permission Engine (capabilities, TTL,
R0–R4), Capability Broker с аудитом, trust labels (10 уровней), envelope,
санитайзер (скрытый HTML, zero-width, redaction), kill switch
(flag + tools/cloud + выгрузка Ollama + hotkey), injection-корпус.

**Инструменты** — `fs.read/list/write`, `shell.exec`, `http.get`
(HTTPS-only, SSRF-защита, denylist, pin IP, sanitiser), `proc.list/kill/start`,
`clipboard.read/write`, `screen.capture`. Все — через `ToolExecutor` с
аудитом и подтверждением для R2/R3.

**Память** — сообщения, факты, предпочтения, артефакты, FTS5, Content
Builder, эмбеддинги (float32 в SQLite), hybrid-search (RRF + recency),
импорт текстовых баз, `kivi memory ...`.

**Документация** — ARCHITECTURE, SECURITY, DECISIONS, ROADMAP, TOOLS, TRUST,
MEMORY, BASELINE, BENCH-GATEWAY, CLOUD; `_INDEX` и `_CODE-MAP` генерируются.

## 4. Где мы находимся

После перепроверки 12.11.2026:

- этапы 0–9 закрыты;
- M5 ~99% (VRM/липсинк/STT; ONNX wake + UI background wake);
- M6 ~95% (расширение AltCRM подключено, чтение/формы работают; нужны 10 заказов и паритет);
- 16 ГБ VRAM снимают прежнее ограничение: 8B Q4, 7B coder @16k, Whisper
  и 3D-аватар помещаются; Qwen3-TTS 1.7B добавляет ~4,1 ГБ.
- следующий приоритет — **M7 видео / M8 файловые операции**: SDXL реалистичен локально;
- параллельно: 14B Q4, затем закрытие M5/M6 внешними ресурсами.

Детальный новый график — [PLAN-2026-11-12](PLAN-2026-11-12.md).

## 5. Что предстоит

### 5.1. Детальный backlog

| # | Задача | Результат | Оценка | Зависит от |
|---|---|---|---:|---|
| 0.1 | Baseline §5 | цифры load/tokens/s/VRAM в `BASELINE.md` | 0.5 дня | закрытые Chrome/Telegram/Broadcast |
| 4.1 | Elevated broker: allowlist | ✅ `dns.flush`, `service.query/restart` + 34 теста | сделано |
| 4.2 | Elevated broker: UAC | ✅ helper, `runas`, аудит, `--wait` | сделано |
| 4.3 | OCR/PDF reader | текст из PDF/картинки с `untrusted:file/vision` | 2–3 дня | библиотеки OCR/PDF |
| 5.1 | Суммаризация | ✅ extractive/injected summary | сделано |
| 5.2 | TTL и очистка | ✅ сообщения/эмбеддинги/approvals | сделано |
| 5.3 | Approvals API | ✅ grant/check/revoke/cleanup | сделано |
| 5.4 | LanceDB/ANN | ✅ LanceDB 0.41 + pyarrow, sync/rebuild | сделано |
| 6.1 | Planner DAG | ✅ validator/executor/verifier/replan | сделано |
| 6.2 | Fast/deep path | ✅ fastpath + planner | сделано |
| 6.3 | CLI-чат/API | ✅ `kivi chat`, `kivi serve` | сделано |
| 6.4 | Silero TTS | ✅ `baya`, 48 кГц, fallback Piper | сделано |
| 6.5 | 2D-аватар | ✅ липсинк и состояния | сделано |
| 6.6 | 20 сценариев | ✅ 22 сценария, fastpath 0.84 с | сделано |

### ✅ P0 — закрыто (этапы 0, 4, 5)

1. **Baseline §5.** Снят 11.11.2026 при закрытых Chrome/Broadcast: пять моделей,
   load/tokens/s/peak VRAM; SDXL/whisper — n/a. Этап 0 закрыт.
2. **Elevated broker.** Allowlist, отдельный helper, UAC, аудит, `--wait`;
   34 теста. Этап 4 закрыт.
3. **Память: суммаризация + TTL.** Summaries, TTL-очистка, approvals API;
   этап 5 закрыт по acceptance.
4. **LanceDB/ANN.** LanceDB 0.41 + pyarrow, `vector-sync`, `memory similar --ann`;
   SQLite остаётся fallback.

Остался отдельный backlog: OCR/PDF-читатели и автоподключение approvals
к PermissionEngine; они не блокируют закрытие этапов 0/4/5.

### ✅ P1 — M1: Core Kivi закрыт

Planner DAG, fast/deep path, `kivi chat`, `kivi serve`, Silero TTS, 2D-аватар
и 22 сценарных теста готовы. p50 fastpath — 0.84 с.

### ✅ P3 — M3: Operator закрыт

Окна/приложения, UI Automation, системные уведомления/громкость/питание,
Playwright-браузер с отдельным профилем и защитой от prompt injection.
10 ПК- и 10 браузерных сценариев, live-smoke `window.list`, `system.volume.get`,
`browser.open/read`.

### ✅ P4 — M4: Communicator закрыт

Gmail OAuth (только чтение и черновики), `mail.*`, Telegram-бот с allowlist и
rate limit, CLI. 10 почтовых и 5 Telegram-сценариев.

### P5 — M5: Voice & Avatar, частично

10. **Voice & Avatar:** код audio/VAD/STT, barge-in, pipeline, avatar3d готов;
    нужны `kivi.onnx`, `kivi.vrm`, микрофон и замеры false wake/WER.

### P6 — M6: Work CRM, частично

11. **Work CRM:** домен, AltCRM CDP, `crm.read/fill/act/task`, CLI, метрики;
    нужны реальный AltCRM, 10 заказов, графическая панель и паритет.

## 6. Следующий шаг

```
этапы 0–9 ✅ + M5 ~98% + M6 ~99% + M7 ~80% + M8 ~95%
   │
   ├─► M7 Cloud Media: image/video-провайдеры
   │
   └─► блоки: ONNX+VRM+микрофон, реальный AltCRM
```

M5/M6 — программные слои готовы, внешние ресурсы блокируют приёмку.
Следующий срез — **M7 видео и M8 файловые операции**.

## 7. Риски

| Риск | Влияние | Мера |
|---|---|---|
| песочница Coder без сети/изоляции | утечка или запись вне workspace | WSL2/Windows Sandbox, монтирование только workspace, лимиты |
| planner начнёт обходить Policy Engine | безопасность | все tools только через `ToolExecutor`, тесты на обход |
| VRAM 16 ГБ, но 14B/Coder-14B @16k ~15 ГБ | OOM/вытеснение | 8192 для 14B, 7B coder — дефолт |
| elevated allowlist слишком узкий | admin-задачи | расширять только ревью и тестами |
| OCR/PDF-библиотеки тянут зависимости | backlog | ставить optional extra, не в ядро |

## 8. Проверка

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\kivi.exe doctor
.venv\Scripts\kivi.exe tools list
.venv\Scripts\kivi.exe memory stats
.venv\Scripts\python.exe scripts\kivi_context.py stage
```
