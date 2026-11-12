# Fish Audio: облачный голос Astrea/s

Fish Audio voice ID `7b220f0a97de48e7a32cf1f09381d0d3` — публичный голос
**Astrea/s** из Voice Library: Female, Young, Character, движок Fish Audio
Drama 3.

Это **не локальный файл**. Голос доступен только через Fish Audio API; для
синтеза нужен ключ и доступ к API.

## S2.1 Pro Free — это API-модель

`fish audio s2.1-pro-free` нельзя «установить» как checkpoint: это model string
облачного Fish API. Kivi отправляет его в заголовке `model`:

```text
POST https://api.fish.audio/v1/tts
Authorization: Bearer $FISH_AUDIO_API_KEY
model: s2.1-pro-free
```

S2.1 Pro Free: 83 языка, включая русский; ~90 мс TTFA на стандартном API;
бесплатно по Fair Use до **30.11.2026** (по данным Fish Audio на 13.11.2026),
без SLA и с возможным retention запросов. Коммерческие ограничения возможны:
сверяйтесь с условиями Fish Audio.

## Настройка

```powershell
$env:FISH_AUDIO_API_KEY = "ваш-ключ"
```

Ключ читается только из env, никогда не пишется в `config/` и не коммитится.
`config/kivi.yaml` хранит только публичный voice ID и model string:

```yaml
voice:
  fish_reference_id: 7b220f0a97de48e7a32cf1f09381d0d3
  fish_model: s2.1-pro-free
  fish_base_url: https://api.fish.audio/v1
  fish_sample_rate: 44100
  fish_latency: balanced
```

## Синтез

```powershell
.venv\Scripts\kivi.exe say --engine fish "Привет! Я Киви." --play
```

Другой публичный voice ID:

```powershell
.venv\Scripts\kivi.exe say --engine fish --reference-id <id> "Текст" --play
```

Чтобы Fish Audio стал голосом по умолчанию:

```yaml
voice:
  provider: fish
```

При отсутствии ключа команда завершается ошибкой
`FISH_AUDIO_API_KEY is not set`, без сетевого запроса.

## Как это работает

```text
kivi say --engine fish
  -> POST https://api.fish.audio/v1/tts
     Authorization: Bearer <env>
     model: s2.1-pro-free
     {"text": ..., "reference_id": ..., "format": "wav", ...}
  -> WAV response
  -> data/cache/tts/fish.wav
```

Модуль: `src/kivi/senses/fish_audio.py`.
Ответ проверяется на `RIFF`/WAV и лимит 20 МБ; ошибки не печатают ключ.
Fish присылает streaming-WAV с placeholder-размерами `0xFFFFFFxx`; Kivi
нормализует `RIFF`/`data` по фактическим байтам, поэтому длительность
считается корректно (например 1,81 с вместо 48 695 с).

## Права и лицензия

- Голос создан пользователем Fish Audio, а не Kivi. Перед использованием
  проверьте разрешение автора и условия Fish Audio; для коммерческого
  использования может требоваться платный план/лицензия.
- Не выдавайте синтетический голос за реального человека и не используйте его
  для обмана/дискредитации.
- Voice Library может меняться: если ID удалён или privатен, API вернёт ошибку.

## Приватность и стоимость

- Текст уходит на сервер Fish Audio — это осознанное исключение из local-first.
- Расход кредитов/лимитов зависит от плана; Kivi не вызывает Fish Audio
  автоматически без явной команды.
- Локальный fallback: Silero/Piper/Qwen3-TTS.

## Ограничения

- Нет офлайн-режима: voice ID и `s2.1-pro-free` нельзя скачать как checkpoint.
- Модель/голос доступны, пока Fish Audio держит free tier; проверяйте сроки и лимиты.
- Неизвестна поддержка русского акцента у конкретного голоса; проверяйте на
  своих фразах.
- Нет потокового воспроизведения в текущем `TtsEngine`; WAV целиком.
- Не тестировалось без ключа; unit-тесты используют fake HTTP.
