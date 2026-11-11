# Kivi — 3D-аватар в браузере

Статическая страница для слоя M5: three.js + `@pixiv/three-vrm`, липсинк по
виземам, состояния `idle/listening/thinking/speaking/happy/error` и процедурная
голова-заглушка, пока нет VRM-ассета.

Сборка не нужна: браузер сам разрешает bare-импорты через `importmap`. Готовый
VRM кладётся в `assets/avatar/kivi.vrm`; при его отсутствии страница рисует
процедурную голову (сфера, глаза, челюсть и рот).

## Файлы

| Файл | Назначение |
|---|---|
| `index.html` | canvas, import map, HUD, подсказка про always-on-top |
| `app.js` | загрузка VRM, процедурный fallback, опрос `state.json`, анимация |
| `state.json` | генерируется Python-рендерером при запуске, не редактировать вручную |

## Зависимости

### Вариант A: CDN (по умолчанию)

`index.html` уже содержит import map:

```json
{
  "imports": {
    "three": "https://unpkg.com/three@0.180.0/build/three.module.js",
    "three/addons/": "https://unpkg.com/three@0.180.0/examples/jsm/",
    "@pixiv/three-vrm": "https://unpkg.com/@pixiv/three-vrm@3.5.5/lib/three-vrm.module.js"
  }
}
```

Нужен доступ к `unpkg.com`. Это единственная сетевая зависимость страницы.

### Вариант B: локальные `node_modules`

Если нужен полностью офлайн-режим, установите те же версии рядом со страницей:

```powershell
cd web\avatar3d
npm install three @pixiv/three-vrm
```

Версии в import map зафиксированы (three@0.180.0, @pixiv/three-vrm@3.5.5).
Для полного совпадения установите именно их:

```powershell
npm install three@0.180.0 @pixiv/three-vrm@3.5.5
```

Затем замените URL в import map на локальные пути:

```json
{
  "imports": {
    "three": "./node_modules/three/build/three.module.js",
    "three/addons/": "./node_modules/three/examples/jsm/",
    "@pixiv/three-vrm": "./node_modules/@pixiv/three-vrm/lib/three-vrm.module.js"
  }
}
```

`ThreeVrmRenderer` раздаёт каталог `web/avatar3d`, поэтому `node_modules` внутри
него доступны без дополнительной настройки. Пересборка не требуется.

## Запуск

```python
from kivi.interfaces.avatar3d import Avatar3DController, ThreeVrmRenderer

controller = Avatar3DController()
renderer = ThreeVrmRenderer(open_browser=True)  # браузер откроется только здесь
renderer.start()
try:
    controller.on_speech_start()
    renderer.update(controller)  # или renderer.update(controller.snapshot())
finally:
    renderer.stop()
```

Без `open_browser=True` сервер работает, но окно не открывается. Адрес —
`renderer.url` (loopback, случайный порт при `port=0`). Сервер отдаёт:

- `/` и `/app.js` — из `web/avatar3d`;
- `/assets/...` — из `assets/`;
- `/state.json` — актуальный снимок состояния (без кэша).

## Формат `state.json`

```json
{
  "schema_version": 1,
  "updated_at": 1760000000.0,
  "state": "speaking",
  "viseme": "A",
  "blendshapes": { "A": 0.62, "I": 0.0, "U": 0.0, "E": 0.0, "O": 0.0 },
  "vrm": {
    "available": false,
    "url": null,
    "path": null,
    "message": "VRM asset not found ...; procedural fallback is active"
  },
  "vrm_available": false,
  "fallback": true
}
```

Виземы `A/I/U/E/O/CLOSED`; `CLOSED` означает «все веса нули». `app.js` проверяет
типы и диапазоны, не выполняет код из JSON и принимает VRM-ссылку только вида
`/assets/*.vrm` на том же origin.

## Поведение страницы

- `state.json` опрашивается каждые 100 мс с `cache: "no-store"`.
- VRM загружается один раз; при ошибке остаётся процедурная голова, повтор — при
  смене URL.
- Для VRM выставляются выражения `aa/ih/ou/ee/oh` (и `a/i/u/e/o` для VRM 0.x),
  плюс `blink`, `happy`, `sad` при наличии.
- Процедурная голова: сфера, глаза с подвижными зрачками, брови, нос, челюсть и
  рот; рот и челюсть масштабируются по уровню виземы, глаза моргают.
- Прозрачный фон canvas (`alpha: true`), но прозрачность и always-on-top задаёт
  окно браузера/OBS, а не веб-страница. Подсказка про PiP/менеджер окон выведена
  в правом нижнем углу.
- При скрытии вкладки (`visibilitychange`) останавливаются RAF-рендер и polling;
  при возврате всё возобновляется.

## Ограничения

Нет настоящего VRM-ассета (`kivi.vrm`), нужен браузер с WebGL2/GPU, страница не
может сама включить always-on-top или прозрачность окна, нет захвата аудио и AEC —
амплитуда приходит извне. Подробности, бюджет VRAM и требования к модели — в
[`docs/AVATAR3D.md`](../../docs/AVATAR3D.md).

## Проверка

```powershell
.venv\Scripts\python.exe -m pytest -q tests\unit\test_avatar3d.py
.venv\Scripts\python.exe -m ruff check src\kivi\interfaces\avatar3d.py tests\unit\test_avatar3d.py
```
