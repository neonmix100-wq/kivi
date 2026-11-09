---
name: kivi-docs
description: Use when writing or updating Kivi documentation, ADRs, roadmap, architecture notes or the knowledge index.
---

# Documentation

Map: `docs/_INDEX.md` (generated). Code map: `docs/_CODE-MAP.md` (generated).

Where to write:
- durable decision → `docs/DECISIONS.md` (ADR with date, context, decision,
  consequences)
- current state / plan → `docs/ROADMAP.md`
- subsystem design → the matching `docs/*.md`, not a new file by default
- open question → `docs/OPEN-QUESTIONS.md`

Rules:
- Prefer editing an existing doc over adding a new one; keep one source of truth.
- Keep Russian prose and English technical terms as in the surrounding doc.
- Do not duplicate large sections between docs; link to the section instead.
- After any docs or module-docstring change run:

```powershell
.venv\Scripts\python.exe scripts\build_knowledge_index.py
.venv\Scripts\python.exe scripts\build_knowledge_index.py --check
```

The index is committed; stale generated notes are a verification failure.
