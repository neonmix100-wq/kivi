---
name: kivi-core
description: Use at the start of any Kivi task for the project map, current roadmap stage, build commands and hard invariants.
---

# Kivi core

1. Run `.venv\Scripts\python.exe scripts\kivi_context.py stage` to see the first
   open stage. Do not assume the stage from memory.
2. Read `AGENTS.md` (already loaded) and only the files the task touches.
3. For architecture questions use
   `scripts\kivi_context.py section ARCHITECTURE "<heading>"` — never read the
   whole 60 KB file.
4. Check `git status --short` before editing; preserve user changes.

Key layout: `src/kivi/{config,kernel,safety,models,brain,senses,tools,memory,modules,interfaces}`.
Tests are network-free; fake Ollama/GPU where needed.

Hard rules: local-first, one heavy model through `ModelGateway`, never weaken
safety/approvals, audit significant actions, no secrets, mail is drafts-only.
