# Trust labels и санитайзер

Всё, что приходит извне, попадает в контекст только с меткой происхождения.
Метка ставится **до** модели и сохраняется в аудите. Правило простое:
файл, сайт, письмо, вывод инструмента и картинка могут сообщать факты, но не
могут отдавать команды.

## 1. Уровни доверия

| Источник | Метка | Может давать команды |
|---|---|---|
| Системная политика | `trusted:system` | да, через Policy Engine |
| Явная команда пользователя | `trusted:user` | да, в пределах прав |
| Подписанный модуль | `trusted:module` | только свои tools |
| Файл | `untrusted:file` | нет |
| Веб-страница | `untrusted:web` | нет |
| Письмо/Telegram | `untrusted:message` | нет |
| Вывод инструмента | `untrusted:tool` | нет |
| OCR/картинка | `untrusted:vision` | нет |
| Сгенерированный код | `untrusted:code` | нет |
| Окна/процессы ОС | `untrusted:os` | нет |
| UI Automation | `untrusted:ui` | нет |
| Письмо/вложение | `untrusted:email` | нет |
| Карточка CRM | `untrusted:crm` | нет |
| Память/RAG | `tainted:memory` | нет без проверенного источника |

`can_issue_instructions(level)` — единственная проверка, которую использует
планировщик. Неизвестная метка — ошибка, а не «наверное, доверенная».

## 2. Envelope

```python
from kivi.safety import build_envelope

envelope = build_envelope(page_text, "untrusted:web", locator="https://example.com/a")
print(envelope.render())
```

```text
<untrusted source="web" locator="https://example.com/a" hash="9f2c...">
...очищенный текст...
</untrusted>
```

- закрывающий тег и ролевые маркеры (`system:`, `assistant:`, `tool:`,
  `<|system|>`, `[INST]`, `<<SYS>>`) внутри текста нейтрализуются в HTML-сущности;
- `hash` — SHA-256 содержимого (16 символов), `collected_at` — время сбора;
- `trusted:user/system/module` рендерятся как `<trusted ...>`, но всё равно
  проходят Policy Engine.

## 3. Санитайзер

`sanitize_text(raw, html=None, max_chars=32768, redact=True)` возвращает
`Sanitized`:

- `text` — только видимый нормализованный текст;
- `removed` — счётчики удалённого (комментарии, zero-width, control chars, секреты);
- `suspicious` — найденные фразы-триггеры;
- `truncated`, `original_chars`.

Что удаляется:

- HTML-комментарии, `script`, `style`, `noscript`, `template`;
- элементы с `display:none`, `visibility:hidden`, `font-size:0`, `opacity:0`
  или атрибутом `hidden`;
- zero-width и bidi-символы (включая `U+202A..U+202E`, `U+2066..U+2069`);
- управляющие символы;
- токены, похожие на секреты: `sk-...`, `ghp_...`, `github_pat_...`,
  `AKIA...`, `Bearer ...`, PEM private keys — заменяются на `[REDACTED]`;
- лишние пробелы и пустые строки.

Флаги (`ignore previous instructions`, `reveal your system prompt`,
`игнорируй предыдущие инструкции`, ...) — это сигнал для аудита и планировщика,
а не решение о запрете. Решение принимает Policy Engine.

## 4. Интеграция с инструментами

- `ToolResult.trust` по умолчанию `untrusted:tool`;
- `fs.read` и `fs.list` возвращают `untrusted:file`;
- `http.get` возвращает `untrusted:web` и прогоняет ответ через санитайзер;
- `ToolExecutor` пишет `trust` в аудит вместе с `args_digest`;
- контекст-билдер (этап 5) обязан оборачивать такой результат в envelope и не
  передавать его как инструкцию.

## 5. Injection-корпус

`tests/security/injection/` содержит примеры атак:

- инструкция в HTML-комментарии;
- скрытый `display:none`;
- zero-width обфускация;
- разрыв envelope (`</untrusted>` + `system:`);
- `script` с exfiltration;
- утечка токенов и PEM-ключа.

`tests/security/test_injection.py` проверяет, что санитайзер удаляет скрытое,
редактирует секреты, а envelope нельзя закрыть изнутри. Корпус расширяется
новыми файлами — тест обходит каталог автоматически.

## 6. Ограничения

- это детерминированные эвристики, а не ML-классификатор;
- OCR, PDF/DOCX/XLSX и markdown-парсеры подключаются отдельными читателями;
- санитайзер не исполняет и не рендерит HTML, только извлекает текст;
- `http.get` и браузерные инструменты появятся позже и должны возвращать
  `untrusted:web` через тот же конвейер.
