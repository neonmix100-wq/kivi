# Cloud Broker

Облако — заёмная мощность, не режим по умолчанию. Вызов проходит проверки:
режим, политика данных, бюджет, квота. Ключи хранятся только в env.

## Политика

- `cloud.mode`: `off` | `manual` | `auto_free` (сейчас `auto_free`).
- `cloud.cost_limit_usd`: `0` означает, что платные вызовы запрещены.
- Данные по умолчанию `private`; провайдер получает только `public`, если
  в `config/cloud.yaml` не включено `allows_private_data`.
- Каждый вызов и каждый отказ пишутся в `audit`.

## Провайдеры

`config/cloud.yaml` содержит только метаданные. Ключ не хранится в файле:
провайдер указывает `api_key_env`, например `OPENROUTER_API_KEY`.
Поддержан OpenAI-compatible протокол (`/chat/completions`).

`config/cloud-candidates.yaml` — каталог кандидатов для Cloud Scout: тоже только
метаданные, без ключей и аккаунтов. `verified_at: null` означает, что лимиты и
условия не проверены у провайдера вживую; перед `--approve` их нужно перепроверить.

## CLI

```powershell
kivi cloud status
kivi cloud ask "вопрос" --provider groq --model <model> --data-class public
kivi cloud scout
kivi cloud scout --goal "доделать отчёт" --next-step "написать вывод"
kivi cloud scout --approve cerebras
kivi cloud handoff
kivi cloud handoff --show 1
```

`cloud status` показывает режим, лимит, наличие ключа, дневные запросы/токены
и лимит. `cloud ask` вызывает провайдера через брокер. `--data-class` по умолчанию
`private`, поэтому для публичных провайдеров нужен явный `public`. При
исчерпании квоты или `429` команда сохраняет checkpoint, показывает карточки
Cloud Scout (если поиск включён) и возвращает код 1: задача продолжается
локально с handoff pack.

`cloud scout` ранжирует кандидатов из `config/cloud-candidates.yaml`: сначала
готовые (ключ уже в env), затем без ключа. Фильтры: способность, класс данных,
обучение на данных, watermark. `--approve <id>` — единственная запись Scout:
он добавляет метаданные провайдера в `config/cloud.yaml` после вашего
подтверждения; ключ остаётся в переменной окружения.

`cloud handoff` показывает сохранённые чекпойнты (цель, критерии, шаги,
артефакты, ограничения, следующий шаг).

## Python

```python
from kivi.cloud import (
    CloudBroker,
    CloudScout,
    ScoutQuery,
    build_handoff,
    load_cloud_registry,
    load_scout_registry,
)

registry = load_cloud_registry(paths.root / "config" / "cloud.yaml")
broker = CloudBroker(db, config.model_gateway.cloud, registry, events=bus)
result = broker.complete(
    "openrouter",
    [{"role": "user", "content": "..."}],
    model="model:free",
    data_class="public",
)

candidates = load_scout_registry(paths.root / "config" / "cloud-candidates.yaml")
scout = CloudScout(db, registry, candidates, events=bus)
outcome = scout.on_exhausted(ScoutQuery(data_class="public"), goal="...")
```

При `429` или исчерпании квоты брокер бросает `QuotaExhausted` и пишет
потребление в SQLite. `on_exhausted` сначала сохраняет `HandoffPack`
(`save_handoff`), затем ищет замену. Если карточки нашлись — их нужно
подтвердить вручную; если нет — локальная модель продолжает по
`handoff.to_prompt()`:

```python
pack = build_handoff(goal="...", reason="quota exhausted", plan=[...])
print(pack.to_prompt())
```

## Не сделано

- Живой вызов с реальным ключом (нужен ключ в env, в репозиторий он не попадает).
- Интеграция denylist и redaction перед отправкой (этап 4).
- Автоматическое обновление каталога Scout из сети: карточки помечены
  `verified_at: null`, лимиты и условия нужно перепроверять у провайдера.
