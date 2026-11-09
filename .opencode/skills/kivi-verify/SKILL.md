---
name: kivi-verify
description: Use when changing Kivi code or before a commit to run focused pytest and ruff with compact output.
---

# Verify Kivi

Preferred command:

```powershell
.venv\Scripts\python.exe scripts\kivi_context.py verify --path tests\unit\test_gateway.py
```

It runs `ruff check src tests` and focused pytest, writes the full log to
`data/logs/verify-*.log`, and prints only the failure tail.

Rules:
- During iteration run only the affected test file.
- Before a commit run the full `python -m pytest -q` and `ruff check src tests`.
- Treat warnings as signals, not noise; fix new warnings.
- If a test needs Ollama, GPU or network, replace it with a fake — unit tests
  must stay deterministic.
- Never use `pytest -x` as the only gate: it hides later failures.

Report: command, PASS/FAIL, failing tests, first actionable error.
