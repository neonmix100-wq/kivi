# Локальный SDXL через Stable Diffusion WebUI API

Срез `media.sdxl`: локальная генерация картинок через уже установленный
Stable Diffusion WebUI (`D:\stable-diffusion-webui`) и его HTTP API. Инструмент
не запускает сервер, не скачивает модели и не ходит в интернет: он делает один
`POST` на loopback-адрес WebUI, проверяет ответ и сохраняет PNG/JPEG в
`data/media`.

Главный принцип тот же, что у `media.image`: сгенерированная картинка — это
**данные**, а не команда. Даже локальный результат помечается `untrusted:web` и
не имеет права инструктировать планировщик.

Модули:

- `src/kivi/tools/sdxl.py` — `SdxlConfig`, `SdxlGenerateArgs`, `SdxlTool`,
  проверка loopback, payload и декодирование ответа;
- `tests/unit/test_sdxl.py` — тесты без сети на `httpx.MockTransport`;
- этот документ.

## 1. Что реализовано, а что нет

| Возможность | Статус |
|---|---|
| `media.sdxl` через `POST /sdapi/v1/txt2img` | ✅ реализовано |
| Loopback-only (`127.0.0.1`, `localhost`, `::1`) | ✅ реализовано |
| `prompt`, `negative_prompt`, размеры, steps, CFG, seed, sampler | ✅ реализовано |
| Проверка PNG/JPEG magic bytes и лимит 15 МБ | ✅ реализовано |
| Сохранение в `data/media/sdxl-<uuid>.<png\|jpg>` через zone check | ✅ реализовано |
| `dry_run` без сети | ✅ реализовано |
| Инъекция `client=` для тестов, без сети в конструкторе | ✅ реализовано |
| Регистрация в `build_registry` и CLI | ✅ отдельным интеграционным шагом |
| Очередь job'ов, прогресс, отмена, batch | ⬜ нет |
| img2img, ControlNet, LoRA, refiner, hires fix | ⬜ нет |
| Переключение чекпойнта через API | ⬜ нет и не планируется в этом срезе |

Инструмент уже зарегистрирован в `build_registry()` как `SdxlTool()` и имеет
CLI-команду `kivi media sdxl`. Эти изменения сделаны отдельным интеграционным
шагом; в текущем срезе файлы `registry.py` и CLI не редактировались.

## 2. Требования к WebUI

### 2.1. Запуск с API

WebUI должен быть запущен и API должен быть включён:

```powershell
cd D:\stable-diffusion-webui
.\webui-user.bat --api
```

В текущем `webui-user.bat` уже стоит `--api --xformers --autolaunch`, но
`--api` можно указать и явно. Без `--api` endpoint `/sdapi/v1/txt2img`
отвечает `404`, а инструмент вернёт `SdxlError` с подсказкой.

Проверка, что API жив:

```powershell
curl.exe http://127.0.0.1:7860/sdapi/v1/sd-models
```

WebUI по умолчанию слушает `127.0.0.1:7860`. **Не включайте `--listen` ради
этого инструмента**: Kivi принимает только loopback, а у API WebUI нет
аутентификации.

### 2.2. Чекпойнт SDXL

SDXL base 1.0 уже лежит в проекте:

```text
D:\AI-assistent\data\models\sdxl\sd_xl_base_1.0.safetensors
```

WebUI ищет чекпойнты в своём каталоге `models\Stable-diffusion`. Есть два
варианта:

1. **Скопировать** (или переместить) файл в
   `D:\stable-diffusion-webui\models\Stable-diffusion\sd_xl_base_1.0.safetensors`
   и перезапустить WebUI.
2. **Указать каталог при запуске**, не копируя 6,9 ГБ:

   ```powershell
   .\webui-user.bat --api --ckpt-dir D:\AI-assistent\data\models\sdxl
   ```

   После старта выберите `sd_xl_base_1.0.safetensors` в UI (или добавьте
   `--ckpt sd_xl_base_1.0.safetensors`).

Инструмент не переключает модель и не передаёт `override_settings`: он
использует тот чекпойнт, который уже загружен в WebUI. Если загружен SD 1.5,
картинка получится, но это будет не SDXL.

### 2.3. VRAM

SDXL base 1.0 в 1024×1024 занимает примерно **7 ГБ VRAM** (с `--xformers`;
на разных драйверах и настройках цифра меняется). 16 ГБ хватает на SDXL, но
не вместе с параллельно загруженной 8B Q4 LLM. Пока нет lease/VRAM-бюджета
через `ModelGateway`, инструмент не резервирует VRAM и не выгружает другие
модели. На 8 ГБ и меньше используйте `--medvram`/`--lowvram` в WebUI и
уменьшайте разрешение.

## 3. Конфигурация

```python
SdxlConfig(
    base_url="http://127.0.0.1:7860",
    timeout_s=300.0,
    steps=20,
    cfg_scale=5.0,
    sampler="DPM++ 2M Karras",
)
```

| Поле | Тип | По умолчанию | Ограничения |
|---|---|---|---|
| `base_url` | `str` | `http://127.0.0.1:7860` | только loopback, `http`/`https`, без credentials/query/fragment |
| `timeout_s` | `float` | `300.0` | 1..1800 |
| `steps` | `int` | `20` | 1..50 |
| `cfg_scale` | `float` | `5.0` | 1..15 |
| `sampler` | `Literal` | `DPM++ 2M Karras` | `DPM++ 2M Karras`, `Euler a`, `DDIM` |

`SdxlConfig` — Pydantic-модель с `extra="forbid"` и `frozen=True`. Хост
проверяется при создании конфига: `example.com`, `192.168.x.x`, `10.x.x.x`,
`0.0.0.0` и IPv4-mapped адреса, не являющиеся loopback, отвергаются. Флага
`allow_remote` нет: удалённый WebUI без аутентификации не поддерживается.

## 4. Аргументы инструмента

`SdxlGenerateArgs` — Pydantic-модель с `extra="forbid"`.

| Поле | Тип | По умолчанию | Ограничения |
|---|---|---|---|
| `prompt` | `str` | — | 1..1000 символов, обрезается по краям |
| `negative_prompt` | `str \| None` | `None` | до 1000 символов |
| `width` | `int` | `1024` | 512..1024, кратно 8 |
| `height` | `int` | `1024` | 512..1024, кратно 8 |
| `steps` | `int \| None` | `None` | 1..50; `None` — взять из `SdxlConfig.steps` |
| `cfg_scale` | `float \| None` | `None` | 1..15; `None` — взять из `SdxlConfig.cfg_scale` |
| `seed` | `int \| None` | `None` | 0..2 147 483 647 |
| `sampler` | `Literal \| None` | `None` | три значения; `None` — взять из `SdxlConfig.sampler` |

Пример:

```json
{
  "prompt": "a red fox in snow, soft morning light",
  "negative_prompt": "blurry, low quality, text",
  "width": 1024,
  "height": 768,
  "steps": 20,
  "cfg_scale": 5.0,
  "seed": 42,
  "sampler": "DPM++ 2M Karras"
}
```

Риск — **R1**: запись нового файла внутри workspace. В `A4-Bounded` это
автоматическое действие; при более строгой автономии срабатывает общая
таблица рисков, как для любого R1-инструмента. Capability —
`media.generate`.

## 5. API-запрос

Единственный endpoint:

```text
POST {base_url}/sdapi/v1/txt2img
```

Payload собирается из фиксированного allow-list. В нём ровно восемь полей:

```json
{
  "prompt": "a red fox in snow",
  "negative_prompt": "",
  "width": 1024,
  "height": 1024,
  "steps": 20,
  "cfg_scale": 5.0,
  "sampler_name": "DPM++ 2M Karras",
  "seed": -1
}
```

- `seed: -1` — стандартное значение WebUI «выбери случайный seed». Если
  пользователь задал `seed`, передаётся он.
- `negative_prompt` всегда строка; `None` превращается в `""`.
- **Нет** `alwayson_scripts`, `override_settings`, `init_images`,
  `batch_size`, `script_name`, `sd_model_checkpoint` и любых других полей.
  Аргументы с такими именами отвергаются на уровне Pydantic.
- Нет загрузки локальных файлов и нет URL, которым можно управлять из
  аргументов. `base_url` задаётся только доверенным кодом при создании
  `SdxlConfig`.
- Таймаут по умолчанию 300 секунд; редиректы не преследуются
  (`follow_redirects=False`).

`dry_run` возвращает `Preview` с endpoint, payload, каталогом вывода и
таймаутом, но **не делает запрос** и не создаёт `data/media`.

## 6. Проверка ответа и файл

WebUI отвечает:

```json
{"images": ["<base64>", "..."]}
```

Инструмент берёт первый элемент и проверяет:

1. **HTTP ровно 200.** Иначе `SdxlError` с кодом; для 404/405 добавляется
   подсказка про `--api`.
2. **JSON-объект.** Битый JSON или массив — `SdxlError`.
3. **`images` — непустой список.** Иначе `SdxlError`.
4. **base64.** Поддерживается и `data:image/png;base64,...`. Пустая строка —
   ошибка; невалидный base64 — ошибка.
5. **Размер.** Не больше **15 МБ** (`MAX_IMAGE_BYTES`); проверяется и длина
   base64, и декодированные байты.
6. **Magic bytes.** Только PNG (`89 50 4E 47 0D 0A 1A 0A`) и JPEG
   (`FF D8 FF`). GIF, WebP, SVG, HTML и произвольные байты отвергаются.
   Расширение берётся из magic bytes, а не из ответа.

Файл:

```text
<workspace>/data/media/sdxl-<32 hex uuid>.<png|jpg>
```

- Путь не задаётся моделью или пользователем: имя генерирует инструмент.
- Перед записью путь проходит `ProtectedZones.check(path, "write")`.
- Запись атомарная: временный файл в том же каталоге, затем `replace`.
- В `ToolResult.meta`: `path`, `bytes`, `seed`, `seed_sent`, `params`,
  `width`, `height`, `content_type`, `base_url`, `endpoint`.
- `artifacts` содержит один путь; trust-метка — `untrusted:web`.
- Картинка не декодируется и не парсится: `width`/`height` в meta — это
  запрошенные значения, а не фактические. Для фактических размеров нужен
  отдельный шаг с явным декодером.

## 7. Ошибки

| Ситуация | Исключение | Что видит executor |
|---|---|---|
| WebUI не запущен, connection refused/connect timeout | `SdxlUnavailable` | `ok=False`, подсказка `webui-user.bat --api` |
| HTTP не 200 | `SdxlError` | `ok=False`, `SD WebUI returned HTTP ...` |
| Ответ не JSON / не объект | `SdxlError` | `ok=False` |
| Нет `images`, пустой список | `SdxlError` | `ok=False` |
| Битый/пустой base64 | `SdxlError` | `ok=False` |
| Не PNG/JPEG или больше 15 МБ | `SdxlError` | `ok=False` |
| Read timeout (сервер завис) | `SdxlError` | `ok=False` |
| Заблокированный промпт | `ToolError` | `ok=False`, до сети |

`SdxlUnavailable` — подкласс `SdxlError`, оба — подклассы `ToolError` из
`kivi.tools.base`, поэтому `ToolExecutor` возвращает структурированный
`ToolResult(ok=False)`, а не падает. Частичный файл не остаётся.

## 8. Политика промптов и права

Два барьера срабатывают **до** сети:

1. **Control-символы.** Промпт и `negative_prompt` не должны содержать
   категории Unicode `Cc`, `Cf`, `Cs`, `Co`, `Cn`: NUL, bidi, zero-width, BOM
   и подобные. Перевод строки и табуляция тоже запрещены.
2. **Denylist.** `DEFAULT_PROMPT_DENYLIST` из `media.py` применяется к
   положительному промпту: NSFW/sexual/minor-термины блокируются по словам.
   `SdxlTool(blocked_terms=(...))` только **добавляет** термины; убрать
   дефолтные нельзя. К `negative_prompt` применяется только проверка
   control-символов, потому что `nsfw` в негативном промпте — нормальная
   практика.

Это грубая эвристика, а не классификатор. Она не гарантирует, что SDXL не
сгенерирует нежелательное изображение, и не заменяет правила WebUI,
расширений и закона. Ответственность за промпт и использование результата
остаётся на пользователе. Обход denylist не поддерживается.

Права и приватность:

- Промпт остаётся на локальном WebUI (loopback), а не уходит в облако. Но сам
  WebUI и его расширения могут логировать запросы; не вставляйте в промпт
  секреты, токены, пароли и персональные данные.
- SDXL base 1.0 распространяется по **CreativeML Open RAIL++-M**. Лицензия
  модели и права на сгенерированное изображение — ответственность
  пользователя; перед коммерческим использованием проверьте условия.
- Инструмент не проверяет лицензию чекпойнта, не читает метаданные и не
  гарантирует «чистоту» результата. Сгенерированное изображение может быть
  неточным или нарушать чужие права.

## 9. Очередь, параллелизм и лимиты

Очереди **пока нет**:

- один вызов — один синхронный `POST`; инструмент не создаёт job и не
  возвращает `job_id`;
- нет retry, отмены, прогресса и batch-генерации;
- WebUI сам сериализует запросы своей очередью, но Kivi её не использует и не
  показывает;
- таймаут 300 секунд; генерация 1024×1024 на слабой GPU может быть медленной;
- не запускайте несколько вызовов параллельно в обход WebUI: это делит VRAM
  и может привести к OOM;
- лимит ответа — 15 МБ на декодированную картинку.

## 10. Ограничения

- WebUI должен быть уже запущен с `--api`; инструмент не запускает и не
  останавливает его.
- Нужен загруженный SDXL-совместимый чекпойнт. Переключения модели нет.
- Только txt2img. Нет img2img, inpaint, ControlNet, LoRA, refiner, hires fix,
  face restore и extra networks.
- Нет `alwayson_scripts` и `override_settings` by design: это не «недоработка»,
  а граница безопасности.
- Только PNG/JPEG; WebP и GIF не принимаются.
- Фактические размеры и фактический seed не извлекаются из `info`; в meta
  лежит запрошенный seed и `seed_sent` (`-1` для случайного).
- Нет аутентификации и TLS: API WebUI доверяет любому локальному процессу,
  поэтому loopback — единственная разрешённая граница.
- Нет fallback на Pollinations. `media.image` и `media.sdxl` — независимые
  инструменты; автоматического переключения между ними нет.
- Нет VRAM-бюджета/lease через `ModelGateway`: инструмент не выгружает LLM и
  не резервирует память.

## 11. Интеграция и тесты

Инструмент зарегистрирован в `build_registry()` и доступен через CLI:

```powershell
.venv\Scripts\kivi.exe media sdxl "kivi portrait" --width 1024
.venv\Scripts\kivi.exe media sdxl "a red fox" --width 768 --height 512 --steps 8
```

Регистрация, CLI-команда, строка в `docs/TOOLS.md` и соответствующие тесты
добавлены отдельным интеграционным шагом. В текущем срезе файлы
`src/kivi/tools/registry.py` и `src/kivi/interfaces/cli.py` не редактировались:
созданы только `sdxl.py`, тесты и этот документ.

Если нужно пересобрать навигацию после правок документации:

```powershell
.venv\Scripts\python.exe scripts\build_knowledge_index.py
```

Проверки среза:

```powershell
.venv\Scripts\python.exe -m pytest -q tests\unit\test_sdxl.py
.venv\Scripts\python.exe -m ruff check src\kivi\tools\sdxl.py tests\unit\test_sdxl.py
```

Тесты не ходят в сеть: клиент собирается на `httpx.MockTransport`. Покрыты
успешный PNG и JPEG, `data:` base64, точный allow-list payload, отсутствие
`alwayson_scripts`/`override_settings`, loopback-валидация, custom base_url,
dry-run без сети, HTTP-ошибка, connect error с подсказкой `--api`, read
timeout, битый JSON, пустой/невалидный base64, не-image magic, превышение
15 МБ, неверные размеры/шаги/CFG/sampler, control-символы, denylist, путь
`data/media`, R1-аудит и отказ без capability `media.generate`. Один live-тест
проверяет `127.0.0.1:7860` и **пропускается**, если WebUI не запущен; он
никогда не стартует сервер.

## 12. Связанные документы

- [MEDIA.md](MEDIA.md) — облачный `media.image` через Pollinations;
- [TOOLS.md](TOOLS.md) — контракт инструментов, риски, зоны, аудит;
- [TRUST.md](TRUST.md) — метки доверия и `untrusted:web`;
- [SECURITY.md](SECURITY.md) — границы доверия и политика данных;
- [ROADMAP.md](ROADMAP.md) — этап 12, M7 целиком;
- [STATUS.md](STATUS.md) — текущий статус проекта.
