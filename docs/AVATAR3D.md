# Kivi: 3D-VRM аватар (M5)

Статус: слой M5 реализован как **pipeline + браузерная страница + процедурный
fallback**. Реального VRM-ассета `assets/avatar/kivi.vrm` пока нет: есть только
концепт-арт в `assets/avatar/concept/`. Страница запускается и без модели.

Документ описывает `src/kivi/interfaces/avatar3d.py` и `web/avatar3d/`. Соседний
2D-слой (`avatar2d.py`, Tk) не меняется; CLI в этом срезе не редактируется.

## 1. Архитектура

```text
TTS / аудио
   │  RMS-амплитуда 0..1 (+ необязательная подсказка-фонема)
   ▼
Avatar3DController.update_amplitude()
   │  состояние: idle/listening/thinking/speaking/happy/error
   ▼
VisemeMapper
   │  attack/decay, доминирующая визема, blendshapes A/I/U/E/O
   ▼
snapshot() → {"state", "viseme", "blendshapes"}
   ▼
ThreeVrmRenderer.update()
   │  state.json + loopback HTTP (web/avatar3d + assets)
   ▼
web/avatar3d/app.js
   ├─ assets/avatar/kivi.vrm есть  → three.js + @pixiv/three-vrm
   └─ ассета нет                  → процедурная голова
```

Ключевое разделение:

- **логика** (`VisemeMapper`, `Avatar3DController`) не знает про браузер, GPU,
  HTTP и Tk; её можно тестировать без окна;
- **транспорт** (`ThreeVrmRenderer`) только раздаёт статику и пишет JSON;
- **рендер** (`app.js`) сам решает, грузить VRM или рисовать заглушку.

## 2. Python API

```python
from kivi.interfaces.avatar3d import (
    Avatar3DController,
    Avatar3DState,
    ThreeVrmRenderer,
    Viseme,
    VisemeMapper,
    find_vrm,
    vrm_status,
)

controller = Avatar3DController(on_event=lambda event: print(event))
controller.set_state("listening")
controller.on_speech_start()
shapes = controller.update_amplitude(0.72, hint="aa")
print(controller.snapshot())
controller.on_speech_end()

renderer = ThreeVrmRenderer()          # браузер не открывается
renderer.start()
renderer.update(controller)            # принимает controller или snapshot()
print(renderer.url)                    # http://127.0.0.1:<port>/
renderer.stop()
```

| Объект | Назначение |
|---|---|
| `Avatar3DState` | `StrEnum`: `idle`, `listening`, `thinking`, `speaking`, `happy`, `error` |
| `Viseme` | `StrEnum`: `A`, `I`, `U`, `E`, `O`, `CLOSED` |
| `VisemeMapper` | амплитуда + hint → сглаженная визема и `blend_shapes()` |
| `Avatar3DController` | state machine, события, `snapshot()` |
| `Avatar3DRenderer` | `Protocol`: `start()`, `update()`, `stop()` |
| `ThreeVrmRenderer` | loopback `ThreadingHTTPServer`, `state.json`, `url` |
| `find_vrm(root)` | `Path \| None`, по умолчанию `assets/avatar/kivi.vrm` |
| `vrm_status(root)` | `VrmStatus(available, path, message)` |

`ThreeVrmRenderer(root, *, open_browser=False, port=0)`: `start()` создаёт
`ThreadingHTTPServer` только на `127.0.0.1`; `update()` пишет `state.json` в
`web/avatar3d/` и обновляет in-memory ответ `/state.json`; `stop()` идемпотентен.
`open_browser=True` — единственный способ открыть окно, и только явно.

## 3. Состояния

| Состояние | Когда | Поведение страницы |
|---|---|---|
| `idle` | ожидание | дыхание, моргание, лёгкий взгляд |
| `listening` | пользователь говорит | наклон головы, внимание |
| `thinking` | планирование/генерация | взгляд в сторону/вверх |
| `speaking` | играет TTS | липсинк по blendshapes, кивки |
| `happy` | эмоция/успех | улыбка, лёгкий подскок |
| `error` | явная ошибка | наклон, лёгкое «замешательство» |

Переход разрешён из любого состояния в любое. В `error` контроллер не уходит
сам: только явный `set_state("error")`. `on_speech_start()` из `error` возвращает
`speaking`.

## 4. Виземы и липсинк

| Viseme | VRM 1.0 expression | Смысл |
|---|---|---|
| `A` | `aa` | широкий открытый рот |
| `I` | `ih` | узкий, растянутый |
| `U` | `ou` | округлённый, маленький |
| `E` | `ee` | средний |
| `O` | `oh` | округлённый |
| `CLOSED` | — | все веса нули |

Алгоритм `VisemeMapper`:

1. амплитуда клампится в `[0, 1]`;
2. ниже `silence_threshold` (по умолчанию `0.05`) — `CLOSED`;
3. hint (`aa/ih/ou/ee/oh`, русские `а/и/у/э/о/ы`, согласные) имеет приоритет;
   без hint — грубые ступени `U → E → A` по громкости;
4. уровень сглаживается: `level += (target - level) * (attack | decay)`;
5. `blend_shapes()` возвращает свежий словарь, где **ровно одна** визема
   ненулевая; `CLOSED` не является ключом blendshapes.

По умолчанию `attack=0.6`, `decay=0.25`. Согласная подсказка закрывает рот,
даже если амплитуда высокая. Значения всегда в `[0, 1]`, NaN/бесконечность
превращаются в `0`.

## 5. VRM-ассет

Файл: `assets/avatar/kivi.vrm` (VRM 1.0). Пока отсутствует; `VrmStatus` вернёт
`available=False`, а страница включит процедурную голову.

Интеграционная заметка: `AvatarSettings.vrm` в `src/kivi/config.py` пока
указывает на `assets/kivi.vrm`. Слой M5 по ТЗ использует
`assets/avatar/kivi.vrm`; конфиг в этом срезе не менялся, выравнивание путей —
отдельный шаг.

Требования к модели (из [`docs/PERSONA.md`](PERSONA.md)):

- стиль — реалистичный аниме, не чиби;
- возраст **18 лет**, рост **156 см**, стройное пропорциональное телосложение;
- волосы и глаза — бирюзовые (turquoise), волосы мягкие, средней длины;
- одежда — вариант **#5 Summer casual** (№5): светло-мятный сарафан, короткий
  кардиган, сандалии; без очков/наушников/украшений;
- нейтральная A-поза, humanoid-риг VRM 1.0;
- blendshapes/expressions: `aa`, `ih`, `ou`, `ee`, `oh`, плюс `blink`, `lookAt`,
  spring bones;
- 10–30 тыс. треугольников, 2–4 материала, текстуры разумного размера;
- лицензия на использование образа и ассета.

Концепт-арт (`kivi_portrait.png`, `kivi_fullbody_summer.png`) — референс, а не
готовая модель. Как только VRM появится, код менять не нужно: страница найдёт
файл через `find_vrm()`/`VrmStatus` и загрузит его.

## 6. Установка и запуск

Сборка не требуется. По умолчанию `web/avatar3d/index.html` использует import
map с `unpkg.com` (three@0.180.0 и @pixiv/three-vrm@3.5.5). Нужен интернет.

Полностью офлайн:

```powershell
cd web\avatar3d
npm install three @pixiv/three-vrm
# для повторяемости:
npm install three@0.180.0 @pixiv/three-vrm@3.5.5
```

Затем замените URL в import map на `./node_modules/...` — точный пример есть в
[`web/avatar3d/README.md`](../web/avatar3d/README.md).

Запуск рендерера:

```python
from kivi.interfaces.avatar3d import Avatar3DController, ThreeVrmRenderer

controller = Avatar3DController()
with ThreeVrmRenderer(open_browser=True) as renderer:
    controller.on_speech_start()
    renderer.update(controller)
    input("Enter, чтобы остановить...")
```

Без `open_browser=True` откройте `renderer.url` вручную. Сервер слушает только
loopback и раздаёт `web/avatar3d`, `assets` и `/state.json`.

## 7. Fallback

Процедурная голова — не «заглушка-заглушка», а рабочий UI:

- сфера-голова, бирюзовые волосы (cap + объём сзади + пряди), глаза с
  подвижными зрачками, брови, нос, уши, шея, плечи/сарафан;
- челюсть и рот масштабируются по уровню виземы; ширина рта зависит от `A/I/U/E/O`;
- моргание, дыхание, наклон головы и взгляд по состояниям;
- при `happy` показывается улыбка, когда рот закрыт.

Если VRM есть, но загрузка упала (битый файл, сеть, GPU), страница логирует
ошибку и остаётся на процедурной голове. Повтор — при смене URL ассета.

## 8. Безопасность

- HTTP-сервер биндится только на `127.0.0.1`; внешний доступ не открывается.
- Раздаются только `web/avatar3d` и `assets`; path traversal блокируется
  проверкой `Path.resolve()` + `relative_to`.
- `state.json` не содержит исполняемого кода. `app.js` валидирует `state`,
  `viseme`, числа blendshapes (0..1), обрезает сообщения и выводит их через
  `textContent`; `eval`/`new Function`/`innerHTML` не используются.
- VRM-ссылка принимается только same-origin и только `/assets/*.vrm`; внешние
  URL и `javascript:` отклоняются.
- Секреты, аудио и микрофон в этом слое не читаются.

## 9. Ограничения

1. **Нет VRM-ассета**: `assets/avatar/kivi.vrm` отсутствует, доступен только
   процедурный fallback. Это осознанно: реалистичного персонажа не подменяем
   сгенерированной «18-летней» моделью.
2. **Нужен браузер с GPU/WebGL2**. Тесты не запускают браузер, GPU и не грузят
   three.js.
3. **Прозрачность (transparent) и always-on-top** — свойства окна
   (Tauri/Electron/WebView2, OBS, менеджер окон), а не веб-страницы. Страница
   даёт прозрачный canvas и подсказку про Picture-in-Picture; настоящий
   always-on-top появится в оконной оболочке.
4. **Нет захвата аудио и AEC**: амплитуду/визему подаёт вызывающий код. Эхо-
   компенсация, barge-in и пауза во время звонков — отдельный слой senses/voice.
5. **Нет animation mixer и клипов**: только процедурные дыхание, моргание,
   взгляд и наклон. Idle/talk/listen/think-анимации — следующий шаг.
6. **Polling 100 мс** — не идеальная синхронизация кадров. Для точного липсинка
   позже нужен WebSocket/SSE или локальный IPC, а не файл.
7. **CLI не подключён**: `cli.py` в этом срезе не меняется; интеграция — через
   `Avatar3DController` + `ThreeVrmRenderer` в вызывающем коде.
8. **CDN-зависимость** по умолчанию: без интернета нужен локальный `npm install`.
9. **Лицензии**: VRM, текстуры и голос должны иметь права на использование;
   чужой образ без лицензии запрещён.

## 10. VRAM и производительность

- Лёгкая VRM: 10–30 тыс. треугольников, 2–4 материала; WebGL-рендер ~100–300 МБ
  VRAM (зависит от текстур).
- 30 FPS в покое, 60 FPS при речи; при скрытии вкладки RAF и polling
  останавливаются.
- Аватар входит в резерв «рабочий стол», а не в бюджет LLM. На 8 ГБ VRAM
  аватар + Chrome + LLM 8B могут не поместиться — следить за бюджетом.
- После апгрейда до RTX 4070 12 ГБ станет комфортнее.
- Процедурная голова заметно дешевле VRM: нет текстур и скелета, но GPU/WebGL
  всё равно нужен.

## 11. Проверка

```powershell
.venv\Scripts\python.exe -m pytest -q tests\unit\test_avatar3d.py
.venv\Scripts\python.exe -m ruff check src\kivi\interfaces\avatar3d.py tests\unit\test_avatar3d.py
```

Тесты покрывают виземы/сглаживание/доминанту, переходы и события контроллера,
`VrmStatus`, loopback-сервер, запись и раздачу `state.json`, идемпотентный
`stop()`, отсутствие автозапуска браузера, защиту от path traversal и наличие
three-vrm в HTML/JS. Реальный браузер, GPU и сеть не используются.

## 12. Следующие шаги

1. Сделать VRM 1.0 в VRoid/Blender по концепту и положить в
   `assets/avatar/kivi.vrm`.
2. Добавить animation mixer и клипы idle/listen/think/talk.
3. Подключить поток амплитуды/визем из TTS и голосового цикла.
4. Сделать оконную оболочку (Tauri/WebView2) для прозрачности, always-on-top,
   перетаскивания и мультимонитора.
5. Заменить polling на WebSocket/SSE для кадровой синхронизации.
