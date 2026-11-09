---
name: kivi-gateway
description: Use when working on model loading, VRAM budgets, Ollama, eviction, leases, fallback or kivi ask.
---

# Model Gateway

Read first:

```powershell
.venv\Scripts\python.exe scripts\kivi_context.py section ARCHITECTURE "Model Gateway"
.venv\Scripts\python.exe scripts\kivi_context.py section DECISIONS "Gateway"
```

Files: `src/kivi/models/{registry,gateway,vram}.py`,
`src/kivi/models/backends/ollama.py`, `config/models.yaml`.

Invariants:
- Only `ModelGateway` loads/unloads models; tools call `gateway.chat`.
- Budget = observed free VRAM (nvidia-smi), not the 8 GB passport.
- Tier 0 (router) may coexist; tier 1 heavy is exclusive and evicts tier 0/2.
- Pinned models (ASR/TTS) are never evicted automatically.
- If shared GPU memory is active, refuse heavy loads instead of OOM.
- Every load/unload emits `model.*` events and increments metrics.
- A missing GPU snapshot must not crash the gateway; degrade explicitly.

Tests: fake backend + fake VRAM + fake clock. Cover budget refusal, eviction
order, pinned protection, TTL expiry, keep-alive parsing and metrics.
