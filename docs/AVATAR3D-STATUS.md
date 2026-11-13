# Kivi 3D VRM: текущий статус

VRM-ассет найден и работает локально:

```text
assets/avatar/kivi.vrm   (~35 МБ, VRM 0.x / glTF 2.0)
```

Проверено 13.11.2026:

- `vrm_status()` → `VRM asset found`;
- страница `web/avatar3d` загружается без интернета;
- three.js и `@pixiv/three-vrm` установлены локально в
  `web/avatar3d/node_modules` (в git не входят);
- камера исправлена: персонаж виден целиком, а не только макушка;
- T-pose убран: руки опущены, добавлено лёгкое idle-дыхание/жест;
- HUD показывает `VRM`, состояние `idle`, визему `CLOSED`.

![Kivi VRM](images/kivi-vrm.png)

## Запуск

Только 3D-аватар:

```powershell
cd D:\AI-assistent
.venv\Scripts\kivi.exe avatar --port 8766
```

Откроется `http://127.0.0.1:8766/`. Для запуска без браузера:

```powershell
kivi avatar --no-browser
```

Чат с 3D-аватаром (открывает страницу и обновляет состояние при речи):

```powershell
kivi chat "Привет! Как дела?" --avatar --speak
```

## Важно

Модель **Big Fat Blue Fish** от Mzkagami: коммерческое использование и
распространение запрещены. Файл исключён из git (`assets/avatar/*.vrm`) и
используется только локально. Подробнее — [ASSET-LICENSE](ASSET-LICENSE.md).

Осталось для полной M5-приёмки:

- animation mixer (idle/listen/think/talk) и более точные жесты;
- связать виземы с UI-аватаром (сейчас 3D-страница синхронизируется с `kivi chat --avatar` через `state.json`);
- кастомный wake ONNX (текстовый wake через Whisper уже есть).
