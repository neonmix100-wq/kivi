---
name: kivi-obsidian
description: Use to navigate the Kivi knowledge base in Obsidian, rebuild the vault index, or add/query notes without rereading all docs.
---

# Obsidian knowledge base

The vault is the repository's `docs/` folder. No copies, no sync step.

Open: Obsidian → **Open folder as vault** → `D:\AI-assistent\docs` → open
`_INDEX.md`.

Structure:
- `_INDEX.md` — map of content: every doc, summary, H2 section links.
- `_CODE-MAP.md` — generated package/module map from docstrings.
- `docs/.obsidian/app.json` — shared vault settings (tracked in git).
- `docs/.obsidian/workspace*.json` — local UI state (ignored by git).

Human workflow: graph view for links, tags `#kivi`, quick switcher (`Ctrl+O`),
backlinks pane, search. Add `[[ARCHITECTURE#Model Gateway]]` links instead of
copying text.

Agent workflow (cheaper than reading docs):

```powershell
.venv\Scripts\python.exe scripts\kivi_context.py index
.venv\Scripts\python.exe scripts\kivi_context.py search "VRAM budget"
.venv\Scripts\python.exe scripts\kivi_context.py section ARCHITECTURE "Model Gateway"
.venv\Scripts\python.exe scripts\kivi_context.py map
```

After editing docs or module docstrings:

```powershell
.venv\Scripts\python.exe scripts\build_knowledge_index.py
```

Do not edit `_INDEX.md` or `_CODE-MAP.md` by hand. Do not move docs out of
`docs/`; external notes can link with `file:///D:/AI-assistent/docs/...`.
