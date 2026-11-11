# Песочница `code.run`

`code.run` запускает сгенерированный Python в **best-effort** песочнице. Это
первый шаг M2 Coder: код изолирован на уровне Python-аудита и ресурсных лимитов
Windows, но это **не** граница безопасности ОС. Полная изоляция — WSL2 или
Windows Sandbox — запланирована отдельно ([ROADMAP](ROADMAP.md)).

Инструмент — R3: `code.run` требует подтверждения пользователя и capability
`code.run`. Решение о запуске принимает `ToolExecutor`, а не сам инструмент
([TOOLS](TOOLS.md)).

## 1. Что именно запускается

1. Код пишется в `<workspace>/data/sandbox/<uuid>/main.py`.
2. Рядом создаётся `bootstrap.py` — загрузчик песочницы.
3. Запускается отдельный интерпретатор:

   ```text
   python -I -X utf8 <bootstrap.py> <workspace> <main.py> -- <args...>
   ```

   `-I` включает isolated mode (`-E`, `-s`, `-P`): переменные `PYTHON*`
   игнорируются, user site и текущий каталог не попадают в `sys.path`.
   `-X utf8` фиксирует кодировку stdio, чтобы stdout/stderr читались одинаково.
4. `cwd` — корень workspace. `stdin` — `DEVNULL`, `stdout`/`stderr` — пайпы,
   shell не используется, env минимальный (`PATH`, `SYSTEMROOT`, `TEMP` и т.п.).
5. `bootstrap.py` ставит `sys.addaudithook(...)` **до** `exec` пользовательского
   кода и только затем читает и выполняет `main.py`.

### 1.1. Почему не `sys.executable` в venv

На Windows `python.exe` внутри venv — это launcher, который запускает базовый
интерпретатор как дочерний процесс. При `ActiveProcessLimit=1` этот дочерний
процесс создать нельзя, и код падает с `Unable to create process`. Поэтому
`code.run` берёт `sys._base_executable`, если он существует: это настоящий
интерпретатор, а не перенаправляющая обёртка. Побочный эффект — в песочницу не
попадают пакеты venv; это согласуется с правилом «без импортов проекта».

### 1.2. Импорты проекта запрещены

`bootstrap.py` дополнительно удаляет из `sys.path` всё, что лежит внутри
`<workspace>/src`. Это закрывает editable-install (`.pth`), который иначе
добавил бы `src` в путь даже при `-I`. Сгенерированный код получает стандартную
библиотеку и пакеты базового интерпретатора, но не `kivi`, не safety-ядро и не
код проекта. Если коду нужен проект, это должно быть отдельным явным решением,
а не побочным эффектом `code.run`.

### 1.3. Каталоги песочницы

Каталоги `data/sandbox/<uuid>/` не удаляются после запуска: они остаются для
отладки и аудита, путь возвращается в `meta.sandbox_dir`. Политика очистки
(сколько хранить и когда удалять) пока не реализована; это backlog, а не
гарантия приватности.

### 1.4. Аргументы

| Поле | Тип | Ограничение | По умолчанию |
|---|---|---|---|
| `code` | `str` | 1..200 000 символов | — |
| `timeout_s` | `float` | 1..60 | 10 |
| `memory_mb` | `int` | 64..2048 | 512 |
| `args` | `list[str] \| None` | до 16 элементов | `None` |

`timeout_s` — wall-clock; то же значение уходит в `JOB_OBJECT_LIMIT_PROCESS_TIME`
как лимит CPU-времени. `memory_mb` действует только при работающем Job Object.
`args` попадают в `sys.argv[1:]` пользовательского скрипта, а `argv[0]` — путь к
`main.py`.

## 2. Модель угроз

**Что считается недоверенным.** Сгенерированный код и всё, что на него повлияло:
ответ модели, prompt injection из файла/веб-страницы, данные из памяти. Код может
пытаться читать секреты, писать вне workspace, ходить в сеть, запускать процессы,
обходить аудит через нативные вызовы.

**Активы.**

- файлы workspace и данные пользователя;
- секреты: `.env`, ключи, `private/`, `voice-dataset/`, `.git/`, `.ssh/`;
- сеть и внешние сервисы;
- процессы и сессия пользователя;
- safety-ядро Kivi, аудит и конфигурация;
- сама машина (реестр, службы, драйверы).

**Граница доверия.** Всё, что делает дочерний процесс, считается недоверенным.
Родитель доверяет только собственному коду: `CodeRunTool`, Job Object, таймауту
и проверкам `ToolExecutor`. Результат помечается `trust="untrusted:code"`.

**Допущения.**

- аудит-хук установлен до пользовательского кода;
- код — обычный Python, а не нативная библиотека;
- Job Object удалось создать и назначить (иначе `job_object: false`);
- R3-подтверждение действительно получено;
- workspace — это каталог задачи, а не весь репозиторий Kivi (см. §5).

**Вне модели угроз.** Полноценная изоляция ОС, защита от нативного кода,
side-channel-атаки, вредоносные драйверы, атаки на ядро Windows.

## 3. Что блокирует audit hook

Хук минимальный и не пытается «исправить» исключение: он поднимает
`PermissionError("sandbox: ...")`. `bootstrap.py` не перехватывает его:
исключение всплывает, Python печатает traceback в stderr и завершает процесс
с кодом 1.

### 3.1. Файлы

| Событие | Правило |
|---|---|
| `open` с режимом записи (`w`, `a`, `x`, `+`) | внутри workspace, кроме защищённых префиксов `src/kivi`, `tests`, `docs`, `config`, `pyproject.toml`, `data/kivi.db`, `data/killswitch.json` |
| `open` на чтение | разрешено внутри workspace, `sys.base_prefix`, `sys.prefix`, каталога `sys.executable`, `stdlib`, `platstdlib`, `purelib`, `platlib` |
| `os.listdir`, `os.scandir` | только внутри тех же read-root'ов |
| `os.remove`, `os.rmdir`, `os.mkdir`, `os.rename`, `os.replace`, `os.link`, `os.symlink`, `os.truncate`, `os.chmod` | только внутри workspace, кроме тех же защищённых префиксов |
| любые пути | `realpath` + проверка префикса; `..` раскрывается до проверки |

**Чувствительные имена** блокируются и на чтение, и на запись, даже внутри
workspace:

- файлы: `.env`, `.env.*`, `*.pem`, `*.key`, `*.pfx`, `*.p12`, `*.kdbx`,
  `id_rsa*`, `id_ed25519*`, `secrets.*`, `credentials.*`;
- каталоги в любом месте пути: `private`, `voice-dataset`, `.git`, `.ssh`.

Список продублирован в `bootstrap.py` и в `src/kivi/safety/zones.py`. Это
сознательная копия: bootstrap не может импортировать `kivi` (иначе он потянет
проект в песочницу). При изменении одного списка нужно править оба; расхождение
— дефект.

### 3.2. Сеть

Блокируются все события `socket.*`, включая обязательные
`socket.__new__`, `socket.connect`, `socket.bind`, `socket.getaddrinfo`,
`socket.gethostbyname`. Сообщение: `sandbox: network access is disabled`.

### 3.3. Процессы и нативные библиотеки

| Событие | Сообщение |
|---|---|
| `subprocess.Popen` | `sandbox: process creation is disabled` |
| `os.system` | то же |
| `os.exec`, `os.spawn`, `os.posix_spawn`, `os.startfile` | то же |
| `import ctypes`, `import _ctypes` | `sandbox: native module is not allowed` |
| `ctypes.dlopen` | `sandbox: native library loading is disabled` |

Импорт `ctypes` блокируется отдельно, потому что сам `import ctypes` на Windows
сразу вызывает `LoadLibrary` для `kernel32`, а `ctypes` ловит `OSError` (включая
`PermissionError`) и превращает его в невнятный `AttributeError`. Блокировка
импорта сохраняет понятное сообщение. Прямой `ctypes.dlopen` тоже заблокирован —
на случай, если модуль уже был загружен до установки хука.

### 3.4. Чего хук не делает

- не переписывает код и не пытается «починить» опасный вызов;
- не блокирует `os.stat` и чтение метаданных вне read-root'ов;
- не реализует произвольные Protected Zones внутри дочернего процесса, но
  блокирует фиксированные защищённые префиксы Kivi (см. §3.1); CLI `kivi coder`
  запускает задачу в отдельном workspace `data/projects/<task>`;
- не является kernel boundary: нативный код может обойти Python-хук.

## 4. Job Object

На Windows родитель создаёт Job Object через `ctypes` и назначает ему процесс
сразу после `Popen`. Лимиты:

| Флаг | Значение | Смысл |
|---|---|---|
| `JOB_OBJECT_LIMIT_PROCESS_MEMORY` | `memory_mb` (64..2048) | лимит памяти на процесс |
| `JOB_OBJECT_LIMIT_PROCESS_TIME` | `timeout_s` (1..60) | лимит user-mode CPU-времени, в 100-нс тиках |
| `JOB_OBJECT_LIMIT_ACTIVE_PROCESS` + `ActiveProcessLimit=1` | 1 | запрет дочерних процессов |
| `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` | — | закрытие handle убивает оставшиеся процессы |

Дополнительно родитель держит **wall-clock** таймаут через
`process.communicate(timeout=...)`. По истечении: `TerminateJobObject`, затем
`process.kill()` (best-effort), затем повторный `communicate`, чтобы забрать
вывод и код возврата. Handle Job Object закрывается в `finally`.

Если Job Object создать или назначить не удалось (не Windows, CI с вложенными
job'ами, гонка с уже завершившимся процессом), запуск **не отменяется**: остаётся
subprocess-таймаут, а в `meta` пишется `job_object: false`. Это важно для
честной диагностики: `sandboxed: true` означает «хук установлен», а
`job_object` — «ресурсные лимиты Windows применены».

`JOB_OBJECT_LIMIT_PROCESS_TIME` ограничивает **CPU-время**, а не wall-clock:
спящий процесс может провисеть до родительского таймаута. Поэтому нужны оба
механизма.

Job Object — это resource/lifetime limit, а не security boundary: он не мешает
читать файлы, не изолирует сеть и не защищает от нативного кода. Его задача —
не дать коду съесть память/CPU и оставить процессы после завершения.

## 5. Что песочница НЕ защищает

Это главный раздел для ревью. Всё ниже — известные ограничения, а не забытые
баги.

### 5.1. Обход через ctypes и нативный код

Аудит-хук — это Python-код в том же процессе, что и недоверенный код. Нативный
код, `ctypes`-трюки, прямые syscall'ы, загруженные заранее DLL, `_winapi` и
другие внутренние модули могут обойти проверки. Блокировка `import ctypes` и
`ctypes.dlopen` закрывает очевидные пути, но не является барьером против
целенаправленного побега. Любой, кто может выполнить нативный код, находится
вне модели угроз этой песочницы.

### 5.2. Нет OS-level изоляции файловой системы

Нет mount namespace, AppContainer, отдельного пользователя, ACL-границы или
виртуального диска. Проверки путей делает Python-хук; это не мешает:

- гонкам TOCTOU (путь проверен, затем заменён symlink/junction);
- `os.stat` и чтению метаданных вне разрешённых корней;
- записи в защищённые префиксы Kivi (`src/kivi`, `tests`, `docs`, `config`,
  `pyproject.toml`, `data/kivi.db`, `data/killswitch.json`) — они заблокированы
  хуком; остальное пространство workspace задачи доступно. Для недоверенного
  кода нужен отдельный workspace задачи, а не корень репозитория;
- нативному коду, который вообще не вызывает Python-хук.

`realpath` и проверка префикса снижают риск обычных `..`/symlink-трюков, но не
дают гарантии ОС.

### 5.3. Сеть

Блокируются только Python-события `socket.*`. Нативный код, сторонние DLL,
DNS через другие API, уже открытые сокеты и сетевые побочные эффекты не
контролируются. Нет network namespace, firewall-правила или `--network none`.

### 5.4. Процессы

`subprocess.Popen`, `os.system`, `os.exec*`, `os.spawn*` и `os.startfile`
блокируются хуком; `ActiveProcessLimit=1` — вторая линия. Но при
`job_object: false` остаётся только `process.kill()`, который не гарантирует
убийство всего дерева процессов. Job Object назначается уже после `Popen`,
поэтому между стартом и `AssignProcessToJobObject` есть короткое окно, в
котором лимиты ещё не действуют; код в это время ещё не выполняется (стартует
интерпретатор), но это не формальная гарантия. Нативный обход снова вне модели.

### 5.5. Ресурсы

Job Object ограничивает память, CPU-время и число процессов, но не:

- диск (код может забить workspace файлами);
- GPU, handles, threads, IO;
- размер вывода до того, как родитель его прочитает (родитель режет уже
  полученные stdout/stderr по `max_output_chars`, по умолчанию 8000 символов
  на поток);
- время на загрузку интерпретатора (оно входит в wall-clock таймаут).

### 5.6. Секреты и данные

Список чувствительных имён конечен: нестандартные имена, секреты в env,
секреты в уже открытых файлах и данные, попавшие в вывод, не redact'ятся
песочницей. `code.run` возвращает `untrusted:code`; санитайзер и redaction —
задача вызывающего слоя. Env минимальный, но не пустой: `USERPROFILE`,
`LOCALAPPDATA`, `TEMP` и подобные наследуются, потому что без них не стартует
интерпретатор.

### 5.7. Целостность workspace и persistence

Код может перезаписать файлы проекта, создать мусор и оставить каталоги
`data/sandbox/<uuid>/`. Нет автоматического отката, бэкапа или очистки.
R3-подтверждение — единственный барьер; для недоверенного кода нужен отдельный
workspace.

### 5.8. Платформа

Job Object доступен только на Windows. На других ОС `job_object: false`, и
остаются только audit hook + subprocess-таймаут. WSL2 и Windows Sandbox пока не
подключены.

## 6. Почему это best-effort

- Python audit hook — это in-process механизм, а не системный вызов ядра; он
  защищает от случайного и типового вредоносного Python-кода, но не от нативного.
- Job Object — лимит ресурсов и времени жизни, а не контейнер.
- На Windows нет дешёвого unprivileged-механизма, который даёт одновременно
  FS-изоляцию, network off и лимиты без установки WSL2/Windows Sandbox.
- Полноценная песочница требует отдельного backend'а, монтирования и
  инфраструктуры; это отдельный этап, а не одна функция.
- Поэтому защита строится слоями: R3-подтверждение → `ToolExecutor`/аудит →
  audit hook → Job Object/таймаут → `untrusted:code` на выходе. Каждый слой
  снижает риск, но ни один не является границей ОС.

## 7. Как перейти на WSL2 / Windows Sandbox

Целевая схема из [ARCHITECTURE](ARCHITECTURE.md): генерированный код — в
WSL2-дистрибутиве `kivi-sandbox` или Windows Sandbox, смонтирован только
workspace, сеть выключена, лимиты CPU/RAM/времени, дерево процессов убивается.

### 7.1. WSL2 (`kivi-sandbox`)

1. Отдельный дистрибутив `kivi-sandbox`, непривилегированный пользователь.
2. Только workspace: bind mount каталога задачи в `/workspace` (для тестов —
   read-only, для патчей — read-write), без остального диска.
3. Сеть: `--network none` либо firewall-правила; проверить, что DNS и outbound
   недоступны.
4. Лимиты: cgroups v2 (`memory.max`, `cpu.max`, `pids.max`), timeout через
   `systemd-run --scope`/`timeout`, при зависании — `wsl --terminate`.
5. Запуск: `python -I /workspace/...`, seccomp/AppArmor по возможности.
6. `code.run` выбирает backend по конфигу; при отсутствии WSL2 — fallback на
   текущий best-effort или отказ с объяснением, как включить.

### 7.2. Windows Sandbox

1. `.wsb`-конфиг: `Networking=Disable`, mapped folder только workspace,
   `ReadOnly` для тестов, `LogonCommand` для запуска.
2. Песочница одноразовая: после закрытия состояние исчезает; артефакты
   забираются через mapped folder.
3. Минусы: дорогой старт, доступна не во всех редакциях Windows, не подходит
   для частого запуска тестов.

### 7.3. Docker Desktop (опционально)

`docker run --rm --network none --memory ... --pids-limit ... --read-only -v
<workspace>:/workspace python:3.12-slim`. Тяжелее WSL2 и требует Docker Desktop;
рассматривать как альтернативу, а не основной путь.

### 7.4. Абстракция backend'а

Чтобы не менять публичный контракт `code.run`, backend стоит вынести за
протокол:

```python
class SandboxBackend(Protocol):
    name: str
    def run(self, code: str, args: list[str], limits: SandboxLimits) -> RunResult: ...
```

- `LocalAuditJob` — текущая реализация;
- `WslSandbox` — WSL2;
- `WindowsSandbox` — `.wsb`;
- `meta.backend` показывает, что реально применилось.

Критерии приёмки для следующего шага: код не пишет вне смонтированного
workspace (проверяет ОС, а не hook), сеть недоступна, fork/memory bomb
останавливаются лимитами, таймаут убивает дерево процессов, импорт проекта
недоступен.

## 8. Наблюдаемость и интеграция

`ToolResult` для `code.run`:

| Поле | Значение |
|---|---|
| `ok` | `returncode == 0` и не было таймаута |
| `output` | stdout + stderr, каждый поток обрезан по `max_output_chars` |
| `error` | tail stderr, либо `timed out after Ns`, либо `exit code N` |
| `meta.returncode` | код возврата |
| `meta.timed_out` | сработал wall-clock таймаут |
| `meta.sandboxed` | audit hook установлен |
| `meta.job_object` | Job Object создан и назначен |
| `meta.bytes` | размер кода в UTF-8 |
| `meta.output_bytes` | суммарный размер сырого stdout/stderr |
| `meta.sandbox_dir` | каталог `data/sandbox/<uuid>` |
| `trust` | `untrusted:code` |

Каждый вызов проходит через `ToolExecutor` и пишет аудит `tool.code.run` с
`args_digest`; сам код в аудит не попадает. `dry_run` показывает размер кода,
таймаут, память и предупреждение о выполнении, ничего не создавая.

Регистрация: `CodeRunTool` добавлен в `build_registry()` в
`src/kivi/tools/registry.py`, поэтому инструмент доступен через
`kivi tools run code.run`. Команда `kivi coder` использует R3-путь через
`Coder.run_code` и запрашивает подтверждение перед запуском. Пример вызова:

```powershell
kivi tools run code.run --args '{"code":"print(1)"}' --dry-run
kivi tools run code.run --args '{"code":"print(1)"}' --approve
```

Тесты: `tests/unit/test_code_tool.py`. Они не ходят в сеть и запускают только
короткие фрагменты; таймаут проверяется `sleep`, который родитель убивает через
секунду.

## 9. Backlog песочницы

- backend WSL2 `kivi-sandbox` и Windows Sandbox;
- политика очистки `data/sandbox/*` и квота на размер;
- redaction секретов в `output` до возврата вызывающему;
- проверка/запрет запуска `code.run` против репозитория Kivi (отдельный
  workspace задачи);
- тесты на попытки обхода: `ctypes`, `_winapi`, symlink/junction, TOCTOU,
  fork/memory bomb;
- единый источник списка чувствительных имён для `zones.py` и `bootstrap.py`.
