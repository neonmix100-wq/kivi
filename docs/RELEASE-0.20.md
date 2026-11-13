# Kivi 0.20.0 — первый рабочий прототип

Дата: 13.11.2026.

## Что входит

- Локальный чат, история, память.
- Голоса Fish/Qwen, микрофон/STT, текстовый wake.
- 3D VRM-аватар, липсинк, состояния, процедурный fallback.
- AltCRM через расширение: чтение, формы fill/act/task без сохранения.
- Live-приёмка M6: fill 5/5, акт 1/1, задача 3/3; сохранение/создание не выполнялись.
- Журнал реальных чтений и счётчик `N / 10`.
- Медиа: изображения и локальный MP4 из кадра.
- Файловый менеджер: вкладки, история, сортировка, поиск, preview
  текста/изображений, контекстное меню, drag-and-drop, copy/move,
  rename, trash, mkdir.
- Запуск без терминала: `Kivi UI.bat`, ярлык, трей, один экземпляр.
- API hardening: loopback Host/Origin, content-type, XFO.

## Приёмка

```powershell
.venv\Scripts\python.exe scripts\acceptance_smoke.py
```

Результат: **PASS**; тесты: **1679**; `ruff` чистый.

M6 чтение: **13 уникальных реальных заказов**.

## Известные ограничения

- fill/act/task проверены live на тестовой карточке без сохранения; создание задачи не выполнялось.
- Видео — локальное движение из кадра, не полноценный text-to-video;
  это принятый fallback, реальный провайдер — после релиза.
- Кастомный wake ONNX не обучен.
- VRM и `web/avatar3d/node_modules` не коммитятся.
- Explorer P2 (shell namespace, undo, thumbnails) не входит в прототип.

## Команды

```powershell
Kivi UI.bat
kivi crm stats
kivi media video "kivi portrait" --duration 4
.venv\Scripts\python.exe scripts\acceptance_smoke.py
```
