# Обучение wake-модели Kivi

Цель — маленькая ONNX-модель, которая слушает только звук и не тратит ресурсы
на постоянный Whisper.

## Что нужно

- 20 коротких записей «Киви» твоим голосом;
- 20 записей обычной речи для негативов;
- микрофон;
- CPU достаточно; GPU не обязателен.

## 1. Запись

```powershell
.venv\Scripts\python.exe scripts\record_wake_samples.py --label positive --count 20
.venv\Scripts\python.exe scripts\record_wake_samples.py --label negative --count 20
```

Каждый раз нажми Enter и скажи фразу. Записи лягут в:

```text
data/wake-samples/positive/
data/wake-samples/negative/
```

Говори так же, как обычно говоришь с Kivi: та же комната, тот же микрофон,
без наушников и без специальной дикции.

## 2. Обучение

```powershell
.venv\Scripts\python.exe scripts\train_wake_model.py `
  --positive-dir data/wake-samples/positive `
  --negative-dir data/wake-samples/negative
```

Результат:

```text
assets/wake/kivi.onnx
data/wake-metrics.json
```

Модель маленькая (сотни КБ), обучается примерно минуту на CPU.

## 3. Проверка

```powershell
kivi wake status
```

Должно появиться:

```text
ONNX wake: доступен (D:\AI-assistent\assets\wake\kivi.onnx)
```

Затем `kivi listen --mic` использует ONNX-детектор, а Whisper запускается
только после срабатывания.

## Синтетический режим

Без записей скрипт умеет генерировать данные через Piper. Это только для
проверки пайплайна: один синтетический голос даёт слабую модель и много
ложных срабатываний. Для реального использования нужны записи твоего голоса.

## Ресурсы

- запись: микрофон, без сети;
- обучение: CPU, ~1 минута, без скачивания датасетов;
- готовая модель: ONNX Runtime, несколько МБ RAM;
- Whisper остаётся только для распознавания команды после wake.

## Ограничения

- модель чувствительна к микрофону и комнате;
- после смены микрофона/гарнитуры перезапиши 10–20 сэмплов и переобучи;
- это не замена полноценному openWakeWord-датасету, но для личного ПК
  дешевле и точнее универсальной модели.
