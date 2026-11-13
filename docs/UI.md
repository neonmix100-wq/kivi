# Kivi UI: полноценный локальный интерфейс

`kivi ui` запускает локальный сервер и открывает интерфейс в браузере:
`http://127.0.0.1:8765`. Это не облако: страница, API и модели остаются на
этом компьютере.

![Kivi UI](images/kivi-ui.png)

![Kivi Media](images/kivi-ui-media.png)

## Запуск без терминала

Дважды щёлкни **`Kivi UI.bat`** в корне проекта. Откроется отдельное окно
приложения (Edge/Chrome `--app`) и иконка Kivi в трее.

Или вручную:

```powershell
cd D:\AI-assistent
.venv\Scripts\kivi.exe ui --app --tray
```

Полезные флаги:

```powershell
kivi ui --port 8765
kivi ui --no-browser
kivi ui --app
kivi ui --tray
kivi ui --model hf.co/Qwen/Qwen3-8B-GGUF:Q4_K_M
kivi ui --whisper-model data/models/faster-whisper-large-v3-turbo
```

## Что умеет интерфейс

- чат с историей (SQLite), новые чаты, очистка;
- **медиа-панель**: prompt, 512/768/1024, flux/turbo, результат и галерея
  сессии через `/media/image`;
- голосовые ответы: кнопка «Озвучить», авто-озвучка, `/tts`;
- **голосовой ввод**: удерживай кнопку микрофона, браузер запишет WAV и
  отправит его в локальный Whisper (`/transcribe`);
- голоса из локальной библиотеки: `kivi voice add/list/use`;
- текущий провайдер и модель в настройках;
- аватар Kivi с состояниями idle/listening/thinking/speaking;
- проверка звука через WebAudio;
- локальный API: `/health`, `/chat`, `/tts`, `/transcribe`, `/history`,
  `/voices`, `/settings`.

## API

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/` | HTML-интерфейс |
| GET | `/health` | статус и версия |
| POST | `/chat` | `{message}` → `{answer, task_id}` |
| POST | `/tts` | `{text}` → `audio/wav` |
| POST | `/transcribe` | WAV-тело → `{text}` (локальный Whisper) |
| POST | `/media/image` | `{prompt,width,height,model}` → `{image: dataURL}` |
| GET | `/history?limit=50` | последние сообщения |
| GET | `/voices` | голоса библиотеки |
| POST | `/voices/active` | `{id}` → активный голос |
| GET | `/settings` | провайдер, модель, sample rate |

API слушает только loopback и не имеет аутентификации; не выставляй его в
интернет. Текст для `/tts` уходит в выбранный TTS-движок: Fish Audio — в
облако, Qwen/Silero/Piper — локально.

## Ограничения текущего среза

- первый голосовой ввод загружает Whisper и может занять несколько секунд;
- браузерный микрофон требует разрешения; при отказе UI покажет причину;
- нет системного трея-уведомления о новых сообщениях;
- нет стриминга Fish Audio: WAV приходит целиком;
- история — последние сообщения, без ветвления диалогов.
