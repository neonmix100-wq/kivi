# Kivi UI: полноценный локальный интерфейс

`kivi ui` запускает локальный сервер и открывает интерфейс в браузере:
`http://127.0.0.1:8765`. Это не облако: страница, API и модели остаются на
этом компьютере.

![Kivi UI](images/kivi-ui.png)

## Запуск без терминала

Дважды щёлкни **`Kivi UI.bat`** в корне проекта. Откроется браузер с Kivi.

Или вручную:

```powershell
cd D:\AI-assistent
.venv\Scripts\kivi.exe ui
```

Полезные флаги:

```powershell
kivi ui --port 8765
kivi ui --no-browser
kivi ui --model hf.co/Qwen/Qwen3-8B-GGUF:Q4_K_M
```

## Что умеет интерфейс

- чат с историей (SQLite), новые чаты, очистка;
- голосовые ответы: кнопка «Озвучить», авто-озвучка, `/tts`;
- голоса из локальной библиотеки: `kivi voice add/list/use`;
- текущий провайдер и модель в настройках;
- аватар Kivi с состояниями idle/listening/thinking/speaking;
- проверка звука через WebAudio;
- локальный API: `/health`, `/chat`, `/tts`, `/history`, `/voices`, `/settings`.

## API

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/` | HTML-интерфейс |
| GET | `/health` | статус и версия |
| POST | `/chat` | `{message}` → `{answer, task_id}` |
| POST | `/tts` | `{text}` → `audio/wav` |
| GET | `/history?limit=50` | последние сообщения |
| GET | `/voices` | голоса библиотеки |
| POST | `/voices/active` | `{id}` → активный голос |
| GET | `/settings` | провайдер, модель, sample rate |

API слушает только loopback и не имеет аутентификации; не выставляй его в
интернет. Текст для `/tts` уходит в выбранный TTS-движок: Fish Audio — в
облако, Qwen/Silero/Piper — локально.

## Ограничения текущего среза

- микрофон в браузере — заготовка push-to-talk; серверный STT ещё не подключён
  к UI (используй `kivi listen --mic`);
- нет системного трея и нативного окна;
- нет стриминга Fish Audio: WAV приходит целиком;
- история — последние сообщения, без ветвления диалогов.
