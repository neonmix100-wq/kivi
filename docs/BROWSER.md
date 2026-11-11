# Браузер (M3): Playwright, отдельный профиль, untrusted-контент

Браузер — это управляемая вкладка Chrome, а не «руки» модели. Kivi открывает
страницу, читает видимый текст, нажимает, заполняет поля и скачивает файлы
только через типизированные инструменты и `ToolExecutor`. JavaScript-вычисления
недоступны, а текст страницы всегда остаётся данными: он не может попросить
Kivi выполнить ещё один инструмент.

Документ описывает `src/kivi/tools/browser.py`, шесть инструментов `browser.*`
и политику, которую они применяют. Реестр подключает их через общую
`BrowserSession`, а CLI-команда `kivi browse` делает безопасный цикл
open → read → close.

## 1. Архитектура

```text
пользователь / planner (trusted:user)
        │  browser.open / read / click / fill / download / close
        ▼
ToolExecutor (capabilities, R0–R4, подтверждение R2, аудит)
        ▼
BrowserSession  ── одна сессия — один BrowserController
        ▼
BrowserController (ленивый Playwright sync API)
        ▼
Chrome persistent context
  data/browser-profile   (отдельный профиль)
  data/downloads         (единственное место для файлов)
```

- `BrowserController` — тонкая обёртка над `playwright.sync_api`. Playwright
  импортируется лениво, только в момент первого запуска; создание объекта не
  запускает браузер.
- На контроллер приходится ровно один persistent context и одна управляемая
  вкладка. `close()` идемпотентен, `with BrowserController(...)` закрывает
  контекст на выходе.
- `BrowserSession` создаёт контроллер при первом обращении и переиспользует его
  для всех `browser.*`. `build_browser_tools()` возвращает шесть инструментов,
  которые делят одну сессию.
- Инструменты не вызывают друг друга и не видят БД. Каждый вызов проходит через
  `ToolExecutor`: capability, риск, подтверждение и аудит — как у остальных
  инструментов.
- `BrowserController` не предоставляет `evaluate` и не исполняет JS. Наружу
  торчат только навигация, видимый текст, клик, заполнение, клавиши и
  скачивание.

### 1.1. Жизненный цикл

1. `browser.open` (или другой инструмент) просит `BrowserSession.controller()`.
2. Контроллер проверяет URL и разрешённый IP, затем запускает Chrome.
3. `data/browser-profile` и `data/downloads` создаются при старте.
4. `browser.close` закрывает контекст; повторный вызов ничего не делает.
5. Сессия остаётся пригодной: следующий `browser.open` лениво создаёт новый
   контроллер. Закрытым окончательно остаётся только сам контроллер — повторный
   `open` на нём возвращает ошибку. Это важно для `kivi serve`, где one-shot
   `browser.close` не должен «отравлять» процесс до перезапуска.

## 2. Профиль и запуск

| Параметр | Значение по умолчанию |
|---|---|
| профиль | `data/browser-profile` (относительно workspace) |
| скачивания | `data/downloads` (относительно workspace) |
| браузер | `channel="chrome"` (системный Chrome) |
| альтернатива | `executable_path="C:\\...\\chrome.exe"` |
| headless | `True` |
| аргументы | `--disable-extensions`, `--no-first-run` |
| downloads | `accept_downloads=True`, `downloads_path=data/downloads` |

Профиль отдельный от личного Chrome: Kivi не читает пароли, cookies и историю
пользователя. Расширения отключены, first-run-мастера нет. Пароли и
автозаполнение чувствительных полей не используются: `browser.fill` вообще
отказывается от полей с `type=password` и от секретоподобного текста.

`channel="chrome"` и `executable_path` взаимоисключающие по смыслу: если задан
путь, используется он, иначе — канал. Несуществующий `executable_path`
отклоняется до запуска. Если Chrome или Playwright не установлены, инструмент
возвращает типизированную ошибку `BrowserUnavailable`, а не падает.

## 3. URL и SSRF-политика

`UrlPolicy` проверяет URL до того, как страница его увидит.

| Правило | Поведение |
|---|---|
| схемы | только `http` и `https` |
| порты | по умолчанию `80` и `443`; другой порт отклоняется |
| порт 8080 | только если явно добавлен в `allowed_ports` |
| credentials | `user:pass@host` запрещены |
| localhost | `localhost` и `*.localhost` запрещены |
| локальные имена | `*.local`, `*.internal`, `*.home.arpa` запрещены |
| IP-литералы | private, loopback, link-local, reserved, multicast и IPv4-mapped IPv6 запрещены |
| DNS | `socket.getaddrinfo`; **каждый** ответ обязан быть глобально маршрутизируемым |
| allow-list | `allowed_hosts` — точное имя или glob (`*.example.com`) |
| редирект | финальный URL после навигации проверяется повторно |
| длина | не больше 2048 символов; пробелы и управляющие символы запрещены |
| backslash | запрещён, чтобы не путать URL и путь Windows |

`http` разрешён требованием этапа, но HTTPS предпочтителен: политика проверяет
адрес, а не шифрование канала. Для чувствительных сценариев используйте HTTPS и
не вводите данные в формы без явного подтверждения.

Проверка DNS идёт через внедряемый resolver, поэтому unit-тесты не ходят в сеть.
Если хотя бы один A/AAAA-ответ приватный, запрос отклоняется целиком — это
защита от подмены DNS и от «смешанных» ответов.

### 3.1. Ограничения SSRF-защиты

- Проверка IP и сама навигация — разные операции. DNS rebinding между ними
  теоретически возможен; полноценное закрытие требует pinning на уровне
  сетевого стека/CDP. Это зафиксировано как future work.
- Playwright следует HTTP-редиректам сам. Kivi проверяет финальный URL, но не
  каждый промежуточный хоп. Если финальный адрес нарушает политику, управляемая
  вкладка закрывается, а не остаётся на заблокированной странице. Для M3 это
  сознательный компромисс.
- Прокси, service workers и загрузка сторонних ресурсов не фильтруются
  поштучно. Страница не получает доступ к локальным файлам: `file:` и `data:`
  запрещены на уровне навигации и селекторов.
- CAPTCHA, анти-бот и агрессивный JS-рендеринг не обходятся.

## 4. Селекторы и клавиши

`validate_selector` принимает только CSS.

- длина не больше 200 символов;
- запрещены управляющие символы;
- запрещены Playwright-движки `xpath=` и `internal:`;
- запрещены маркеры `javascript:`, `data:`, `file:`;
- запрещён выбор элементов `script` (включая `body > script`);
- `text=...` технически разрешён как текстовый движок, но предпочтителен CSS;
- обычные CSS-селекторы (`#main`, `article h1`, `input[name=q]`) разрешены.

`browser.fill` дополнительно вызывает `validate_fill_selector`:

- поле с `type=password` (в разных кавычках и регистре) отклоняется;
- текст, похожий на токен/ключ/пароль (`sk-...`, `Bearer ...`,
  `password=...`, `api_key=...`), отклоняется до ввода;
- длина текста — не больше 4096 символов.

`dry_run` для `browser.fill` показывает селектор и число символов, но не сам
текст: значение может содержать персональные данные, а preview попадает в аудит.

Важно: тип поля может быть выставлен JavaScript уже после загрузки, поэтому
проверка `type=password` — best-effort. Настоящая граница — отказ от
секретоподобного текста и общий egress-контроль. Kivi не должен заполнять
пароли, платёжные данные и токены; для таких сценариев нужен отдельный
vault-адаптер, а не свободные клики по DOM.

Для `BrowserController.press` действует whitelist: одиночные буквы/цифры,
`Enter`, `Escape`, `Tab`, `Backspace`, `Delete`, `Home`, `End`, `PageUp`,
`PageDown`, `Arrow*`, `Insert`, `Space`, `F1–F12` и комбинации с
`Control`/`Alt`/`Shift`/`Meta`. Инструмента `browser.press` в этом срезе нет;
метод контроллера готов для следующего шага.

## 5. Скачивания

- `browser.download` не принимает путь. Модель выбирает только селектор.
- Файл всегда сохраняется в `data/downloads` (workspace-relative).
- Имя из страницы недоверенное: убираются каталоги, `..`, запрещённые для
  Windows символы, зарезервированные имена (`CON`, `NUL`, `COM1`, ...) и
  слишком длинные хвосты.
- Существующий файл не перезаписывается: добавляется `-1`, `-2`, ...
- Размер ограничен 64 МБ; слишком большой файл удаляется и возвращается ошибка.
- Скачанный файл не открывается автоматически и не исполняется. Результат
  помечен `untrusted:web`, а имя файла очищается санитайзером.
- Проверка выхода за пределы каталога выполняется после `resolve()`.

## 6. Модель prompt injection

Правило: **текст страницы — данные, а не полномочие.**

- `browser.read` возвращает результат с `trust="untrusted:web"`.
- Перед возвратом текст проходит общий `sanitize_text`: убираются управляющие
  и zero-width символы, нормализуются пробелы, вырезаются секретоподобные
  токены (`[REDACTED]`), длина ограничивается `max_chars`.
- Эвристики (`ignore previous instructions`, `system override`, `выполни
  команду`, ...) попадают в `meta["suspicious"]`. Это **сигнал**, а не
  разрешение и не автоматическое действие.
- Инструменты не принимают «следующее действие» из текста страницы. Аргументы
  приходят только от planner'а/пользователя; страница не может вызвать
  `browser.click`, `fs.write`, `shell.exec` или другой инструмент.
- `browser.open` тоже очищает title/URL и возвращает `untrusted:web`: заголовок
  вкладки — такой же недоверенный текст, как и body.
- Если страница требует одно, а пользователь другое, побеждает пользователь;
  страница помечается как враждебная. Никакого «наследования» разрешений из
  текста нет.
- `browser.read` не возвращает HTML: только видимый текст. Скрытые элементы,
  комментарии и `script/style` не попадают в результат.

Типичный безопасный поток:

```text
пользователь: «открой страницу и перескажи»
  → browser.open (R1, browser.control)
  → browser.read (R1, browser.read) → untrusted:web
  → planner строит ответ по данным
```

Если для продолжения нужен клик, planner предлагает **отдельный** вызов
`browser.click`; `ToolExecutor` требует подтверждение R2. Текст страницы сам по
себе такого вызова не создаёт.

## 7. Инструменты

| Инструмент | Риск | Capability | Аргументы | Результат |
|---|---|---|---|---|
| `browser.open` | R1 | `browser.control` | `url` | title/url/status, `untrusted:web` |
| `browser.read` | R1 | `browser.read` | `selector="body"`, `max_chars=20000` | видимый текст, `untrusted:web` |
| `browser.click` | R2 | `browser.control` | `selector` | подтверждение executor'а |
| `browser.fill` | R2 | `browser.control` | `selector`, `text` | подтверждение; секреты запрещены |
| `browser.download` | R2 | `browser.download` | `selector` | путь и размер в `data/downloads` |
| `browser.close` | R0 | `browser.control` | — | идемпотентное закрытие; сессия переиспользуема |

Все аргументы — Pydantic-модели с `extra="forbid"`. R2 не обходится: без
`approved=True` executor возвращает `confirmation required`, а `dry_run`
показывает точное действие, не запуская браузер.

`browser.read` возвращает `truncated=True`, если текст обрезан санитайзером или
мог быть обрезан контроллером. `meta` содержит `selector`, `url`,
`original_chars`, `removed` и `suspicious`.

## 8. API контроллера

```python
controller = BrowserController(
    workspace=Path("D:/AI-assistent"),
    headless=True,
    channel="chrome",
    allowed_hosts={"example.com", "*.example.org"},
    allowed_ports=(80, 443),
)

info = controller.open("https://example.com/")   # PageInfo(title, url, status)
text = controller.read_text("body", max_chars=20_000)
controller.click("#next")
controller.fill("input[name=q]", "kivi")
controller.press("input[name=q]", "Enter")
file = controller.download("a#export")            # DownloadInfo(path, size, filename)
controller.close()
```

`PageInfo` и `DownloadInfo` — frozen dataclasses с `as_dict()`.
`BrowserController.policy` отдаёт `UrlPolicy`, `current_url` — текущий URL,
`is_open` — состояние контекста. `start()` запускает браузер без навигации
(используется live-тестом).

## 9. Ограничения

- нет обхода CAPTCHA, anti-bot, Cloudflare и «умных» проверок headless;
- нет ввода паролей, платёжных данных и токенов; password-поля отклоняются;
- нет `evaluate`/произвольного JS, нет чтения cookies и localStorage;
- нет скриншотов, PDF и OCR страницы — это отдельный backlog;
- sync API Playwright не потокобезопасен: один контроллер живёт в одном потоке;
- скачивание, которое не порождает Playwright download-событие, не
  поддерживается; системные диалоги сохранения не используются;
- редиректы проверяются по финальному URL, а не по каждому хопу; DNS rebinding
  закрывается только будущим CDP/сетевым слоем;
- несколько вкладок не поддерживаются: одна управляемая вкладка на контроллер;
- future work: CDP-перехват запросов и ответов, allow-list на каждый ресурс,
  сетевой журнал, несколько вкладок, screenshot/PDF, загрузка файлов на сайт,
  профиль с явным consent.

## 10. Тесты

`tests/unit/test_browser.py` не ходит в сеть:

- fake launcher возвращает fake persistent context; проверяются профиль, флаги,
  `accept_downloads`, `downloads_path`;
- fake resolver проверяет SSRF: private/loopback IPv4 и IPv6, DNS-ошибки,
  редирект на приватный адрес;
- проверяются схемы, credentials, порты, `.local`, `allowed_hosts`, длина URL;
- селекторы: CSS разрешён, XPath/JS/`data:`/`script`/длина/control отклоняются;
- `browser.read`: cap, redaction, control chars, `untrusted:web`, suspicious;
- `browser.click`/`fill`/`download`: подтверждение R2, запрет секретов и
  password-полей, путь и размер скачивания;
- `close()` идемпотентен; после закрытия `open` на том же контроллере возвращает
  ошибку, а `BrowserSession` при следующем вызове создаёт новый контроллер;
- injection-текст остаётся данными: результат помечен `untrusted:web`,
  `can_issue_instructions()` возвращает `False`, никаких дополнительных вызовов
  контроллера нет;
- опциональный live-тест запускает и сразу закрывает headless Chrome, **не
  навигируясь** никуда. Он пропускается, если Chrome/Playwright недоступны, и
  отключается переменной `KIVI_SKIP_LIVE_BROWSER=1`.

`tests/scenarios/test_m3_browser.py` прогоняет те же инструменты через
`ToolExecutor` на fake-контроллере: open/read/click/fill/download/close, отказ
`ftp`, private IP и XPath, а также проверку `untrusted:web` для injection-текста.

Команды:

```powershell
.venv\Scripts\python.exe -m pytest -q tests\unit\test_browser.py
.venv\Scripts\python.exe -m ruff check src\kivi\tools\browser.py tests\unit\test_browser.py
```

## 11. Интеграция

Инструменты создаются одной фабрикой, чтобы делить сессию:

```python
from kivi.tools.browser import build_browser_tools

for tool in build_browser_tools(
    allowed_hosts={"example.com", "*.example.org"},
):
    registry.register(tool)
```

`build_browser_tools()` принимает также `controller_factory` (для тестов или
внешнего контроллера), `allowed_ports` и `resolver`. Реестр использует тот же
принцип: создаёт одну `BrowserSession` и передаёт её всем шести инструментам.
Владелец процесса должен вызвать `BrowserSession.close()` (или закрыть
`browser.close`) при завершении задачи, чтобы не оставлять Chrome.

Задача должна получить grant с нужными capability:

- `browser.control` — open/click/fill/close;
- `browser.read` — read;
- `browser.download` — download.

CLI-команда для базового чтения:

```powershell
.venv\Scripts\kivi.exe browse https://example.com --selector body --max-chars 4000
```

`kivi browse` выдаёт grant на `browser.control` + `browser.read`, открывает
страницу, печатает title и очищенный текст, затем закрывает браузер. Клики,
формы и скачивания из CLI не выполняются: для них нужен planner и
подтверждение R2. Вызовы также доступны через обычный `kivi tools run`, а
`kivi tools list` показывает инструменты, потому что реестр их содержит.
