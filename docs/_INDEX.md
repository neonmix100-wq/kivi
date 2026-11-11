---
tags: [kivi, moc, index]
updated: 2026-11-11
generated: true
---

# База знаний Kivi

> Карта содержимого. Сгенерирована `scripts/build_knowledge_index.py`;
> не редактируй вручную. Агент читает этот файл вместо всей документации.

## Как пользоваться

- **Человеку:** открой `D:\AI-assistent\docs` как vault в Obsidian.
- **Агенту:** `python scripts/kivi_context.py search <термин>` или
  `python scripts/kivi_context.py section ARCHITECTURE "Model Gateway"`.
- Граф Obsidian показывает связи между документами и разделами.

## Документы

| Документ | Размер | Разделов | О чём |
| --- | ---: | ---: | --- |
| [[API]] | 10.7 KB | 9 | Небольшой HTTP-слой над M1-чатом. Только стандартная библиотека Python (ThreadingHTTPServer), без FastAPI и внешних зависимостей. API локальный по умолчанию: слушает loopback и не имеет аутентификации, поэтому loopback —… |
| [[ARCHITECTURE]] | 60.2 KB | 18 | Версия: 0.2 (проектирование). Целевая платформа: Windows 11, один пользователь, один ПК. Персона: Kivi, женский голос, 3D-аватар. Долгосрочно — псевдо-ОС поверх Windows, позже Linux. |
| [[BASELINE]] | 7.8 KB | 8 | Дата первого замера: 08.11.2026. Baseline моделей: 11.11.2026 (см. §5). |
| [[BENCH-GATEWAY]] | 4.0 KB | 5 | Дата: 2026-11-10. Машина: Windows 11, NVIDIA GPU с 8192 МБ VRAM (NVML), Ollama на 127.0.0.1:11434. Модели измерены на этой машине: qwen3.5:4b (router, ~3800 МБ), qwen2.5-coder:7b (code, ~4700 МБ при 8K), bge-m3 (embeddin… |
| [[BROWSER]] | 22.0 KB | 11 | Браузер — это управляемая вкладка Chrome, а не «руки» модели. Kivi открывает страницу, читает видимый текст, нажимает, заполняет поля и скачивает файлы только через типизированные инструменты и ToolExecutor. JavaScript-в… |
| [[CLOUD]] | 4.9 KB | 5 | Облако — заёмная мощность, не режим по умолчанию. Вызов проходит проверки: режим, политика данных, бюджет, квота. Ключи хранятся только в env. |
| [[CODER-TOOLS]] | 8.0 KB | 6 | M2 coder tooling adds three read/write surfaces to the tool layer: |
| [[CODER]] | 15.7 KB | 8 | M2-срез цикла написания кода. Модель предлагает набор файлов, Coder проверяет пути и размеры, пишет файлы только через ToolExecutor, запускает тесты и при неудаче просит модель исправить код — не больше maxfixes раз. |
| [[DECISIONS]] | 25.7 KB | 11 | Статус: принято 08.11.2026. Заменяет предположения из первой версии плана. |
| [[ELEVATED]] | 17.1 KB | 10 | Версия: 0.1 (11.11.2026). Стадия 4. Связанные документы: docs/SECURITY.md §5.13, docs/ARCHITECTURE.md §11.7, docs/TOOLS.md. |
| [[LIFECYCLE]] | 10.7 KB | 5 | Этап 5 добавляет к памяти управляемый жизненный цикл: старые сообщения можно свернуть в компактные сводки, TTL-уборка удаляет просроченные сообщения вместе с их векторами, а таблица approvals из миграции 4 получает явный… |
| [[MEMORY]] | 7.5 KB | 7 | Память Kivi — это SQLite плюс файлы артефактов. Первый срез даёт сообщения, факты, предпочтения, content-addressed артефакты, FTS5-поиск и Context Builder. Векторная база и эмбеддинги подключаются следующим срезом. |
| [[MODULES]] | 9.7 KB | 9 | Цель: подключать возможности (например, workcrm из D:\ai-agent) так, чтобы их можно было обновлять, тестировать и откатывать, не переписывая ядро. |
| [[OPEN-QUESTIONS]] | 9.4 KB | 11 | Версия: после второй итерации ответов, 09.11.2026. Полные решения — в docs/DECISIONS.md. Безопасность — docs/SECURITY.md. Shell — docs/SHELL.md. |
| [[OPERATOR-SYSTEM]] | 15.4 KB | 11 | Версия: 0.1 (11.11.2026). Стадия 8 / M3: Operator. Связанные документы: docs/TOOLS.md, docs/SECURITY.md, docs/ELEVATED.md, docs/STATUS.md. |
| [[OPERATOR-UI]] | 12.6 KB | 9 | Version: 0.1 (11.11.2026). Stage: M3. Related: docs/TOOLS.md, docs/SECURITY.md, docs/CODER-TOOLS.md. |
| [[OPERATOR-WINDOWS]] | 16.9 KB | 11 | Версия: 0.1 (11.11.2026). Стадия M3 «Operator PC control». Связанные документы: TOOLS.md, SECURITY.md, TRUST.md, ROADMAP.md, STATUS.md. |
| [[PERSONA]] | 17.4 KB | 7 | Правило по умолчанию: |
| [[PLANNER]] | 21.8 KB | 11 | Документ описывает M1-контур глубокого и быстрого пути: |
| [[REUSE-AUDIT]] | 8.9 KB | 9 | Дата: 08.11.2026. Проверены соседние проекты на диске D:. |
| [[ROADMAP]] | 34.1 KB | 21 | Версия: 0.2 (после решений от 08.11.2026). Оценки — для одного разработчика с ИИ-помощником, 10–15 часов в неделю. Первый полезный текстовый Kivi — 6–8 недель; полный v1 — 6–9 месяцев. |
| [[SANDBOX]] | 24.6 KB | 9 | code.run запускает сгенерированный Python в best-effort песочнице. Это первый шаг M2 Coder: код изолирован на уровне Python-аудита и ресурсных лимитов Windows, но это не граница безопасности ОС. Полная изоляция — WSL2 ил… |
| [[SECURITY]] | 26.4 KB | 11 | Версия: 0.1 (09.11.2026). Связанные документы: docs/DECISIONS.md, docs/ARCHITECTURE.md, docs/MODULES.md, docs/SHELL.md. |
| [[SHELL]] | 22.6 KB | 15 | Версия: 0.1 (09.11.2026). Связанные документы: docs/ARCHITECTURE.md, docs/SECURITY.md, docs/ROADMAP.md. |
| [[STATUS]] | 11.2 KB | 8 | Дата среза: 11.11.2026. Дорожная карта: ROADMAP. Тесты: 392, ruff чистый. Репозиторий собирается, kivi doctor работает. |
| [[TOOLS]] | 13.9 KB | 10 | Инструмент — единственный способ, которым Kivi меняет что-то вне своего процесса. Контракт, права и аудит появились раньше первого инструмента: это инвариант проекта, а не надстройка. |
| [[TRUST]] | 5.5 KB | 6 | Всё, что приходит извне, попадает в контекст только с меткой происхождения. Метка ставится до модели и сохраняется в аудите. Правило простое: файл, сайт, письмо, вывод инструмента и картинка могут сообщать факты, но не м… |
| [[VECTOR]] | 2.0 KB | 4 | SQLite остаётся источником истины: эмбеддинги пишутся в таблицу embeddings. LanceDB — опциональный ANN-кэш для больших баз. Без него Kivi работает на SQLite + cosine-переборе. |
| [[VOICE-AVATAR]] | 12.1 KB | 4 | Этап 6 (M1). Документ описывает два новых модуля: |
| [[VOICE-CLONING]] | 17.2 KB | 8 | Статус: черновик плана, 09.11.2026. Связанные документы: docs/PERSONA.md, docs/DECISIONS.md, docs/SECURITY.md. |

## [[API|Local HTTP API]]

> Небольшой HTTP-слой над M1-чатом. Только стандартная библиотека Python (ThreadingHTTPServer), без FastAPI и внешних зависимостей. API локальный по умолчанию: слушает loopback и не имеет аутентификации, поэтому loopback —…

- [[API#Назначение и границы|Назначение и границы]]
- [[API#Быстрый старт|Быстрый старт]]
- [[API#Local-first и безопасность|Local-first и безопасность]]
- [[API#Эндпоинты|Эндпоинты]]
- [[API#Ошибки|Ошибки]]
- [[API#Лимиты и конкурентность|Лимиты и конкурентность]]
- [[API#Интеграция с KiviBrain|Интеграция с KiviBrain]]
- [[API#Будущий Web-UI|Будущий Web-UI]]
- [[API#Файлы и проверка|Файлы и проверка]]

## [[ARCHITECTURE|Архитектура Kivi]]

> Версия: 0.2 (проектирование). Целевая платформа: Windows 11, один пользователь, один ПК. Персона: Kivi, женский голос, 3D-аватар. Долгосрочно — псевдо-ОС поверх Windows, позже Linux.

- [[ARCHITECTURE#1. Резюме|1. Резюме]]
- [[ARCHITECTURE#2. Требования и ограничения|2. Требования и ограничения]]
- [[ARCHITECTURE#3. Слои системы|3. Слои системы]]
- [[ARCHITECTURE#4. Kernel|4. Kernel]]
- [[ARCHITECTURE#5. Model Gateway — послойное развёртывание|5. Model Gateway — послойное развёртывание]]
- [[ARCHITECTURE#6. Brain (оркестратор)|6. Brain (оркестратор)]]
- [[ARCHITECTURE#7. Инструменты и модули|7. Инструменты и модули]]
- [[ARCHITECTURE#8. Память|8. Память]]
- [[ARCHITECTURE#9. Восприятие|9. Восприятие]]
- [[ARCHITECTURE#10. Интерфейсы|10. Интерфейсы]]
- [[ARCHITECTURE#11. Безопасность|11. Безопасность]]
- [[ARCHITECTURE#12. Конфигурация|12. Конфигурация]]
- [[ARCHITECTURE#13. Структура репозитория|13. Структура репозитория]]
- [[ARCHITECTURE#14. Сценарии|14. Сценарии]]
- [[ARCHITECTURE#15. Нефункциональные требования и бюджет|15. Нефункциональные требования и бюджет]]
- [[ARCHITECTURE#16. Риски и меры|16. Риски и меры]]
- [[ARCHITECTURE#17. Что осознанно не делаем в v1|17. Что осознанно не делаем в v1]]
- [[ARCHITECTURE#18. Связанные документы|18. Связанные документы]]

## [[BASELINE|Baseline: железо и окружение]]

> Дата первого замера: 08.11.2026. Baseline моделей: 11.11.2026 (см. §5).

- [[BASELINE#1. Железо|1. Железо]]
- [[BASELINE#2. Окружение|2. Окружение]]
- [[BASELINE#3. Модели Ollama|3. Модели Ollama]]
- [[BASELINE#4. Замер VRAM в момент проектирования|4. Замер VRAM в момент проектирования]]
- [[BASELINE#5. Замер моделей (11.11.2026)|5. Замер моделей (11.11.2026)]]
- [[BASELINE#6. Idle-бюджет|6. Idle-бюджет]]
- [[BASELINE#7. Известные ограничения|7. Известные ограничения]]
- [[BASELINE#8. Планируемый апгрейд|8. Планируемый апгрейд]]

## [[BENCH-GATEWAY|Benchmark: переключение моделей Gateway]]

> Дата: 2026-11-10. Машина: Windows 11, NVIDIA GPU с 8192 МБ VRAM (NVML), Ollama на 127.0.0.1:11434. Модели измерены на этой машине: qwen3.5:4b (router, ~3800 МБ), qwen2.5-coder:7b (code, ~4700 МБ при 8K), bge-m3 (embeddin…

- [[BENCH-GATEWAY#Команда|Команда]]
- [[BENCH-GATEWAY#Результат|Результат]]
- [[BENCH-GATEWAY#Что показал первый прогон|Что показал первый прогон]]
- [[BENCH-GATEWAY#Выводы|Выводы]]
- [[BENCH-GATEWAY#Что делать дальше|Что делать дальше]]

## [[BROWSER|Браузер (M3): Playwright, отдельный профиль, untrusted-контент]]

> Браузер — это управляемая вкладка Chrome, а не «руки» модели. Kivi открывает страницу, читает видимый текст, нажимает, заполняет поля и скачивает файлы только через типизированные инструменты и ToolExecutor. JavaScript-в…

- [[BROWSER#1. Архитектура|1. Архитектура]]
- [[BROWSER#2. Профиль и запуск|2. Профиль и запуск]]
- [[BROWSER#3. URL и SSRF-политика|3. URL и SSRF-политика]]
- [[BROWSER#4. Селекторы и клавиши|4. Селекторы и клавиши]]
- [[BROWSER#5. Скачивания|5. Скачивания]]
- [[BROWSER#6. Модель prompt injection|6. Модель prompt injection]]
- [[BROWSER#7. Инструменты|7. Инструменты]]
- [[BROWSER#8. API контроллера|8. API контроллера]]
- [[BROWSER#9. Ограничения|9. Ограничения]]
- [[BROWSER#10. Тесты|10. Тесты]]
- [[BROWSER#11. Интеграция|11. Интеграция]]

## [[CLOUD|Cloud Broker]]

> Облако — заёмная мощность, не режим по умолчанию. Вызов проходит проверки: режим, политика данных, бюджет, квота. Ключи хранятся только в env.

- [[CLOUD#Политика|Политика]]
- [[CLOUD#Провайдеры|Провайдеры]]
- [[CLOUD#CLI|CLI]]
- [[CLOUD#Python|Python]]
- [[CLOUD#Не сделано|Не сделано]]

## [[CODER-TOOLS|Coder tools: repo index, patch and git]]

> M2 coder tooling adds three read/write surfaces to the tool layer:

- [[CODER-TOOLS#Tool table|Tool table]]
- [[CODER-TOOLS#Repository index (repo.index)|Repository index (repo.index)]]
- [[CODER-TOOLS#Patch semantics (code.patch)|Patch semantics (code.patch)]]
- [[CODER-TOOLS#Git allowlist|Git allowlist]]
- [[CODER-TOOLS#Registration and grants|Registration and grants]]
- [[CODER-TOOLS#Limitations|Limitations]]

## [[CODER|Coder: generated → tests → fixed]]

> M2-срез цикла написания кода. Модель предлагает набор файлов, Coder проверяет пути и размеры, пишет файлы только через ToolExecutor, запускает тесты и при неудаче просит модель исправить код — не больше maxfixes раз.

- [[CODER#1. Публичный API|1. Публичный API]]
- [[CODER#2. Протокол генерации|2. Протокол генерации]]
- [[CODER#3. Executor и grant|3. Executor и grant]]
- [[CODER#4. Тестовый цикл|4. Тестовый цикл]]
- [[CODER#5. R3 code.run: только с approval|5. R3 code.run: только с approval]]
- [[CODER#6. Ограничения|6. Ограничения]]
- [[CODER#7. Проверка|7. Проверка]]
- [[CODER#8. Интеграция с CLI (без правки cli.py)|8. Интеграция с CLI (без правки cli.py)]]

## [[DECISIONS|ADR-0001: рамки проекта Kivi]]

> Статус: принято 08.11.2026. Заменяет предположения из первой версии плана.

- [[DECISIONS#1. Решения пользователя|1. Решения пользователя]]
- [[DECISIONS#2. Что означает A4 на практике|2. Что означает A4 на практике]]
- [[DECISIONS#3. Облако: только бесплатные лимиты|3. Облако: только бесплатные лимиты]]
- [[DECISIONS#4. Персона Kivi|4. Персона Kivi]]
- [[DECISIONS#5. Приоритеты|5. Приоритеты]]
- [[DECISIONS#6. Апгрейд железа|6. Апгрейд железа]]
- [[DECISIONS#7. ОС и псевдо-ОС|7. ОС и псевдо-ОС]]
- [[DECISIONS#8. Модуль работы|8. Модуль работы]]
- [[DECISIONS#9. Следствия для архитектуры|9. Следствия для архитектуры]]
- [[DECISIONS#10. Решения второй итерации (09.11.2026)|10. Решения второй итерации (09.11.2026)]]
- [[DECISIONS#11. Оставшиеся вопросы|11. Оставшиеся вопросы]]

## [[ELEVATED|Elevated broker: admin-операции через UAC]]

> Версия: 0.1 (11.11.2026). Стадия 4. Связанные документы: docs/SECURITY.md §5.13, docs/ARCHITECTURE.md §11.7, docs/TOOLS.md.

- [[ELEVATED#1. Назначение|1. Назначение]]
- [[ELEVATED#2. Threat model|2. Threat model]]
- [[ELEVATED#3. Allowlist|3. Allowlist]]
- [[ELEVATED#4. Поток UAC|4. Поток UAC]]
- [[ELEVATED#5. Request / response JSON|5. Request / response JSON]]
- [[ELEVATED#6. Аудит|6. Аудит]]
- [[ELEVATED#7. Как добавить операцию|7. Как добавить операцию]]
- [[ELEVATED#8. Ограничения и инварианты|8. Ограничения и инварианты]]
- [[ELEVATED#9. Тесты|9. Тесты]]
- [[ELEVATED#10. Подключение к CLI (зона интеграции)|10. Подключение к CLI (зона интеграции)]]

## [[LIFECYCLE|Жизненный цикл памяти]]

> Этап 5 добавляет к памяти управляемый жизненный цикл: старые сообщения можно свернуть в компактные сводки, TTL-уборка удаляет просроченные сообщения вместе с их векторами, а таблица approvals из миграции 4 получает явный…

- [[LIFECYCLE#1. Политика хранения (retention)|1. Политика хранения (retention)]]
- [[LIFECYCLE#2. Суммаризация|2. Суммаризация]]
- [[LIFECYCLE#3. Approvals: семантика|3. Approvals: семантика]]
- [[LIFECYCLE#4. Ограничения|4. Ограничения]]
- [[LIFECYCLE#5. CLI (план интеграции)|5. CLI (план интеграции)]]

## [[MEMORY|Память и контекст]]

> Память Kivi — это SQLite плюс файлы артефактов. Первый срез даёт сообщения, факты, предпочтения, content-addressed артефакты, FTS5-поиск и Context Builder. Векторная база и эмбеддинги подключаются следующим срезом.

- [[MEMORY#1. Схема|1. Схема]]
- [[MEMORY#2. MemoryStore|2. MemoryStore]]
- [[MEMORY#3. Context Builder|3. Context Builder]]
- [[MEMORY#4. Гибридный поиск|4. Гибридный поиск]]
- [[MEMORY#5. CLI|5. CLI]]
- [[MEMORY#6. Retention, суммаризация и approvals|6. Retention, суммаризация и approvals]]
- [[MEMORY#7. Ограничения|7. Ограничения]]

## [[MODULES|Модули Kivi (Plugin API)]]

> Цель: подключать возможности (например, workcrm из D:\ai-agent) так, чтобы их можно было обновлять, тестировать и откатывать, не переписывая ядро.

- [[MODULES#1. Принципы|1. Принципы]]
- [[MODULES#2. Манифест module.yaml|2. Манифест module.yaml]]
- [[MODULES#3. Контракт модуля|3. Контракт модуля]]
- [[MODULES#4. Изоляция|4. Изоляция]]
- [[MODULES#5. Жизненный цикл|5. Жизненный цикл]]
- [[MODULES#6. Work CRM: миграция из D:\ai-agent|6. Work CRM: миграция из D:\ai-agent]]
- [[MODULES#7. Обновление модулей агентом|7. Обновление модулей агентом]]
- [[MODULES#8. Contract tests|8. Contract tests]]
- [[MODULES#9. Будущие модули|9. Будущие модули]]

## [[OPEN-QUESTIONS|Открытые вопросы (v0.3)]]

> Версия: после второй итерации ответов, 09.11.2026. Полные решения — в docs/DECISIONS.md. Безопасность — docs/SECURITY.md. Shell — docs/SHELL.md.

- [[OPEN-QUESTIONS#1. Что уже решено|1. Что уже решено]]
- [[OPEN-QUESTIONS#2. Голос|2. Голос]]
- [[OPEN-QUESTIONS#3. Одежда|3. Одежда]]
- [[OPEN-QUESTIONS#4. Kivi Shell|4. Kivi Shell]]
- [[OPEN-QUESTIONS#5. Железо|5. Железо]]
- [[OPEN-QUESTIONS#6. Почта и Telegram|6. Почта и Telegram]]
- [[OPEN-QUESTIONS#7. Облако|7. Облако]]
- [[OPEN-QUESTIONS#8. Работа (CRM)|8. Работа (CRM)]]
- [[OPEN-QUESTIONS#9. Аватар и концепт|9. Аватар и концепт]]
- [[OPEN-QUESTIONS#10. Резервные копии и автозапуск|10. Резервные копии и автозапуск]]
- [[OPEN-QUESTIONS#Шаблон ответа|Шаблон ответа]]

## [[OPERATOR-SYSTEM|Operator: системные инструменты (уведомления, громкость, питание)]]

> Версия: 0.1 (11.11.2026). Стадия 8 / M3: Operator. Связанные документы: docs/TOOLS.md, docs/SECURITY.md, docs/ELEVATED.md, docs/STATUS.md.

- [[OPERATOR-SYSTEM#1. Назначение и границы|1. Назначение и границы]]
- [[OPERATOR-SYSTEM#2. Контракт backend|2. Контракт backend]]
- [[OPERATOR-SYSTEM#3. Инструменты|3. Инструменты]]
- [[OPERATOR-SYSTEM#4. Права и approvals|4. Права и approvals]]
- [[OPERATOR-SYSTEM#5. Уведомления и fallback|5. Уведомления и fallback]]
- [[OPERATOR-SYSTEM#6. Громкость и mute|6. Громкость и mute]]
- [[OPERATOR-SYSTEM#7. Питание и блокировка|7. Питание и блокировка]]
- [[OPERATOR-SYSTEM#8. Аудит|8. Аудит]]
- [[OPERATOR-SYSTEM#9. Интеграция (registry/CLI не изменялись)|9. Интеграция (registry/CLI не изменялись)]]
- [[OPERATOR-SYSTEM#10. Реальные smoke-команды|10. Реальные smoke-команды]]
- [[OPERATOR-SYSTEM#11. Проверка|11. Проверка]]

## [[OPERATOR-UI|Operator UI automation (`ui.*`)]]

> Version: 0.1 (11.11.2026). Stage: M3. Related: docs/TOOLS.md, docs/SECURITY.md, docs/CODER-TOOLS.md.

- [[OPERATOR-UI#1. Architecture|1. Architecture]]
- [[OPERATOR-UI#2. Tools|2. Tools]]
- [[OPERATOR-UI#3. Safety model|3. Safety model]]
- [[OPERATOR-UI#4. Coordinate and secret policy|4. Coordinate and secret policy]]
- [[OPERATOR-UI#5. Failsafe|5. Failsafe]]
- [[OPERATOR-UI#6. Risks|6. Risks]]
- [[OPERATOR-UI#7. Limitations|7. Limitations]]
- [[OPERATOR-UI#8. Integration|8. Integration]]
- [[OPERATOR-UI#9. Tests|9. Tests]]

## [[OPERATOR-WINDOWS|Operator PC: окна и приложения (M3)]]

> Версия: 0.1 (11.11.2026). Стадия M3 «Operator PC control». Связанные документы: TOOLS.md, SECURITY.md, TRUST.md, ROADMAP.md, STATUS.md.

- [[OPERATOR-WINDOWS#1. Назначение|1. Назначение]]
- [[OPERATOR-WINDOWS#2. Слои|2. Слои]]
- [[OPERATOR-WINDOWS#3. Инструменты|3. Инструменты]]
- [[OPERATOR-WINDOWS#4. Как резолвится окно|4. Как резолвится окно]]
- [[OPERATOR-WINDOWS#5. Защищённые процессы|5. Защищённые процессы]]
- [[OPERATOR-WINDOWS#6. Политика app.launch|6. Политика app.launch]]
- [[OPERATOR-WINDOWS#7. app.terminate|7. app.terminate]]
- [[OPERATOR-WINDOWS#8. Аудит и trust|8. Аудит и trust]]
- [[OPERATOR-WINDOWS#9. Проверка|9. Проверка]]
- [[OPERATOR-WINDOWS#10. Интеграция|10. Интеграция]]
- [[OPERATOR-WINDOWS#11. Ограничения|11. Ограничения]]

## [[PERSONA|Kivi: персона, голос, аватар]]

> Правило по умолчанию:

- [[PERSONA#1. Характер|1. Характер]]
- [[PERSONA#2. Адаптивная подробность|2. Адаптивная подробность]]
- [[PERSONA#3. Голос|3. Голос]]
- [[PERSONA#4. Аватар|4. Аватар]]
- [[PERSONA#5. Присутствие и приватность|5. Присутствие и приватность]]
- [[PERSONA#6. Речевые состояния|6. Речевые состояния]]
- [[PERSONA#7. Ограничения|7. Ограничения]]

## [[PLANNER|Planner: DAG, executor, verifier, fast path]]

> Документ описывает M1-контур глубокого и быстрого пути:

- [[PLANNER#1. Модули|1. Модули]]
- [[PLANNER#2. JSON-схема плана|2. JSON-схема плана]]
- [[PLANNER#3. Валидация|3. Валидация]]
- [[PLANNER#4. Planner|4. Planner]]
- [[PLANNER#5. PlanExecutor|5. PlanExecutor]]
- [[PLANNER#6. Verifier|6. Verifier]]
- [[PLANNER#7. Fast path|7. Fast path]]
- [[PLANNER#8. KiviBrain|8. KiviBrain]]
- [[PLANNER#9. Интеграция с CLI (без правки cli.py)|9. Интеграция с CLI (без правки cli.py)]]
- [[PLANNER#10. Ограничения|10. Ограничения]]
- [[PLANNER#11. Проверка|11. Проверка]]

## [[REUSE-AUDIT|Аудит существующих наработок]]

> Дата: 08.11.2026. Проверены соседние проекты на диске D:.

- [[REUSE-AUDIT#Сводка|Сводка]]
- [[REUSE-AUDIT#D:\ai-agent — что взять|D:\ai-agent — что взять]]
- [[REUSE-AUDIT#D:\workspace — что взять|D:\workspace — что взять]]
- [[REUSE-AUDIT#D:\stable-diffusion-webui|D:\stable-diffusion-webui]]
- [[REUSE-AUDIT#D:\AI-base|D:\AI-base]]
- [[REUSE-AUDIT#D:\ollamamodels и установленные модели|D:\ollamamodels и установленные модели]]
- [[REUSE-AUDIT#Вывод|Вывод]]
- [[REUSE-AUDIT#Решение по ai-agent|Решение по ai-agent]]
- [[REUSE-AUDIT#Обновляемость модулей|Обновляемость модулей]]

## [[ROADMAP|Дорожная карта Kivi]]

> Версия: 0.2 (после решений от 08.11.2026). Оценки — для одного разработчика с ИИ-помощником, 10–15 часов в неделю. Первый полезный текстовый Kivi — 6–8 недель; полный v1 — 6–9 месяцев.

- [[ROADMAP#1. Принципы|1. Принципы]]
- [[ROADMAP#2. Обзор|2. Обзор]]
- [[ROADMAP#2.1. Статус на 11.11.2026|2.1. Статус на 11.11.2026]]
- [[ROADMAP#3. Этап 0 — решения и baseline (2–3 дня)|3. Этап 0 — решения и baseline (2–3 дня)]]
- [[ROADMAP#4. Этап 1 — скелет ядра (5–7 дней)|4. Этап 1 — скелет ядра (5–7 дней)]]
- [[ROADMAP#5. Этап 2 — Model Gateway (7–10 дней)|5. Этап 2 — Model Gateway (7–10 дней)]]
- [[ROADMAP#6. Этап 3 — Cloud Broker (4–6 дней)|6. Этап 3 — Cloud Broker (4–6 дней)]]
- [[ROADMAP#7. Этап 4 — инструменты, A4-Bounded, аудит (7–10 дней)|7. Этап 4 — инструменты, A4-Bounded, аудит (7–10 дней)]]
- [[ROADMAP#8. Этап 5 — память и контекст (5–7 дней)|8. Этап 5 — память и контекст (5–7 дней)]]
- [[ROADMAP#9. Этап 6 — M1: Core Kivi (7–10 дней)|9. Этап 6 — M1: Core Kivi (7–10 дней)]]
- [[ROADMAP#10. Этап 7 — M2: Coder (8–12 дней)|10. Этап 7 — M2: Coder (8–12 дней)]]
- [[ROADMAP#11. Этап 8 — M3: Operator (8–12 дней)|11. Этап 8 — M3: Operator (8–12 дней)]]
- [[ROADMAP#12. Этап 9 — M4: Communicator (6–9 дней)|12. Этап 9 — M4: Communicator (6–9 дней)]]
- [[ROADMAP#13. Этап 10 — M5: Voice & Avatar (10–14 дней)|13. Этап 10 — M5: Voice & Avatar (10–14 дней)]]
- [[ROADMAP#14. Этап 11 — M6: Work CRM (10–14 дней)|14. Этап 11 — M6: Work CRM (10–14 дней)]]
- [[ROADMAP#15. Этап 12 — M7: Cloud Media (6–9 дней)|15. Этап 12 — M7: Cloud Media (6–9 дней)]]
- [[ROADMAP#16. Этап 13 — M8: Kivi Shell + v1.0 (25–40 дней)|16. Этап 13 — M8: Kivi Shell + v1.0 (25–40 дней)]]
- [[ROADMAP#17. Развитие после v1.0|17. Развитие после v1.0]]
- [[ROADMAP#18. Первые 14 дней|18. Первые 14 дней]]
- [[ROADMAP#19. Backlog (MoSCoW)|19. Backlog (MoSCoW)]]
- [[ROADMAP#20. Контрольные точки|20. Контрольные точки]]

## [[SANDBOX|Песочница `code.run`]]

> code.run запускает сгенерированный Python в best-effort песочнице. Это первый шаг M2 Coder: код изолирован на уровне Python-аудита и ресурсных лимитов Windows, но это не граница безопасности ОС. Полная изоляция — WSL2 ил…

- [[SANDBOX#1. Что именно запускается|1. Что именно запускается]]
- [[SANDBOX#2. Модель угроз|2. Модель угроз]]
- [[SANDBOX#3. Что блокирует audit hook|3. Что блокирует audit hook]]
- [[SANDBOX#4. Job Object|4. Job Object]]
- [[SANDBOX#5. Что песочница НЕ защищает|5. Что песочница НЕ защищает]]
- [[SANDBOX#6. Почему это best-effort|6. Почему это best-effort]]
- [[SANDBOX#7. Как перейти на WSL2 / Windows Sandbox|7. Как перейти на WSL2 / Windows Sandbox]]
- [[SANDBOX#8. Наблюдаемость и интеграция|8. Наблюдаемость и интеграция]]
- [[SANDBOX#9. Backlog песочницы|9. Backlog песочницы]]

## [[SECURITY|Безопасность Kivi: модель угроз и защита от prompt injection]]

> Версия: 0.1 (09.11.2026). Связанные документы: docs/DECISIONS.md, docs/ARCHITECTURE.md, docs/MODULES.md, docs/SHELL.md.

- [[SECURITY#1. Главный принцип|1. Главный принцип]]
- [[SECURITY#2. Почему одного промпта недостаточно|2. Почему одного промпта недостаточно]]
- [[SECURITY#3. Активы, которые защищаем|3. Активы, которые защищаем]]
- [[SECURITY#4. Модель угроз|4. Модель угроз]]
- [[SECURITY#5. Архитектура защиты|5. Архитектура защиты]]
- [[SECURITY#6. Детектирование|6. Детектирование]]
- [[SECURITY#7. Аудит и incident response|7. Аудит и incident response]]
- [[SECURITY#8. Тесты и критерии приёмки|8. Тесты и критерии приёмки]]
- [[SECURITY#9. Остаточные риски|9. Остаточные риски]]
- [[SECURITY#10. План внедрения|10. План внедрения]]
- [[SECURITY#11. Что нельзя делать|11. Что нельзя делать]]

## [[SHELL|Kivi Shell: полноэкранная замена Explorer]]

> Версия: 0.1 (09.11.2026). Связанные документы: docs/ARCHITECTURE.md, docs/SECURITY.md, docs/ROADMAP.md.

- [[SHELL#1. Цель|1. Цель]]
- [[SHELL#2. Честная оценка|2. Честная оценка]]
- [[SHELL#3. Матрица паритета|3. Матрица паритета]]
- [[SHELL#4. Архитектура|4. Архитектура]]
- [[SHELL#5. Файловый менеджер|5. Файловый менеджер]]
- [[SHELL#6. Рабочий стол и панель задач|6. Рабочий стол и панель задач]]
- [[SHELL#7. Аватар в Shell|7. Аватар в Shell]]
- [[SHELL#8. Производительность|8. Производительность]]
- [[SHELL#9. Безопасность|9. Безопасность]]
- [[SHELL#10. Fail-safe и восстановление|10. Fail-safe и восстановление]]
- [[SHELL#11. Тестирование|11. Тестирование]]
- [[SHELL#12. Этапы|12. Этапы]]
- [[SHELL#13. Риски|13. Риски]]
- [[SHELL#14. Решения, которые нужны от пользователя|14. Решения, которые нужны от пользователя]]
- [[SHELL#15. Итог|15. Итог]]

## [[STATUS|Статус проекта Kivi]]

> Дата среза: 11.11.2026. Дорожная карта: ROADMAP. Тесты: 392, ruff чистый. Репозиторий собирается, kivi doctor работает.

- [[STATUS#1. Краткий ответ|1. Краткий ответ]]
- [[STATUS#2. Этапы|2. Этапы]]
- [[STATUS#3. Что уже сделано|3. Что уже сделано]]
- [[STATUS#4. Где мы находимся|4. Где мы находимся]]
- [[STATUS#5. Что предстоит|5. Что предстоит]]
- [[STATUS#6. Следующий шаг|6. Следующий шаг]]
- [[STATUS#7. Риски|7. Риски]]
- [[STATUS#8. Проверка|8. Проверка]]

## [[TOOLS|Tools и Permission Engine]]

> Инструмент — единственный способ, которым Kivi меняет что-то вне своего процесса. Контракт, права и аудит появились раньше первого инструмента: это инвариант проекта, а не надстройка.

- [[TOOLS#1. Контракт|1. Контракт]]
- [[TOOLS#2. Риски и режимы|2. Риски и режимы]]
- [[TOOLS#3. Permission Engine|3. Permission Engine]]
- [[TOOLS#4. Protected Zones|4. Protected Zones]]
- [[TOOLS#5. Инструменты|5. Инструменты]]
- [[TOOLS#6. CLI|6. CLI]]
- [[TOOLS#7. Аудит|7. Аудит]]
- [[TOOLS#8. Kill switch|8. Kill switch]]
- [[TOOLS#9. Подписанный манифест зон|9. Подписанный манифест зон]]
- [[TOOLS#10. Осталось|10. Осталось]]

## [[TRUST|Trust labels и санитайзер]]

> Всё, что приходит извне, попадает в контекст только с меткой происхождения. Метка ставится до модели и сохраняется в аудите. Правило простое: файл, сайт, письмо, вывод инструмента и картинка могут сообщать факты, но не м…

- [[TRUST#1. Уровни доверия|1. Уровни доверия]]
- [[TRUST#2. Envelope|2. Envelope]]
- [[TRUST#3. Санитайзер|3. Санитайзер]]
- [[TRUST#4. Интеграция с инструментами|4. Интеграция с инструментами]]
- [[TRUST#5. Injection-корпус|5. Injection-корпус]]
- [[TRUST#6. Ограничения|6. Ограничения]]

## [[VECTOR|Векторный индекс LanceDB]]

> SQLite остаётся источником истины: эмбеддинги пишутся в таблицу embeddings. LanceDB — опциональный ANN-кэш для больших баз. Без него Kivi работает на SQLite + cosine-переборе.

- [[VECTOR#Установка|Установка]]
- [[VECTOR#Синхронизация|Синхронизация]]
- [[VECTOR#API|API]]
- [[VECTOR#Ограничения|Ограничения]]

## [[VOICE-AVATAR|Kivi: голос и 2D-аватар]]

> Этап 6 (M1). Документ описывает два новых модуля:

- [[VOICE-AVATAR#1. TTS|1. TTS]]
- [[VOICE-AVATAR#2. 2D-аватар|2. 2D-аватар]]
- [[VOICE-AVATAR#3. Интеграция с CLI (без правок cli.py)|3. Интеграция с CLI (без правок cli.py)]]
- [[VOICE-AVATAR#4. Проверка|4. Проверка]]

## [[VOICE-CLONING|Голос Kivi: улучшение Silero и клонирование голоса]]

> Статус: черновик плана, 09.11.2026. Связанные документы: docs/PERSONA.md, docs/DECISIONS.md, docs/SECURITY.md.

- [[VOICE-CLONING#1. Текущее решение|1. Текущее решение]]
- [[VOICE-CLONING#2. Улучшение Silero baya|2. Улучшение Silero baya]]
- [[VOICE-CLONING#3. Клонирование голоса: границы|3. Клонирование голоса: границы]]
- [[VOICE-CLONING#4. Требования к датасету|4. Требования к датасету]]
- [[VOICE-CLONING#5. Пайплайн клонирования|5. Пайплайн клонирования]]
- [[VOICE-CLONING#6. Модели и лицензии|6. Модели и лицензии]]
- [[VOICE-CLONING#7. Приватность и безопасность|7. Приватность и безопасность]]
- [[VOICE-CLONING#8. Что нужно от пользователя|8. Что нужно от пользователя]]
