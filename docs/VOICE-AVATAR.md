# Kivi: голос и 2D-аватар

Этап 6 (M1). Документ описывает два новых модуля:

- `src/kivi/senses/tts.py` — Silero TTS, Piper fallback, WAV и пайплайн по предложениям.
- `src/kivi/interfaces/avatar2d.py` — состояния аватара, RMS-липсинк и опциональный Tk-рендер.

Оба модуля работают локально. Сеть не используется: модели и бинарники берутся только
из уже установленного `.venv` и `assets/`.

## 1. TTS

### 1.1. Silero `baya` (`v5_ru`, 48 кГц)

Основной движок — `SileroTts`. Модель лежит в пакете Silero внутри виртуального
окружения:

```text
.venv/Lib/site-packages/silero/model/v5_ru.pt
```

Загрузка ленивая: конструктор не импортирует `torch` и не читает 145 МБ модель.
При первом `synth()` используется `torch.package.PackageImporter` и
`load_pickle("tts_models", "model")`. Никакого `torch.hub.download_url_to_file`.

```python
from pathlib import Path

from kivi.senses.tts import SileroTts

engine = SileroTts()  # путь по умолчанию; модель ещё не загружена
result = engine.synth("Привет, я Киви.", Path("data/cache/tts/hello.wav"))
print(result.path, result.sample_rate, result.duration_s, result.engine)
```

Параметры `synth(text, output, *, speaker="baya", sample_rate=48000)`. Модель
вызывает `apply_tts(text=..., speaker="baya", sample_rate=48000)` и возвращает
`torch.Tensor`; тензор переводится в PCM16 без numpy-зависимости.

Первый синтез занимает несколько секунд (импорт torch + загрузка модели), поэтому
`SileroTts` нужно держать одним долгоживущим экземпляром. Лицензия модели —
CC BY-NC 4.0; коммерческое использование требует отдельного решения.

### 1.2. Piper fallback

`PiperTts` — офлайн-запасной движок:

```text
.venv/Scripts/piper.exe
assets/voice-samples/piper-voices/ru_RU-irina-medium.onnx
```

Запуск идёт без shell и без сети: `piper.exe -m <model> -f <output>`, текст
передаётся в stdin как UTF-8. Пути и runner инъектируются:

```python
from kivi.senses.tts import PiperTts

engine = PiperTts(
    executable=Path(".venv/Scripts/piper.exe"),
    model=Path("assets/voice-samples/piper-voices/ru_RU-irina-medium.onnx"),
)
result = engine.synth("Привет", Path("data/cache/tts/piper.wav"))
```

Голос Irina — 22050 Гц; `PiperTts` сообщает фактический sample rate из WAV, а не
подменяет его на 48000. `runner` с сигнатурой `(argv: list[str], text: str) -> None`
нужен для тестов; при инъекции реальный процесс не запускается. Если runner не
задан, отсутствие exe/модели даёт `TtsError` до запуска.

### 1.3. WAV

`write_wav(path, samples, sample_rate)` пишет **mono, 16-bit PCM, little-endian**:

- `samples` — `bytes`/`bytearray`/`memoryview` с уже готовым PCM16 либо
  последовательность float в `[-1, 1]` (масштаб 32767) или int16;
- заголовок создаётся стандартным `wave`, родительские каталоги создаются;
- длительность результата считается как `frames / sample_rate`.

`NullTts` пишет детерминированную тишину и используется в тестах и для отключения
озвучки.

### 1.4. `SpeechPipeline`

```python
from kivi.senses.tts import PiperTts, SileroTts, SpeechPipeline

pipeline = SpeechPipeline(
    SileroTts(),
    fallback=PiperTts(),
    player=None,              # Callable[[Path], object] | None
    enable_fallback=True,
    speaker="baya",
    sample_rate=48000,
)
results = pipeline.synthesize("Привет. Как дела?", Path("data/cache/tts"))
```

- Текст нормализуется по пробелам и режется по `.!?…` (закрывающие кавычки и
  скобки остаются в предложении). Пустые фрагменты пропускаются.
- Каждое предложение дополнительно режется по словам до `max_chars=200`.
- Результат — список `TtsResult(path, sample_rate, duration_s, engine)`, файлы
  `speech_000.wav`, `speech_001.wav`, ...
- Если основной движок бросил исключение и fallback включён, предложение
  синтезируется через Piper, и пайплайн переключается на fallback до конца ответа.
- `enable_fallback=False` превращает ошибку в `TtsError` без второго движка.
- `player` вызывается после каждого файла; исключение плеера оборачивается в
  `TtsError`. Без `player` аудиоустройство не нужно.
- Пустой текст возвращает `[]` и не создаёт каталог.

## 2. 2D-аватар

### 2.1. Состояния

`AvatarState` — `StrEnum` со значениями:

| Состояние | Когда |
|---|---|
| `idle` | ожидание, дыхание/моргание |
| `listening` | пользователь говорит |
| `thinking` | идёт планирование или генерация |
| `speaking` | проигрывается TTS, работает липсинк |
| `error` | явная ошибка, ставится только вызовом `set_state("error")` |

`AvatarController` намеренно «прощающий»: переход разрешён из любого состояния в
любое, включая `error`. Автоматически в `error` контроллер не уходит. Неизвестная
строка даёт `AvatarError`.

```python
from kivi.interfaces.avatar2d import AvatarController, AvatarState

controller = AvatarController(on_event=lambda event: print(event))
controller.set_state("listening")
controller.on_speech_start()
openness = controller.update_pcm(pcm16_bytes)
controller.on_speech_end()
print(controller.snapshot())  # {"state": <AvatarState.IDLE>, "openness": 0.0}
```

Событие — словарь `{"type": ..., "state": ..., "previous": ..., "openness": ...}`.
Типы: `state_changed`, `speech_start`, `speech_end`, `lip_sync`.

### 2.2. Липсинк по амплитуде

`LipSync` не использует аудио-библиотеки. Вход — PCM16 mono `bytes` (little-endian)
или последовательность int16. Буфер режется на окна по 20 мс. Для каждого окна:

```text
RMS    = sqrt(mean(sample_i²)) / 32768
target = min(1, RMS * gain)          # gain по умолчанию 3.0
target = 0, если target < noise_floor

openness += (target - openness) * attack   # если target >= openness
openness += (target - openness) * decay    # если target <  openness
openness = clamp(openness, 0, 1)
```

Значения по умолчанию: `attack=0.6`, `decay=0.2`, `noise_floor=0.01`,
`window_ms=20`, `sample_rate=48000`. `reset()` мгновенно закрывает рот.
`update_pcm()` — псевдоним `update()`.

Такой липсинк — огибающая, а не ви земы: он не различает «А» и «О», но даёт
естественное открытие/закрытие рта и не требует ML.

### 2.3. Рендерер

`AvatarRenderer` — протокол с `start()`, `render(state, openness)`, `stop()`.
`TkAvatarRenderer` — минимальная заглушка:

- конструктор **не** создаёт Tk и не открывает окно;
- `start()` создаёт окно, грузит `assets/avatar/concept/kivi_portrait.png`, если
  файл существует, и рисует овал рта;
- `render()` до `start()` безопасно запоминает состояние;
- `stop()` идемпотентен; окно можно не создавать вовсе;
- без дисплея `start()` поднимает `AvatarError` с понятным сообщением.

Ограничения заглушки: нет прозрачного фона, always-on-top-поведения рабочего стола,
перетаскивания, нескольких мониторов, моргания и анимации дыхания. Это
development-заглушка для проверки липсинка, а не финальный Tauri/VRM-клиент.
Tk-окно должно создаваться в главном потоке; `start()`/`stop()` нельзя вызывать
из фонового потока TTS.

## 3. Интеграция с CLI (без правок `cli.py`)

`cli.py` в этом срезе не меняется. Предлагаемая точка подключения — после
получения текстового ответа в REPL:

```python
from pathlib import Path

from kivi.config import load_config
from kivi.interfaces.avatar2d import AvatarController
from kivi.senses.tts import PiperTts, SileroTts, SpeechPipeline

config = load_config()
voice = config.voice
avatar = AvatarController()
pipeline = SpeechPipeline(
    SileroTts(),
    fallback=PiperTts(),
    enable_fallback=voice.fallback.provider == "piper",
    speaker=voice.speaker,
    sample_rate=voice.sample_rate,
)

avatar.on_speech_start()
try:
    results = pipeline.synthesize(answer, Path("data/cache/tts"))
finally:
    avatar.on_speech_end()
```

Практические замечания:

1. `data/cache/tts/` — временный каталог; не писать озвучку в `assets/`, чтобы не
   засорять репозиторий. Каталог создаётся автоматически.
2. Для липсинка нужен PCM без 44-байтного WAV-заголовка: откройте файл через
   `wave` и передайте `readframes()` в `avatar.update_pcm(...)`.
3. Если используется `player`, он вызывается на каждый файл; удобно обновлять
   аватар внутри callback. Если плеера нет, тесты и CLI работают без звука.
4. Первый вызов Silero прогревает модель несколько секунд. Создавайте
   `SpeechPipeline` один раз на сессию, а не на каждый ответ.
5. `voice.rate`, `put_accent`/`put_yo` пока не передаются в `SileroTts`: это
   следующий шаг (просодия/нормализация). Текущий API — только текст, спикер и
   sample rate.
6. Ошибки TTS уже обёрнуты в `TtsError`; CLI должен показать предупреждение и
   продолжить текстовый чат, а не падать.
7. Для потоковой озвучки «первый звук < 700 мс» в будущем нужен синтез по одному
   предложению с немедленным player-callback, а не ожидание всего списка.

## 4. Проверка

```powershell
.venv\Scripts\python.exe -m pytest -q tests\unit\test_tts.py tests\unit\test_avatar2d.py
.venv\Scripts\python.exe -m ruff check src\kivi\senses\tts.py src\kivi\interfaces\avatar2d.py tests\unit\test_tts.py tests\unit\test_avatar2d.py
```

Тесты не запускают Piper, не загружают Silero-модель и не открывают Tk-окно.
Реальный Silero-тест помечен как optional: при отсутствии torch или модели он
пропускается.
