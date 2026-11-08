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
| `silero_baya.wav` | Silero `baya` | локальный PyTorch | тёплый, естественный |
| `silero_kseniya.wav` | Silero `kseniya` | локальный PyTorch | спокойный |
| `silero_xenia.wav` | Silero `xenia` | локальный PyTorch | мягкий |

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

1. Пользователь выбирает голос.
2. Голос фиксируется в `config/kivi.yaml`.
3. Для выбранного голоса настраиваются скорость, паузы и потоковая озвучка.
4. Для локального голоса проверяется разборчивость на 1.0–1.15×.
