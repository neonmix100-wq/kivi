# Cloud Broker

Облако — заёмная мощность, не режим по умолчанию. Вызов проходит проверки:
режим, политика данных, бюджет, квота. Ключи хранятся только в env.

## Политика

- `cloud.mode`: `off` | `auto_free` | `on_demand` (сейчас `auto_free`).
- `cloud.cost_limit_usd`: `0` означает, что платные вызовы запрещены.
- Данные по умолчанию `private`; провайдер получает только `public`, если
  в `config/cloud.yaml` не включено `allows_private_data`.
- Каждый вызов и каждый отказ пишутся в `audit`.

## Провайдеры

`config/cloud.yaml` содержит только метаданные. Ключ не хранится в файле:
провайдер указывает `api_key_env`, например `OPENROUTER_API_KEY`.
Поддержан OpenAI-compatible протокол (`/chat/completions`).

## CLI

```powershell
kivi cloud status
```

Показывает режим, лимит, наличие ключа, дневные запросы/токены и лимит.

## Python

```python
from kivi.cloud import CloudBroker, build_handoff, load_cloud_registry

registry = load_cloud_registry(paths.root / "config" / "cloud.yaml")
broker = CloudBroker(db, config.model_gateway.cloud, registry, events=bus)
result = broker.complete(
    "openrouter",
    [{"role": "user", "content": "..."}],
    model="model:free",
    data_class="public",
)
```

При `429` или исчерпании квоты брокер бросает `QuotaExhausted`, пишет
потребление в SQLite и оставляет контекст для локального продолжения:

```python
pack = build_handoff(goal="...", reason="quota exhausted", plan=[...])
print(pack.to_prompt())
```

## Не сделано

- Cloud Scout и автоматический подбор провайдера.
- Интеграция denylist из этапа 4.
- Живые ключи: проверка на реальном провайдере.
