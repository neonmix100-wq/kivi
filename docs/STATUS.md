# Статус проекта Kivi

Дата среза: **11.11.2026**. Дорожная карта: [ROADMAP](ROADMAP.md).
Тесты: **392**, `ruff` чистый. Репозиторий собирается, `kivi doctor` работает.

## 1. Краткий ответ

- **Этап 0 закрыт:** baseline моделей снят 11.11.2026 (Chrome/Broadcast закрыты),
  цифры в `docs/BASELINE.md` §5. SDXL/whisper — n/a: нет чекпойнта/пакета.
- **Этапы 1–3 закрыты:** ядро, Model Gateway и Cloud Broker.
- **Этап 4 закрыт:** инструменты, Permission Engine, Protected Zones,
  trust labels, санитайзер, kill switch, hotkey, elevated broker и
  injection-корпус готовы. OCR/PDF-читатели — отдельный backlog.
- **Этап 5 закрыт:** память, FTS5, эмбеддинги, hybrid-search, Context Builder,
  импорт, суммаризация, TTL, approvals API и LanceDB-индекс готовы.
  Автоподключение approvals к PermissionEngine — отдельный backlog.
- **Этап 0** закрыт: baseline §5 заполнен (SDXL/whisper отмечены как n/a).
- **Этап 6 (M1) закрыт:** planner DAG, fast/deep path, `kivi chat`, `kivi serve`,
  Silero TTS, 2D-аватар и 22 сценарных теста. p50 fastpath — 0.84 с.
- **Этап 7 (Coder) не начат** — следующий крупный срез.

## 2. Этапы

| Этап | Статус | Что готово | Что осталось |
|---|---|---|---|
| 0. Baseline | ✅ закрыт | ADR, решения, `OLLAMA_MODELS`, замер пяти моделей; SDXL/whisper n/a | — |
| 1. Скелет ядра | ✅ готово | конфиг, SQLite/миграции, Event Bus, JSONL, CLI, `doctor` | — |
| 2. Model Gateway | ✅ готово | registry, Ollama, NVML, lease/eviction, watchdog, CPU fallback, benchmark 20 переключений | — |
| 3. Cloud Broker | ✅ готово | провайдеры, квоты, `QuotaExhausted`, handoff, Scout, `cloud ask/scout/handoff` | живой ключ (внешнее) |
| 4. Tools + safety | ✅ готово | Tool contract, разрешения R0–R4, зоны, MAC-манифест, trust/sanitizer, fs/shell/http/proc/clipboard/screen, kill switch, hotkey, elevated broker, injection-корпус | OCR/PDF — backlog |
| 5. Память и контекст | ✅ готово | messages/facts/preferences/artifacts, FTS5, embeddings, RRF hybrid, Context Builder, импорт, суммаризация, TTL, approvals, LanceDB, CLI | автоподключение approvals — backlog |
| 6. M1: Core Kivi | ✅ готово | planner DAG, fastpath, chat, API, Silero/TTS, 2D-аватар, 22 сценария | — |
| 7. M2: Coder | ⬜ не начат | — | sandbox, code.run, патчи/диффы, git, цикл тестов |
| 8. M3: Operator | ⬜ не начат | — | управление ПК, браузер, UIA/OCR |
| 9. M4: Communicator | ⬜ не начат | — | Gmail-черновики, Telegram-бот |
| 10. M5: Voice & Avatar | ⬜ не начат | — | wake-word, STT, 3D-VRM, липсинк |
| 11. M6: Work CRM | ⬜ не начат | — | карточки, документы, модуль `work_crm` |
| 12. M7: Cloud Media | ⬜ не начат | — | бесплатные image/video-провайдеры |
| 13. M8: Kivi Shell | ⬜ не начат | — | Explorer-замена, P0/P1, закалка |

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

Сейчас проект стоит на границе **этапа 5 → этапа 6 (M1)**:

- этапы 0–6 закрыты;
- прикладной слой инструментов, безопасности, памяти и M1 готов;
- следующий крупный вертикальный срез — M2: Coder.

Формальный трекер `kivi_context.py stage` показывает этап 0, потому что
единственный открытый чекбокс — baseline. Это не отражает реальный прогресс;
приоритет — закрыть baseline и elevated broker, затем M1.

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

### P2 — этап 7 (M2: Coder) и далее

7. **Coder:** `code.run` в песочнице, монтирование workspace, сеть off,
   лимиты CPU/RAM/времени, патчи/диффы, git и цикл тестов.
8. Operator: управление ПК и браузер.
9. Communicator: Gmail-черновики и Telegram.
10. Voice & Avatar: wake-word, STT, 3D-VRM.
11. Work CRM, Cloud Media, Kivi Shell.

## 6. Следующий шаг

```
этапы 0–6 ✅
   │
   ├─► M2 Coder: sandbox ─► code.run ─► патчи/диффы ─► git ─► тесты
   │
   └─► backlog: OCR/PDF, approvals → PermissionEngine
```

M1 закрыт: planner/executor/verifier, fastpath, `kivi chat`, `kivi serve`,
Silero TTS, 2D-аватар и 22 сценария. Следующий срез — **M2: Coder**. 

## 7. Риски

| Риск | Влияние | Мера |
|---|---|---|
| песочница Coder без сети/изоляции | утечка или запись вне workspace | WSL2/Windows Sandbox, монтирование только workspace, лимиты |
| planner начнёт обходить Policy Engine | безопасность | все tools только через `ToolExecutor`, тесты на обход |
| VRAM 8 ГБ при 8B + аватар + браузер | M1/Coder демо | квантование, выгрузка, CPU fallback |
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
