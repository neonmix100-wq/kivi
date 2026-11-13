# Media jobs (M7)

`kivi.media.jobs.MediaJobQueue` — локальная персистентная очередь для
изображений/видео: **submit → poll → artifact**. Она не привязана к провайдеру:
runner получает `params` и каталог задания.

## Контракт

```python
from kivi.media.jobs import MediaJobQueue

queue = MediaJobQueue("data/media/jobs")
job = queue.submit(
    "image",
    {"prompt": "kivi portrait"},
    lambda params, job_dir: generate(params, job_dir),
)
queue.get(job.id).status       # queued/running/done/failed
queue.wait(job.id, timeout_s=120)
```

Статусы: `queued`, `running`, `done`, `failed`, `cancelled`, `interrupted`.

- каждый job хранится в `data/media/jobs/<id>/job.json`;
- артефакт возвращается как путь или `{"artifact": "..."}`;
- после перезапуска незавершённые задания помечаются `interrupted`, а не
  притворяются работающими;
- `cancel()` отменяет только ещё не запущенный job;
- params ограничены 32 КБ; id проверяется, path traversal запрещён;
- один процесс, один worker по умолчанию; SQLite/Redis не требуются.

## Почему нет автозапуска видео

Бесплатный Pollinations Classic не предоставляет видео-эндпоинт. Для видео
нужен провайдер с API-ключом (Pollinations enter / HF / Runway и т.п.) и
соблюдение watermark/квот/ToS. Очередь готова принять такой runner, но
публичного бесплатного видео-провайдера без ключа в этом срезе нет.
Не автоматизируем регистрацию, CAPTCHA и обход лимитов.

## Ограничения

- нет UI-панели очереди;
- нет автоповторов и прогресса в процентах;
- runner не сохраняется в JSON (это код, а не данные); после рестарта
  незавершённый job нельзя продолжить автоматически.
