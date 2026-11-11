# Telegram (M4): бот, allowlist и rate limits

Kivi общается с Telegram только через **Bot API** и только тремя методами:
`getMe`, `getUpdates`, `sendMessage`. User-account (MTProto, вход по номеру
телефона) не поддерживается и не будет добавлен: это другой протокол, другие
риски бана и доступа к личным чатам.

Документ описывает `src/kivi/integrations/telegram.py`,
`src/kivi/tools/telegram.py` и политику, которую они применяют.

## 1. Что поддерживается

| Возможность | Статус |
|---|---|
| `getMe` — имя и id бота | ✅ |
| `getUpdates` — входящие сообщения (long polling) | ✅ |
| `sendMessage` — текст в разрешённый чат | ✅ |
| Секреты в исходящем тексте | ❌ отклоняются |
| Broadcast / рассылка по списку | ❌ нет |
| Файлы, фото, документы, `getFile`, скачивание | ❌ нет |
| User-account / MTProto | ❌ нет |
| Webhook | ❌ нет, только long polling |
| Произвольный метод Bot API | ❌ нет |

Ограничения намеренные: у инструмента нет способа скачать файл из Telegram или
загрузить туда файл, поэтому украденный токен не превращается в канал
эксфильтрации через этот модуль. Вложения — отдельный срез, см. §10.

## 2. Настройка через BotFather

1. Откройте `@BotFather` в Telegram, отправьте `/newbot`.
2. Задайте отображаемое имя и username (username обязан заканчиваться на `bot`).
3. BotFather покажет токен вида `123456789:AA...`. Он показывается один раз;
   сохраните его в менеджер паролей, а не в репозиторий.
4. Для групп: добавьте бота в группу. Если бот должен видеть все сообщения, у
   `@BotFather` выполните `/setprivacy` → выберите бота → `Disable`. Иначе бот
   видит только команды и упоминания.
5. Узнайте `chat_id`: напишите боту в личку или в группу и вызовите
   `telegram.updates` (или `getUpdates` вручную). У групп и каналов id
   отрицательный, например `-1002003004005`.
6. Пропишите токен и id в переменные окружения (см. §3). Файл `.env` Kivi не
   читает; секреты не должны попадать в `config/kivi.yaml`, логи и коммиты.

Токен даёт полный контроль над ботом. Компрометация токена — это смена токена
через BotFather (`/revoke`), а не «перевыпуск пароля».

## 3. Переменные окружения

| Переменная | Назначение | Пример |
|---|---|---|
| `KIVI_TELEGRAM_TOKEN` | токен бота | `123456789:AA...` |
| `KIVI_TELEGRAM_CHAT_IDS` | allowlist chat id через запятую | `100500,-1002003004005` |

PowerShell:

```powershell
$env:KIVI_TELEGRAM_TOKEN = "123456789:AA..."
$env:KIVI_TELEGRAM_CHAT_IDS = "100500,-1002003004005"
```

Правила:

- **Пустой allowlist означает «никому нельзя»** — fail closed. Это дефолт
  свежей установки; `telegram.send` вернёт ошибку allowlist, а не отправит
  сообщение «куда-нибудь».
- Явные аргументы `TelegramConfig(token=..., allowed_chat_ids=...)` важнее
  переменных окружения. Явный пустой `allowed_chat_ids` тоже означает «никому»,
  а не «взять из env».
- `0` не является валидным chat id и отклоняется.
- Пробелы вокруг значений и пустые элементы `1, 2,` допускаются.
- `api_base` по умолчанию `https://api.telegram.org`; разрешён только `https`,
  query и fragment запрещены.

## 4. Конфиг и клиент

```python
from kivi.integrations.telegram import TelegramClient, TelegramConfig

config = TelegramConfig()  # token и allowlist из env
with TelegramClient(config) as client:
    me = client.get_me()
    print(me.id, me.username)
```

- `TelegramConfig.token` исключён из `repr`; токен не попадает ни в `repr`, ни в
  `str`, ни в результаты инструментов, ни в текст исключений.
- HTTP-ошибки `httpx` перехватываются и превращаются в generic
  `TelegramError`; исходное исключение отсоединяется (`from None`), потому что
  его текст содержит URL с токеном.
- Токен обязателен в path (`/bot<token>/method`) по требованию Bot API. Чтобы
  он не утёк даже в INFO-лог `httpx`, клиент один раз ставит фильтр, который
  заменяет `/bot<token>/` на `/bot[REDACTED]/`.
- `httpx.Client(timeout=15)`, redirects не следуются: иначе 3xx мог бы увести
  токен на чужой хост.
- `close()` идемпотентен; `with TelegramClient(...)` закрывает пул соединений.
- Клиент умеет ровно три метода. `_request("getFile", ...)` вернёт
  `TelegramError: telegram method is not allowed`, не сетевой вызов.

## 5. Rate limits

Лимиты локальные, на процесс; они защищают от случайного спама и от
«зацикленного» планировщика, а не от лимитов Telegram.

| Лимит | Значение | Поведение |
|---|---|---|
| На чат | 1.0 с между сообщениями | `RateLimiter.acquire` спит остаток интервала |
| Глобально | 20 сообщений / 60 с | token bucket; 21-е сообщение сразу даёт `RateLimitError` |

- Глобальный бюджет — token bucket: 20 токенов, пополнение `20/60` в секунду.
  Это допускает всплеск до 20 сообщений, а затем плавное пополнение; это не
  строгое скользящее окно.
- `RateLimitError` не ждёт и не ставит сообщение в очередь: инструмент
  возвращает ошибку, сообщение не отправляется.
- Токен лимитера расходуется **после** проверки allowlist, длины и секретов.
  Отклонённый вызов не съедает бюджет.
- Часы и `sleep` инъектируются (`RateLimiter(clock=..., sleep=...)`), поэтому
  тесты не ждут реальные секунды.
- Серверные 429 приходят как generic `TelegramError`; автоматического retry нет.
- Перезапуск процесса сбрасывает лимиты: состояние не персистится.

## 6. Инструменты

| Инструмент | Риск | Capability | Аргументы | Результат |
|---|---|---|---|---|
| `telegram.status` | R0 | `telegram.read` | — | id, username, first_name, флаги; токена нет |
| `telegram.updates` | R0 | `telegram.read` | `limit` 1..50, `offset` ≥ 0, `timeout_s` 0..10 | сообщения, `untrusted:message` |
| `telegram.send` | R2 | `telegram.send` | `chat_id`, `text` 1..4096, `parse_mode` | квитанция, `trusted:system` |

Общие правила:

- `extra="forbid"`: неизвестное поле (например, `broadcast`) — ошибка
  валидации, а не тихо проигнорированный аргумент.
- нет полей для файлов, media, нескольких получателей и произвольного метода;
- каждый вызов проходит через `ToolExecutor`: capability, риск, подтверждение
  R2 и аудит — как у остальных инструментов.

### `telegram.status`

Вызывает `getMe` и возвращает `id`, `username`, `first_name`,
`can_join_groups`, `can_read_all_group_messages`. Токен не возвращается ни в
`output`, ни в `meta`. Без токена инструмент падает до сети с ошибкой
`telegram bot token is not configured`. `dry_run` сети не касается.

### `telegram.updates`

Читает `getUpdates` и возвращает JSON:

```json
{
  "count": 1,
  "next_offset": 101,
  "updates": [
    {
      "update_id": 100,
      "chat_id": 100500,
      "message_id": 7,
      "date": 1700000000,
      "sender": "alice",
      "chat_title": "",
      "has_media": false,
      "text": "привет",
      "suspicious": [],
      "removed": {}
    }
  ]
}
```

- Текст сообщения всегда недоверенный: результат помечен
  `untrusted:message`, `can_issue_instructions()` для него `False`.
- Перед возвратом текст проходит `sanitize_text`: убираются управляющие и
  zero-width символы, секретоподобные строки заменяются на `[REDACTED]`,
  подозрительные фразы попадают в `suspicious`.
- `has_media=true` только сообщает, что в сообщении есть вложение; Kivi его не
  скачивает и не читает.
- Содержимое сообщений не логируется.
- `next_offset` — следующий `update_id`, который имеет смысл запросить.
  Смещение не хранится на диске: его передаёт вызывающая сторона.
- `limit` ограничен 50, хотя Bot API допускает 100: инструмент намеренно
  консервативнее. `timeout_s` не больше 10, чтобы уложиться в HTTP-таймаут 15 с.

### `telegram.send`

Порядок проверок в `run`:

1. токен сконфигурирован;
2. `chat_id` не `0` и входит в allowlist;
3. `text` длиной 1..4096 символов;
4. текст не похож на секрет;
5. только теперь списывается токен rate limiter;
6. `sendMessage`.

- R2: `ToolExecutor` требует `--approve`/`approved=True`. `dry_run` показывает
  preview без сети и без подтверждения.
- Секретоподобный текст (**пароли, токены, ключи, `Bearer`, private key**)
  отклоняется, а не отправляется в отредактированном виде: пользователь должен
  сам решить, что именно уходит наружу. Сообщение об ошибке не содержит текст.
- В `dry_run` preview показываются `chat_id`, число символов и первые 120
  символов после redaction; полный текст в аудит не пишется.
- Результат `trusted:system`: текст составил сам Kivi, а Telegram подтвердил
  доставку. Это не даёт права считать trusted текст, пришедший обратно.
- Один вызов — один чат. Broadcast отсутствует.

## 7. Безопасность и egress

- **Allowlist fail closed.** Пустой список = запрещено всё. Явный список
  побеждает env, env побеждает «по умолчанию».
- **Нет user-account.** Только бот; личные чаты и телефон не используются.
- **Токен.** Только env или явный аргумент; не в `repr`, не в результате, не в
  ошибках; фильтр для INFO-логов `httpx`; HTTPS; redirects выключены.
- **Egress.** Исходящий канал один — `sendMessage` с текстом. Egress-политика
  из `docs/SECURITY.md` §5.6 применяется в упрощённом виде: секретоподобный
  текст блокируется; файлы из Protected Zones невозможно приложить, потому что
  API вложений в этом срезе нет; инструмент не читает workspace.
- **Недоверенный контент.** Входящие сообщения — данные, не инструкции. Они
  санитизируются и помечаются `untrusted:message`.
- **Нет произвольного API.** Набор методов зашит в клиент; новые методы
  добавляются кодом и тестами, а не аргументом инструмента.
- **Аудит.** Каждый вызов пишется `ToolExecutor`; аргументы хешируются
  (`args_digest`), текст сообщения не логируется.
- **Подтверждение.** R2 для отправки — обязательный барьер; `dry_run` не
  считается разрешением на сеть.
- **Лимиты.** 1 с на чат и 20/мин глобально снижают риск случайного спама и
  «дорогого» цикла.

Чего эта защита **не** делает: не проверяет смысл текста, не мешает отправить
персональные данные, если они не похожи на секрет, и не заменяет серверные
лимиты Telegram. Решение об отправке остаётся за пользователем (R2).

## 8. Интеграция

Реестр и CLI в этом срезе не меняются. Точка подключения —
`build_telegram_tools()`; она создаёт три инструмента с одним runtime, одним
клиентом и одним лимитером:

```python
from kivi.tools.registry import ToolRegistry
from kivi.tools.telegram import build_telegram_tools

registry = ToolRegistry()
telegram_tools = build_telegram_tools()  # TelegramConfig() из env
for tool in telegram_tools:
    registry.register(tool)

# при остановке приложения:
telegram_tools[0].runtime.close()
```

Для тестов или нестандартного транспорта:

```python
import httpx
from kivi.integrations.telegram import (
    RateLimiter,
    TelegramClient,
    TelegramConfig,
)
from kivi.tools.telegram import build_telegram_tools

config = TelegramConfig(token="123:test", allowed_chat_ids=frozenset({100}))
transport = httpx.MockTransport(handler)  # handler возвращает JSON Bot API
tools = build_telegram_tools(
    config=config,
    client_factory=lambda: TelegramClient(config, transport=transport),
    limiter=RateLimiter(clock=fake_clock, sleep=fake_clock.sleep),
)
```

Вызовы `telegram.send` идут как R2: `kivi tools run telegram.send --args ...
--approve` после `--dry-run`, когда инструменты будут добавлены в реестр.
`telegram.read` и `telegram.send` должны быть в grant задачи.

## 9. Тесты

```powershell
.venv\Scripts\python.exe -m pytest -q tests\unit\test_telegram.py tests\unit\test_telegram_tools.py
.venv\Scripts\python.exe -m ruff check src\kivi\integrations\telegram.py src\kivi\tools\telegram.py tests\unit\test_telegram.py tests\unit\test_telegram_tools.py
```

51 тест, сеть не используется:

- конфиг: env, приоритет явных аргументов, пустой allowlist, `repr` без токена,
  невалидные значения без эха;
- клиент: `getMe`, `getUpdates`, `sendMessage`, payload, generic-ошибки,
  отсутствие токена в исключениях и в INFO-логе, отсутствие файловых методов;
- лимитер: интервал на чат, глобальные 20/мин, `RateLimitError`, пополнение;
- инструменты: allowlist, отказ при пустом списке, секреты, длина, R2
  confirmation, dry-run без сети, `untrusted:message` и `trusted:system`,
  общий client factory, rate limits через реальный клиент с `MockTransport`.

Все HTTP-вызовы идут через `httpx.MockTransport`; часы и `sleep` — фейковые.

## 10. Ограничения и следующие шаги

- **Вложения и файлы** — отдельный срез. Нужны: allowlist типов и размеров,
  egress-проверка содержимого, запрет Protected Zones, отдельный capability и
  подтверждение. До этого `sendDocument`/`sendPhoto`/`getFile` в коде нет.
- **Webhook** не реализован: только long polling. Для постоянного процесса
  понадобится хранение offset и аккуратное завершение.
- **Очередь.** При глобальном лимите сообщение не ставится в очередь; вызывающая
  сторона решает, повторить позже или нет.
- **Мультибот.** Один runtime = один токен; несколько ботов — несколько
  runtime с разными allowlist.
- **Группы.** При включённом privacy mode бот не видит обычные сообщения; это
  настройка BotFather, а не Kivi.
- **Персистентность лимитов.** Счётчики живут в памяти процесса.
