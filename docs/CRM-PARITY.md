# CRM parity and rollback

## Adapter parity

Both CRM backends implement the same five methods:

| Operation | CDP `AltCrmClient` | Extension `ExtensionCrmClient` |
|---|---|---|
| read the visible order | `read_order()` | `read_order()` |
| fill comment/checklist | `fill_order()` | `fill_order()` |
| add act rows | `add_work_rows()` | `add_work_rows()` |
| fill task dialog | `create_task()` | `create_task()` |
| close adapter | `close()` | `close()` |

No adapter has a save, submit, send or delete method. The extension bridge only
accepts `ping`, `read_order`, `fill_order`, `add_work_rows`, `create_task` and
`inspect`.

Automated checks:

```powershell
.venv\Scripts\python.exe -m pytest -q tests\unit\test_extension_crm.py tests\unit\test_crm_tools.py
```

The tests assert the shared method set and that fill/act/task actions never
carry a save/submit/send/delete action.

## Rollback

Kivi deliberately never presses «Сохранить», «Создать задачу» or a client
send button. A rollback of an unsaved fill therefore means:

1. Do **not** save the card.
2. Reload the AltCRM tab (`Ctrl+R`) to discard unsaved DOM changes.
3. Re-read with `kivi crm read` or **Прочитать заказ** in the UI.

This was checked live on the test card: fill 5/5, act 1/1, task 3/3; no save
was clicked. The real 10-order counter is closed for this release.
