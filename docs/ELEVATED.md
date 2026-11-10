# Elevated broker: admin-операции через UAC

Версия: 0.1 (11.11.2026). Стадия 4.
Связанные документы: `docs/SECURITY.md` §5.13, `docs/ARCHITECTURE.md` §11.7,
`docs/TOOLS.md`.

---

## 1. Назначение

Ядро Kivi, CLI, UI и аватар всегда работают с обычными правами пользователя.
Некоторые действия (например, очистка DNS-кэша или перезапуск службы) требуют
администратора. Для них сделан **узкий elevated broker**, а не «запуск
PowerShell от админа».

Правила:

- ядро не повышает свои права и не переиспользует админ-токен;
- админ-действие — отдельный процесс `kivi.elevated_helper`;
- набор действий фиксирован allowlist'ом в коде;
- каждый вызов требует явного подтверждения пользователя и UAC;
- каждое решение брокера пишется в аудит;
- помощник повторно проверяет запрос и не доверяет клиенту.

Реализация:

- `src/kivi/safety/elevated.py` — allowlist, `ElevatedBroker`, файлы
  запроса/результата, аудит;
- `src/kivi/elevated_helper.py` — отдельная точка входа, которая исполняет
  ровно одну разрешённую операцию;
- `tests/unit/test_elevated.py` — тесты без UAC, без сети и без реальных
  админ-команд.

## 2. Threat model

### Что защищаем

- права администратора на машине пользователя;
- системные службы и сетевые настройки;
- целостность аудита: факт запроса нельзя скрыть от журнала;
- предсказуемость: LLM не может превратить admin-действие в произвольную
  команду.

### От кого защищаемся

1. **Prompt injection / скомпрометированная модель.** Модель предлагает
   действие, но не формирует argv и не запускает процесс. Она может выбрать
   только имя операции из allowlist и её валидированные параметры.
2. **Скомпрометированное или ошибочное ядро.** Даже если код ядра попытается
   передать `command`, `argv` или путь к чужому exe, брокер отклонит запрос:
   таких полей в API нет.
3. **Подмена файла запроса.** Помощник заново проверяет версию, `request_id`,
   операцию и параметры по тому же allowlist. Лишние поля игнорируются.
4. **Случайный запуск без согласия.** `approved=False` по умолчанию; без
   явного подтверждения брокер отказывает. UAC — вторая, независимая
   преграда: пользователь видит системный запрос.
5. **Зависший процесс.** У помощника есть timeout; брокер не ждёт его
   завершения.

### Что явно вне модели угроз

- пользователь-администратор, который сам обходит Kivi и запускает что угодно;
- скомпрометированный Python-интерпретатор, site-packages или сам helper;
- вредонос на уровне ОС, руткит, отключённый UAC;
- подделка уже одобренного пользователем запроса между записью файла и
  запуском (файл лежит в пользовательском `data/elevated` с ограниченным
  доступом; это не защита от локального админа).

Elevated broker уменьшает поверхность атаки, но **не является песочницей
против локального администратора**.

## 3. Allowlist

Allowlist — статический mapping `OPERATIONS` в `kivi/safety/elevated.py`.
Каждая операция хранит описание, список параметров, timeout и builder,
который возвращает фиксированный argv.

| Операция | Фиксированный argv | Параметры | Timeout |
|---|---|---|---|
| `dns.flush` | `["ipconfig", "/flushdns"]` | нет | 30 с |
| `service.query` | `["sc.exe", "query", service]` | `service` | 30 с |
| `service.restart` | `["sc.exe", "restart", service]` | `service` | 60 с |

`service` обязан **точно** входить в `ALLOWED_SERVICES`:

```python
ALLOWED_SERVICES = frozenset({"KiviAgent"})
```

Это намеренно крошечный набор. Расширять его нужно осознанно, вместе с
тестами и ревью: служба должна принадлежать Kivi или управляться им. Служба
вроде `Spooler`, `WinDefend` или `RpcSs` не должна попасть сюда «на всякий
случай».

Ограничения allowlist:

- нет параметра `executable` или `argv`;
- нет `shell=True` и нет строки команды;
- параметры проверяются на точный набор ключей (`extra` запрещён);
- значения параметров проверяются по типу и allowlist;
- builder — единственное место, где собирается argv.

## 4. Поток UAC

```
CLI / вызывающий код
  │  request(operation, params, approved=True)   ← только после явного «да»
  ▼
ElevatedBroker
  ├─ killswitch?           → deny + audit
  ├─ approved?             → deny + audit
  ├─ allowlist/params?     → deny + audit
  ├─ пишет data/elevated/request-<uuid>.json
  ├─ ShellExecuteW(None, "runas", sys.executable,
  │                helper + --request <path>, None, SW_HIDE)
  │     код <= 32 → error + audit
  └─ возвращает status="started" и пути файлов (не ждёт)
        │
        ▼
Windows UAC: пользователь подтверждает запуск
        │
        ▼
kivi.elevated_helper --request <path>
  ├─ читает JSON, проверяет version / request_id / operation / params
  ├─ строит argv заново по allowlist
  ├─ subprocess.run(argv, shell=False, env=minimal, timeout=...)
  ├─ пишет request-<uuid>.result.json
  └─ exit 0 (успех) / 1 (любая ошибка)
```

Важно: `approved=True` — это не «подтверждение задним числом». Вызывающий
код обязан получить согласие пользователя **до** вызова. Брокер не умеет
показывать UI и не считает наличие UAC заменой явному подтверждению.

Реальный launcher — `shell_execute_launcher`; в тестах подменяется
callable-объектом, который получает пути helper'а и запроса. Код возврата
`ShellExecuteW` больше 32 считается успешным запуском; `0` и коды `<= 32`
(включая отмену UAC) — ошибка.

## 5. Request / response JSON

Запрос (`data/elevated/request-<uuid>.json`):

```json
{
  "version": 1,
  "request_id": "4f4c2f0e7d9a4b6e8c1d2a3b4c5d6e7f",
  "operation": "service.restart",
  "params": {"service": "KiviAgent"},
  "created_at": "2026-11-11T01:00:00+00:00"
}
```

Полей `argv`, `executable`, `command`, `cwd` здесь нет и быть не должно.
`request_id` — hex UUID, он же используется в имени файла. Дополнительные
поля помощник игнорирует; они никогда не становятся аргументами процесса.

Ответ (`request-<uuid>.result.json`, тот же basename):

```json
{
  "version": 1,
  "request_id": "4f4c2f0e7d9a4b6e8c1d2a3b4c5d6e7f",
  "operation": "service.restart",
  "ok": true,
  "returncode": 0,
  "stdout": "SERVICE_NAME: KiviAgent\n",
  "stderr": "",
  "truncated": false,
  "error": "",
  "finished_at": "2026-11-11T01:00:01+00:00"
}
```

- `returncode` — `int` или `null`, если процесс не удалось запустить;
- `stdout`/`stderr` обрезаются до `MAX_STREAM_CHARS` (8000 символов) **на
  поток**; `truncated=true`, если обрезано хоть что-то;
- `ok=true` только при `returncode == 0` и отсутствии timeout;
- `error` — короткое описание (`exit code N`, `operation timed out`, причина
  отказа allowlist);
- файл результата читается через `read_result(path)`, который проверяет
  форму; битый/чужой JSON отклоняется.

Файлы не удаляются автоматически: это след аудита. Чистить `data/elevated`
можно вручную, когда нет незавершённых запросов.

## 6. Аудит

Брокер пишет запись `Database.append_audit` на **каждое** решение:

```python
db.append_audit(
    actor="elevated",
    action="elevated.<operation>",
    target=str(request_path),      # или имя операции до создания файла
    decision="allow" | "deny" | "error",
    reason="approval required" | "unknown operation" | ...,
    details={...},
)
```

Записываются:

- отказ из-за killswitch;
- отсутствие `approved=True`;
- неизвестная операция;
- невалидные параметры;
- ошибка записи request-файла;
- отсутствие helper'а;
- исключение launcher'а или код `ShellExecuteW <= 32`;
- успешный запуск (`allow`, reason `approved and launched`).

В `details` для успешного запуска есть `approved`, `request_id`,
`request_path`, `result_path`, `argv`, `params`, `launcher_code`. Для отказа —
`approved` и `params_preview` (обрезанный JSON, чтобы нельзя было раздуть
аудит огромным параметром).

Для неизвестной операции action тоже безопасен: имя приводится к
`elevated.unknown`, если не похоже на нормальный идентификатор.

Helper **не пишет в БД**: elevated-процесс не должен иметь лишнего доступа, а
запуск уже зафиксирован брокером. Результат операции — второй источник
истины в `*.result.json`. Ограничение: если helper упал после запуска, брокер
не добавит отдельную audit-запись; CLI при необходимости может дописать её
по факту чтения результата.

## 7. Как добавить операцию

1. Выберите **фиксированную** команду. Если нужен произвольный argv — это не
   сюда, это `shell.exec` с обычными правами и отдельным подтверждением.
2. Напишите builder с точной проверкой параметров:
   ```python
   def _build_my_operation(params: Mapping[str, Any]) -> tuple[str, ...]:
       _require_params(params, frozenset({"name"}))
       name = params["name"]
       if not isinstance(name, str) or name not in MY_ALLOWED_NAMES:
           raise InvalidParams("name is not allowlisted")
       return ("my-tool.exe", "--action", name)
   ```
3. Добавьте `OperationSpec` в `OPERATIONS` с timeout и списком параметров.
4. Если добавляете службу — расширьте `ALLOWED_SERVICES` отдельным коммитом и
   объясните, почему Kivi вправе ей управлять.
5. Добавьте тесты: валидный argv, неизвестная операция, плохие параметры,
   audit-строка, fake runner (реальную команду не запускать).
6. Обновите эту таблицу и ссылку на `docs/SECURITY.md` §5.13 при необходимости.

Правило ревью: **helper не меняется** при добавлении операции. Если для
новой операции нужен новый код в helper'е — скорее всего, вы пытаетесь
протащить произвольную команду.

## 8. Ограничения и инварианты

- Ядро никогда не запускается elevated и не хранит админ-токен.
- Нет произвольного PowerShell, `cmd.exe`, строк команд и `shell=True`.
- Нет параметра с путём к исполняемому файлу.
- Нет сети и загрузки кода в helper'е.
- Окружение минимальное: только переменные, нужные для старта процесса.
  `PATH` принудительно ограничен `%SystemRoot%\System32` и `%SystemRoot%`,
  пользовательский `PATH` не используется; выставлен
  `NoDefaultCurrentDirectoryInExePath=1`, чтобы текущая папка не подменяла
  `ipconfig.exe`/`sc.exe`.
- `stdin` не передаётся; timeout обязателен; вывод ограничен.
- Брокер не ждёт завершения: статус `started` означает «процесс запущен», а
  не «операция успешна». Успех — только `ok=true` в result-файле.
- `ShellExecuteW` с `runas` показывает UAC; отмена UAC — это ошибка запуска,
  а не «тихий» обход.
- Kill switch опционален, но при подключении имеет приоритет над всем; если
  проверка killswitch падает с исключением, брокер считает switch включённым
  (fail-closed).
- Allowlist — единственный источник admin-возможностей. Расширение
  `ALLOWED_SERVICES` и `OPERATIONS` — осознанное изменение политики, а не
  настройка конфига.
- Request-файл не подписан. Это приемлемо, пока impact ограничен allowlist'ом
  и UAC, но не защищает от процесса того же пользователя, который подложит
  свой request и дождётся подтверждения UAC. Подпись/nonce — возможное
  усиление на следующем шаге, а не текущая гарантия.

## 9. Тесты

```powershell
.venv\Scripts\python.exe -m pytest -q tests\unit\test_elevated.py
.venv\Scripts\python.exe -m ruff check src\kivi\safety\elevated.py `
    src\kivi\elevated_helper.py tests\unit\test_elevated.py
```

Тесты не показывают UAC и не запускают `ipconfig`/`sc.exe`: launcher
подменяется fake-объектом, а helper получает fake `runner`. Проверяются
allowlist, отказ без approval, неизвестная операция, плохая служба,
killswitch, аудит, форма request/result JSON, игнорирование клиентского
`argv`, timeout, truncation и malformed-файлы.

## 10. Подключение к CLI (зона интеграции)

Модуль сознательно не меняет `cli.py`. Ожидаемая схема:

```python
from kivi.safety.elevated import ElevatedBroker, read_result

broker = ElevatedBroker(db, killswitch=kill_switch)
result = broker.request(args.operation, params, approved=args.approve)
# result.status == "started"; позже:
response = read_result(result.result_path)
```

CLI должен:

- иметь команду с `--approve` (без него `approved=False`);
- печатать `operation`, `request_id`, путь к result-файлу;
- отдельной командой/флагом читать результат и показывать `ok`, `returncode`,
  `error`, обрезанный вывод;
- не передавать пользовательские строки в обход `params` allowlist'а;
- проверять killswitch до вызова (брокер тоже проверяет).
