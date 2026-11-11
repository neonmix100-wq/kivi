# Почта (M4): Gmail OAuth, чтение и черновики, отправка невозможна

Gmail — внешний сервис, поэтому почта в Kivi устроена как **данные, а не команды**:
входящее письмо не может попросить Kivi что-то выполнить, а единственное
изменение на стороне Gmail — создание черновика. Отправка не реализована:
в коде нет вызова `messages.send`/`drafts.send`, в OAuth-запросах нет scope
`gmail.send`, а политика запрещает эти методы. `auto_send=false` — не
настройка, а инвариант проекта (`AGENTS.md`, `docs/SECURITY.md` §5.10).

Модули:

- `src/kivi/integrations/gmail.py` — `GmailAuth` (OAuth, токен, refresh, проверка
  scopes) и `GmailClient` (чтение, поиск, черновики, вложения);
- `src/kivi/tools/mail.py` — пять инструментов `mail.*` через общий
  `ToolExecutor`;
- `tests/unit/test_gmail.py`, `tests/unit/test_mail_tools.py` — тесты без сети.

## 1. Архитектура

```text
пользователь / planner (trusted:user)
        │  mail.list / search / read / attachment / draft
        ▼
ToolExecutor  (capabilities, R0–R2, подтверждение R2, аудит)
        ▼
mail.py        (trust=untrusted:email, sanitize + redact, лимиты вывода)
        ▼
GmailClient    (messages.get/list, drafts.create, attachments.get)
        ▼
googleapiclient.discovery.build("gmail", "v1", credentials=...)
        ▼
Gmail API (OAuth token из data/oauth/gmail-token.json)
```

- `GmailAuth` и `GmailClient` не зависят от реестра инструментов и не читают
  конфиг Kivi целиком. `scopes` берутся из `kivi.config`
  (`GMAIL_ALLOWED_SCOPES`), чтобы список был один.
- `GmailClient` строит сервис лениво: при создании объекта сети нет, а
  тесты передают готовый `service` или `credentials`.
- Инструменты разрешают клиент лениво через `client_factory`; создание
  инструмента не требует токена. В `dry_run` клиент вообще не создаётся.
- Ни один инструмент не вызывает другой и не видит БД: всё идёт через
  `ToolExecutor`, поэтому аудит и подтверждения обязательны.

## 2. Scopes

| Scope | Зачем |
|---|---|
| `https://www.googleapis.com/auth/gmail.readonly` | чтение писем, поиск, метаданные вложений |
| `https://www.googleapis.com/auth/gmail.compose` | создание черновиков |

Запрещены и отклоняются при загрузке токена:

- `https://www.googleapis.com/auth/gmail.send`;
- `https://www.googleapis.com/auth/gmail.modify`;
- `https://mail.google.com/` (полный доступ);
- любой scope вне пары readonly+compose.

Загрузчик токена требует **оба** разрешённых scope и **ни одного** лишнего.
Токен без записанного списка scopes отклоняется: доказать его безопасность
нельзя, нужно повторить авторизацию.

Важное ограничение модели угроз: `gmail.compose` технически позволяет
отправлять письма, отдельного scope «только черновики» у Gmail нет. Поэтому
«отправка невозможна» обеспечивается тремя независимыми слоями:

1. **Код:** в `gmail.py` нет методов `send`, нет обращений к
   `messages().send` и `drafts().send`; тест читает исходник и проверяет это.
2. **Политика:** `config.MailSettings` хранит `drafts_only=true`,
   `auto_send=false` и `denied_methods = {users.messages.send, users.drafts.send}`.
3. **Инструменты:** `mail.draft` умеет только `drafts.create`; инструмента
   отправки нет и его нельзя запросить capability.

Если токен украден, scope `gmail.compose` позволит отправить письмо **вне**
Kivi. Это ограничение OAuth, а не Kivi; для чувствительных аккаунтов
используйте отдельный аккаунт и отзывайте доступ в
`myaccount.google.com/permissions`.

## 3. Настройка

### 3.1. Google Cloud

1. Создайте проект в Google Cloud Console и включите **Gmail API**.
2. Настройте **OAuth consent screen**: тип External (или Internal для
   Workspace), добавьте себя в Test users, если приложение в Testing.
3. Создайте **OAuth client ID** типа **Desktop app**.
4. Скачайте JSON клиента и положите его в `config/gmail-client.json`.
5. Не коммитьте этот файл: в нём `client_secret`.

Требуемый фрагмент client secret:

```json
{
  "installed": {
    "client_id": "...apps.googleusercontent.com",
    "client_secret": "...",
    "redirect_uris": ["http://localhost"],
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token"
  }
}
```

В репозитории `config/gmail-client.json` сейчас **не** в `.gitignore` —
перед первым запуском добавьте его вручную (и `config/gmail-client.example.json`
как шаблон без секретов). Kivi не читает этот файл моделью и не возвращает
его содержимое в результаты инструментов.

### 3.2. Авторизация

```python
from kivi.integrations.gmail import GmailAuth

auth = GmailAuth()                 # config/gmail-client.json, data/oauth/gmail-token.json
credentials = auth.authorize()     # откроет браузер, localhost redirect
```

- При первом запуске открывается браузер и локальный redirect на
  `localhost` со случайным портом.
- Токен сохраняется в `<profile>/gmail-token.json` (если задан `profile`) или
  в `data/oauth/gmail-token.json` (`KIVI_HOME` учитывается через `kivi.paths`).
- Файл пишется атомарно, с `chmod 0600` на POSIX; на Windows это best effort:
  `os.chmod` управляет только read-only битом, поэтому реальная защита —
  права NTFS на каталог `data/oauth`. Не публикуйте и не синхронизируйте
  `data/`.
- Токен переиспользуется; при истечении access token выполняется `refresh`,
  результат снова сохраняется. Истёкший токен без refresh token отклоняется
  с просьбой повторить `authorize()`.
- Если файл токена испорчен, содержит запрещённый scope или не имеет refresh
  token, `authorize()` вернёт `GmailAuthError`: удалите (или переместите)
  `<profile>/gmail-token.json` и повторите авторизацию — Kivi не перезаписывает
  подозрительный токен молча.

Пути можно переопределить:

```python
GmailAuth(
    client_secret_path="config/gmail-client.json",
    token_path="data/oauth/gmail-token.json",
    # или profile=Path("private/profiles/main"),
)
```

## 4. Инструменты

| Инструмент | Риск | Capability | Что делает |
|---|---|---|---|
| `mail.list` | R0 | `mail.read` | последние письма: id, thread, subject, from, to, date, snippet, labels |
| `mail.search` | R0 | `mail.read` | то же по Gmail-запросу (`from:`, `is:unread`, ...) |
| `mail.read` | R0 | `mail.read` | заголовки, text/plain, метаданные вложений |
| `mail.attachment` | R1 | `mail.read` | сохраняет одно вложение в `data/attachments`, ≤ 20 MB, имя санитизируется |
| `mail.draft` | R2 | `mail.draft` | создаёт черновик (plain text), требует `--approve`, не отправляет |

Все результаты помечены `trust="untrusted:email"`. Текст письма может
описывать факты, но не может дать Kivi инструкцию. Сниппеты и тело проходят
`sanitize_text` (скрытый HTML, управляющие символы, секретоподобные токены) и
обрезаются по `ctx.limits.max_output_chars`.

`mail.draft` ограничивает: 1–20 получателей, subject ≤ 200 символов без
переводов строк, body ≤ 100 000 символов. Черновик собирается через
`email.message.EmailMessage`, кодируется base64url и уходит в
`users().drafts().create`. `Cc`, `Bcc` и вложения в черновике не
поддерживаются. Возвращается `draft_id`; тело не печатается и не пишется в
аудит.

### 4.1. Примеры

```powershell
.venv\Scripts\kivi.exe mail status
.venv\Scripts\kivi.exe mail auth
.venv\Scripts\kivi.exe mail list --limit 5
.venv\Scripts\kivi.exe mail search "from:alice is:unread" --limit 10
.venv\Scripts\kivi.exe mail read 18c1f2a3b4c5d6e7
.venv\Scripts\kivi.exe mail attachment 18c1f2a3b4c5d6e7 ANGjdJ...
.venv\Scripts\kivi.exe mail draft --to bob@example.com --subject "Re: отчёт" --body "Спасибо!" --approve
```

`mail draft` требует `--approve` (или интерактивного `y` в терминале); без
подтверждения executor возвращает `confirmation required`, и черновик не
создаётся. Альтернативный путь — `kivi tools run mail.*`, где аргументы
передаются JSON-ом.

Инструменты зарегистрированы в `build_registry()` через `build_mail_tools()`,
поэтому CLI и `kivi tools list` видят их без дополнительной настройки.
Тесты собирают собственный реестр с `client_factory` и не требуют токена.

```python
from kivi.integrations.gmail import GmailAuth, GmailClient
from kivi.tools.mail import build_mail_tools

credentials = GmailAuth().credentials()
client = GmailClient(credentials=credentials, max_results=50)
for summary in client.search("is:unread", max_results=5):
    print(summary.date, summary.from_, summary.subject)

info = client.create_draft(["bob@example.com"], "Hello", "Body text")
print(info.draft_id, info.thread_id)
```

## 5. Чтение и поиск

- `list_messages(query="", max_results=10, label_ids=None)` возвращает
  `MessageSummary`; под капотом — `messages.list` + `messages.get` в формате
  `metadata` для каждого id.
- `search(query, max_results=10)` — алиас с обязательным запросом.
- `get_message(message_id)` возвращает `Message`: заголовки, `text/plain`
  (HTML конвертируется в текст и очищается, если plain отсутствует), метаданные
  вложений (`filename`, `mime_type`, `size`, `attachment_id`), labels.
- `max_results` клиента — потолок (по умолчанию 50) и жёсткий предел API 500.
- Тело обрезается до 100 000 символов; флаг `truncated` виден в результате.

## 6. Вложения

`mail.attachment` — единственный способ получить байты вложения:

- каталог фиксирован: `<workspace>/data/attachments`; путь проходит
  `ProtectedZones.check(..., "write")`, поэтому выйти из workspace нельзя;
- имя санитизируется: убираются `..`, оба вида разделителей, управляющие
  символы, `<>:"/\|?*`, зарезервированные имена Windows (`CON`, `LPT1`, ...);
  пустое имя заменяется на `attachment.bin`; длина ≤ 120 с сохранением
  расширения;
- существующий файл не перезаписывается: добавляется `-1`, `-2`, ...;
- потолок — 20 MB, проверяются и заявленный размер, и длина base64, и
  декодированные байты;
- результат: путь, размер, `artifacts`, `quarantined=true`, trust
  `untrusted:email`. Файл не открывается и не сканируется антивирусом —
  это карантин, а не «безопасное вложение»;
- `dry_run` не делает API-вызовов и не создаёт каталог.

## 7. Секреты

- OAuth-токен никогда не попадает в prompt, ToolResult, аудит или логи:
  пишется только путь к файлу, не содержимое.
- Ошибки Gmail API проходят через `redact_secrets` (`sk-...`, `ghp_...`,
  `Bearer ...`, приватные ключи) и обрезаются; тело письма в ошибки не
  попадает.
- `mail.list`/`mail.read` редактируют секретоподобные токены в тексте письма.
- `mail.draft` **отказывается** создавать черновик, если в теле есть
  секретоподобный токен. Пользовательский текст не переписывается и не
  редактируется молча: возвращается ошибка без эха тела, черновик не
  создаётся. Это egress-контроль из `docs/SECURITY.md` §5.6.
- Аудит хранит `args_digest` (SHA-256 аргументов), а не текст письма; тест
  проверяет, что тело черновика не появляется в аудите.
- Client secret (`config/gmail-client.json`) не читается моделью и не
  возвращается инструментами; он нужен только `GmailAuth`.

## 8. Ограничения

- **Отправки нет by design.** Ни `mail.send`, ни автоответов, ни «отправить
  после подтверждения» в этом срезе.
- Нет push/IMAP IDLE и фоновой синхронизации: только запросы по требованию.
- Нет HTML-интерфейса и рендера писем; HTML сводится к тексту, форматирование
  и ссылки теряются.
- Нет Cc/Bcc, вложений в черновиках, шаблонов и подписи.
- Заголовок `From` не задаётся: черновик создаётся от имени авторизованного
  аккаунта; алиасы и подписи не поддерживаются.
- Нет изменения меток, удаления, архивации: scope `gmail.modify` запрещён.
- Один аккаунт и один токен на профиль.
- Gmail-запросы и лимиты API не обходятся; при 429/403 возвращается
  `GmailClientError` с redaction.
- Вложения не проверяются антивирусом и не открываются автоматически.
- Поиск идёт средствами Gmail; полнотекстового индекса локально нет.

## 9. Тесты

```powershell
.venv\Scripts\python.exe -m pytest -q tests\unit\test_gmail.py
.venv\Scripts\python.exe -m pytest -q tests\unit\test_mail_tools.py
.venv\Scripts\python.exe -m ruff check src\kivi\integrations\gmail.py src\kivi\tools\mail.py tests\unit\test_gmail.py tests\unit\test_mail_tools.py
```

Тесты не ходят в сеть:

- OAuth-флоу внедряется через `flow_factory`, refresh — через
  `request_factory`;
- Gmail API — dict-backed fake service, который записывает вызовы;
- fake `send` поднят в `messages`/`drafts` и падает, если его вызвать, поэтому
  «черновик не отправляет» проверяется исполнением;
- покрыты: scopes (включая запрет send/modify), отсутствие токена, битый JSON,
  refresh, MIME-декодирование, list/get/search, лимиты черновика,
  path traversal и 20 MB cap, redaction секретов, trust-метка, аудит.

## 10. Интеграция

- `build_mail_tools(client_factory=...)` возвращает пять инструментов с общим
  ленивым клиентом; `build_registry()` подключает их, CLI `kivi mail` умеет
  `status`, `auth`, `list`, `search`, `read`, `attachment` и `draft`.
- Для прямого вызова нужен grant с `mail.read`/`mail.draft`; отправка не
  требует отдельного запрета, потому что её нет в коде.
- `google-api-python-client`, `google-auth`, `google-auth-oauthlib` пока не
  объявлены в `pyproject.toml`; они установлены в `.venv`. Перед публикацией
  стоит добавить optional extra `mail` и зафиксировать версии.
- Перед коммитом: `config/gmail-client.json` в `.gitignore`, шаблон
  `config/gmail-client.example.json` без секретов, `data/` не коммитится.
