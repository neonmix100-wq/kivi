# Tools и Permission Engine

Инструмент — единственный способ, которым Kivi меняет что-то вне своего
процесса. Контракт, права и аудит появились раньше первого инструмента: это
инвариант проекта, а не надстройка.

## 1. Контракт

```python
class Tool(Protocol):
    name: str                     # fs.read, shell.exec, ...
    description: str
    risk: Risk                    # R0..R4
    capabilities: tuple[str, ...] # запрашиваемые права
    args_model: type[BaseModel]   # Pydantic, extra="forbid"

    def dry_run(self, args, ctx) -> Preview: ...
    def run(self, args, ctx) -> ToolResult: ...
```

- аргументы типизированы и валидируются до любых действий;
- `dry_run` ничего не меняет и показывает точное действие;
- `ToolResult` возвращает ошибку как значение, а не падение;
- инструмент не видит БД и не вызывает другие инструменты — только `ToolContext`;
- результат ограничен по размеру, артефакты — ссылками.

## 2. Риски и режимы

| Риск | Пример | A4-Bounded |
|---|---|---|
| R0 | чтение, список файлов | авто |
| R1 | запись в workspace | авто |
| R2 | `shell.exec`, перезапись вне workspace | подтверждение |
| R3 | отправка, установка, admin | подтверждение |
| R4 | деньги, необратимые внешние действия | запрет |

Autonomy ниже `A4-Bounded` ужесточает таблицу: `A2` требует подтверждения для
R1, `A0` запрещает всё. Понизить строгость конфигом нельзя — таблица в коде.

## 3. Permission Engine

- `issue(task_id, capabilities, ttl_s)` выдаёт grant на 5 минут по умолчанию;
- grant привязан к задаче, TTL и точному набору capabilities;
- отозванный или просроченный grant не работает;
- инструмент не может выдать себе новый capability;
- каждый отказ и подтверждение пишутся в `audit` (`actor=permission`);
- подтверждение (`--approve`) превращает `confirm` в `allow`, но **не** для R4.

## 4. Protected Zones

Проверка идёт после канонизации пути:

- запрещены UNC, alternate data streams, device paths, `..`, 8.3-имена;
- symlink и junction запрещены; путь после `realpath` обязан остаться в workspace;
- встроенный список (в коде, не расширяется инструментом):
  - `src/kivi/safety` — safety core;
  - `src/kivi/kernel` — аудит и хранилище;
  - `config` — конфигурация и denylist;
  - `data/kivi.db`, `data/logs`, `data/backups` — аудит и логи;
  - системные префиксы Windows;
  - `.env`, `*.pem`, `*.key`, `*.kdbx`, `id_rsa*` и подобные;
  - `private/`, `voice-dataset/`, `.git/`, `.ssh/`.
- всё остальное внутри workspace доступно; вне workspace — hard deny.

Ограничение среза: проверка идёт на уровне путей, а не ОС. Команда `shell.exec`
запускается с подтверждением и минимальным env, но может обратиться к
абсолютному пути из своего кода. Полная изоляция появится с песочницей и
elevated broker; до тех пор R2 — обязательный барьер.

## 5. Инструменты

| Инструмент | Риск | Capability | Что делает |
|---|---|---|---|
| `fs.read` | R0 | `fs.read` | читает UTF-8 файл, до 64 КБ по умолчанию |
| `fs.list` | R0 | `fs.read` | список каталога без перехода по ссылкам |
| `fs.write` | R1 | `fs.write` | атомарная запись UTF-8, только workspace |
| `shell.exec` | R2 | `shell.exec` | argv без shell, минимальный env, таймаут |

## 6. CLI

```powershell
kivi tools list
kivi tools list --json
kivi tools run fs.read --args '{"path":"README.md","max_bytes":2000}'
kivi tools run fs.list --args '{"path":"src/kivi"}'
kivi tools run fs.write --args '{"path":"data/artifacts/note.txt","content":"hi"}'
kivi tools run fs.write --args '{"path":"docs/x.md","content":"new","overwrite":true}'
kivi tools run shell.exec --args '{"argv":["python","-c","print(1)"]}' --dry-run
kivi tools run shell.exec --args '{"argv":["python","-c","print(1)"]}' --approve
```

`--dry-run` показывает JSON-preview и не требует подтверждения. R2 без
`--approve` возвращает ошибку `confirmation required`; после просмотра dry-run
команда повторяется с `--approve`.

## 7. Аудит

Каждая попытка пишет строку `audit`:

- `action=tool.<name>`, `actor=tool`;
- `decision=allow|deny|confirm|error`;
- `target` — путь, cwd или имя инструмента;
- `details` — риск, capability, task_id, `args_digest` (SHA-256 аргументов),
  признак dry-run.

Секреты в аргументах не хешируются в открытом виде, но и не печатаются в
результат; хранить пароли в аргументах всё равно нельзя.

## 8. Осталось

- подписанный манифест зон и добавление зон из конфига;
- trust labels и санитайзер недоверенного контента;
- `proc.*`, `clipboard`, `screen.capture`, `http.get`;
- elevated broker (UAC) и kill switch;
- injection-корпус `tests/security/injection`.
