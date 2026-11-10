---
name: kivi-cloud
description: Use for cloud providers, quotas, cost limits, data policy, 429 handling, handoff packs or the Cloud Scout.
---

# Cloud Broker

Read: `docs/CLOUD.md`, `src/kivi/cloud/{providers,broker}.py`,
`config/cloud.yaml`.

Rules:
- Keys only from environment variables; never write a key to a file.
- Default data class is private; only public data may leave the machine.
- `cost_limit_usd=0` forbids paid calls; free tiers still need quota checks.
- Every call and denial is audited in SQLite.
- On 429/quota exhaustion raise `QuotaExhausted` and continue with
  `build_handoff(...).to_prompt()`, not with a silent failure.
- Run focused tests: `pytest -q tests/unit/test_cloud.py`.
