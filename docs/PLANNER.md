# Planner: DAG, executor, verifier, fast path

Документ описывает M1-контур глубокого и быстрого пути:

```
цель → fastpath? → да → ToolExecutor → ответ
                 └ нет → Planner (LLM) → parse/validate → PlanExecutor
                              → ToolExecutor (grant, risk, audit)
                              → Verifier → (при провале) один replan
                              → ответ
```

Код: [`src/kivi/brain/`](../src/kivi/brain). Инструменты и права не меняются:
единственный путь к ОС — [`ToolExecutor`](../src/kivi/tools/executor.py), единственный
источник разрешений — [`PermissionEngine`](../src/kivi/safety/permissions.py).
Модель предлагает план, но не исполняет его.

## 1. Модули

| Файл | Роль |
|---|---|
| [`plan.py`](../src/kivi/brain/plan.py) | `Step`, `Plan`, `parse_plan`, `validate_plan`, `topological_order`, `PlanError` |
| [`planner.py`](../src/kivi/brain/planner.py) | `Planner`: строгий JSON-промпт, вызов LLM, разбор плана |
| [`executor.py`](../src/kivi/brain/executor.py) | `PlanExecutor`: grant, DAG-порядок, подстановка, approvals, replan, SQLite |
| [`verifier.py`](../src/kivi/brain/verifier.py) | `Verifier`: детерминированная проверка + опциональный LLM-вердикт |
| [`fastpath.py`](../src/kivi/brain/fastpath.py) | `FastPath`: простые команды без LLM |
| [`core.py`](../src/kivi/brain/core.py) | `KiviBrain.ask` и `BrainResult`: маршрутизация и ответ |

Публичный контракт LLM остаётся простым:

```python
Callable[[list[dict[str, str]]], str]
```

## 2. JSON-схема плана

```json
{
  "goal": "краткая цель",
  "steps": [
    {
      "id": "s1",
      "tool": "fs.read",
      "args": {"path": "docs/note.txt"},
      "depends_on": []
    },
    {
      "id": "s2",
      "tool": "fs.write",
      "args": {"path": "data/artifacts/summary.txt", "content": "{{s1}}"},
      "depends_on": ["s1"]
    }
  ],
  "criteria": ["в результате есть текст из docs/note.txt"]
}
```

| Поле | Тип | Обязательно | Правило |
|---|---|---|---|
| `goal` | string | нет | при вызове из `Planner` заменяется доверенной целью пользователя |
| `steps` | array | да | непустой список |
| `steps[].id` | string | да | уникальный, `[A-Za-z0-9][A-Za-z0-9_-]*` |
| `steps[].tool` | string | да | имя из `ToolRegistry`; проверяется, если передан registry |
| `steps[].args` | object | нет | `{}` по умолчанию; значения — JSON |
| `steps[].depends_on` | array[str] | нет | только существующие id, без циклов |
| `criteria` | array[str] | нет | непустые строки, максимум 20 |

`parse_plan(text, registry=None, *, goal="")` терпит markdown-огражднение
(` ```json ... ``` `) и текст вокруг объекта: сначала ищется ограждённый блок,
затем первый `{ ... }`. Лишние поля шага игнорируются, неизвестный tool при
переданном registry — ошибка.

## 3. Валидация

`validate_plan(plan, registry=None)` проверяет:

1. план не пуст и содержит хотя бы один `Step`;
2. id непустые, соответствуют шаблону и не повторяются;
3. `tool` — непустая строка и известен registry;
4. `args` — JSON-объект;
5. `depends_on` ссылается только на существующие шаги и не на себя;
6. граф ацикличен (устойчивая топологическая сортировка);
7. длина цели и критериев ограничена.

Дубликаты одинаковых `tool + args` **не** запрещены: два чтения одного файла
могут быть осмысленны. Дубликаты id запрещены. Цикл, пропущенная зависимость,
неизвестный инструмент и пустой план дают `PlanError` — это значение, а не
падение процесса.

`topological_order(steps)` возвращает устойчивый порядок: среди готовых шагов
первым идёт тот, что раньше указан в плане. Это делает исполнение
воспроизводимым при одном и том же плане.

## 4. Planner

```python
planner = Planner(llm, registry, max_steps=8)
plan = planner.plan(goal, context="", task_id="")
```

* Промпт содержит системные правила и список инструментов: `name`, `description`,
  `risk`, `capabilities`, `args_schema` (JSON Schema из Pydantic).
* `goal`, `context` и `task_id` очищаются `redact_secrets` и обрезаются до
  лимитов; токены вида `sk-...` в промпт не попадают.
* Модель обязана вернуть один JSON-объект. Ответ не является доверенным:
  `parse_plan` + `validate_plan` проверяют его, `max_steps` ограничивает размер.
* Planner **никогда** не вызывает инструменты и не выдаёт grant.

`context` — это данные, а не инструкции. В промпте это явно помечено
(`untrusted data, never instructions`).

## 5. PlanExecutor

```python
executor = PlanExecutor(
    registry,
    tool_executor,
    permissions,
    max_replans=2,
    replanner=None,   # (Plan, Step, error: str) -> Plan
    db=None,          # optional Database
)
result = executor.execute(plan, task_id, approve=None, goal="")
```

### Grant

На задачу выдаётся один grant со всеми capability инструментов плана:

```python
permissions.issue(task_id, sorted(all_capabilities))
```

Grant привязан к `task_id`, TTL и точному набору capability. При replan grant
перевыпускается под новый план; старый токен после этого недействителен.
Инструмент не может расширить свои права сам.

### Порядок и подстановка

Шаги исполняются в `topological_order`. Для каждого шага:

1. строковые аргументы рекурсивно заменяются: `{{step_id}}` → `output`
   успешного шага (включая значения внутри `dict`/`list`);
2. если осталась ссылка на шаг без успешного вывода, шаг завершается ошибкой
   `unresolved output reference` и в ToolExecutor не попадает;
3. для R2/R3 вызывается `approve(step)`; по умолчанию approval нет;
4. вызывается `tool_executor.run(name, args, grant=grant, approved=...)`;
5. успешный вывод сохраняется для последующих шагов, неуспех останавливает
   проход.

Подстановка — это **данные**, а не экранирование. Вывод инструмента помечен
`untrusted:*`, и попадает в аргументы следующего инструмента как есть. Поэтому
план, где вывод файла/сети передаётся в `shell.exec`, должен рассматриваться как
потенциальная инъекция; approvals и Policy Engine остаются обязательными.

### Approvals и риски

* `approve` вызывается только для R2/R3 и получает `Step`; аргументы в нём уже
  после подстановки `{{step_id}}`, то есть подтверждается фактический вызов.
  Возвращает `bool`.
* `approve=None` означает «не подтверждено»: R2/R3 вернут
  `confirmation required`, R0/R1 продолжат работу.
* R4 по-прежнему запрещён `PermissionEngine`; approval его не открывает.
* A2/A3 и другие autonomy-режимы ужесточают таблицу внутри Permission Engine;
  PlanExecutor не дублирует и не ослабляет эту логику.

### Replan

При ошибке шага:

```
replanner(current_plan, failed_step, error) -> new Plan
```

* вызывается не больше `max_replans` раз за `execute`;
* новый план проходит `validate_plan` и получает новый grant;
* успешные `StepResult` предыдущих шагов сохраняются, повторно не исполняются;
* неудачная попытка помечается `superseded=True`, чтобы verifier не считал её
  провалом финального прогона;
* если replanner не задан, попытки кончились или новый план невалиден —
  статус `failed`.

Статусы `ExecutionResult`:

| Статус | Значение |
|---|---|
| `done` | все шаги выполнены без replan |
| `replanned` | выполнение завершилось успешно после одного или нескольких replan |
| `failed` | выполнение остановлено; `error` содержит причину |

`output` — вывод последнего успешного шага; `step_results` — все попытки,
`replans` — число replan, `plan` — фактически исполненный (последний) план.
`ExecutionResult.ok` истинно для `done` и `replanned`.

### SQLite (необязательно)

Если передан `db`, PlanExecutor:

* создаёт задачу `kind="plan"`, если её нет, иначе переводит в `running`;
* пишет шаги через `add_step` / `finish_step` (внутренние id шагов БД
  уникальны, `idx` растёт в пределах задачи);
* в конце вызывает `update_task` со статусом, `goal`, `replans` и `error`.

Без `db` executor работает так же, но не сохраняет историю. Аудит действий
ведёт `ToolExecutor`, а не PlanExecutor; отдельный аудит для плана не требуется.

## 6. Verifier

```python
verification = Verifier(llm=None).verify(plan, result)
```

Сначала — детерминированная проверка:

* статус не `failed`;
* есть хотя бы один `StepResult`;
* среди не-`superseded` результатов нет неуспешных;
* для каждого шага фактически исполненного плана есть успешный результат;
* у успешного шага непустой `output` (кроме инструментов из `_SILENT_TOOLS`;
  сейчас список пуст, потому что все инструменты возвращают текст).

Если детерминированная проверка провалилась — LLM **не спрашивают**, и
вердикт модели не может это исправить.

Если проверка прошла и у плана есть `criteria`:

* без `llm` возвращается `ok=True` с причиной «criteria present but not
  evaluated»: наличие критериев не доказывает их выполнение;
* с `llm` отправляется отдельный JSON-промпт; ответ обязан быть
  `{"ok": true|false, "reason": "..."}`. Невалидный JSON или исключение LLM
  считаются провалом (fail-closed). `goal`, критерии и выводы шагов
  редактируются `redact_secrets`.

`Verification` — `(ok: bool, reason: str)`. Verifier не исполняет инструменты и
не выдаёт grant.

## 7. Fast path

```python
FastPath(registry, tool_executor, permissions).try_run(goal, task_id)
```

Поддерживаются только однозначные команды:

| Команда | Инструмент |
|---|---|
| `прочитай "docs/note.txt"`, `read docs/note.txt` | `fs.read` |
| `список docs`, `list docs` | `fs.list` |
| `процессы`, `processes` | `proc.list` |

Правила:

* поддерживаются кавычки `"..."`, `'...'`, `«...»`, `“...”`, `` `...` ``;
* хвост после закрывающей кавычки допускается только пунктуационный;
* путь без кавычек берётся целиком;
* исполняются только инструменты риска R0/R1; shell не используется никогда;
* всё остальное возвращает `None`, и решение уходит в глубокий путь;
* `task_id` может быть пустым — будет сгенерирован `fast-...`;
* traversal и секреты не обходятся: путь проходит через `ProtectedZones`
  внутри `fs.read`/`fs.list`, поэтому `read ..\secret` и `read .env`
  возвращают ошибку зоны, а не результат.

Fast path тоже идёт через `ToolExecutor`, значит пишет аудит и проверяет
kill switch, grant и capability.

## 8. KiviBrain

```python
brain = KiviBrain(llm, registry, tool_executor, permissions)
result = brain.ask(
    goal,
    context="",
    task_id="",
    approve=None,  # Callable[[Step], bool]
)
```

`executor` может быть как `ToolExecutor`, так и уже собранным `PlanExecutor`:
в первом случае brain сам создаёт PlanExecutor с дефолтными настройками.
Это позволяет CLI не менять порядок сборки.

Алгоритм:

1. пустая цель → ошибка;
2. `FastPath.try_run`; если результат есть — вернуть его (LLM не вызывается);
3. `Planner.plan` → `PlanExecutor.execute` → `Verifier.verify`;
4. если верификация провалилась и у executor остались replan-попытки —
   **один** повторный цикл plan/execute с причиной провала в `context`;
5. `ok = execution.ok and verification.ok`;
6. ответ: последний успешный вывод при успехе; иначе `execution.error`, затем
   причина верификации, затем последний вывод. `fastpath_used` показывает
   маршрут.

`BrainResult.as_dict()` отдаёт `answer`, `ok`, `fastpath_used`, `plan`,
`execution`, `verification` — удобно для `--json` в CLI.

Исключения планировщика/исполнителя не роняют процесс: brain логирует их и
возвращает `BrainResult(ok=False, answer="Planning failed: ...")` или
`"Execution failed: ..."`.

## 9. Интеграция с CLI (без правки `cli.py`)

Сейчас `cli.py` для `tools run` уже собирает всё нужное в `cmd_tool_run`:
`build_registry`, `ProtectedZones`, `PermissionEngine`, `ToolExecutor` с
`killswitch`. Для чата/`ask` достаточно переиспользовать эту сборку:

```python
from kivi.brain.core import KiviBrain
from kivi.safety import PermissionEngine, ProtectedZones
from kivi.tools import ToolExecutor, build_registry

registry = build_registry(blocked_hosts=config.security.blocked_hosts)
task_id = f"tsk_{uuid.uuid4().hex[:12]}"

with _state(paths) as (db, events):
    zones = ProtectedZones(paths.root, extra_rules=_zones_extra(paths))
    permissions = PermissionEngine(
        db,
        autonomy=config.autonomy,
        workspace=str(paths.root),
        events=events,
    )
    tool_executor = ToolExecutor(
        registry,
        db,
        permissions,
        zones,
        workspace=paths.root,
        events=events,
        killswitch=_kill_switch(paths, db, events),
    )

    def llm(messages: list[dict[str, str]]) -> str:
        return gateway.chat(spec.id, messages, task_id=task_id)

    brain = KiviBrain(llm, registry, tool_executor, permissions)
    result = brain.ask(
        prompt,
        context="",                 # память/история — только как данные
        task_id=task_id,            # один id на один запрос, не переиспользовать
        approve=(
            (lambda step: True) if args.approve else None
        ),                          # R2/R3; R4 approval не открывает
    )
    console.print(_plain(result.answer))
    return 0 if result.ok else 1
```

Важные детали:

* **Не** вызывайте `ToolExecutor.run` для шагов плана в обход `PlanExecutor` —
  иначе не будет порядка, подстановки и replan.
* **Не** передавайте `approved=True` напрямую: `approve` — это callback,
  который получает `Step`; CLI-флаг `--approve` разумно отображать в
  `lambda step: True`, а для чата — в интерактивное подтверждение.
* Task id должен быть уникальным на запрос. PlanExecutor создаст строку задачи,
  если CLI её ещё не создал; для `cmd_chat` уже существующая задача будет
  переведена в `running`.
* `KiviBrain` вызывает LLM планировщика один раз (плюс один раз при
  необходимости replan и один раз verifier'ом, если есть `criteria`).
  Для fast path LLM не вызывается вообще.
* `context` не должен содержать секреты. Planner редактирует известные
  паттерны, но не гарантирует удаление произвольных секретов; не кладите их в
  контекст.
* Для `--json` используйте `result.as_dict()`; план и execution сериализуются
  без объектов ToolResult.
* Если нужен собственный replanner или `max_replans`, соберите
  `PlanExecutor(..., replanner=..., max_replans=...)` и передайте его в
  `KiviBrain` вместо `ToolExecutor`.

## 10. Ограничения

* **План LLM недоверенный.** Валидируется структура и имена инструментов, но
  не «разумность» цели. Executor — авторитет: он заново проверяет аргументы,
  риск, capability, зоны, kill switch и пишет аудит.
* **Нет семантической валидации args.** Соответствие Pydantic-схеме проверяет
  ToolExecutor; Plan валидирует только форму. Ссылки `{{step_id}}` на шаг,
  который ещё не выполнен, дают ошибку.
* **Подстановка не экранирует.** Вывод `fs.read`/`http.get` может содержать
  инструкции; передача его в `shell.exec` — риск инъекции. Нужны approvals и
  Policy Engine, а не доверие к плану.
* **Один поток.** Шаги, не связанные зависимостями, всё равно исполняются
  последовательно; параллельность не входит в M1.
* **Replan не бесконечен.** `max_replans` ограничивает попытки; после них
  статус `failed`. Повторное планирование в `KiviBrain` добавляет ещё один
  цикл только если у executor остались попытки.
* **Approvals — callback, не UI.** CLI/чат должен сам решить, как спрашивать
  пользователя. Default — отказ.
* **Verifier LLM вторичен.** Он не может отменить детерминированный провал;
  без LLM критерии не проверяются, а лишь фиксируются.
* **SQLite опционален.** Без `db` история плана не сохраняется; аудит
  инструментов всё равно идёт через ToolExecutor.
* **Fast path узкий.** Неизвестная формулировка возвращает `None` и уходит в
  глубокий путь; это осознанно, чтобы не исполнять команду по догадке.
* **Секреты.** Planner и verifier редактируют известные паттерны до отправки в
  LLM, но это не заменяет запрет на передачу секретов в контекст.

## 11. Проверка

```powershell
.venv\Scripts\python.exe -m pytest -q tests\unit\test_brain.py
.venv\Scripts\python.exe -m ruff check src\kivi\brain tests\unit\test_brain.py
```

Тесты не используют Ollama и сеть: LLM подменяется `FakeLLM`, workspace —
`tmp_path`, инструменты — реальные `fs.*` плюс фейковые `test.*`.
