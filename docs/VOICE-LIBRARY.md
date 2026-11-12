---
tags: [kivi, voice, library, qwen, fish]
updated: 2026-11-13
---

# Kivi: локальная библиотека голосов

Статус: **модуль готов**, CLI не изменён (интеграция — за родительским агентом).
Модуль: `src/kivi/senses/voice_library.py`.
Тесты: `tests/unit/test_voice_library.py` (41 тест, без сети и без реальных кодеков).

Связанные документы: [[VOICE-QWEN]], [[VOICE-FISH]], [[VOICE-CLONING]].

`VoiceLibrary` — локальное хранилище голосовых reference-записей. Профиль
содержит короткий WAV, транскрипт, движок и запись о согласии. Qwen3-TTS
клонирует reference; будущие движки могут использовать тот же профиль.
Модуль ничего не скачивает сам по себе: URL импортируется только по явному
вызову `add_url`, модели не загружаются, сеть в тестах не используется.

## 1. Согласие и права

Импорт невозможен без явного `consent=True`:

```python
library.add_file(path, "Мой голос", consent=True, transcript="Привет, это я.")
```

- `consent=False` или любое не-`True` значение (включая `1`) отклоняется
  с `VoiceLibraryError` — согласие нельзя «включить по умолчанию»;
- согласие фиксируется в `profile.json` вместе с `created_at`;
- автоматической проверки прав нет: ответственность на вызывающем коде и
  пользователе. Загружайте только свой голос или голос, на который есть
  письменное разрешение владельца;
- не импортируйте голоса публичных лиц, детей и третьих лиц без разрешения;
- не выдавайте синтетическую речь за реального человека; для публикаций
  проверяйте требования площадки к раскрытию синтетического контента;
- удаление профиля удаляет локальные файлы, но не является юридическим
  отзывом согласия — исходное разрешение храните отдельно.

## 2. Хранилище

```text
data/voices/
├── index.json          # отсортированный индекс, пишется атомарно
├── active.json         # {"version": 1, "id": "<профиль>"}
└── kivi-warm/
    ├── profile.json    # один VoiceProfile
    ├── reference.wav   # 24 кГц, моно, PCM16
    ├── reference.txt   # транскрипт (sidecar для Qwen3-TTS)
    └── original.wav    # исходник, если copy=True (по умолчанию)
```

- `index.json` — `{"version": 1, "profiles": [...]}`, профили отсортированы
  по `id`. Каждая мутация пересобирает индекс из папок `*/profile.json`,
  поэтому устаревший или повреждённый индекс самовосстанавливается при
  чтении. Запись идёт через временный файл в той же папке и `os.replace`.
- `active.json` — выбранный голос. `active()` возвращает `VoiceProfile | None`.
- `reference.txt` нужен Qwen3-TTS: `qwen_tts` читает его автоматически, если
  `QwenTtsConfig.reference_text` пуст.
- `original.<ext>` — архив исходника при `copy=True`; при `copy=False`
  остаётся только нормализованный `reference.wav`.
- Метаданные не содержат секретов: URL без credentials, чувствительные
  query-параметры (`token`, `signature`, `key`, `auth`, `expires`, …)
  вырезаются из `source_url`.

Поля профиля:

| Поле | Тип | Смысл |
|---|---|---|
| `id` | slug | `[a-z0-9-]`, генерируется из имени |
| `name` | str | человекочитаемое имя, уникальное без учёта регистра |
| `engine` | `qwen` / `fish` / `silero` | для какого движка профиль |
| `reference_wav` | str | путь относительно `root`, например `kivi-warm/reference.wav` |
| `transcript` | str | произнесённый в reference текст |
| `source` | `file` / `url` / `design` | происхождение |
| `source_url` | str | очищенный HTTPS-URL или `""` |
| `consent` | bool | всегда `True` для профилей, созданных библиотекой |
| `created_at` | str | ISO 8601 UTC, `Z` |
| `duration_s` | float | длительность reference, ≤ 120 с |
| `notes` | str | заметки; флаг `transcript missing`, если текста нет |

## 3. Python API

```python
from pathlib import Path

from kivi.senses.voice_library import VoiceLibrary

library = VoiceLibrary()                 # Path("data/voices")
library = VoiceLibrary(Path("data/voices"))
```

### Импорт из файла

```python
profile = library.add_file(
    "C:/audio/my_voice.wav",
    "Мой тёплый голос",
    consent=True,
    transcript="Привет! Меня зовут Киви.",
    engine="qwen",          # qwen | fish | silero
    copy=True,              # сохранить исходник рядом
)
```

Пустой транскрипт можно заполнить инъектируемым транскрайбером (например,
обёрткой над faster-whisper):

```python
def transcribe(path: Path) -> str:
    return my_whisper.transcribe(path).text

profile = library.add_file(
    "voice.flac",
    "Голос из FLAC",
    consent=True,
    transcriber=transcribe,
)
```

- `transcriber(path: Path) -> str` вызывается только при пустом `transcript`;
- если транскрибера нет, профиль создаётся с пустым текстом, а в `notes`
  добавляется `transcript missing`; Qwen3-TTS не сможет клонировать голос,
  пока текст не добавлен;
- ошибка транскрибера откатывает импорт: папка профиля удаляется, индекс не
  меняется.

### Импорт по URL

```python
profile = library.add_url(
    "https://example.com/voices/sample.wav",
    "Голос из интернета",
    consent=True,
    transcript="Текст, произнесённый в записи.",
)
```

`downloader(url) -> bytes` можно подменить в тестах; `resolver(host) -> [ip]`
— для проверки DNS без сети.

### Чтение и управление

```python
library.list()                        # list[VoiceProfile], сортировка по id
library.get("Мой тёплый голос")       # по имени или id; None, если нет
library.get("moy-teplyy-golos")
library.set_active("moy-teplyy-golos")
library.active()                      # VoiceProfile | None
library.reference_path(profile)       # абсолютный путь к reference.wav
library.update_transcript("moy-teplyy-golos", "Исправленный текст.")
library.remove("moy-teplyy-golos")    # удаляет только папку внутри root
```

`remove` удаляет `<root>/<id>` и очищает `active.json`, если удалён активный
профиль. Любой путь строится только из проверенного slug и дополнительно
сверяется с `root`; `../` в имени/URL не может вывести за пределы библиотеки.

## 4. Нормализация

`normalize_audio(src, dst) -> NormalizedAudio` (поля `path`, `duration_s`,
`trimmed`):

- лениво импортирует `librosa`, `soundfile`, `numpy`;
- декодирует любой librosa-читаемый формат;
- ресемплирует в **24 000 Гц, моно, PCM16 WAV**;
- обрезает до **120 с** (`trimmed=True`);
- вход и выход ограничены **20 МБ**, пустой файл отклоняется;
- `VoiceLibraryError` при отсутствии декодера, пустом файле и не-аудио.

`add_file` дополнительно проверяет расширение по белому списку и размер до
декодирования.

## 5. Qwen3-TTS: клонирование

```python
from kivi.senses.qwen_tts import QwenTtsConfig, QwenTtsEngine
from kivi.senses.voice_library import VoiceLibrary

library = VoiceLibrary()
profile = library.active() or library.get("kivi-warm")
if profile is None or not profile.has_transcript:
    raise RuntimeError("нужен профиль с транскриптом")

engine = QwenTtsEngine(
    QwenTtsConfig(
        reference_wav=library.reference_path(profile),
        reference_text=profile.transcript,
    )
)
```

- `reference_wav` — абсолютный путь из `reference_path()`;
- `reference_text` можно не передавать: рядом с WAV лежит `reference.txt`,
  и `qwen_tts` прочитает его сам;
- один профиль = один reference; для другого тембра создайте другой профиль;
- профиль с `engine="fish"` или `"silero"` для Qwen не предназначен, даже
  если у него есть локальный WAV.

## 6. Fish Audio и Silero

- **Fish Audio** использует облачные voice ID, а не локальные WAV.
  `FishTtsConfig(reference_id=...)` — это идентификатор голоса в Fish; файл
  `reference.wav` в профиле служит только локальной копией/аудитом и никуда
  не загружается. Ключ читается только из `FISH_AUDIO_API_KEY`, его нельзя
  писать в `notes` или `source_url`.
- **Silero** выбирает голос по `speaker` (`baya` и т.п.), а не по WAV.
  Профиль с `engine="silero"` фиксирует согласие и параметры (speaker можно
  записать в `notes`), но локальный reference для синтеза не нужен.
- Для Fish/Silero транскрипт не обязателен для синтеза, но полезен как
  документация и для будущих движков.

## 7. URL: правила безопасности

| Проверка | Правило |
|---|---|
| Схема | только `https`; `http`, `file`, `ftp` отклоняются |
| Credentials | `user:password@host` запрещены |
| Порт | только стандартные 80/443 |
| Хост | `localhost`, `*.localhost`, `*.local`, `*.internal`, `*.home.arpa`, `*.lan` запрещены |
| IP | литерал и **все** resolved-адреса должны быть публичными (`is_global`) |
| Redirect | не следуем; ответ 3xx — ошибка, поэтому redirect на приватный хост недостижим |
| Размер | `Content-Length` и фактическое тело ≤ 20 МБ |
| Тип | `Content-Type: audio/*` или расширение из белого списка |
| Метаданные | `source_url` без fragment и чувствительных query-параметров |

Проверка DNS и последующее соединение не «пинятся» на один IP, поэтому
теоретически возможен TOCTOU/DNS-rebinding между проверкой и запросом; для
локальной библиотеки это принятый компромисс, а приватные литералы и
localhost отклоняются детерминированно.

## 8. Поддерживаемые форматы

Белый список расширений: `wav`, `wave`, `flac`, `ogg`, `oga`, `opus`, `mp3`,
`m4a`, `aac`, `aiff`, `aif`, `aifc`, `wma`, `webm`. Реальная поддержка
зависит от сборки `libsndfile`: WAV/FLAC/OGG читаются всегда, MP3/M4A — на
libsndfile 1.2+. Если формат не декодируется, импорт завершается
`VoiceLibraryError`, а не создаёт битый профиль.

## 9. CLI-интеграция (добавляет родительский агент)

`src/kivi/interfaces/cli.py` здесь **не изменялся**. Предлагаемое
соответствие команд:

| Команда | Вызов |
|---|---|
| `kivi voice add <path> --name ... --consent` | `VoiceLibrary.add_file(...)` |
| `kivi voice add-url <url> --name ... --consent` | `VoiceLibrary.add_url(...)` |
| `kivi voice list` | `VoiceLibrary.list()` |
| `kivi voice use <name|id>` | `VoiceLibrary.set_active(...)` |
| `kivi voice transcript <name|id> --text ...` | `VoiceLibrary.update_transcript(...)` |
| `kivi voice remove <name|id>` | `VoiceLibrary.remove(...)` |

Рекомендации для CLI:

- `--consent` — обязательный флаг без значения по умолчанию; текст справки
  должен объяснять права на голос;
- при `profile.notes` с `transcript missing` печатать предупреждение и
  подсказку `kivi voice transcript`;
- `--engine {qwen,fish,silero}` прокидывать в `engine=`;
- `say --engine qwen` может брать `library.active()`, если явный
  `--reference` не задан;
- в `VoiceSettings` можно добавить `library_dir: Path = Path("data/voices")`
  (Pydantic `extra="forbid"`, значение по умолчанию — отдельной правкой
  конфига родителем).

После добавления документа запустите генератор индекса документации:

```powershell
.venv\Scripts\python.exe scripts\build_knowledge_index.py
```

## 10. Проверка

```powershell
.venv\Scripts\python.exe -m pytest -q tests\unit\test_voice_library.py
.venv\Scripts\python.exe -m ruff check src\kivi\senses\voice_library.py tests\unit\test_voice_library.py
```

Тесты не ходят в сеть: URL-скачивание подменяется fake-downloader'ом,
`_fetch_audio` — monkeypatch'ем, DNS — инъектируемым resolver'ом. WAV
генерируется стандартным модулем `wave`.

## 11. Ограничения

- нет шифрования: `data/voices` — обычные файлы; это не секретное хранилище,
  хотя `data/` не попадает в git;
- один reference на профиль, ≤ 120 с и ≤ 20 МБ; длинное аудио обрезается;
- нет автоматической проверки согласия и watermarking;
- качество клонирования зависит от чистоты записи и точности транскрипта;
- нет переименования и слияния профилей; дубликат имени или slug отклоняется;
- нет блокировки на несколько процессов: запись индекса атомарна, но
  одновременные писатели могут перезаписать друг друга;
- папки без `profile.json` игнорируются и не удаляются автоматически;
- голоса Fish Audio остаются в облаке и локально не воспроизводятся;
- redirect-цепочки не поддерживаются;
- `source_url` очищается от query-параметров с «секретными» именами, но
  произвольный текст в `transcript`/`notes` не фильтруется — не записывайте
  туда ключи.
