# Приёмка Kivi

## Одной командой

```powershell
cd D:\AI-assistent
.venv\Scripts\python.exe scripts\acceptance_smoke.py
```

Проверка офлайн: конфиг, UI-ассеты, `pythonw`, реестр инструментов, CRM-токен,
ffmpeg, VRM и локальный three.js. Сеть и файлы не изменяются.

Машиночитаемый вариант:

```powershell
.venv\Scripts\python.exe scripts\acceptance_smoke.py --json
```

## Матрица прототипа

| Блок | Статус | Как проверить |
|---|---|---|
| Конфиг | ✅ | `kivi config check` |
| Тесты | ✅ 1674 | `pytest -q` |
| Линт | ✅ | `ruff check src tests` |
| Чат | ✅ | `kivi chat "Привет"`, UI → Чат |
| История | ✅ | UI → История |
| Голос Fish/Qwen | ✅ | `kivi say "Привет"`, UI → Голоса |
| Микрофон/STT | ✅ | UI → микрофон, `kivi listen --mic` |
| Wake | ✅ текстовый / ⚠️ ONNX нет | `kivi wake status` |
| 3D VRM | ✅ | `Kivi Avatar.bat`, `kivi avatar` |
| Липсинк | ✅ | `kivi chat "..." --avatar --speak` |
| AltCRM чтение | ✅ 13 реальных заказов | `kivi crm stats`, UI → CRM |
| AltCRM fill | ✅ live 5/5, не сохранено | UI → CRM → Заполнить |
| AltCRM акт | ✅ live 1/1, не сохранено | UI → CRM → Акт |
| AltCRM задача | ✅ live 3/3, диалог не создан | UI → CRM → Задача |
| Медиа-изображение | ✅ | `kivi media image`, UI → Медиа |
| Видео | ✅ локальный fallback | UI → Медиа → Видео, `kivi media video` |
| Файлы | ✅ | UI → Файлы: поиск, sort, copy/move, rename, trash, image preview, menu, DnD |
| Запуск без консоли | ✅ | ярлык `Kivi` / `Kivi UI.bat` |
| API security | ✅ | loopback Host/Origin, JSON/audio CT, XFO |
| Explorer-замена | ❌ не полностью | вкладки/полный контекстный набор |

## Ручной smoke

1. Запустить **Kivi** с рабочего стола.
2. Чат: «Привет» → ответ.
3. Голоса: выбрать Fish Astrea, сказать фразу.
4. CRM: «Проверить» → «Прочитать заказ» (13 уникальных уже в журнале).
5. Медиа: изображение 512, затем видео 2 с.
6. Файлы: открыть `docs/images/kivi-vrm.png`, правый клик, drag файла в папку.

## Известные ограничения

- заполнение CRM меняет живую карточку, но не сохраняет её;
- видео — движение из одного кадра, не полноценный text-to-video;
- кастомный wake ONNX не обучен; текстовый wake через Whisper работает;
- VRM и `web/avatar3d/node_modules` не коммитятся (лицензия/размер).
