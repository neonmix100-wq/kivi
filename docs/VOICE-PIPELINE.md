# Kivi: голосовой пайплайн M5

Статус: **черновик M5**, 11.11.2026.
Модуль: `src/kivi/senses/voice.py`. Тесты: `tests/unit/test_voice.py`.

Документ описывает оркестрацию голосового хода: wake-gate, VAD-сегменты, STT,
ответ, TTS, barge-in («Киви, стоп»), голосовые подтверждения и метрики.
Микрофон, VAD и STT сюда не входят — это инъектируемые порты из
`kivi.senses.audio`; сам `voice.py` не импортирует этот модуль.

## 1. Поток одного хода

```text
source (frames)
   │
   ├─► segmenter.feed(frame) ─► segment | None
   │                              │
   │                              ▼
   │                       transcriber.transcribe(segment.pcm)
   │                              │ text
   │                              ▼
   │                       strip_wake_word ("киви"/"kivi")
   │                              │
   │                              ▼
   │                       barge-in: «стоп»? ──► stopped turn
   │                              │ нет
   │                              ▼
   │                       wake(raw_text)? ────► not addressed
   │                              │ да / push-to-talk
   │                              ▼
   │                       answer(text) ──────► answer_ms
   │                              │ stop? ─────► stopped turn, без TTS
   │                              ▼
   └────────────────────── speak(answer) ─────► tts_ms
                                  │ stop? ─────► stopped turn
                                  ▼
                         VoiceTurn(text, answer, stopped, metrics)
```

Все стадии синхронные. Один вызов `run_once()` читает кадры до первого
сегмента, транскрибирует его и обслуживает ровно один ход. `run_loop()` повторяет
это не больше `max_turns` раз.

## 2. `VoiceSession`

```python
VoiceSession(
    source,                 # Any: поток кадров
    segmenter,              # Any: feed(frame) -> segment | None
    transcriber,            # Any: transcribe(pcm) -> transcript
    *,
    answer: Callable[[str], str],
    speak: Callable[[str], object] | None = None,
    wake: Callable[[str], bool] | None = None,
    barge_in: BargeInController | None = None,
    max_turns: int = 1,
)
```

Порты проверяются в конструкторе только на наличие методов: `segmenter.feed`,
`transcriber.transcribe`; `answer` обязателен. `source` принимает несколько форм:

| Форма источника | Как читается |
|---|---|
| `source.frames()` | вызывается один раз за ход, результат итерируется |
| `source.frames` (iterable) | итерируется напрямую |
| `source.stream` (iterable) | итерируется напрямую |
| сам `source` — iterable | итерируется напрямую |
| `source.read_frame()` / `source.read()` / `source.read(frames)` | вызывается до `None` или пустого массива/`bytes`; для `read(frames)` размер берётся из `frames_per_chunk`, иначе из `samplerate * chunk_ms`, иначе 480 |

`kivi.senses.audio.AudioSource` использует именно `read(frames)`, где `frames` —
число сэмплов; EOF обозначается пустым `numpy`-массивом. `VoiceSession`
определяет арность `read` через `inspect.signature` и сам подставляет размер
кадра, поэтому с `ArraySource`, `WaveFileSource` и `SoundDeviceSource` он
совместим без адаптера.

Источник должен быть **потоком**: повторный `frames()` на каждом ходе должен
продолжать поток, а не начинать его заново. Обычный список кадров подходит для
одного `run_once()`, но при `run_loop()` будет переигрываться с начала.

### 2.1. `run_once(push_to_talk=False) -> VoiceTurn`

1. Если stop уже запрошен — сразу вернуть `stopped=True`, не читая аудио.
2. Читать кадры и кормить `segmenter.feed(frame)`, пока не вернётся сегмент.
3. `transcriber.transcribe(segment.pcm)`; ошибка STT логируется, ход получает
   пустые `text`/`answer`, сессия не падает.
4. Убрать ведущее «киви»/«kivi» (см. `strip_wake_word`).
5. Проверить стоп-фразу; при совпадении — `stopped=True`, `answer=""`.
6. Если задан `wake`, вызвать его на исходном тексте (до очистки). `False` —
   ход помечается `addressed=False`; `answer` и `speak` не вызываются.
   `push_to_talk=True` полностью обходит wake-gate.
7. Вызвать `answer(text)`.
8. Если `speak` задан и stop не запрошен — вызвать `speak(answer)`.

`VoiceTurn` — frozen dataclass:

```python
VoiceTurn(
    text: str,              # текст после удаления wake-word
    answer: str,
    stopped: bool = False,
    metrics: VoiceMetrics = ...,
    addressed: bool = True, # False — wake-gate отклонил фразу
)
```

`turn.as_dict()` даёт JSON-совместимый снимок, включая `metrics`.

### 2.2. `run_loop(max_turns=None, push_to_talk=False, on_turn=None)`

- `max_turns=None` берёт значение из конструктора; `0` — пустой список.
- Считаются только **принятые** ходы. Ходы, отклонённые wake-gate, не попадают
  в результат, но поток продолжает читаться; число таких промахов ограничено,
  чтобы цикл не мог крутиться бесконечно.
- Пустой ход (нет сегмента, пустой текст, ошибка STT) завершает цикл.
- `turn.stopped` завершает цикл и попадает в результат последним.
- `on_turn(turn)` вызывается для каждого добавленного хода; его исключения
  логируются и не ломают цикл.
- Если stop был запрошен до `run_loop`, цикл сразу возвращает `[]`. Перед новым
  прослушиванием после остановки вызовите `session.reset()`.

### 2.3. `stop()`, `reset()`, `is_stopped`, `last_turn`

`session.stop()` — кооперативный barge-in: выставляет Event, который проверяется
между стадиями. `session.reset()` снимает флаг. `session.last_turn` хранит
последний созданный `VoiceTurn` (включая отклонённые и пустые).

## 3. Barge-in

`BargeInController` — потокобезопасный флаг с callback'ами:

```python
controller = BargeInController()
controller.on_stop(lambda: print("stop"))
controller.check("Киви, стоп")   # True, Event выставлен, callback вызван
controller.is_stopped            # True
controller.reset()               # можно слушать снова
```

- Внутри — `threading.Event` и `Lock`; `request_stop()` идемпотентен: пока флаг
  выставлен, повторный вызов возвращает `False` и не дублирует callback'и.
- `on_stop(callback)` принимает функцию без аргументов. Ошибки callback'ов
  логируются и проглатываются, чтобы сломанный наблюдатель не остановил аудио.
- `request_stop()` возвращает `True`, только если состояние изменилось.
- `stop_event` отдаёт сам `Event` — его удобно передать в TTS-воркер, который
  умеет прерывать воспроизведение по предложениям.
- `wait(timeout)` ждёт остановку; `is_stop_phrase(text)` проверяет текст без
  изменения состояния.
- `check(text)` проверяет фразу и при совпадении вызывает `request_stop()`.

### 3.1. Стоп-фразы

```python
STOP_PHRASES = (
    "киви, стоп",
    "киви стоп",
    "стоп",
    "kivi stop",
    "kivi, stop",
    "stop",
)
```

Нормализация: нижний регистр, `ё → е`, пунктуация заменяется пробелами,
повторные пробелы схлопываются. Поэтому `"  КИВИ,   СТОП!!!"` и
`"Киви — стоп"` совпадают с фразой.

- Совпадение точное; дополнительно допускается одно короткое слово из
  `STOP_TAILS` (`"пожалуйста"`, `"please"`, `"now"`, `"хватит"` и т.п.):
  `"стоп, пожалуйста"` и `"kivi stop now"` срабатывают.
- Голое `"стоп"`/`"stop"` не ищется подстрокой: длинная фраза с этим словом
  игнорируется. Для голого стопа действует лимит `MAX_BARE_STOP_CHARS = 40`;
  `"стоп-кран"` и `"please stop the music"` не являются командой.
- Свой список передаётся в `BargeInController(phrases=[...])` и в
  `is_stop_phrase(text, phrases=[...])`.

### 3.2. Где именно проверяется остановка

| Точка | Что происходит |
|---|---|
| до чтения кадров | ход сразу `stopped=True` |
| внутри чтения кадров | чтение прекращается |
| после сегмента | ход `stopped=True`, STT не вызывается |
| после STT | стоп-фраза в тексте → `stopped=True`, `answer` не вызывается |
| после wake-gate | stop, пришедший во время wake, отменяет ответ |
| после `answer` | `answer` уже выполнен, `speak` не вызывается |
| после `speak` | ход помечается `stopped=True` |

Если stop случился во время `answer`, ответ сохраняется в `turn.answer`, но не
озвучивается. Если во время `speak` — TTS уже отработал, но ход помечен
остановленным. Это следствие синхронной модели; см. ограничения.

## 4. Голосовые подтверждения

`VoiceConfirmation` не угадывает и никогда не возвращает `True` по умолчанию.

```python
confirmation = VoiceConfirmation()
confirmation.parse("да")            # True
confirmation.parse("нет")           # False
confirmation.parse("да нет")        # None (неоднозначно)
confirmation.parse("не знаю")       # None
```

| Ответ | Слова |
|---|---|
| Да | `да`, `yes`, `конечно`, `подтверждаю`, `ага` |
| Нет | `нет`, `no`, `отмена`, `неа`, фраза `не надо` |
| Неоднозначно | оба класса сразу (`"да нет"`, `"конечно нет"`), пустой/неизвестный текст |

`"не подтверждаю"` трактуется как **нет**. `"stop"` — не подтверждение, а
команда barge-in, поэтому `parse` вернёт `None`.

`ask(text, listener)` вызывает `listener(text)` и разбирает ответ:

- `bool` возвращается как есть;
- `str` разбирается через `parse`;
- объект с `.text` (например, `Transcript`) разбирается по `.text`;
- `None`, неизвестный тип или исключение listener'а → `None`.

```python
approved = confirmation.ask("Удалить файл?", lambda prompt: session.run_once().text)
if approved is not True:
    # всё, кроме явного «да», — отказ
    ...
```

Для опасных действий проверяйте именно `is True`, а не truthiness: `None` — это
«не подтверждено», а не «можно».

## 5. Метрики

`VoiceMetrics` — frozen dataclass, все значения в миллисекундах:

| Поле | Что измеряет |
|---|---|
| `wake_ms` | вызов `wake(raw_text)`; 0 без wake-gate и в push-to-talk |
| `stt_ms` | `transcriber.transcribe(pcm)`, включая время ошибки |
| `answer_ms` | `answer(text)` |
| `tts_ms` | `speak(answer)`; 0, если `speak` не задан или был пропущен |
| `total_ms` | весь `run_once`, включая чтение кадров и накладные расходы |

```python
turn.metrics.as_dict()
# {"wake_ms": 1.2, "stt_ms": 340.0, "answer_ms": 820.0,
#  "tts_ms": 610.0, "total_ms": 1790.0}

format_metrics(turn.metrics)
# "wake=1.2 ms | stt=340.0 ms | answer=820.0 ms | tts=610.0 ms | total=1790.0 ms"
```

Метрики — диагностические. Ориентиры из `docs/PERSONA.md` и `docs/ROADMAP.md`:
первый звук < 700 мс, простая голосовая команда < 3 с; здесь они не
проверяются автоматически.

## 6. Интеграция

`voice.py` не импортирует `kivi.senses.audio` и ничего не знает про конкретные
модели. Сборка портов происходит в точке композиции приложения:

```python
from pathlib import Path

from kivi.brain.core import KiviBrain
from kivi.interfaces.avatar2d import AvatarController
from kivi.senses.tts import SpeechPipeline
from kivi.senses.voice import BargeInController, VoiceSession, has_wake_word

# audio_source, vad, stt создаются в приложении из kivi.senses.audio.
# voice.py не импортирует этот модуль на уровне модуля.
barge_in = BargeInController()
avatar = AvatarController()

def on_wav(path: Path) -> None:
    # player из SpeechPipeline: проиграть WAV и обновить липсинк.
    ...

tts = SpeechPipeline(player=on_wav)

def speak(text: str) -> object:
    avatar.on_speech_start()
    try:
        return tts.synthesize(text, Path("data/cache/tts"))
    finally:
        avatar.on_speech_end()

def answer(text: str) -> str:
    return brain.ask(text).answer

session = VoiceSession(
    audio_source,
    vad,
    stt,
    answer=answer,
    speak=speak,
    wake=has_wake_word,
    barge_in=barge_in,
)
```

### 6.1. KiviBrain

`answer` — тонкая обёртка: `brain.ask(text).answer`. Голосовой слой не обходит
`PolicyEngine`, approvals и аудит; он только передаёт распознанный текст и
возвращает ответ. Ошибки brain'а остаются его ошибками; `VoiceSession` не
превращает их в успешный ответ. Для подтверждений опасных действий используйте
`VoiceConfirmation.ask(...) is True` в approver'е, а не автоматический yes.

### 6.2. TTS

`speak` обычно замыкает `SpeechPipeline.synthesize` и плеер. Пайплайн режет ответ
на предложения и вызывает `player` после каждого WAV — это точка для
`avatar.update_pcm(...)` и для проверки `barge_in.stop_event` между
предложениями. Если `speak` не передан, ответ остаётся текстовым; метрика
`tts_ms` будет 0.

### 6.3. Аватар

`AvatarController` имеет состояния `listening`, `thinking`, `speaking`, `idle`.
Рекомендуемая схема: `listening` перед чтением кадров, `thinking` перед
`answer`, `speaking` внутри `speak` до конца проигрывания, `idle` в `finally`.
`on_turn` можно использовать для обновления UI/логов, но он не должен блокировать
аудио.

### 6.4. CLI

`src/kivi/interfaces/cli.py` в этом срезе не меняется. Подключение сессии —
отдельный шаг: команда `kivi voice` сможет собрать порты из `kivi.senses.audio`
и вызвать `run_loop()`, не затрагивая текстовый REPL.

## 7. Ограничения

1. **Синхронность.** `answer` и `speak` блокируют поток; остановка проверяется
   только после их возврата. Чтобы прервать длинную генерацию или TTS, нужен
   отдельный воркер, который следит за `barge_in.stop_event`.
2. **Нет AEC.** Микрофон слышит собственный TTS; без эхоподавления barge-in по
   фразе может ложно срабатывать на собственную речь. На текущем этапе это
   ответственность аудио-слоя/железа.
3. **Stop только между стадиями.** Внутри `answer`/`speak` прерывания нет;
   `VoiceSession.stop()` не убивает поток и не обрывает уже начатый вызов.
4. **Нет wake-модели.** `wake` — это callback-гейт, а не openWakeWord. Реальное
   акустическое пробуждение и порог ложных срабатываний — следующий слой.
5. **Нет streaming STT.** Один сегмент → один финальный транскрипт; частичные
   гипотезы и ранний barge-in по промежуточному тексту не поддерживаются.
6. **Нет реального аудио в тестах.** Все тесты используют фейковые source,
   segmenter и transcriber; микрофон, WAV-плеер и модели не запускаются.
7. **Источник должен быть потоковым.** Список кадров при повторном `run_once`
   начнётся заново; для сессии нужен stateful-источник.
8. **Защита от зависания.** `MAX_FRAMES_PER_TURN = 100_000` ограничивает чтение
   кадров в одном ходе; при исчерпании бюджета ход завершается пустым.
9. **Ошибки `answer`/`speak` пробрасываются.** Обрабатываются только ошибки
   STT, wake-gate, listener'а подтверждений и наблюдателей `on_turn`; это сделано
   намеренно, чтобы сбой ответа/TTS не выглядел как успешный ход.
10. **Нет истории и состояния диалога.** `VoiceSession` не хранит контекст;
    `text`/`answer` — ответственность `KiviBrain` и памяти.
11. **Жизненный цикл источника — на вызывающей стороне.** `VoiceSession` не
    вызывает `close()` у `source`; приложение закрывает микрофон или WAV-файл
    в `finally`, когда сессия больше не нужна.

## 8. Проверка

```powershell
.venv\Scripts\python.exe -m pytest -q tests\unit\test_voice.py
.venv\Scripts\python.exe -m ruff check src\kivi\senses\voice.py tests\unit\test_voice.py
```

Тесты покрывают:

- варианты и нормализацию стоп-фраз, ложные срабатывания, кастомный список;
- идемпотентность `request_stop`, callback'и и их ошибки, `reset`, `wait`;
- yes/no/ambiguous для подтверждений, listener, отсутствие auto-yes;
- удаление wake-word и wake-gate, push-to-talk;
- один ход, отсутствие сегмента, ошибку STT, stop до/во время стадий;
- `speak`, метрики, `run_loop` с лимитом, barge-in, `on_turn`;
- разные формы `source`;
- отсутствие фоновых потоков: тесты не создают ни одного.
