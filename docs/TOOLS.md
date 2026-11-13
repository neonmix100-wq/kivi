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
| `fs.search` | R0 | `fs.read` | поиск имён файлов/папок в workspace |
| `fs.copy` | R1 | `fs.write` | копирование файла/папки без перезаписи |
| `fs.move` | R1 | `fs.write` | перемещение файла/папки без перезаписи |
| `fs.write` | R1 | `fs.write` | атомарная запись UTF-8, только workspace |
| `fs.mkdir` | R1 | `fs.write` | создать одну папку в workspace |
| `fs.rename` | R1 | `fs.write` | переименовать без перезаписи |
| `fs.trash` | R2 | `fs.delete` | переместить в корзину Windows; обратимо |
| `shell.exec` | R2 | `shell.exec` | argv без shell, минимальный env, таймаут |
| `http.get` | R1 | `net.http` | HTTPS GET только на публичные адреса, санитайзер, `untrusted:web` |
| `proc.list` | R0 | `proc.list` | pid, имя и RSS процессов; без командных строк |
| `proc.kill` | R3 | `proc.kill` | завершить процесс после проверки имени; системные запрещены |
| `proc.start` | R3 | `proc.start` | запустить фоновый процесс, вернуть pid; policy + минимальный env |
| `clipboard.read` | R2 | `clipboard.read` | текст из буфера обмена, санитайзер и redaction |
| `clipboard.write` | R2 | `clipboard.write` | заменить текст буфера обмена; подтверждение |
| `screen.capture` | R1 | `screen.capture` | снимок рабочего стола в BMP внутри workspace, `untrusted:vision` |
| `code.run` | R3 | `code.run` | Python в audit-песочнице + Job Object; сеть/процессы/запись вне workspace запрещены |
| `repo.index` | R0 | `repo.read` | индекс файлов/символов репозитория, поиск, summary; только workspace |
| `code.patch` | R1 | `fs.write` | точная замена `old → new` с unified diff и атомарной записью |
| `git.status` | R0 | `git.read` | фиксированный `git status --porcelain=v1 --branch` |
| `git.diff` | R0 | `git.read` | `git diff --stat` и diff одного файла |
| `git.branch` | R2 | `git.branch` | текущая ветка или `git switch -c`; имя валидируется |
| `git.commit` | R3 | `git.commit` | `git add -- <paths>` + commit; без push/amend/обхода hooks |
| `window.list` | R0 | `window.read` | окна: handle, pid, процесс, заголовок, состояние |
| `window.focus` | R1 | `window.control` | фокус по handle или уникальному заголовку |
| `window.minimize/maximize` | R1 | `window.control` | свернуть/развернуть окно |
| `window.close` | R2 | `window.control` | WM_CLOSE с подтверждением; системные процессы запрещены |
| `app.launch` | R2 | `app.launch` | запуск `.exe` без shell, argv, подтверждение |
| `app.terminate` | R2 | `app.terminate` | завершение проверенного процесса |
| `ui.click` | R2 | `ui.control` | клик по координатам, подтверждение, bounds |
| `ui.type` | R2 | `ui.control` | ввод текста, секреты запрещены, redaction |
| `ui.hotkey` | R2 | `ui.control` | whitelist клавиш, Ctrl+Alt+Del запрещён |
| `ui.find` | R1 | `ui.read` | поиск текста/элементов через UIA, без кликов |
| `system.notify` | R1 | `system.notify` | toast через PowerShell, fallback beep, redaction |
| `system.volume.get` | R0 | `system.volume` | громкость 0..100 |
| `system.volume.set` | R1 | `system.volume` | уровень/delta/mute |
| `system.lock` | R2 | `system.power` | блокировка рабочей станции |
| `system.sleep` | R3 | `system.power` | сон, подтверждение |
| `system.shutdown/restart` | R4 | `system.power` | всегда deny политикой |
| `browser.open` | R1 | `browser.control` | HTTP(S) через Playwright/Chrome, SSRF-защита |
| `browser.read` | R1 | `browser.read` | текст страницы, `untrusted:web`, sanitize/redaction |
| `browser.click/fill` | R2 | `browser.control` | CSS-селектор, подтверждение, без JS |
| `browser.download` | R2 | `browser.download` | скачивание в `data/downloads` |
| `browser.close` | R0 | `browser.control` | закрыть сессию, идемпотентно |
| `mail.list/search` | R0 | `mail.read` | список/поиск писем, `untrusted:email` |
| `mail.read` | R0 | `mail.read` | заголовки, plain-text тело, метаданные вложений |
| `mail.attachment` | R1 | `mail.read` | сохранить вложение в `data/attachments`, лимит 20 МБ |
| `mail.draft` | R2 | `mail.draft` | создать черновик; **отправки нет** |
| `telegram.status` | R0 | `telegram.read` | getMe без токена в выводе |
| `telegram.updates` | R0 | `telegram.read` | входящие updates, `untrusted:message` |
| `telegram.send` | R2 | `telegram.send` | allowlist чатов, rate limit, redaction |
| `crm.read` | R1 | `crm.read` | карточка заказа AltCRM через CDP, `untrusted:crm` |
| `crm.fill` | R2 | `crm.write` | комментарий/чек-лист; **не сохраняет** |
| `crm.act` | R2 | `crm.write` | строки акта; **не сохраняет** |
| `crm.task` | R2 | `crm.write` | форма задачи; создание задачи вручную |
| `media.image` | R1 | `media.generate` | бесплатный Pollinations (flux/turbo), prompt 1..500, PNG/JPEG/WebP в `data/media` |
| `media.sdxl` | R1 | `media.generate` | локальный SDXL через SD WebUI API, только loopback, 512–1024 |
| `media.video` | R1 | `media.generate` | локальный MP4 из AI-кадра, 2–8 с, без видео-ключа |

`http.get` дополнительно проверяет `security.blocked_hosts` из
`config/kivi.yaml` (точное имя или glob, например `*.tracker.example`).
Доменный denylist проверяется до DNS; для SSRF-защиты каждый IP всё равно
обязан быть публичным. Соединение идёт на разрешённый IP, а `Host` и SNI
остаются исходным доменом — это закрывает DNS rebinding между проверкой и
подключением.

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
kivi tools run http.get --args '{"url":"https://example.com/","max_bytes":20000}' --dry-run
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

## 8. Kill switch

```powershell
kivi killswitch status
kivi killswitch on --reason "остановить задачу"
kivi killswitch off
```

Флаг хранится в `data/killswitch.json`. Пока он включён:

- `ToolExecutor` отказывает любому инструменту (`fs.*`, `shell.exec`, `http.get`);
- Cloud Broker не делает сетевых вызовов;
- Scout не добавляет провайдеров;
- каждый отказ пишется в аудит (`safety.killswitch`, `tool.*`).

Флаг fail-closed: повреждённый JSON считается включённым. `on` идемпотентен и
сохраняет исходное время срабатывания; `off` удаляет файл и пишет аудит.
При включении Kivi пытается выгрузить модели, которые сейчас обслуживает Ollama
(best-effort); недоступная Ollama не отменяет сам флаг и пишет
`safety.killswitch_unload` в аудит.

Глобальная горячая клавиша (`Ctrl+Alt+J`) обслуживается отдельным listener:

```powershell
kivi hotkey --check     # проверить строку и показать virtual-key
kivi hotkey --once      # дождаться одного нажатия и выйти
kivi hotkey             # слушать до Ctrl+C
```

Listener регистрирует `RegisterHotKey` с `MOD_NOREPEAT` и в цикле
`GetMessageW`; нажатие включает kill switch (`source=hotkey`) и пишет аудит.
Это отдельный процесс: закройте его Ctrl+C, когда не нужен.

## 9. Подписанный манифест зон

Встроенный список Protected Zones живёт в коде. Дополнительные зоны можно
добавить только через подписанный файл:

```powershell
kivi zones init          # создать ключ, манифест и подпись
kivi zones status        # состояние и число зон
kivi zones check         # проверить подпись
kivi zones sign          # переподписать после ручной правки
```

- `config/protected-zones.yaml` — `version: 1` и список зон
  (`id`, абсолютный `path`, `access: [read|write|exec]`, `reason`);
- `config/protected-zones.sig` — HMAC-SHA256 по каноническому JSON;
- `private/safety/zones.key` — 32-байтный ключ, gitignored, создаётся один раз;
- подпись проверяется при каждом запуске `kivi tools run`;
- нет ключа, нет подписи, битая подпись или неизвестные поля → **fail closed**:
  инструменты не запускаются, пока манифест не исправлен или не переподписан;
- манифест только добавляет зоны и не может отменить встроенные.

## 10. Осталось

- подписанный манифест зон и добавление зон из конфига;
- trust labels и санитайзер недоверенного контента (сделано в [TRUST.md](TRUST.md));
- `proc.start`;
- elevated broker (UAC) и глобальная горячая клавиша kill switch;
- pin resolved IP при подключении (закрыть DNS rebinding);
- injection-корпус `tests/security/injection` расширять по мере роста инструментов.
