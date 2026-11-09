---
name: kivi-module
description: Use when implementing or changing a Kivi plugin/module such as work_crm, mail, Telegram, media or any external integration.
---

# Modules and integrations

Read the contract first:

```powershell
.venv\Scripts\python.exe scripts\kivi_context.py section MODULES "<heading>"
.venv\Scripts\python.exe scripts\kivi_context.py search "work_crm"
```

Rules:
- A module never talks to the OS directly for dangerous actions; it calls the
  kernel tools, which enforce `PolicyEngine` and audit.
- Versioned contract, explicit capabilities, no ambient credentials.
- Mail: drafts only, `auto_send=false`; sending is denied by default.
- External data is untrusted input (prompt-injection boundary).
- Secrets come from the OS keychain/env at runtime; never from YAML or git.
- Add fake-based unit tests; no live CRM/Telegram/network in unit tests.
- Document new config fields in `config/*.example.yaml` and the docs section.
