# Модули Kivi (Plugin API)

Цель: подключать возможности (например, `work_crm` из `D:\ai-agent`) так, чтобы их можно было
**обновлять, тестировать и откатывать**, не переписывая ядро.

---

## 1. Принципы

1. Ядро не импортирует код модулей напрямую — только через SDK и контракт.
2. Модуль не получает больше прав, чем указано в его манифесте.
3. У модуля своя версия, миграции, конфиг, пространство памяти и тесты.
4. Обновление модуля — отдельная транзакция: проверка → миграция → старт → healthcheck → откат при ошибке.
5. Модуль не может менять safety-ядро, аудит и список запретов.
6. UI модуля — панель в Kivi Shell, а не отдельное приложение (но может запускаться отдельно).

---

## 2. Манифест `module.yaml`

```yaml
id: work_crm
name: "Работа: AltCRM"
version: 1.0.0
api_version: 1
entrypoint: work_crm.module:WorkCrmModule
description: "Заказы, звонки, карточка, акт, задачи"

requires:
  kivi: ">=0.4,<1.0"
  python: ">=3.12"

permissions:
  - capability: net.browser
    domains: ["https://gs-leads.ru/*"]
    mode: allow
  - capability: fs.read
    paths: ["D:\\AI-assistent\\workspace\\crm\\**"]
    mode: allow
  - capability: fs.write
    paths: ["D:\\AI-assistent\\workspace\\crm\\**"]
    mode: allow
  - capability: audio.capture
    mode: confirm
  - capability: ui.control
    mode: confirm

tools:
  - work.read_order
  - work.fill_card
  - work.draft_act
  - work.create_task
  - work.listen_calls

ui:
  panel: work_crm.ui:WorkPanel
  icon: assets/icon.svg

voice:
  commands: work_crm.voice:commands

memory:
  namespace: work_crm

config:
  schema: work_crm.config:Schema
  defaults: config.yaml

migrations:
  - work_crm.migrations:0001_init
```

---

## 3. Контракт модуля

```python
class Module(Protocol):
    id: str
    version: str

    async def start(self, ctx: ModuleContext) -> None: ...
    async def stop(self) -> None: ...
    async def healthcheck(self) -> Health: ...
    def tools(self) -> list[Tool]: ...
    def ui_panels(self) -> list[PanelSpec]: ...
    def voice_commands(self) -> list[VoiceCommand]: ...
```

`ModuleContext` даёт только SDK:

- `ctx.memory` — пространство имён модуля;
- `ctx.models` — вызов модели через Gateway;
- `ctx.tools` — вызов разрешённых инструментов;
- `ctx.events` — публикация/подписка;
- `ctx.logger`, `ctx.config`, `ctx.artifacts`;
- `ctx.permissions` — проверка, но не выдача прав.

Модуль **не** получает прямой доступ к SQLite ядра, секретам и внутренним классам.

---

## 4. Изоляция

- Отдельный Python-пакет `modules/<id>/`.
- Свои зависимости (можно venv/uv environment на модуль, если конфликт).
- Опционально — отдельный процесс с JSON-RPC, чтобы падение модуля не роняло ядро.
- Ограничения: таймауты, лимит памяти, лимит логов, запрет на сеть вне манифеста.
- Секреты модуля — в его namespace vault, не в общем промпте.

---

## 5. Жизненный цикл

```
discover → validate(manifest, api_version, permissions)
        → backup → migrate → start → healthcheck
        → ready
        → stop → (при ошибке) rollback
```

Правила:

- Миграции только вперёд; откат — из бэкапа.
- Перед обновлением — git-тег/архив текущей версии.
- Несовместимый `api_version` → модуль не загружается, ядро работает дальше.
- Провал healthcheck → автоматический откат и уведомление.

---

## 6. Work CRM: миграция из `D:\ai-agent`

### 6.1. Что отделить от UI

Сейчас `modes/work/mode.py` — это ~35 КБ, где смешаны tkinter-панель и логика.
План:

| Слой | Что переносим | Куда |
|---|---|---|
| Домен | `schema.py`, `extractor.py`, `drafts.py`, `coach.py`, `timeparse.py` | `work_crm/domain` |
| История | `history.py`, парсинг звонков | `work_crm/history` |
| CRM | `crm_ext.py`, `crm_altcrm.py`, `selectors.toml` | `work_crm/adapters` |
| Инструменты | чтение заказа, заполнение, акт, задача, звонки | `work_crm/tools` |
| UI | `WorkPanel`, вкладки, карточки | `work_crm/ui` (React) |
| Голос | `work/voice.py` | `work_crm/voice` |
| Конфиг | `[work]`, `[crm]` | `work_crm/config.yaml` |

### 6.2. Правила переноса

- Сначала — **headless-домен + тесты**, без tkinter.
- Затем — tools с типизированными аргументами и правами.
- Затем — UI-панель в Kivi.
- Старый `ai-agent` остаётся рабочим до полного паритета.
- Никаких `import tkinter` в домене и tools.

### 6.3. Автоматизация заказов

Политика по действиям:

| Действие | По умолчанию | A4 для work |
|---|---|---|
| Прочитать заказ, звонки | авто | авто |
| Сформировать факты/черновик | авто | авто |
| Заполнить карточку/акт/задачу | подготовить | авто в разрешённых полях |
| Сохранить/отправить в CRM | подтверждение | по политике |
| Позвонить/написать клиенту | подтверждение | по политике |
| Деньги/возврат/скидка сверх лимита | запрет | запрет/двойное подтверждение |

Цель: убрать рутину, сохранив контроль над необратимыми действиями.

### 6.4. Метрики

- время на заказ до/после;
- доля заказов без вмешательства;
- ошибки заполнения;
- сколько раз понадобилось подтверждение;
- стоимость (локально — 0).

---

## 7. Обновление модулей агентом

Kivi может улучшать собственные модули, но по процедуре:

1. Создать git-ветку `module/work_crm/<task>`.
2. Внести изменения.
3. Запустить unit + contract + e2e тесты.
4. Показать дифф и результаты.
5. Получить подтверждение (если политика требует).
6. Слить и перезапустить модуль; при ошибке — откат.

Даже при A4 **нельзя** автоматически менять safety-ядро и аудит. Код модулей — можно, с тестами и откатом.

---

## 8. Contract tests

Для каждого модуля обязательны:

- манифест валиден;
- tools соответствуют схемам;
- права не шире манифеста;
- миграции идемпотентны;
- healthcheck;
- запуск/остановка не оставляет процессов;
- UI-панель не блокирует ядро;
- обновление и откат.

---

## 9. Будущие модули

- `work_crm` — работа.
- `media` — картинки/видео.
- `browser` — сценарии браузера.
- `mail` — почта.
- `telegram` — бот/канал.
- `smart_home` — умный дом.
- `games` — осторожно, античит.
- `knowledge` — импорт Obsidian/заметок.

Каждый модуль — отдельный пакет, версия и набор прав.
