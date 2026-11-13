# AltCRM через расширение браузера

Kivi использует **существующее расширение ИИ-агента** из `D:\ai-agent\extension`,
а не отдельный Chrome с `--remote-debugging-port`.

## Как это работает

```text
Яндекс.Браузер / Chrome
  расширение AltCRM (content.js)
        │  WebSocket 127.0.0.1:8799
        ▼
Kivi extension_bridge (loopback, token)
        │  read_order / fill_order / add_work_rows / create_task
        ▼
Kivi CRM tools + UI
```

- мост слушает только `127.0.0.1`;
- подключаться может только расширение (проверка Origin);
- обязателен общий токен из `D:/ai-agent/data/bridge_token.txt`;
- расширение выполняет фиксированный набор действий и **никогда не нажимает
  «Сохранить»**;
- Kivi показывает прочитанную карточку как `untrusted:crm`.

## Настройка

В `config/kivi.yaml`:

```yaml
crm:
  backend: extension
  bridge_port: 8799
  bridge_token_file: D:/ai-agent/data/bridge_token.txt
  selectors: config/crm-selectors.toml
```

Токен не коммитится и не печатается.

Если файла токена ещё нет:

```powershell
kivi crm token
```

Команда создаст файл и покажет его путь; значение скопируйте в `config.js`
расширения и перезагрузите расширение.

## Проверка

1. Расширение установлено и включено в Яндекс.Браузере; на значке **ON**.
2. Открыта карточка заказа AltCRM (`https://gs-leads.ru/...`).
3. В PowerShell:

```powershell
cd D:\AI-assistent
.venv\Scripts\kivi.exe crm read
```

Kivi поднимет мост на 8799, дождётся расширения и прочитает карточку.

Каждое успешное чтение пишется в `data/crm-reads.jsonl` (только URL, код
заказа и device_id, без текста страницы). Сводка:

```powershell
kivi crm stats
```

В UI:

1. `kivi ui --app --tray`
2. раздел **CRM**
3. **Проверить**
4. **Прочитать заказ**

## Команды

```powershell
kivi crm read
kivi crm fill --comment "..." --field "deadline=..."
kivi crm act --row "Работа|1500|30" --approve
kivi crm task "Позвонить клиенту" --when "завтра"
```

`fill`, `act`, `task` заполняют/готовят карточку, но не сохраняют её.

## Если не подключается

- на значке расширения не ON — перезагрузи расширение;
- **порт 8799 занят** (например, старым ИИ-агентом): Kivi напишет PID.
  Закрой старое окно/процесс либо смени порт:
  `crm.bridge_port: 8800` в `config/kivi.yaml`, затем `port: 8800` в
  `D:\ai-agent\extension\config.js`, перезагрузи расширение в
  `browser://extensions`;
- токен в `config.js` расширения должен совпадать с
  `D:/ai-agent/data/bridge_token.txt`;
- вкладка AltCRM должна быть открыта и видима;
- после перезапуска Kivi расширение переподключается автоматически
  (повтор каждые 3 с).

## Ограничения

- мост живёт, пока открыт процесс Kivi UI, и освобождает порт при выходе;
  терминальный `kivi crm read` в это время не сможет занять 8799 — закройте
  UI или смените порт;
- нет потокового чтения звонков в UI;
- сохранение/отправка остаются за человеком.
