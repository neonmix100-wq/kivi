# Work CRM: headless domain

Домен Work CRM выделен из старого `D:\ai-agent\modes\work` без tkinter и без
импортов `core`/`modes`. Это чистый Python-слой: факты звонка, проверка цитат,
черновики для CRM, промпты extractor/coach, история звонков и метрики. Сеть,
Ollama и UI сюда не входят — LLM передаётся вызывающим кодом как callable.

Код: [`src/kivi/modules/work_crm/`](../src/kivi/modules/work_crm/).
Тесты: [`tests/unit/test_work_crm_domain.py`](../tests/unit/test_work_crm_domain.py)
(49 тестов, без сети и Ollama).

Атрибуция: портированные модули помечены комментарием
`Adapted from D:\ai-agent\modes\work` и сохраняют поведение старых функций,
кроме исправленных очевидных ошибок (например, безопасный разбор чисел в акте и
обработка мусора вместо падения).

---

## 1. Доменная модель

Один звонок превращается в объект фактов (`facts`) со строгой формой. Схема
описана в [`schema.py`](../src/kivi/modules/work_crm/schema.py):

| Секция | Значение | Статус | Цитата |
|---|---|---|---|
| `price` | `amount_rub`, целое | `agreed` / `discussed` / `not_mentioned` | `evidence` |
| `works` | `items[]`: `name`, `price_rub`, `warranty_days`, `status`, `evidence` | вычисляется по работам | у каждой работы своя |
| `parts` | `items[]`, строки | да | да |
| `deadline` | `text` | да | да |
| `old_parts` | `answer`: `yes` / `no` / `not_discussed` | да | да |
| `warranty` | `text` | да | да |
| `risks` | `items[]`, строки | да | да |

Дополнительно:

- `decision.value` — `agreed` / `refused` / `thinking` / `unclear`, плюс
  `evidence` и вычисленный `verified`;
- `objections` — список возражений клиента;
- `followup` — `needed`, `when_text`, `reason`;
- `summary` — короткий итог разговора.

`FACTS_SCHEMA` — JSON Schema для модели, `VALUE_KEY` — соответствие секции и
ключу значения, `SECTIONS` — русские названия разделов.

### Статусы

- **`agreed`** — клиент явно согласился именно с этим пунктом. Допускается
  только при проверенной цитате из речи клиента.
- **`discussed`** — пункт обсуждался, но согласия нет: менеджер предложил, а
  клиент молчит, торгуется, сомневается или говорит «подумаю».
- **`not_mentioned`** — пункт не упоминался или значение пустое (`0`, `[]`,
  `""`, `not_discussed`).

Раздел `works` получает `agreed` только если **все** работы согласованы;
иначе `discussed`. Пустой список работ — `not_mentioned`.

---

## 2. Защита от галлюцинаций: цитаты

Главное правило: **`agreed` без дословной цитаты клиента невозможно**.
[`quote_found`](../src/kivi/modules/work_crm/schema.py) нормализует текст
(нижний регистр, `ё → е`, пунктуация → пробелы, сжатие пробелов) и ищет цитату
точно или нечётко через `difflib.SequenceMatcher` с порогом `min_ratio=0.8`.
Порог терпит небольшие ошибки распознавания, но не выдуманные фразы.

Алгоритм [`normalize`](../src/kivi/modules/work_crm/schema.py):

1. Если модель поставила `agreed`, цитата ищется в `client_text` — только в
   репликах клиента.
2. Не найдена — статус понижается до `discussed`, а цитата проверяется по
   полному тексту (для истории и комментария).
3. Если `discussed`, цитата проверяется по полному тексту.
4. Пустое значение принудительно даёт `not_mentioned`.
5. Для `decision` значения `agreed`/`refused` без цитаты клиента становятся
   `unclear`; `thinking` и `unclear` не требуют цитаты.
6. Мусор на входе (не словарь, строка вместо секции, неверный тип) не бросает
   исключение: возвращаются пустые факты.

У каждой секции и каждой работы есть поле `verified` — прошла ли цитата
проверку. Это не «модель уверена», а результат детерминированной проверки.

### Разбор расшифровки

[`extractor.py`](../src/kivi/modules/work_crm/extractor.py) содержит helpers:

- `parse_transcript(text) -> list[Utterance]` — строки `Клиент:` / `Менеджер:`,
  `Client` / `Manager` / `Оператор`, `Голос 1` / `Голос 2`, `Участник`,
  необязательные метки времени `[00:05]` / `00:05:12`;
- `client_only_text(text)` — только реплики клиента (источник согласий);
- `manager_only_text(text)`, `speaker_only_text(text, speaker)`;
- `transcript_as_text(utterances, last_n=None)` — обратная сборка.

Соответствие дорожек: **`Голос 1` — менеджер (микрофон), `Голос 2` — клиент**
(системный звук). Это соглашение старого STT-конвейера. `Участник` означает, что
дорожки не разделены: атрибуция клиента невозможна, поэтому автоматических
`agreed` в такой записи не будет. Если внешний код знает разметку лучше, он
может передать `client_text` в `FactExtractor.extract` явно.

---

## 3. Черновики для CRM

[`drafts.py`](../src/kivi/modules/work_crm/drafts.py) — детерминированные
функции без LLM. Они не переспрашивают модель и не добавляют факты.

| Функция | Результат |
|---|---|
| `build_comment(facts, now=None)` | Комментарий звонка: решение, «Согласовано», «Обсуждалось, не согласовано», возражения, следующий контакт |
| `build_checklist(facts)` | Поля «Чеклист менеджера»: только `agreed` |
| `build_act_rows(facts)` | Строки акта: только согласованные работы |
| `act_rows_to_text(rows)` | `Название \| гарантия, дн. \| цена, руб.` |
| `parse_act_text(text)` | Обратный разбор с безопасным числом |
| `price_check(facts, rows)` | Предупреждения о нулевой цене и расхождении суммы со стоимостью |
| `build_task(facts)` | Задача на перезвон или `None` |

Важно: чеклист и акт никогда не получают `discussed`-значения. Обсуждённые
пункты остаются в свободном комментарии, чтобы менеджер видел контекст.
`price_check` сравнивает сумму **согласованных** строк акта с согласованной
общей стоимостью; если стоимость не `agreed`, расхождение не считается ошибкой.

---

## 4. Extractor

```python
from kivi.modules.work_crm import FactExtractor

def llm(messages: list[dict[str, str]]) -> str:
    ...  # любой адаптер: Gateway, Ollama, fake в тестах

extractor = FactExtractor(llm)
facts = extractor.extract(
    transcript_text,          # текст расшифровки, не объект Transcript
    order_context="",          # текст карточки, не источник согласий
    client_text="",            # необязательно; иначе выводится из меток
)
```

- `extract` строит prompt из старого `SYSTEM` + `FACTS_SCHEMA` в JSON Schema,
  вызывает `llm(messages)`, толерантно достаёт JSON и передаёт его в
  `normalize(raw, full_text, client_text)`.
- Пустая расшифровка возвращает `empty_facts()` и **не вызывает** LLM.
- `order_context` обрезается до `max_order_chars` (по умолчанию 1500) и помечен
  как «не источник согласований».
- Ответ может быть в ```` ```json ````-блоке или с текстом вокруг: используется
  `parse_json_loose`.
- Ошибки LLM (исключение) и невалидный JSON заворачиваются в `ExtractError`.
- Прямого импорта Ollama нет; callback — единственная точка вызова модели.

`FactExtractor` также принимает `system_prompt` для инъекции своего промпта.

---

## 5. Coach

[`coach.py`](../src/kivi/modules/work_crm/coach.py) — промпты для брифа перед
звонком и живых подсказок. Файл `playbook.toml` больше не читается: правила
передаются словарём.

```python
from kivi.modules.work_crm import Coach

coach = Coach(
    llm,
    playbook={"limits": {"max_discount_percent": 10}},
    order_chars=3500,          # лимит карточки без истории
    order_chars_with_history=2200,
    history_chars=4000,
    calls_chars=2500,
    transcript_chars=1800,
    tip_order_chars=1200,
)
brief = coach.brief(order_text, history_text="", calls_text="")
tip = coach.tip(order_text, transcript="", history_text="")
```

- `brief` учитывает историю прошлых звонков (`history_text`) и/или краткий
  дайджест (`calls_text`), сокращает карточку заказа и просит 3 варианта
  предложения в рамках правил компании.
- `tip` берёт последние 14 непустых реплик, обрезает их и просит 2–3 коротких
  совета. Решение клиента определяется по разговору, а не по UI-фактам.
- `playbook_text(playbook)` — чистая функция рендера правил; без файлового
  ввода-вывода.
- Ограничения длины инъектируются через конструктор, поэтому длинная карточка
  не переполняет контекст.

---

## 6. История звонков

[`history.py`](../src/kivi/modules/work_crm/history.py) хранит только текст:
один JSON на звонок.

```
data/calls/                     # root, обычно data/calls
└── 202610-1913/                # safe_order_code
    └── 20261111T235000123456_1a2b3c4d.json
```

Запись содержит `order_code`, `saved_at` (UTC ISO), `time` (для дайджеста),
`text` (расшифровка) и нормализованный `summary`. Аудио не сохраняется никогда.

```python
from pathlib import Path
from kivi.modules.work_crm import CallHistory, summarize

history = CallHistory(Path("data/calls"))
summary = summarize(llm, transcript_text)      # dict, без записи на диск
record = history.save("202610-1913", transcript_text, summary)
records = history.list("202610-1913")          # старые → новые
digest = history.digest("202610-1913", max_chars=4000)
```

- `save` использует атомарную запись: временный файл в каталоге заказа,
  `fsync`, затем `os.replace`; при ошибке временный файл удаляется.
- `safe_order_name` заменяет всё, кроме `\w` и `-`, на `_`, ограничивает имя
  60 символами и экранирует зарезервированные имена Windows (`CON`, `NUL`, …).
  `..` и `/` не могут вывести запись за пределы root.
- `list` пропускает повреждённые JSON и сортирует по `saved_at`.
- `digest` добавляет итог, предложения, возражения, названные числа и обещания;
  при превышении `max_chars` первыми отбрасываются самые старые строки.
- `summarize` портирует старый `SUMMARY_SYSTEM`/`SUMMARY_SCHEMA`; вызовов LLM
  нет, если в разговоре меньше 8 слов (автоответчик/обрыв) — возвращается
  `no_conversation`.
- `HistoryError` — ошибка вызова или разбора ответа модели.

Старые `call_key`, `listen_to_calls`, скачивание и STT не портированы: это
адаптеры CRM/аудио, а не домен.

---

## 7. Метрики

[`metrics.py`](../src/kivi/modules/work_crm/metrics.py) — потокобезопасный
счётчик на `threading.Lock`:

```python
from kivi.modules.work_crm import CrmMetrics

metrics = CrmMetrics()
metrics.record_order(seconds=12.5, prepared=True, filled=True, error=False)
metrics.as_dict()
```

Поля и формулы:

| Поле | Смысл |
|---|---|
| `orders_total` | число попыток обработки заказа |
| `prepared` | сколько заказов получили черновики |
| `filled` | сколько заполнены автоматически до конца |
| `errors` | сколько попыток завершились ошибкой |
| `seconds_total` | суммарное время, секунды (отрицательное значение зажимается в 0) |
| `automation_rate` | `filled / orders_total`, иначе `0.0` |
| `average_seconds` | `seconds_total / orders_total`, иначе `0.0` |

`as_dict` снимает согласованный снимок под одним lock, `reset` обнуляет всё.
Ошибка не исключает заказ из `orders_total`: это попытка, которая тоже стоила
времени.

---

## 8. Миграция из `D:\ai-agent`

| Старый файл | Новый модуль | Что изменилось |
|---|---|---|
| `modes/work/schema.py` | `schema.py` | Без изменений поведения; guard для мусора, `Any`-типы |
| `modes/work/drafts.py` | `drafts.py` | Без изменений; вынесен `_parse_number` |
| `modes/work/extractor.py` | `extractor.py` | Вместо `Transcript`/`OllamaClient` — текст и callback; helpers спикеров и `client_only_text` |
| `modes/work/coach.py` | `coach.py` | Убран `tomllib`/`Path`; playbook — словарь; добавлены `calls_text`, лимиты и `tip` |
| `modes/work/history.py` | `history.py` | `HistoryStore` → `CallHistory`; `put/get/all` → `save/list/digest`; атомарная запись; без аудио и STT |
| — | `metrics.py` | Новый headless-счётчик для автоматизации |

Что осталось в старом `ai-agent` и **не** входит в этот срез:

- `mode.py`, `controller.py`, `crm_ext.py`, `crm_altcrm.py`, `selectors.toml`;
- `assistant.py`, `ask_panel.py`, `facts_view.py`, `fill_all.py`, `voice.py`,
  `timeparse.py`;
- tkinter-панель, браузерное расширение, скачивание записей и STT.

Совместимость:

- `normalize`, `quote_found`, `build_comment`, `build_checklist`,
  `build_act_rows`, `parse_act_text`, `price_check`, `build_task` сохраняют
  старые сигнатуры и поведение; старые тесты переносятся с заменой импортов.
- `FactExtractor.extract` принимает текст, а не `Transcript`, поэтому старый
  вызов `extract(Transcript.from_text(text))` нужно заменить на
  `extract(text)`; `client_text` выводится автоматически.
- `Coach.brief` больше не принимает `playbook` из файла; `Coach.tip` заменил
  `live_tip(transcript, facts, order_text)`.
- CLI, registry и `module.yaml` не менялись. Подключение к `ToolExecutor` и
  панели — следующий шаг после стабилизации домена.

Проверка:

```powershell
.venv\Scripts\python.exe -m pytest -q tests\unit\test_work_crm_domain.py
.venv\Scripts\python.exe -m ruff check src\kivi\modules\work_crm tests\unit\test_work_crm_domain.py
```

---

## 9. Ограничения

- Домен не вызывает модель сам: без переданного callable нет extraction/coach.
- `CallHistory` хранит JSON без БД, миграций и блокировок между процессами;
  конкурентная запись одного заказа из разных процессов не сериализуется
  (атомарность отдельного файла сохраняется).
- Краткие итоги звонков не создаются автоматически внутри `save`: сначала
  вызывается `summarize`, потом `save` — так вызывающий код контролирует
  расход модели.
- `Участник` (моно) не даёт автоматических согласий; для стерео принято
  соглашение `Голос 1` = менеджер, `Голос 2` = клиент.
- `playbook` не загружается из файла и не валидируется схемой; это
  responsibility интегратора.
- Промпты и лимиты — константы/параметры конструктора, без hot-reload.
- Нет реальных действий в CRM (заполнение карточки, акт, задача): модуль
  готовит данные, а не отправляет их.
