# Qwen3-TTS: голос Kivi

Модуль `kivi.senses.qwen_tts` добавляет Qwen3-TTS 1.7B как полноценный
`TtsEngine`. Он состоит из двух стадий:

1. **VoiceDesign** — создаёт оригинальный голос по текстовому описанию и
   сохраняет короткий reference WAV + `.txt` с произнесённым текстом;
2. **Base / voice clone** — клонирует этот reference и синтезирует любые фразы
   стабильным тембром.

Это не клон конкретной актрисы/персонажа: голос создаётся с нуля, поэтому
нет проблемы прав на чужой тембр.

## Модели

| Модель | Назначение | Размер | Лицензия |
|---|---|---:|---|
| `Qwen3-TTS-12Hz-1.7B-VoiceDesign` | создать голос по описанию | ~3,8 ГБ | Apache-2.0 |
| `Qwen3-TTS-12Hz-1.7B-Base` | клонировать reference и синтезировать | ~3,8 ГБ | Apache-2.0 |
| `Qwen3-TTS-12Hz-0.6B-CustomVoice` | лёгкий fallback | ~1,2 ГБ | Apache-2.0 |

Языки: русский, английский, китайский, японский, корейский, немецкий,
французский, португальский, испанский, итальянский.

## Установка

Qwen3-TTS требует CUDA-сборку torch для нормальной скорости. CPU-синтез
2,8 секунды речи занимает около 100 секунд — только для отладки.

```powershell
.venv\Scripts\python.exe -m pip install "torch==2.11.0+cu128" "torchaudio==2.11.0+cu128" --index-url https://download.pytorch.org/whl/cu128
.venv\Scripts\python.exe -m pip install "qwen-tts>=0.1.1" librosa soundfile sox einops
```

`qwen-tts` закрепляет `transformers==4.57.3`, `accelerate==1.12.0` и
`huggingface-hub<1`. Если нужен `diffusers`/SDXL, держите Qwen3-TTS в отдельном
venv или ставьте `qwen` extra в изолированное окружение.

Модели скачиваются один раз:

```powershell
$env:HF_HOME='D:\AI-assistent\data\hf-cache'
.venv\Scripts\hf.exe download Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign --local-dir data\models\qwen3-tts\1.7B-VoiceDesign
.venv\Scripts\hf.exe download Qwen/Qwen3-TTS-12Hz-1.7B-Base --local-dir data\models\qwen3-tts\1.7B-Base
```

## Создать голос

```powershell
.venv\Scripts\kivi.exe voice design `
  --text "Привет! Меня зовут Киви. Я твой локальный ассистент." `
  --out assets\voice-samples\kivi_voice_warm.wav
```

По умолчанию используется инструкция:

> Adult female voice, 22-25 years old, medium-low warm alto: soft, calm,
> natural Russian pronunciation, mature and reassuring, slight smile in the
> voice, not childish, not squeaky and not high-pitched.

Можно передать свою и понизить тон:

```powershell
.venv\Scripts\kivi.exe voice design --instruct "Взрослый женский голос, 24 года, низкое тёплое контральто, спокойный, естественный русский" --pitch -4 --out assets\voice-samples\kivi_voice_deep.wav
```

`--pitch` — сдвиг в полутонах (отрицательный — ниже). Для взрослого голоса
обычно достаточно **-3…-5**; сильнее — риск «мужского» тембра.

Рядом появится `.txt` — транскрипт reference. Он обязателен для клонирования.

Готовые варианты (F0 измерен):

| Файл | Сдвиг | Median F0 |
|---|---:|---:|
| `kivi_voice_natural.wav` | −3 | 204 Гц |
| `kivi_voice_warm.wav` | −4 | 193 Гц |
| `kivi_voice_deep.wav` | −5 | 182 Гц |

Старые высокие варианты (F0 400+ Гц) удалены как неподходящие.

## Синтез

```powershell
.venv\Scripts\kivi.exe say --engine qwen "Добрый вечер! Чем помочь?" --play
```

По умолчанию:

- Base-модель: `data/models/qwen3-tts/1.7B-Base`;
- reference: `assets/voice-samples/kivi_voice_warm.wav`;
- язык: `Russian`;
- устройство: `cuda` (если CUDA недоступна — ошибка с подсказкой).

Можно выбрать другой reference:

```powershell
.venv\Scripts\kivi.exe say --engine qwen "Текст" --reference assets\voice-samples\kivi_voice_deep.wav --play
```

## Архитектура

```text
QwenVoiceDesigner
  VoiceDesign model
  text + instruct -> reference WAV + transcript

QwenTtsEngine (TtsEngine)
  Base model
  reference WAV + transcript -> voice_clone_prompt
  text -> generate_voice_clone -> WAV
```

- загрузка моделей ленивая;
- reference и prompt кэшируются в памяти;
- размер prompt не меняется от фразы, поэтому тембр стабилен;
- аудио пишется как WAV PCM16 через `kivi.senses.tts.write_wav`;
- `synth(..., sample_rate=...)` игнорируется: Qwen сам выбирает 24 кГц;
- ошибки оборачиваются в `QwenTtsError` (наследник `TtsError`).

## Производительность на этой машине

RTX 4060 Ti 16 ГБ, torch 2.11.0+cu128, BF16, без flash-attn:

| Сценарий | Результат |
|---|---|
| VoiceDesign, 4,6 с речи | load 11,9 с, первый синтез 63,7 с, peak 4 087 МБ |
| Base clone, короткая фраза | load 3–14 с, синтез 6–8 с |
| `kivi say --engine qwen`, 1,8 с WAV | ~35 с вместе с загрузкой модели |
| CPU float32, 2,8 с речи | load 9,8 с, синтез 101 с |

Вывод: 1.7B даёт качественный тембр, но **не real-time** на этой карте без
flash-attn. Для живого диалога используйте 0.6B CustomVoice/Base или
предзагружайте модель и синтезируйте по предложениям.

## VRAM

| Сценарий | Ожидаемо |
|---|---:|
| Qwen3-TTS 1.7B (BF16) + desktop | ~4,1 ГБ (измерено) |
| 8B Q4 + Qwen3-TTS одновременно | ~14 ГБ + запас — впритык |
| 4B router + Qwen3-TTS | ~10–12 ГБ — да |
| 0.6B CustomVoice + 8B | ~11–12 ГБ — да |

Для 16 ГБ лучше выгружать LLM перед длинной озвучкой или использовать 4B/0.6B.

## Ограничения

- нет потокового аудио в `TtsEngine`; сейчас синтез по предложениям;
- Qwen3-TTS обучен на больших данных, но русская просодия может отличаться от
  носителя; VoiceDesign позволяет подобрать тембр, не акцент;
- `sox` в Windows может отсутствовать; модель работает через librosa/soundfile,
  предупреждение SoX не фатально;
- watermarks/лицензии: Qwen3-TTS Apache-2.0, но не выдавайте синтетический голос
  за реального человека;
- для максимального качества нужен GPU; CPU — только отладка.
