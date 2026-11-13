# ECC в Kivi

ECC — MIT-инструмент **affaan-m/ECC** для инженерного цикла:

```text
plan -> test -> implement -> review -> verify -> remember -> improve
```

## Что установлено

- Локальный checkout: `D:\ECC`, версия **2.2.3**.
- Нативный Claude Code плагин `ecc@ecc` установлен, но **отключён**: полный
  набор добавляет ~34k токенов в каждую сессию. Для Kivi это неоправданно.
- Для DeepSeek Harness выбраны только нужные skills:
  `D:\AI-assistent\.agents\skills`.
- DSH подхватывает их автоматически из project root; полный каталог 314 skills
  не копировался.

## Выбранные skills

| Skill | Когда применять |
|---|---|
| `plan-canvas` | план и просмотр артефактов |
| `tdd-workflow` | новая функциональность, тест сначала |
| `python-testing` | pytest, фикстуры, моки |
| `coding-standards` | код-ревью и единый стиль |
| `security-review` | API, секреты, пользовательский ввод |
| `verification-loop` | проверка перед коммитом/релизом |
| `context-and-decisions` | ADR и границы домена |
| `docs-governance` | документация и ссылки |
| `token-budget-advisor` | экономия контекста/ресурсов |
| `ecc-conventions` | конвенции ECC |

## Рабочий цикл для Kivi

1. **Plan** — `plan-canvas` / `docs/ROADMAP.md`.
2. **Test first** — `tdd-workflow`, `python-testing`; pytest.
3. **Implement** — минимальный diff, `coding-standards`.
4. **Review** — `security-review` для API/CRM/файлов.
5. **Verify** — `verification-loop`: ruff, pytest, acceptance, git diff.
6. **Docs** — `docs-governance`, обновить `STATUS/ACCEPTANCE`.
7. **Commit** — conventional commits; публикация скриптом.

Пример проверки:

```powershell
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe scripts\acceptance_smoke.py
git diff --stat
```

## Обновление

```powershell
cd D:\ECC
git pull
```

Скопировать обновлённые выбранные skills в `.agents\skills` (или заменить
symlink/junction). Полный плагин не включать без необходимости: он дорогой
по контексту.

## Hooks (опционально)

ECC умеет мостить hooks в DSH через
`@deepseek-ai/dsh-hooks-claude-code`. Инструкция:
`D:\ECC\docs\DEEPSEEK-HARNESS-GUIDE.md`.

Для Kivi hooks **не включены**: они добавляют принудительные проверки и риск
блокировки инструментов. Включать после отдельного теста на копии профиля.

## Проверка установки

```powershell
claude plugin list          # ecc@ecc disabled (экономия контекста)
Get-ChildItem .agents\skills
```

В DSH skills доступны через штатный `skill` tool; текущая сессия видит
`tdd-workflow`, `verification-loop`, `security-review`, `coding-standards`,
`python-testing`, `ecc-conventions` и остальные выбранные.

## Лицензия

ECC — MIT, Copyright (c) 2026 Affaan Mustafa.
Текст лицензии: `.agents\skills\ECC-LICENSE`.
