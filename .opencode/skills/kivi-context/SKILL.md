---
name: kivi-context
description: Use when designing Kivi's context builder, memory retrieval, summarization or token budgets for local models.
---

# Context budget

Kivi's own context discipline is separate from OpenCode's. Design constraints:

- Explicit `token_budget` per request in config; no implicit "fit the window".
- Budget split: system/persona, tools, working memory, retrieved memory,
  recent turns, reserve for output. Never let retrieval starve the answer.
- Prefer retrieval over dumping: semantic search + episodic summaries, then
  assemble. Keep citations (file/URL + line/section) for every injected chunk.
- Tool output is truncated at ingestion, not at prompt time; keep a full copy
  on disk and a stable marker in context.
- Summarize old turns incrementally; never rewrite history silently.
- Tests: budget overflow, truncation markers, summarizer failure, empty
  retrieval, and a 4K/8K window profile.
- Local models: measure tokens with the actual tokenizer, not `len(text)/4`.

Related docs: `ARCHITECTURE` (Memory, Context Builder), `ROADMAP` stage 5.
