# Сэмплы голосов Kivi

Один и тот же текст, чтобы сравнивать тембр и разборчивость.

Текст: `sample_text_ru.txt`

> Привет. Я Киви, ваш локальный ассистент. Помогу с кодом, документами и управлением
> компьютером. Говорю кратко, но могу объяснить подробнее, если попросите.

| Файл | Голос | Тип | Примечание |
|---|---|---|---|
| `piper_irina.wav` | Piper `ru_RU-irina-medium` | локальный ONNX | быстрый, офлайн, 0 VRAM |
| `sapi_irina.wav` | Windows SAPI `Microsoft Irina Desktop` | локальный системный | уже есть в Windows |
| `edge_svetlana.mp3` | Edge TTS `ru-RU-SvetlanaNeural` | облако Microsoft | текст уходит в Microsoft |
| `silero_baya.wav` | Silero `v4_ru` `baya` | локальный PyTorch | тёплый, естественный; базовая версия |
| `silero_v5_baya_48k.wav` | Silero `v5_ru` `baya`, 48 кГц | локальный PyTorch | чище, больше деталей |
| `silero_v5_baya_48k_clean.wav` | Silero `v5_ru` `baya`, очищенный | локальный PyTorch | high-pass 70 Гц, нормализация −1 dBFS |
| `silero_v5_baya_48k_ssml.wav` | Silero `v5_ru` `baya`, SSML | локальный PyTorch | паузы между фразами, rate 95%, pitch −2% |
| `silero_v5_baya_48k_ssml_clean.wav` | Silero `v5_ru` `baya`, SSML + очистка | локальный PyTorch | SSML + high-pass/нормализация |
| `silero_v5_baya_24k.wav` | Silero `v5_ru` `baya`, 24 кГц | локальный PyTorch | для совместимости |
| `silero_kseniya.wav` | Silero `kseniya` | локальный PyTorch | спокойный |
| `silero_xenia.wav` | Silero `xenia` | локальный PyTorch | мягкий |

**Выбор пользователя (09.11.2026): Silero `baya`.** Рабочий вариант — `v5_ru` 48 кГц;
очищенная версия используется, если high-pass и нормализация не портят тембр.
Piper `irina` остаётся fallback для CPU; Edge `Svetlana` — только с разрешением на облако.

## Как слушать

Открыть файлы в проводнике или:

```powershell
Start-Process .\assets\voice-samples\piper_irina.wav
Start-Process .\assets\voice-samples\sapi_irina.wav
Start-Process .\assets\voice-samples\edge_svetlana.mp3
```

## Лицензии и приватность

- Piper: модель `ru_RU-irina-medium`, локальная; проверьте лицензию голоса перед
  коммерческим использованием.
- Windows SAPI: системный голос Microsoft; условия — лицензия Windows.
- Edge TTS: онлайн-сервис Microsoft; не использовать для секретных текстов
  без согласия. `edge-tts` — не локальная модель.
- Silero: CC BY-NC 4.0 — личное использование, не коммерческое. Модель работает локально,
  но требует PyTorch и заметно тяжелее Piper.

## Что дальше

1. ~~Пользователь выбирает голос.~~ Выбран Silero `baya`.
2. Голос фиксируется в `config/kivi.yaml` (`provider: silero`, `voice: baya`, `sample_rate: 48000`).
3. Для выбранного голоса настраиваются скорость, паузы и потоковая озвучка.
4. Сравнить `v4` и `v5` 48 кГц на длинном тексте; выбрать чистый или исходный вариант.
5. Клонирование голоса жены — только после письменного согласия; см. `docs/VOICE-CLONING.md`.
