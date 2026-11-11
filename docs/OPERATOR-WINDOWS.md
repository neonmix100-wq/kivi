# Operator PC: окна и приложения (M3)

Версия: 0.1 (11.11.2026). Стадия M3 «Operator PC control».
Связанные документы: [TOOLS.md](TOOLS.md), [SECURITY.md](SECURITY.md),
[TRUST.md](TRUST.md), [ROADMAP.md](ROADMAP.md), [STATUS.md](STATUS.md).

Реализация: `src/kivi/tools/windows.py`, тесты: `tests/unit/test_windows.py`.

---

## 1. Назначение

M3 даёт Kivi управление обычным пользовательским рабочим столом Windows 11:
перечислить окна, вывести окно на передний план, свернуть, развернуть, попросить
приложение закрыться, запустить `.exe` из workspace и завершить процесс.

Границы среза:

- только top-level окна текущей интерактивной сессии;
- только обычные права пользователя, без UAC и elevated broker;
- только `.exe` из workspace, без shell, URL, скриптов и установщиков;
- защищённые процессы и системные окна не трогаем;
- каждое действие идёт через `ToolExecutor`, с аудитом и подтверждением для R2.

## 2. Слои

| Слой | Что делает | Зависимости |
|---|---|---|
| `WindowInfo` | снимок окна: handle, title, process, pid, visible, minimized, bounds | — |
| `WindowBackend` | чистый протокол из 7 методов, без policy/approval/audit | — |
| `Win32Backend` | `EnumWindows`, `ShowWindow`, `SetForegroundWindow`, `PostMessageW`, `OpenProcess`/`TerminateProcess`, `subprocess.Popen` | ctypes user32/kernel32 |
| `PywinautoBackend` | необязательный UIA-fallback только для focus/close; остальное делегирует Win32 | pywinauto (optional) |
| инструменты `window.*`, `app.*` | типы, resolution, защита, preview, результат | `ToolContext`, `ToolResult` |

`backend=None` в конструкторе инструмента всегда означает `Win32Backend()`.
`pywinauto` не импортируется на обычном пути и не влияет на старт Kivi.

Протокол:

```python
class WindowBackend(Protocol):
    def list(self, *, visible_only: bool = False) -> list[WindowInfo]: ...
    def focus(self, handle: int) -> None: ...
    def minimize(self, handle: int) -> None: ...
    def maximize(self, handle: int) -> None: ...
    def close(self, handle: int) -> None: ...
    def launch(self, executable: str, args: Sequence[str], *, cwd: str | None = None) -> int: ...
    def terminate(self, pid: int) -> None: ...
```

`list()` по умолчанию возвращает только видимые окна; `visible_only=False`
добавляет скрытые. `launch` возвращает pid, `close` только отправляет
`WM_CLOSE` (приложение само решает, выходить ли).

## 3. Инструменты

| Инструмент | Риск | Capability | Аргументы | Действие |
|---|---|---|---|---|
| `window.list` | R0 | `window.read` | `visible_only: bool=True`, `limit: int=50` (1..200) | таблица окон; `untrusted:os` |
| `window.focus` | R1 | `window.control` | `handle: int` **или** `title: str` | восстановить и вывести на передний план |
| `window.minimize` | R1 | `window.control` | `handle` или `title` | `SW_MINIMIZE`, проверка `IsIconic` |
| `window.maximize` | R1 | `window.control` | `handle` или `title` | `SW_MAXIMIZE`, проверка `IsZoomed` |
| `window.close` | R2 | `window.control` | `handle` или `title` | `PostMessageW(WM_CLOSE)` после подтверждения |
| `app.launch` | R2 | `app.launch` | `path: str`, `args: list[str]` (≤16), `cwd: str=""` | `Popen` без shell, вернуть pid |
| `app.terminate` | R2 | `app.terminate` | `pid: int` | проверить процесс и завершить, вернуть имя |

Ровно один из `handle`/`title` обязателен: оба или ни одного — `invalid
arguments` от executor'а, инструмент не запускается.

Решения по риску при `A4-Bounded`: R0 и R1 выполняются, R2 требует
`--approve` (или `approved=True`). Более строгие режимы (`A3`, `A2`, …)
спускаются по общей таблице `PermissionEngine`, ослаблять её нельзя.

### dry-run

- `window.close`, `app.launch`, `app.terminate` — полноценный preview:
  проверяются target, protected-процессы, зоны, policy и pid, но ни одно
  действие не выполняется;
- `window.focus/minimize/maximize` — preview только валидирует target;
- dry-run не требует подтверждения, пишет аудит с `decision=allow` и
  `dry_run=true`, возвращает JSON `Preview`.

## 4. Как резолвится окно

1. `handle` — точное совпадение с `WindowInfo.handle` из `window.list`;
   разрешено и для скрытого окна.
2. `title` — регистронезависимая **подстрока** (`casefold`), только видимые
   окна. Никаких regex, fuzzy-поиска и «первого похожего».
3. Ноль совпадений — ошибка `no window matches title '...'`.
4. Больше одного — ошибка `ambiguous title '...': N windows match; use a
   handle: <handle> ('<title>'), ...` (до 8 пар handle/заголовок). Инструмент
   ничего не меняет и не выбирает окно сам.
5. Неизвестный handle — `window not found: handle N`.

Заголовки не считаются доверенными: перед выводом они схлопываются в одну
строку, управляющие символы заменяются пробелами, длинные обрезаются.
Заголовок никогда не исполняется и не попадает в аргументы других вызовов
как команда.

## 5. Защищённые процессы

`window.close` отказывает (fail closed), если owner окна:

| Группа | Процессы | Почему |
|---|---|---|
| ядро системы | `system`, `smss.exe`, `csrss.exe`, `wininit.exe`, `winlogon.exe`, `services.exe`, `lsass.exe`, `lsaiso.exe`, `svchost.exe`, `dwm.exe`, `fontdrvhost.exe`, `sihost.exe`, `ctfmon.exe`, `audiodg.exe`, `registry`, `memcompression`, `lsm.exe` | закрытие дестабилизирует сессию |
| оболочка | `explorer.exe`, `searchhost.exe`, `startmenuexperiencehost.exe`, `shellexperiencehost.exe`, `textinputhost.exe` | ломает рабочий стол и меню |
| Kivi и Python | `python.exe`, `pythonw.exe`, `kivi.exe`, текущий pid процесса | Kivi не закрывает сам себя |

Дополнительно отклоняются `pid <= 4` и окна с неизвестным owner-процессом.
`window.list` такие окна показывает (кроме console/own/untitled), чтобы
оператор видел картину, но управлять ими нельзя.

`app.terminate` использует тот же набор плюс `cmd.exe`, `powershell.exe`,
`pwsh.exe`; отдельно запрещены системные pid и pid самого Kivi. Перед
завершением процесс проверяется через `psutil` (существует, имя, не
защищён), затем вызывается `TerminateProcess` через backend.

## 6. Политика app.launch

Разрешено запускать только:

- файл с расширением `.exe` (регистр не важен);
- существующий обычный файл;
- внутри workspace: путь проходит `ProtectedZones.check(..., "read")` и
  `check(..., "exec")`, затем `PolicyEngine.check_path(..., "exec")`;
- с cwd внутри workspace (по умолчанию — сам workspace);
- с максимум 16 аргументами, каждый ≤1024 символа;
- без shell: `subprocess.Popen(argv, shell=False)` и минимальный env.

Отклоняется:

- URL (`http:`, `https:`, `file:`, `shell:`, `mailto:`, `://`) и UNC-пути;
- `.bat`, `.cmd`, `.ps1`, `.vbs`, `.js`, `.com`, `.msi`, `.lnk`, `.url`,
  `.scr`, `.cpl`, `.msc`, `.pif` — сообщение говорит, что разрешён только `.exe`;
- хосты-интерпретаторы и «полезные» LOLBins: `cmd.exe`, `powershell.exe`,
  `pwsh.exe`, `wscript.exe`, `cscript.exe`, `mshta.exe`, `rundll32.exe`,
  `regsvr32.exe`, `certutil.exe`, `msiexec.exe`, `installutil.exe`,
  `wsl.exe`, `bash.exe` — иначе `app.launch` стал бы обходом `shell.exec`;
- shell-метасимволы `&`, `|`, `;`, `<`, `>`, `^`, обратная кавычка и переводы
  строк в пути и аргументах (defense-in-depth: shell всё равно не используется);
- кавычки в пути;
- пути вне workspace и защищённые зоны (`zone-scope`, `system-*`, `core-*`);
- системные команды из `PolicyEngine` (`diskpart.exe`, `reg.exe` с HKLM и
  т.п.);
- отсутствующий exe и несуществующий cwd.

**Program Files / Windows.** Запуск установленных программ из `C:\Program
Files` и `C:\Windows` намеренно не разрешён: зоны требуют workspace, а policy
считает эти префиксы защищёнными. Для «запустить установленное приложение»
нужен отдельный allowlist/брокер — это следующая задача, а не ослабление
текущих проверок. Копирование чужого exe в workspace тоже требует
подтверждения R2: policy не умеет отличить добрый бинарник от злого.

**Возврат.** `ToolResult.meta` содержит `pid`, `path`, `cwd`, `args`;
`output` — `launched <name> (pid N)`.

## 7. app.terminate

1. `pid <= 4` → отказ (системные процессы).
2. `pid == os.getpid()` → отказ.
3. `psutil.Process(pid)` → `process not found` / `access denied`.
4. Имя из защищённого набора или `kivi*` → отказ.
5. `backend.terminate(pid)` (Win32: `OpenProcess(PROCESS_TERMINATE)` +
   `TerminateProcess`).
6. `process.wait(timeout=5)`; таймаут — ошибка «did not exit; may be
   elevated or hung». Успех — `terminated <name> (pid N)`, audit allow.

Завершение принудительное (`TerminateProcess`), без предварительного
`WM_CLOSE`: несохранённые данные могут потеряться. Поэтому R2 и
подтверждение. `WM_CLOSE` — только `window.close`.

## 8. Аудит и trust

- каждый вызов: `audit.action=tool.<name>`, `actor=tool`,
  `decision=allow|deny|confirm|error`, `details.risk/capability/task_id/
  dry_run/approved`;
- preview и результат не содержат секретов; аргументы хешируются executor'ом;
- `window.list`, `focus`, `minimize`, `maximize`, `close` возвращают
  `trust="untrusted:os"` — заголовки окон приходят из чужих процессов;
- `app.launch`, `app.terminate` — `untrusted:tool`.

## 9. Проверка

Юнит-тесты (fake backend, реальные окна не трогаются):

```powershell
.venv\Scripts\python.exe -m pytest -q tests\unit\test_windows.py
.venv\Scripts\python.exe -m ruff check src\kivi\tools\windows.py tests\unit\test_windows.py
```

Один live-тест только перечисляет окна и пропускается вне Windows.

Реальные smoke-команды (инструменты зарегистрированы в `build_registry()`,
см. §10):

```powershell
kivi tools list
kivi tools run window.list --args '{"limit":10}'
kivi tools run window.list --args '{"visible_only":false,"limit":20}'
kivi tools run window.focus --args '{"title":"notepad"}'
kivi tools run window.focus --args '{"handle":123456}'
kivi tools run window.minimize --args '{"title":"Steam"}'
kivi tools run window.maximize --args '{"handle":123456}'
kivi tools run window.close --args '{"title":"Notepad"}' --dry-run
kivi tools run window.close --args '{"title":"Notepad"}' --approve
kivi tools run app.launch --args '{"path":"dist\\demo.exe","args":["--flag"]}' --dry-run
kivi tools run app.launch --args '{"path":"dist\\demo.exe"}' --approve
kivi tools run app.terminate --args '{"pid":12345}' --approve
```

Безопасный порядок проверки: `window.list` → `--dry-run` → `--approve`.
`window.close` и `app.terminate` сначала тестируйте на блокноте/калькуляторе,
не на Explorer и не на Kivi.

## 10. Интеграция

Инструменты зарегистрированы в `build_registry()`
(`src/kivi/tools/registry.py`) вместе с остальными M3-инструментами: `window.*`
и `app.*` попадают в `kivi tools list` и доступны планировщику.

CLI менять не нужно: `kivi tools run` выдаёт grant из `tool.capabilities`
автоматически (`src/kivi/interfaces/cli.py:1204`). Capabilities среза:
`window.read`, `window.control`, `app.launch`, `app.terminate`.

После добавления документации стоит пересобрать навигацию:

```powershell
.venv\Scripts\python.exe scripts\build_knowledge_index.py
```

Известный зазор: `untrusted:os` пока нет в `TRUST_LEVELS`
(`src/kivi/safety/trust.py`). `ToolResult` принимает строку, а Context
Builder подставляет `untrusted:tool` для неизвестной метки; но
`memory.store` отвергнет её, если результат попытаются сохранить в память.
Перед сохранением оконных результатов в память метку нужно добавить в trust
levels.

## 11. Ограничения

- **Нет UIA по умолчанию.** Win32 видит top-level окна, но не элементы
  управления. UWP-приложения часто живут в `ApplicationFrameHost`, дочерние
  окна и вкладки не перечисляются. `PywinautoBackend` — только явный
  fallback для focus/close, не общий UIA-драйвер.
- **Нет admin/elevated.** Запуск не повышает права, `SetForegroundWindow` и
  `WM_CLOSE` к elevated-окнам могут быть отвергнуты; `app.terminate` не
  завершит процесс с более высокими правами. Для admin-операций — отдельный
  elevated broker, не этот слой.
- **Foreground lock.** Windows может отказать в первом `SetForegroundWindow`,
  если фокус у другого приложения; backend делает вторую попытку и затем
  возвращает понятную ошибку. Надёжный фокус — после ручного клика.
- **`WM_CLOSE` — просьба.** Приложение может показать диалог сохранения,
  свернуться в трей или проигнорировать сообщение; «окно закрылось» не
  гарантируется. `close` не ждёт исчезновения окна.
- **Console/own/untitled окна скрыты** из `window.list`; скрытые окна
  доступны только по handle.
- **Нет ввода.** Ни клавиатуры, ни мыши, ни скриншотов (для снимков —
  `screen.capture`), ни перемещения/ресайза окон, ни виртуальных столов и
  раскладки по мониторам.
- **PID reuse.** `app.terminate` проверяет процесс до завершения, но между
  проверкой и `TerminateProcess` pid теоретически может быть переиспользован.
- **Заголовки — данные.** Оконный заголовок может содержать текст, похожий
  на инструкцию; он помечен `untrusted:os`, обрезан и не исполняется.
