# Local HTTP API

Небольшой HTTP-слой над M1-чатом. Только стандартная библиотека Python
(`ThreadingHTTPServer`), без FastAPI и внешних зависимостей. API локальный по
умолчанию: слушает loopback и не имеет аутентификации, поэтому loopback —
граница безопасности.

## Назначение и границы

- Один обработчик чата: `handler(message: str) -> str`.
- Два маршрута: `GET /health` и `POST /chat`.
- Все ответы — JSON в UTF-8; ошибки тоже JSON, без stack trace.
- Тело запроса — не больше 64 КиБ.
- Обработчик вызывается последовательно, по одному запросу за раз.

API не принимает решений о политике, памяти и аудите. Это ответственность
`handler` и ядра Kivi; HTTP-слой только передаёт сообщение и возвращает ответ.

## Быстрый старт

```python
from kivi.interfaces.api import LocalApi

api = LocalApi(lambda message: f"Принято: {message}")
api.start()
try:
    print(api.url)  # http://127.0.0.1:<эфемерный порт>
finally:
    api.stop()
```

`start()` идемпотентен и возвращает `self`. `stop()` тоже идемпотентен: его
можно вызывать повторно. После остановки `api.url` поднимает `RuntimeError`.
`port=0` (по умолчанию) выбирает свободный порт; фактический адрес доступен
только через `api.url`.

Проверка из PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/chat `
  -ContentType 'application/json' `
  -Body '{"message":"привет","task_id":"tsk_abc123"}'
```

## Local-first и безопасность

- `host="127.0.0.1"` по умолчанию. Допустимы `localhost`, `127.0.0.0/8` и
  `::1` (loopback-адреса).
- Не-loopback адрес (`0.0.0.0`, `::`, LAN-IP, внешнее имя) при создании
  `LocalApi` вызывает `ValueError`. Осознанный обход — `allow_remote=True`.
- API проверяет loopback `Host` и `Origin` (если он передан), требует
  `application/json` для JSON-мутаций и `audio/wav` для `/transcribe`,
  отдаёт `X-Frame-Options: DENY`. CORS не включён.
- `allow_remote=True` **не добавляет** аутентификацию, TLS или rate limit.
  Используйте его только за доверенным reverse proxy.
- Access-log стандартной библиотеки отключён. Аудит запросов и действий должен
  писать вызывающий код (обёртка вокруг `handler`), а не HTTP-слой.

## Эндпоинты

| Метод | Путь | Назначение |
| --- | --- | --- |
| `GET` | `/health` | Проверка живости и версии |
| `POST` | `/chat` | Один запрос к чату |

### `GET /health`

Ответ `200`:

```json
{
  "status": "ok",
  "version": "0.1.0"
}
```

`/health` не берёт блокировку обработчика, поэтому отвечает, даже пока
выполняется долгий `/chat`.

### `POST /chat`

Запрос:

```json
{
  "message": "привет",
  "task_id": "tsk_abc123"
}
```

- `message` — обязательная непустая строка (пробельная строка считается пустой).
- `task_id` — необязательная строка; API возвращает её без изменений.
- `task_id` не передаётся в `handler`: это только корреляционный идентификатор
  для клиента. Если он нужен Brain, создавайте задачу внутри обёртки или
  генерируйте собственный id.

Ответ `200`:

```json
{
  "answer": "Привет! Чем помочь?",
  "task_id": "tsk_abc123"
}
```

Если `task_id` не передан, в ответе будет `"task_id": null`. Ответ handler'а
возвращается как `answer` без дополнительной обработки.

## Ошибки

Все ошибки имеют вид `{"error": "<code>"}` и
`Content-Type: application/json; charset=utf-8`. Тексты исключений и stack
trace клиенту не отдаются.

| Код | Когда | Тело |
| --- | --- | --- |
| `400` | Невалидный JSON, JSON не объект, нет/пустой/нестроковый `message`, нестроковый `task_id`, нет `Content-Length` | `{"error": "invalid JSON"}` и другие краткие коды |
| `404` | Неизвестный путь или метод | `{"error": "not found"}` |
| `413` | Тело больше 64 КиБ | `{"error": "request body too large"}` |
| `500` | `handler` бросил исключение | `{"error": "internal error"}` |

Примеры:

```json
{"error": "invalid JSON"}
```

```json
{"error": "message must be a non-empty string"}
```

```json
{"error": "internal error"}
```

При `500` подробности пишутся только в локальный лог (факт ошибки, без текста
исключения). Сервер после такой ошибки продолжает работать.

## Лимиты и конкурентность

- `MAX_BODY_BYTES = 64 * 1024` — максимум тела `POST /chat`. При превышении
  возвращается `413`; небольшое превышение дочитывается, чтобы клиент успел
  получить ответ.
- Тело — один JSON-объект в UTF-8. Массивы, `null` и скаляры отклоняются с
  `400`.
- `handler` вызывается под `threading.Lock`: одновременно выполняется не более
  одного запроса к чату. Это сохраняет инвариант «одна тяжёлая локальная модель
  за раз»; параллельные HTTP-запросы ждут в очереди.
- `/health` блокировку не берёт.
- Сервер не прерывает уже выполняющийся `handler` при `stop()`: поток запроса —
  daemon-поток и может завершиться после остановки. Клиенту нужен собственный
  timeout (в тестах — 5 секунд).
- Ответ не ограничивается по размеру на уровне API; бюджет ответа задаёт
  handler/Brain.

## Интеграция с KiviBrain

HTTP-слой ничего не знает о `KiviBrain`; ему нужна только функция
`Callable[[str], str]`. Текущий `KiviBrain` живёт в `kivi.brain.core` и отвечает
через `ask(goal, *, context="", task_id="", approve=None) -> BrainResult`:

```python
from kivi.brain.core import KiviBrain
from kivi.interfaces.api import LocalApi

brain = KiviBrain(llm, registry, executor, permissions)

def handle(message: str) -> str:
    # Создание задачи, память, политика и аудит — внутри Brain/обёртки.
    return brain.ask(message).answer

api = LocalApi(handle)
api.start()
```

Правила интеграции:

- `handler` синхронный и блокирующий; он выполняется в потоке HTTP-запроса, вне
  event loop.
- `BrainResult.answer` возвращается как `answer`; если Brain вернул `ok=False`,
  HTTP-код всё равно `200`, потому что это ответ, а не сбой транспорта.
- Создание `task_id`, запись в память, событие `chat.request` и аудит — задача
  обёртки/Brain, а не `LocalApi`. `LocalApi` только эхо-возвращает `task_id` из
  запроса; в `handler` он не попадает. `KiviBrain.ask` создаёт свой
  `brain-<id>`, если `task_id` не передан.
- **Approvals.** В `/chat` нет канала подтверждения R2/R3-действий. Если
  `approve` не передан, `PlanExecutor` не подтверждает такие шаги, и они
  отклоняются политикой. Не подставляйте `approve=lambda step: True`: это
  обошло бы Action Firewall. Для интерактивных подтверждений нужен отдельный
  UI/канал, а не этот эндпоинт.
- Исключения из Brain перехватывает API и превращает в `500`; наружу не должны
  попадать секреты и трассы.
- Так как вызовы сериализованы, тяжёлую модель не нужно дополнительно
  блокировать на уровне API.

Целевая цепочка для Web-UI: `Web-UI → LocalApi → handler → KiviBrain → Model
Gateway`.

## Будущий Web-UI

- Этап 6: Web-UI может работать в том же процессе и на том же origin, тогда
  CORS не нужен.
- Для отдельного dev-origin понадобится явный, выключенный по умолчанию
  CORS-флаг; сейчас его нет.
- Стриминг (SSE/WebSocket), история сессий, загрузка файлов и аутентификация в
  этот срез не входят. Для них нужен отдельный контракт, а не расширение
  текущих двух маршрутов.
- API остаётся loopback-only; Web-UI не должен требовать `allow_remote=True`.

## Файлы и проверка

- `src/kivi/interfaces/api.py` — `LocalApi`, HTTP-handler, лимиты.
- `tests/unit/test_api.py` — health, chat, task_id, ошибки, лимит тела,
  сериализация handler'а, loopback-guard, идемпотентность `stop()`.

```powershell
.venv\Scripts\python.exe -m pytest -q tests\unit\test_api.py
.venv\Scripts\ruff.exe check src\kivi\interfaces\api.py tests\unit\test_api.py
```
