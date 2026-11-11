# Operator: системные инструменты (уведомления, громкость, питание)

Версия: 0.1 (11.11.2026). Стадия 8 / M3: Operator.
Связанные документы: `docs/TOOLS.md`, `docs/SECURITY.md`, `docs/ELEVATED.md`,
`docs/STATUS.md`.

---

## 1. Назначение и границы

Этот срез добавляет первый вертикальный блок M3: локальные уведомления,
громкость и управление питанием/блокировкой. Горячие клавиши уже существуют
отдельно (`src/kivi/safety/hotkey.py`, `kivi hotkey`) и здесь не меняются.

Реализация:

- `src/kivi/tools/system.py` — протокол backend, Windows-backend и семь
  инструментов;
- `tests/unit/test_system_tools.py` — тесты с fake-backend, без реального
  аудио, блокировки, сна и выключения;
- `docs/OPERATOR-SYSTEM.md` — этот документ.

Границы среза:

- нет браузера, UIA/OCR и сценариев — это следующие части M3;
- нет admin/UAC: elevated broker остаётся отдельным механизмом;
- `registry.py` и CLI намеренно **не изменены**; инструменты подключаются
  одной строкой в `build_registry` (см. §9);
- уведомление — best-effort: при неудаче toast используется системный beep,
  а не ошибка задачи.

## 2. Контракт backend

```python
class SystemBackend(Protocol):
    def notify(self, title: str, message: str, timeout_s: int) -> None: ...
    def get_volume(self) -> int: ...          # 0..100
    def set_volume(self, level: int) -> None: # 0..100
    def lock(self) -> None: ...
    def sleep(self) -> None: ...
    def shutdown(self) -> None: ...
    def restart(self) -> None: ...
```

`WindowsSystemBackend` реализует контракт для Windows 11. Ожидаемые сбои ОС
поднимаются как `SystemUnavailable`; инструменты превращают их в `ToolError`,
а executor пишет `decision=error`.

Необязательные расширения (не входят в `SystemBackend`; описаны отдельным
`MuteBackend` и ищутся через `getattr`):

- `is_muted() -> bool`;
- `set_muted(muted: bool) -> None`.

Если backend их не реализует, `system.volume.get` возвращает `muted: null`, а
`system.volume.set` с `mute` завершается `ToolError` до изменения громкости.

## 3. Инструменты

| Инструмент | Риск | Capability | Что делает |
|---|---|---|---|
| `system.notify` | R1 | `system.notify` | локальный toast, title 1..80, message 1..500 |
| `system.volume.get` | R0 | `system.volume` | текущая громкость 0..100 и mute |
| `system.volume.set` | R1 | `system.volume` | `level`, `delta` или `mute`; dry-run |
| `system.lock` | R2 | `system.power` | блокировка рабочей станции |
| `system.sleep` | R3 | `system.power` | сон (не гибернация) |
| `system.shutdown` | R4 | `system.power` | `shutdown.exe /s /t 0`; hard deny |
| `system.restart` | R4 | `system.power` | `shutdown.exe /r /t 0`; hard deny |

Все инструменты получают backend через конструктор; `build_system_tools()`
создаёт общий экземпляр. Публичные имена и риски проверяются `ToolRegistry`.

### `system.notify`

- `title`: 1..80 символов, `message`: 1..500; пробелы по краям срезаются;
- `timeout_s`: 1..30, подсказка длительности; Windows toast не умеет
  произвольные секунды, поэтому вызов PowerShell всегда ограничен 5 секундами;
- title и message проходят `redact_secrets` **до** backend; результат содержит
  только длины и число redaction, без текста;
- `trust="trusted:system"`.

### `system.volume.set`

- ровно один режим изменения: `level` (0..100) или `delta` (-100..100);
- `mute` (`true`/`false`) можно комбинировать с любым режимом или использовать
  отдельно;
- `delta` применяется к текущему значению и зажимается в 0..100;
- `dry_run` читает текущее состояние, но не вызывает `set_volume`/`set_muted`;
- если mute запрошен, а backend его не поддерживает, инструмент падает до
  изменения громкости (без частичного side effect);
- смена громкости и mute — две операции; если вторая не удалась, первая уже
  могла примениться. Это отражено в документации, а не скрыто.

## 4. Права и approvals

Решение принимает `PermissionEngine` в `ToolExecutor`, а не инструмент.

| Режим | R0 | R1 | R2 | R3 | R4 |
|---|---|---|---|---|---|
| A4-Bounded (default) | allow | allow | confirm | confirm | **deny** |
| A2 | allow | confirm | deny | deny | deny |

- `system.volume.get` — R0, работает без подтверждения;
- `system.notify` и `system.volume.set` — R1, в A4-Bounded работают авто;
- `system.lock` — R2: без `--approve` executor возвращает
  `confirmation required`, backend не вызывается; с `--approve` — allow;
- `system.sleep` — R3: то же самое, но подтверждение строже по смыслу;
- `system.shutdown` и `system.restart` — R4: **hard deny** в любом режиме,
  включая `--approve` и `dry_run`. До backend они не доходят никогда.

Это не настройка, которую можно ослабить конфигом: таблица рисков находится в
коде `safety/permissions.py`. Даже при `A3` R3 запрещён, R4 запрещён всегда.

## 5. Уведомления и fallback

`WindowsSystemBackend.notify()` вызывает один фиксированный скрипт:

```text
powershell.exe -NoProfile -NonInteractive -Command <fixed script>
```

Свойства вызова:

- argv — список, `shell=False`; никакой конкатенации командной строки;
- `CREATE_NO_WINDOW`, минимальное окружение (`_minimal_env`), `stdin` не
  используется;
- timeout процесса — 5 секунд;
- скрипт использует WinRT `ToastNotificationManager` и `XmlDocument`;
- title и message экранируются как XML (`&`, `<`, `>`, `"`, `'`), а
  одинарные кавычки дополнительно удваиваются для PowerShell;
- AppUserModelID — AUMID Windows PowerShell, поэтому источник toast может
  отображаться как «Windows PowerShell», а не «Kivi».

Если toast не удался (нет `powershell.exe`, timeout, ненулевой exit code,
WinRT/AppID недоступен), backend:

1. пишет `log.warning` с причиной;
2. вызывает `winsound.MessageBeep(winsound.MB_ICONASTERISK)`;
3. не поднимает исключение: уведомление — best-effort.

Если и beep не удался, остаётся только warning. Внешние пакеты (BurntToast и
т.п.) не нужны.

Ограничение: Windows может отключить уведомления политикой, фокус-ассистентом
или «Не беспокоить». Kivi это не обходит; в таком случае остаётся beep.

## 6. Громкость и mute

`pycaw` и `comtypes` импортируются лениво, только при первом вызове
`get_volume`/`set_volume`/`is_muted`/`set_muted`. Остальные системные
инструменты не требуют этих пакетов.

Последовательность:

1. `AudioUtilities.GetSpeakers()`;
2. современный pycaw: `AudioDevice.EndpointVolume`;
3. старый pycaw: `device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)`
   и `QueryInterface(IAudioEndpointVolume)`;
4. `GetMasterVolumeLevelScalar()` / `SetMasterVolumeLevelScalar()`;
5. `GetMute()` / `SetMute()`.

Скаляр Windows 0.0..1.0 переводится в проценты и зажимается в 0..100.
`GetMasterVolumeLevelScalar` может вернуть больше 1.0 при boost — результат
всё равно 100. `set_volume` зажимает вход и передаёт `target / 100.0`.

Ошибки COM, отсутствие устройства или отсутствие `pycaw` превращаются в
`SystemUnavailable` → `ToolError`. Реальное аудио в тестах не используется:
fake-backend и monkeypatch endpoint.

## 7. Питание и блокировка

- `system.lock`: `ctypes.windll.user32.LockWorkStation()`; возврат `0` —
  `SystemUnavailable`.
- `system.sleep`: `powrprof.SetSuspendState(False, False, False)`.
  Первый `False` — сон, а не гибернация; второй — без force; третий — wake
  events остаются включены.
- `system.shutdown`: фиксированный argv `shutdown.exe /s /t 0`.
- `system.restart`: фиксированный argv `shutdown.exe /r /t 0`.

Ограничения:

- Kivi не запрашивает admin/UAC и не повышает права;
- нет `/f` и другого форсирования: приложение с несохранёнными данными может
  заблокировать выключение;
- нет гибернации (`/h`) и нет «выключить принудительно»;
- политика домена, Fast Startup, обновления Windows и права сессии могут
  запретить операцию; это будет `ToolError`, а не тихий успех;
- `shutdown`/`restart` не имеют admin-обхода и не должны запускаться из
  инструмента: R4 hard deny гарантирует, что через `ToolExecutor` они не
  выполнятся. Прямой вызов backend в обход broker — вне политики Kivi и в
  документации не рекомендуется.

## 8. Аудит

Каждая попытка проходит через `ToolExecutor` и пишет одну строку аудита:

- `actor=tool`, `action=tool.system.*`;
- `decision=allow|confirm|deny|error`;
- `target` — имя инструмента;
- `details` — risk, capability, task_id, `args_digest` (SHA-256 аргументов),
  `dry_run`, `approved`, признак результата.

Текст уведомления не попадает в `output` и в audit-details; в preview
dry-run сохраняется только **отредактированный** title и длина message.
Секреты в аргументах всё равно не следует передавать: redaction — последний
барьер, а не разрешение хранить пароли в tool-args.

Сами инструменты не видят БД и не пишут аудит напрямую — только executor.

## 9. Интеграция (registry/CLI не изменялись)

Чтобы инструменты появились в `kivi tools list`, добавьте в
`src/kivi/tools/registry.py::build_registry`:

```python
from kivi.tools.system import build_system_tools

# ... после существующих инструментов
for tool in build_system_tools():
    registry.register(tool)
```

`build_system_tools()` создаёт один `WindowsSystemBackend` на все семь
инструментов. CLI `kivi tools run` уже generic и отдельной регистрации имени
не требует. Для R2/R3 нужен `--approve`, для R4 — нет: он всегда deny.

Проверка после интеграции:

```powershell
kivi tools list
kivi tools run system.volume.get --args '{}'
kivi tools run system.notify --args '{"title":"Kivi","message":"test"}'
kivi tools run system.volume.set --args '{"delta":-10}'
kivi tools run system.volume.set --args '{"mute":true}'
kivi tools run system.lock --args '{}' --dry-run
kivi tools run system.sleep --args '{}' --dry-run
kivi tools run system.lock --args '{}' --approve
```

`system.shutdown` и `system.restart` после интеграции также доступны в списке,
но любой запуск, включая `--approve`, вернёт `denied: risk R4 is denied in
A4-Bounded`. Это ожидаемое поведение, а не баг.

## 10. Реальные smoke-команды

Уведомление и чтение громкости безопасны; установка громкости меняет звук.

```powershell
# toast; при сбое будет beep и warning в логе
.venv\Scripts\python.exe -c "from kivi.tools.system import WindowsSystemBackend as B; B().notify('Kivi', 'smoke test', 5)"

# чтение громкости, без изменений
.venv\Scripts\python.exe -c "from kivi.tools.system import WindowsSystemBackend as B; print(B().get_volume())"

# изменение громкости до 30%
.venv\Scripts\python.exe -c "from kivi.tools.system import WindowsSystemBackend as B; B().set_volume(30)"
```

Блокировку и сон запускайте только вручную, сохранив работу; в CI и
автотестах они не выполняются:

```powershell
# .venv\Scripts\python.exe -c "from kivi.tools.system import WindowsSystemBackend as B; B().lock()"
# .venv\Scripts\python.exe -c "from kivi.tools.system import WindowsSystemBackend as B; B().sleep()"
```

`shutdown`/`restart` не входят в smoke-набор: через Kivi они всегда
hard-deny. Прямой вызов backend не документируется как способ обхода.

## 11. Проверка

```powershell
.venv\Scripts\python.exe -m pytest -q tests\unit\test_system_tools.py
.venv\Scripts\python.exe -m ruff check src\kivi\tools\system.py tests\unit\test_system_tools.py
```

Тесты покрывают:

- риски и capabilities всех семи инструментов;
- redaction title/message и отсутствие секрета в backend-вызове;
- XML-экранирование и фиксированный argv PowerShell, `shell=False`,
  timeout 5;
- fallback на beep и warning при ошибке PowerShell;
- громкость: get, set, delta, clamp, mute, mute-only, dry-run;
- отсутствие опционального mute и ошибки backend → `ToolError`;
- approval для R2/R3 и hard deny R4 даже с `--approve` и `--dry-run`;
- запись аудита для allow/confirm/deny/error.

Реальный звук, LockWorkStation, SetSuspendState и shutdown.exe в тестах не
вызываются.
