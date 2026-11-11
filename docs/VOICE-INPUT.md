---
tags: [kivi, voice, audio, m5]
updated: 2026-11-11
---

# Kivi: голосовой ввод (M5)

Статус: **библиотека M5 готова**, CLI не изменён.
Модуль: `src/kivi/senses/audio.py`. Тесты: `tests/unit/test_audio.py` (50 тестов).
Оркестрация хода и barge-in: [`VOICE-PIPELINE.md`](VOICE-PIPELINE.md).

Документ описывает нижний слой голоса: захват звука, VAD, сегментацию,
распознавание речи, wake-word и push-to-talk. Импорт модуля не открывает
микрофон и не загружает модели: `sounddevice`, `silero_vad`, `faster_whisper`
и `openwakeword` импортируются лениво, только при первом реальном использовании.

## 1. Что входит

| Слой | API | Назначение |
|---|---|---|
| Захват | `SoundDeviceSource`, `WaveFileSource`, `ArraySource` | моно PCM16, блоки по 30 мс |
| VAD | `EnergyVad`, `SileroVad` | речь / не речь по кадру |
| Сегментация | `VadSegmenter`, `Segment` | от начала фразы до паузы, int16 PCM + длительность |
| STT | `WhisperTranscriber`, `FakeTranscriber`, `Transcript` | локальное распознавание |
| Wake-word | `OpenWakeWordDetector`, `TextWakeWordDetector` | только кастомная модель Kivi |
| Push-to-talk | `PushToTalkGate` | обход wake-word, пока клавиша удерживается |

Контракты — `typing.Protocol` (`AudioSource`, `Vad`, `Transcriber`,
`WakeWordDetector`), поэтому тесты и приложение могут подставлять свои объекты
без наследования.

### 1.1. Окружение

Нужен Python 3.12 и проектный `.venv`. В текущем окружении уже стоят
`sounddevice`, `numpy`, `faster-whisper`, `silero-vad`, `openwakeword`,
`onnxruntime` и `torch` (CPU). Для чистой установки:

```powershell
.venv\Scripts\python.exe -m pip install numpy sounddevice faster-whisper
.venv\Scripts\python.exe -m pip install silero-vad openwakeword onnxruntime
```

`torch` нужен Silero VAD; на Windows проще поставить CPU-сборку с
[официального индекса](https://pytorch.org/get-started/locally/), чтобы не
тянуть CUDA. Проверка импортов без открытия микрофона:

```powershell
.venv\Scripts\python.exe -c "import sounddevice, faster_whisper; print('ok')"
.venv\Scripts\python.exe -c "import silero_vad, openwakeword; print('ok')"
```

В Windows разрешите доступ к микрофону: **Параметры → Конфиденциальность →
Микрофон → Разрешить приложениям доступ**. Без этого `SoundDeviceSource.read()`
вернёт `AudioUnavailable`, а не «тишину».

## 2. Поток данных

```text
SoundDeviceSource / WaveFileSource / ArraySource
        │  read(frames) -> np.int16, 16 kHz, mono
        ▼
VadSegmenter(EnergyVad | SileroVad)
        │  feed(frame) -> Segment | None
        ▼
Segment(pcm: int16, duration_s: float, sample_rate: int)
        │
        ▼
WhisperTranscriber.transcribe(pcm) -> Transcript
        │  text, language, duration_s, segments
        ▼
TextWakeWordDetector / has_wake_word / PushToTalkGate
        │
        ▼
VoiceSession (docs/VOICE-PIPELINE.md) -> answer -> TTS
```

`VadSegmenter` копит кадры, пока VAD сообщает речь, и отдаёт сегмент после
паузы. `WhisperTranscriber` не вызывается на каждом кадре: только на готовом
сегменте, иначе CPU будет занят постоянно.

Интеграционное замечание: `VoiceSession` из [`VOICE-PIPELINE.md`](VOICE-PIPELINE.md)
читает источник до пустого кадра и не вызывает `segmenter.flush()` на EOF.
Поэтому поток (WAV/массив) должен заканчиваться паузой не короче `hangover`,
либо `flush()` нужно вызвать в своём цикле после конца источника. Для микрофона
это не проблема: следующий кадр тишины закроет сегмент.

## 3. Быстрый старт без микрофона

`ArraySource` и `FakeTranscriber` полностью детерминированы, поэтому этот пример
работает в CI и не требует звуковой карты.

```python
import numpy as np

from kivi.senses.audio import (
    ArraySource,
    EnergyVad,
    FakeTranscriber,
    VadSegmenter,
)

samplerate = 16000
# 1 секунда: 0.4 с тишины, 0.3 с тона, 0.3 с тишины.
pcm = np.zeros(samplerate, dtype=np.int16)
tone = (0.3 * np.sin(2 * np.pi * 440 * np.arange(4800) / samplerate) * 32767).astype(
    np.int16
)
pcm[6400:11200] = tone

source = ArraySource(pcm, samplerate=samplerate)
vad = EnergyVad(threshold=0.01, min_speech_ms=250, hangover_ms=500)
segmenter = VadSegmenter(vad, samplerate=samplerate, hangover_ms=0)
transcriber = FakeTranscriber("Киви, привет")

while True:
    frame = source.read(480)  # 30 мс
    if frame.size == 0:
        break
    segment = segmenter.feed(frame)
    if segment is not None:
        print(transcriber.transcribe(segment.pcm))

pending = segmenter.flush()
if pending is not None:
    print(transcriber.transcribe(pending.pcm))
```

`hangover_ms=0` у сегментера здесь не случаен: `EnergyVad` уже держит речь
500 мс после паузы. Если сегментеру передать «сырой» VAD (например,
`SileroVad`), ему нужен собственный `hangover_ms=400…500`.

## 4. Источники аудио

### 4.1. Микрофон

```python
from kivi.senses.audio import AudioUnavailable, SoundDeviceSource

source = SoundDeviceSource(samplerate=16000, chunk_ms=30)
try:
    frame = source.read(source.frames_per_chunk)
finally:
    source.close()
```

- Конструктор только сохраняет параметры. `import sounddevice` и
  `sd.InputStream(...)` происходят при первом `read()`.
- Поток открывается в моно, `dtype="int16"`, 16 кГц. Устройство можно выбрать
  через `device=...` (индекс или имя, как в sounddevice).
- `last_overflow` показывает, потерял ли драйвер часть кадров при последнем
  чтении; для диагностики пропусков.
- `AudioUnavailable` — единая ошибка «нет пакета / нет устройства / поток
  закрыт». В тестах вместо микрофона передаётся `stream_factory`.

### 4.2. WAV-файл

```python
from kivi.senses.audio import WaveFileSource

with WaveFileSource("data/voice-test.wav", samplerate=16000) as source:
    chunk = source.read(480)
```

`WaveFileSource` принимает только моно, 16-битный PCM WAV с частотой ровно
`samplerate`. Стерео, 8/24/32-бит и другая частота дают `AudioUnavailable`:
ресемплинга в этом слое нет, чтобы не портить речь незаметно.

### 4.3. Массив для тестов

```python
from kivi.senses.audio import ArraySource

source = ArraySource(np.zeros(1600, dtype=np.int16), samplerate=16000)
```

Принимает `np.int16`, `np.float32` в `[-1, 1]`, `bytes` PCM16 и массив формы
`(frames, 1)`. В конце потока возвращает пустой массив; после `close()` чтение
даёт `AudioUnavailable`.

### 4.4. Контракт `AudioSource`

```python
class AudioSource(Protocol):
    samplerate: int

    def read(self, frames: int) -> np.ndarray: ...
    def close(self) -> None: ...
```

`read()` возвращает до `frames` сэмплов; пустой массив — конец потока.
`frames_per_chunk` — удобное свойство у всех трёх реализаций, его использует
`VoiceSession` из [`VOICE-PIPELINE.md`](VOICE-PIPELINE.md).

## 5. VAD и сегментация

### 5.1. `EnergyVad`

```python
from kivi.senses.audio import EnergyVad

vad = EnergyVad(
    threshold=0.01,       # RMS в [0, 1], не «сырой» int16
    min_speech_ms=250,    # столько речи нужно, чтобы открыть сегмент
    hangover_ms=500,      # столько тишины терпим после речи
)
```

| Параметр | По умолчанию | Смысл |
|---|---:|---|
| `threshold` | `0.01` | RMS-порог; ниже — тишина/шум |
| `min_speech_ms` | `250` | защита от щелчков и клавиатуры |
| `hangover_ms` | `500` | не рвать фразу на короткой паузе |
| `samplerate` | `16000` | нужен для перевода кадров в миллисекунды |

Ориентиры для `threshold` (подбирать под комнату и микрофон):

| Условие | Стартовое значение |
|---|---:|
| тихая комната, близкий микрофон | `0.006…0.012` |
| офис, работающий ноутбук | `0.015…0.03` |
| шумная комната, вентилятор | `0.03…0.07` |

Если порог слишком низкий, VAD «слышит» шум и сегменты не заканчиваются. Если
слишком высокий — пропадает начало тихой фразы. `last_rms` хранит уровень
последнего кадра, по нему удобно калибровать.

`EnergyVad` — не ML-модель: он не отличает музыку и стук от речи. Для качества
лучше `SileroVad`, а `EnergyVad` оставить дешёвым fallback'ом и тестовым VAD.

### 5.2. `SileroVad`

```python
from kivi.senses.audio import SileroVad

vad = SileroVad(threshold=0.5, samplerate=16000)
speech = vad.is_speech(frame)  # модель загрузится здесь, один раз
```

- Модель `silero-vad` идёт в комплекте пакета; Kivi ничего не скачивает.
  `load()` можно вызвать заранее, чтобы убрать задержку с первого кадра.
- Silero ждёт окно 512 сэмплов (32 мс при 16 кГц). Обёртка сама буферизует
  кадры по 30 мс и обрабатывает только полные окна, поэтому `frame` может быть
  любого размера.
- `is_speech()` возвращает `True`, если хотя бы одно полное окно в кадре
  превысило порог. Остаток буфера переносится в следующий вызов.
- `reset()` очищает и буфер, и состояние модели (`reset_states`).
- При отсутствии `silero-vad`/`torch` — `AudioUnavailable` с понятным текстом.

Порог `0.5` — нейтральный старт. Для тихой речи `0.35…0.45`, для шумной
`0.6…0.75`. Проверяйте на своих записях, а не на синтетическом тоне.

### 5.3. `VadSegmenter`

```python
from kivi.senses.audio import EnergyVad, VadSegmenter

segmenter = VadSegmenter(
    EnergyVad(),
    samplerate=16000,
    chunk_ms=30,
    max_segment_s=30.0,
    hangover_ms=0,        # EnergyVad уже делает hangover
    preroll_ms=300,       # вернуть начало фразы, которое VAD «съел» на старте
    speech_pad_ms=100,    # оставить 100 мс после последней речи
)

segment = segmenter.feed(frame)   # Segment | None
pending = segmenter.flush()       # Segment | None, конец потока
```

| Параметр | По умолчанию | Смысл |
|---|---:|---|
| `samplerate` | `16000` | частота кадров |
| `chunk_ms` | `30` | номинальный размер кадра |
| `max_segment_s` | `30` | принудительная отдача длинного сегмента |
| `hangover_ms` | `0` | дополнительная пауза после `False` от VAD |
| `preroll_ms` | `300` | сколько последних кадров добавить к началу |
| `speech_pad_ms` | `100` | хвост после последней речи |

Поведение:

1. Пока VAD молчит, сегментер хранит только ограниченный pre-roll.
2. На первом `True` начинается сегмент; pre-roll добавляется, чтобы не потерять
   первый слог (особенно если VAD открывается через `min_speech_ms`).
3. Пока VAD говорит `True`, кадры добавляются, `hangover`-таймер сброшен.
4. После `hangover_ms` тишины сегмент закрывается; хвост обрезается до
   последней речи + `speech_pad_ms`.
5. `max_segment_s` закрывает сегмент принудительно; следующий кадр речи
   начинает новый. Это защита от «вечной» фразы и от роста памяти.
6. `flush()` отдаёт незакрытый сегмент; после него `flush()` возвращает `None`.

`Segment` — frozen dataclass:

```python
@dataclass(frozen=True, slots=True)
class Segment:
    pcm: np.ndarray          # int16, mono
    duration_s: float
    sample_rate: int = 16000
```

Важно: `VadSegmenter` не ресемплит и не нормализует громкость. Он только
собирает и обрезает PCM. `duration_s` считается по фактическому числу сэмплов,
а не по числу кадров.

## 6. Распознавание речи

### 6.1. Установка и модель

`faster-whisper` уже стоит в `.venv` этого проекта. Для чистой установки:

```powershell
.venv\Scripts\python.exe -m pip install faster-whisper
```

`WhisperTranscriber` по умолчанию использует `small`, `cpu`, `int8`, `ru`.
При первом `transcribe()` faster-whisper скачает веса в свой кэш (обычно
`%USERPROFILE%\.cache\huggingface`), если модели там ещё нет. Это единственный
сетевой шаг; импорт модуля и конструктор ничего не скачивают.

Полностью офлайновый вариант — заранее скачанная модель и `model_path`:

```python
from pathlib import Path

from kivi.senses.audio import WhisperTranscriber

transcriber = WhisperTranscriber(
    model_size="small",
    device="cpu",
    compute_type="int8",
    language="ru",
    model_path=Path("data/models/whisper-small"),
)
transcript = transcriber.transcribe(segment.pcm)
```

Если `model_path` задан, но пути нет, — `SttUnavailable("Whisper model not
found: ...")` до обращения к faster-whisper. Ошибки загрузки модели и импорта
тоже приходят как `SttUnavailable`; пустой звук и сбой распознавания —
`SttError`.

### 6.2. `Transcript`

```python
@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    text: str
    start_s: float
    end_s: float

@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    language: str
    duration_s: float
    segments: tuple[TranscriptSegment, ...] = ()
```

`text` — склейка непустых сегментов, `language` — определённый язык или
заданный `language`, `duration_s` — из `TranscriptionInfo` либо по длине
входного PCM. `segments` сохраняют таймкоды для субтитров, логов и отладки.

`WhisperTranscriber` передаёт в faster-whisper **float32 в `[-1, 1]`**, а не
сырой int16: модель ждёт нормализованный сигнал. Вход может быть `int16`,
`float32`, `bytes` PCM16 или массивом `(frames, 1)`; всё приводится к моно.

### 6.3. `FakeTranscriber`

```python
from kivi.senses.audio import FakeTranscriber

transcriber = FakeTranscriber("Киви, привет", language="ru")
transcript = transcriber.transcribe(segment.pcm)
assert transcriber.calls  # записанный PCM
```

Детерминированный транскрибер для тестов и текстовых сценариев. Он не
загружает модели, не ходит в сеть и считает `duration_s` по числу сэмплов.

## 7. Wake-word

### 7.1. Только кастомная модель Kivi

`OpenWakeWordDetector` **не подставляет** `hey_jarvis` или другую стоковую
фразу. Если кастомной модели нет, первый `load()`/`detect()` выбрасывает
`WakeWordUnavailable` с текстом:

```text
custom Kivi model is not installed: expected ...\assets\wake\kivi.onnx
or ...\data\models\wake\kivi.onnx
```

Порядок поиска по умолчанию:

1. `<data>/models/wake/kivi.onnx` — профильная модель (учитывает `KIVI_HOME`);
2. `<repo>/assets/wake/kivi.onnx` — модель в репозитории.

Явный `model_path` имеет приоритет. Имя файла должно быть `kivi.onnx`:
детектор берёт ключ оценки из `Path.stem` (`kivi`) и не угадывает по другим
моделям в словаре.

```python
from kivi.senses.audio import OpenWakeWordDetector

detector = OpenWakeWordDetector(threshold=0.5)
if detector.detect(frame):
    detector.reset()  # перевзвести детектор после срабатывания
```

- `detect()` возвращает `True` **один раз** на пересечение порога, а не на
  каждом кадре выше порога. Это защищает от серии ложных сегментов.
- `reset()` очищает edge-детектор и, если модель загружена, вызывает
  `model.reset()`.
- Модель грузится при первом `detect()`; конструктор не импортирует
  `openwakeword`.
- Для `.onnx` используется `inference_framework="onnx"`, нужен
  `onnxruntime` (уже стоит). Для `.tflite` — `tflite-runtime`, если он есть.
- openWakeWord получает int16 PCM; `detect()` сам приводит float-кадры.

### 7.2. Где взять модель

Готовой `assets/wake/kivi.onnx` в репозитории нет — это осознанное ограничение
текущего среза. Модель обучается отдельно (openWakeWord custom model) на
записях «Киви»/«Kivi» и кладётся по одному из путей выше. Не используйте чужие
wake-модели и не переименовывайте `hey_jarvis` в `kivi.onnx`: детектор будет
срабатывать на чужую фразу.

Требования к модели:

- вход 16 кГц, моно, 16-бит;
- один класс `kivi`, без мультиклассовых словарей;
- порог `0.5` — стартовый; false wake < 1/час проверяется на своих записях;
- лицензия модели должна разрешать локальное использование.

### 7.3. Текстовый детектор

`TextWakeWordDetector` проверяет **начало** уже готового транскрипта. Это не
акустический детектор, а удобный helper для push-to-talk, `VoiceSession` и
тестов.

```python
from kivi.senses.audio import TextWakeWordDetector

detector = TextWakeWordDetector(("киви", "kivi"))
detector.detect("Киви, включи музыку")   # True
detector.detect("привет, Киви")          # False — не в начале
detector.detect("кивик")                 # False — это другое слово
detector.strip_wake_word("Киви, стоп")   # "стоп"
```

Слова сравниваются без учёта регистра; ведущие кавычки/пунктуация допускаются.
Детектор смотрит только первый токен, поэтому «привет, Киви» не считается
обращением. `strip_wake_word` удаляет слово и следующую пунктуацию.

В `src/kivi/senses/voice.py` есть аналогичные `has_wake_word`/
`strip_wake_word`; используйте их, если работаете через `VoiceSession`, а
`TextWakeWordDetector` — если нужен объект с `detect()`/`reset()`.

## 8. Push-to-talk

`PushToTalkGate` — это bypass wake-word: пока клавиша удерживается, каждый кадр
считается обращением к Kivi.

```python
from kivi.senses.audio import PushToTalkGate

gate = PushToTalkGate()
gate.press()          # нажатие глобального хоткея
assert gate.detect(frame) is True
gate.release()        # отпускание
assert gate.active is False
```

- `press()`, `release()`, `toggle()`, `reset()`; `active` — текущее состояние.
- `detect(frame)` игнорирует кадр и возвращает `active`; так gate можно
  подставить туда же, где стоит `OpenWakeWordDetector`.
- `with gate:` автоматически нажимает и отпускает — удобно в тестах.
- Глобальный хоткей и CLI в этом срезе **не менялись**. Точка интеграции —
  приложение/`VoiceSession`: нажатие вызывает `gate.press()`, отпускание —
  `gate.release()` и `segmenter.flush()`.

Пример выбора между PTT и wake-word:

```python
gate = PushToTalkGate()
wake = OpenWakeWordDetector(threshold=0.5)
# ... в цикле чтения кадров:
if gate.active or wake.detect(frame):
    segment = segmenter.feed(frame)
    if segment is not None:
        text = transcriber.transcribe(segment.pcm).text
```

При отпускании PTT не забудьте `flush()`: последняя фраза может не успеть
закрыться по hangover. Если wake-детектор сработал, вызовите `wake.reset()`.

## 9. Задержки и бюджеты

Ориентиры для 16 кГц, 30 мс, CPU:

| Этап | Типичная задержка | Комментарий |
|---|---:|---|
| один кадр | 30 мс | шаг `read()`/`feed()` |
| `EnergyVad` onset | 150–300 мс | `min_speech_ms` |
| `SileroVad` окно | 32 мс | плюс буферизация 30 мс кадров |
| конец фразы | 300–500 мс | `hangover_ms` |
| STT `small/int8` CPU | 1–3 с на 5 с речи | зависит от CPU и длины |
| первый `load()` Whisper | секунды–десятки секунд | скачивание/чтение весов |
| wake-word | < 1.5 с | цель из ROADMAP |

Рекомендации:

- держите `chunk_ms=30`: это компромисс между отзывчивостью и накладными
  расходами;
- не запускайте Whisper на каждый кадр — только на `Segment`;
- загрузите Whisper и wake-модель заранее (`vad.load()`, первый `detect()`),
  чтобы первый ход не платил за инициализацию;
- `max_segment_s=15…30` ограничивает патологически длинную фразу;
- `speech_pad_ms=100` уменьшает обрезку последнего слога, но не добавляйте
  больше 200–300 мс без необходимости: Whisper получает лишнюю тишину;
- для `SileroVad` ставьте `VadSegmenter(hangover_ms=400…500)`, для
  `EnergyVad` — `hangover_ms=0` (у него свой hangover).

Цель M5: пробуждение < 1.5 с и простая команда < 3 с. Замеряйте на своём
железе; таблица выше — не SLA, а стартовые ожидания.

## 10. Ограничения

1. **В CI нет микрофона.** Все тесты используют `ArraySource`, WAV во временной
   папке и `FakeTranscriber`/fake-модели. `sounddevice`, `faster-whisper` и
   `openwakeword` не открывают устройство и не ходят в сеть.
2. **Нет кастомной wake-модели.** `assets/wake/kivi.onnx` отсутствует; без неё
   `OpenWakeWordDetector` честно падает с `WakeWordUnavailable`. Стоковые фразы
   не используются.
3. **Только 16 кГц, моно, PCM16.** Нет ресемплинга, микширования стерео,
   нормализации громкости и шумоподавления. WAV с другой частотой отклоняется.
4. **Нет streaming STT.** Один сегмент — один финальный `Transcript`; частичные
   гипотезы не поддерживаются.
5. **Нет AEC.** Микрофон слышит TTS; barge-in по фразе может ложно срабатывать
   на собственную речь. Это ограничение железа/следующего слоя.
6. **Нет CLI-команды.** `kivi voice` и глобальный PTT-хоткей — отдельный шаг;
   `src/kivi/interfaces/cli.py` в этом срезе не изменялся.
7. **`EnergyVad` не различает речь и музыку/стук.** Для реального микрофона
   предпочтителен `SileroVad`; энергетический VAD остаётся дешёвым fallback'ом.
8. **Первый запуск Whisper может скачать модель.** Для офлайна задайте
   `model_path` и проверьте, что веса лежат локально.
9. **Windows-устройства.** Эксклюзивный режим, неправильная частота или
   отключённый микрофон дают `AudioUnavailable`; выбирайте устройство через
   `SoundDeviceSource(device=...)`.

## 11. Приватность

- **Аудио не уходит в облако.** `SoundDeviceSource` читает локальное
  устройство, `faster-whisper` и `openWakeWord` работают локально. В этом слое
  нет HTTP-клиента и облачных TTS/STT.
- **Не логируйте сырое аудио.** `data/logs/` и аудит должны получать только
  факт операции, длительность и идентификатор, а не PCM и не путь к личному
  WAV. Сегменты держите в памяти; временные WAV удаляйте.
- **Транскрипты — персональные данные.** Они могут содержать имена, адреса и
  секреты. Не отправляйте их в облако, не включайте в публичные демо и не
  добавляйте в RAG без явного разрешения.
- **Голос — биометрия.** Согласие, датасет и клонирование — по
  [`VOICE-CLONING.md`](VOICE-CLONING.md); по умолчанию клонирование выключено.
- **Сеть только при явной загрузке модели.** Импорт `audio.py` и конструкторы
  не ходят в сеть. Первый `WhisperTranscriber.transcribe()` может скачать веса,
  если модели нет; для приватного контура используйте `model_path`.
- **PTT снижает ложный захват.** Пока gate не нажат, микрофон можно не
  использовать вовсе; это предпочтительный режим в присутствии других людей.

## 12. Проверка

```powershell
.venv\Scripts\python.exe -m pytest -q tests\unit\test_audio.py
.venv\Scripts\python.exe -m ruff check src\kivi\senses\audio.py tests\unit\test_audio.py
```

Тесты покрывают:

- чтение `ArraySource`/`WaveFileSource`, конец потока, `close`, ошибки WAV;
- ленивый `SoundDeviceSource` и отсутствие пакета `sounddevice`;
- RMS, `min_speech_ms`, `hangover_ms`, reset и валидацию `EnergyVad`;
- ленивую загрузку, буферизацию 30 мс, порог и reset `SileroVad`;
- старт/конец/hangover/pre-roll/max-segment/flush `VadSegmenter`;
- ленивый Whisper, нормализацию int16→float32, `model_path`, ошибки STT;
- отсутствие кастомной wake-модели, порог, edge-детект, reset;
- текстовый wake-word, PTT-gate и интеграцию source→segmenter→transcriber.

Ни один тест не открывает микрофон и не использует сеть.

## 12.1. CLI

```powershell
# без --mic команда не открывает устройство и завершается с подсказкой
.venv\Scripts\kivi.exe listen --mic --push-to-talk
.venv\Scripts\kivi.exe listen --mic --turns 3 --speak
```

`--mic` — явное согласие на захват; без него Kivi не трогает микрофон.
При отсутствии устройства команда завершается с ошибкой, а не висит.

## 13. Ссылки

- [`PERSONA.md`](PERSONA.md) — требования к голосу и barge-in.
- [`VOICE-AVATAR.md`](VOICE-AVATAR.md) — TTS и 2D-аватар.
- [`VOICE-PIPELINE.md`](VOICE-PIPELINE.md) — `VoiceSession`, barge-in, метрики.
- [`VOICE-CLONING.md`](VOICE-CLONING.md) — согласие, датасет, приватность.
- [`ROADMAP.md`](ROADMAP.md) — M5: Voice & Avatar.
- [`STATUS.md`](STATUS.md) — актуальный статус этапов.
