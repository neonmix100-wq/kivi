---
tags: [kivi, code, moc]
updated: 2026-11-13
generated: true
---

# Карта кода Kivi

> Сгенерирована из docstring'ов модулей. Не редактируй вручную.

## (root)

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.__init__` | 3 | Kivi - local-first AI assistant. |
| `src.kivi.__main__` | 8 | Allow ``python -m kivi``. |
| `src.kivi.config` | 229 | Typed configuration for Kivi. |
| `src.kivi.doctor` | 208 | Environment diagnostics for ``kivi doctor``. |
| `src.kivi.elevated_helper` | 255 | Elevated helper: execute exactly one allowlisted operation. |
| `src.kivi.paths` | 68 | Filesystem layout for Kivi runtime data. |

## brain

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.brain.__init__` | 30 | Brain: request routing, planning, execution and verification. |
| `src.kivi.brain.coder` | 601 | Coder loop: generated -> tests -> fixed (M2). |
| `src.kivi.brain.core` | 185 | KiviBrain: fast path first, then plan -> execute -> verify. |
| `src.kivi.brain.executor` | 359 | Plan executor: run a validated DAG through the ToolExecutor. |
| `src.kivi.brain.fastpath` | 98 | Fast path: one deterministic tool call for simple, unambiguous commands. |
| `src.kivi.brain.plan` | 247 | Plan model, JSON parser and structural validator for the deep path. |
| `src.kivi.brain.planner` | 119 | LLM planner: turn a goal into a validated plan DAG, never execute it. |
| `src.kivi.brain.verifier` | 160 | Plan verifier: deterministic checks first, LLM criteria second. |

## cloud

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.cloud.__init__` | 61 | Cloud Broker: policy, quotas and local handoff. |
| `src.kivi.cloud.broker` | 543 | Cloud Broker: policy, quotas, audited calls and local handoff. |
| `src.kivi.cloud.providers` | 121 | Cloud providers: metadata only, never secrets. |
| `src.kivi.cloud.scout` | 545 | Cloud Scout: free-provider discovery, cards and manual confirmation. |

## integrations

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.integrations.__init__` | 0 | — |
| `src.kivi.integrations.gmail` | 901 | Gmail integration: OAuth with readonly + compose, drafts and reading only. |
| `src.kivi.integrations.telegram` | 516 | Telegram Bot API integration: bot-only, allowlisted and rate-limited. |

## interfaces

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.interfaces.__init__` | 21 | User-facing interfaces: CLI, API, voice, tray, avatar. |
| `src.kivi.interfaces.api` | 320 | Local-only HTTP API for Kivi chat. |
| `src.kivi.interfaces.avatar2d` | 383 | Pure 2D avatar state and lip-sync logic plus an optional Tk renderer. |
| `src.kivi.interfaces.avatar3d` | 637 | 3D avatar (VRM) state, viseme lip-sync and a loopback web renderer. |
| `src.kivi.interfaces.cli` | 2818 | Command-line interface (stage 1). |

## kernel

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.kernel.__init__` | 1 | Kernel primitives: storage, events and audit. |
| `src.kivi.kernel.db` | 465 | SQLite storage with forward-only migrations. |
| `src.kivi.kernel.events` | 75 | In-process event bus. |
| `src.kivi.kernel.logs` | 43 | Append-only JSONL logs. |

## memory

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.memory.__init__` | 32 | Memory: working, episodic, semantic, profile and artifacts. |
| `src.kivi.memory.context` | 193 | Context builder: profile, relevant memory and recent messages within budget. |
| `src.kivi.memory.importer` | 148 | Read-only import of text knowledge bases into memory. |
| `src.kivi.memory.lifecycle` | 352 | Memory lifecycle: summarization, TTL retention and durable approvals. |
| `src.kivi.memory.search` | 201 | Hybrid memory search: FTS5 and embeddings fused with reciprocal rank. |
| `src.kivi.memory.store` | 381 | Durable memory: messages, facts, preferences and content-addressed artifacts. |
| `src.kivi.memory.vector_index` | 247 | Optional LanceDB ANN index for memory embeddings. |

## models

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.models.__init__` | 46 | Model Gateway: registry, VRAM/RAM budgets, leases and fallback. |
| `src.kivi.models.backends.__init__` | 10 | Model backends: Ollama first, others follow in later stages. |
| `src.kivi.models.backends.base` | 65 | Backend protocol shared by Ollama, llama.cpp, ComfyUI and cloud workers. |
| `src.kivi.models.backends.ollama` | 216 | Ollama backend. |
| `src.kivi.models.gateway` | 546 | Model Gateway: budget checks, leases and eviction. |
| `src.kivi.models.registry` | 205 | Model registry: what can be loaded, how much it costs and who may use it. |
| `src.kivi.models.vram` | 116 | VRAM accounting via ``nvidia-smi``. |
| `src.kivi.models.watchdog` | 141 | Background watchdog for the Model Gateway. |

## modules

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.modules.__init__` | 1 | Versioned plugins: work_crm, mail, telegram, media. |
| `src.kivi.modules.work_crm.__init__` | 95 | Headless Work CRM domain extracted from the old ``ai-agent`` Work mode. |
| `src.kivi.modules.work_crm.altcrm` | 1098 | AltCRM adapter over Playwright/CDP with a strict no-save, no-send policy. |
| `src.kivi.modules.work_crm.coach` | 204 | Coaching prompts for Work CRM: pre-call briefs and live hints. |
| `src.kivi.modules.work_crm.drafts` | 197 | Deterministic CRM drafts built only from verified call facts. |
| `src.kivi.modules.work_crm.extractor` | 260 | Headless facts extraction from call transcripts using an injected LLM callable. |
| `src.kivi.modules.work_crm.history` | 267 | Per-order call history: transcripts and structured summaries on disk. |
| `src.kivi.modules.work_crm.metrics` | 114 | Thread-safe automation metrics for the Work CRM module. |
| `src.kivi.modules.work_crm.schema` | 293 | Work CRM facts schema and anti-hallucination quote checks. |
| `src.kivi.modules.work_crm.tools` | 590 | ``crm.*`` tools: read, fill, act rows and task form through AltCRM. |

## safety

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.safety.__init__` | 71 | Safety layer: policy, protected zones, approvals and trust labels. |
| `src.kivi.safety.elevated` | 616 | Elevated broker: a narrow allowlist of admin operations behind UAC. |
| `src.kivi.safety.hotkey` | 218 | Global kill-switch hotkey. |
| `src.kivi.safety.killswitch` | 157 | Kill switch: one durable flag that stops tools and cloud calls. |
| `src.kivi.safety.permissions` | 224 | Permission Engine: task grants, risk levels and allow/confirm/deny. |
| `src.kivi.safety.policy` | 227 | A4-Bounded policy checks. |
| `src.kivi.safety.sanitize` | 257 | Sanitizer for untrusted content. |
| `src.kivi.safety.trust` | 226 | Trust labels and envelope rendering for external content. |
| `src.kivi.safety.zone_manifest` | 237 | Signed Protected Zones manifest. |
| `src.kivi.safety.zones` | 233 | Protected Zones: hard-deny paths and escape checks. |

## senses

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.senses.__init__` | 31 | Senses: voice, hearing, vision and text input. |
| `src.kivi.senses.audio` | 1095 | Local audio input: capture, voice activity detection, STT and wake words. |
| `src.kivi.senses.fish_audio` | 170 | Fish Audio cloud TTS engine. |
| `src.kivi.senses.qwen_tts` | 289 | Qwen3-TTS voice engine for Kivi. |
| `src.kivi.senses.tts` | 485 | Local text-to-speech engines and the sentence streaming pipeline. |
| `src.kivi.senses.voice` | 727 | Voice session orchestration: wake gate, barge-in, confirmations and metrics. |

## tools

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.tools.__init__` | 29 | Tools: files, shell, processes, browser, code, media and OS control. |
| `src.kivi.tools.base` | 143 | Tool contract: typed arguments, previews and structured results. |
| `src.kivi.tools.browser` | 1018 | Playwright browser automation with a managed profile and untrusted output. |
| `src.kivi.tools.clipboard` | 205 | Clipboard tools: explicit, confirmed access to the Windows clipboard. |
| `src.kivi.tools.code` | 627 | ``code.run``: run generated Python in a best-effort process sandbox. |
| `src.kivi.tools.executor` | 288 | Capability Broker: the only path from a proposed tool call to the OS. |
| `src.kivi.tools.fs` | 221 | Filesystem tools: read, list and write inside the workspace. |
| `src.kivi.tools.git` | 348 | Fixed git operations: status, diff, branch and commit. |
| `src.kivi.tools.mail` | 472 | Gmail tools: list, search, read, save an attachment and create a draft. |
| `src.kivi.tools.net` | 209 | HTTP GET with an SSRF guard and an explicit ``untrusted:web`` label. |
| `src.kivi.tools.patch` | 209 | Exact-text patch tool: replace a counted occurrence inside one UTF-8 file. |
| `src.kivi.tools.proc` | 278 | Process list tool: names and memory only, never command lines. |
| `src.kivi.tools.registry` | 160 | Tool registry: one place that knows which tools exist. |
| `src.kivi.tools.repo` | 524 | Repository index: a read-only map of the text files inside the workspace. |
| `src.kivi.tools.screen` | 306 | Screen capture tool: save the desktop as a BMP inside the workspace. |
| `src.kivi.tools.shell` | 132 | Shell tool: one approved process, no shell expansion, minimal environment. |
| `src.kivi.tools.system` | 636 | System tools: notifications, volume and power control for the Operator stage. |
| `src.kivi.tools.telegram` | 352 | Telegram tools: status, incoming updates and an allowlisted send. |
| `src.kivi.tools.ui` | 698 | Operator UI automation: mouse, keyboard and window-text lookup. |
| `src.kivi.tools.windows` | 1010 | Windows desktop control: top-level windows and application processes. |

## Тесты

Всего файлов: **63**.

- `tests/scenarios/test_m1_scenarios.py`
- `tests/scenarios/test_m3_browser.py`
- `tests/scenarios/test_m3_pc.py`
- `tests/scenarios/test_m4_mail.py`
- `tests/scenarios/test_m4_telegram.py`
- `tests/security/test_injection.py`
- `tests/unit/test_admin_cli.py`
- `tests/unit/test_altcrm.py`
- `tests/unit/test_api.py`
- `tests/unit/test_audio.py`
- `tests/unit/test_avatar2d.py`
- `tests/unit/test_avatar3d.py`
- `tests/unit/test_bench_gateway.py`
- `tests/unit/test_brain.py`
- `tests/unit/test_browser.py`
- `tests/unit/test_cli.py`
- `tests/unit/test_clipboard.py`
- `tests/unit/test_cloud.py`
- `tests/unit/test_cloud_providers.py`
- `tests/unit/test_cloud_scout.py`
- `tests/unit/test_code_tool.py`
- `tests/unit/test_coder.py`
- `tests/unit/test_config.py`
- `tests/unit/test_crm_tools.py`
- `tests/unit/test_db.py`
- `tests/unit/test_doctor.py`
- `tests/unit/test_elevated.py`
- `tests/unit/test_events.py`
- `tests/unit/test_fish_audio.py`
- `tests/unit/test_gateway.py`
- `tests/unit/test_git_tools.py`
- `tests/unit/test_gmail.py`
- `tests/unit/test_hotkey.py`
- `tests/unit/test_http_tool.py`
- `tests/unit/test_killswitch.py`
- `tests/unit/test_knowledge.py`
- `tests/unit/test_lifecycle.py`
- `tests/unit/test_mail_tools.py`
- `tests/unit/test_memory.py`
- `tests/unit/test_ollama.py`
- `tests/unit/test_patch_tool.py`
- `tests/unit/test_paths.py`
- `tests/unit/test_permissions.py`
- `tests/unit/test_policy.py`
- `tests/unit/test_proc.py`
- `tests/unit/test_qwen_tts.py`
- `tests/unit/test_registry.py`
- `tests/unit/test_repo_tools.py`
- `tests/unit/test_screen.py`
- `tests/unit/test_system_tools.py`
- `tests/unit/test_telegram.py`
- `tests/unit/test_telegram_tools.py`
- `tests/unit/test_tools.py`
- `tests/unit/test_tts.py`
- `tests/unit/test_ui.py`
- `tests/unit/test_vector_index.py`
- `tests/unit/test_voice.py`
- `tests/unit/test_vram.py`
- `tests/unit/test_watchdog.py`
- `tests/unit/test_windows.py`
- `tests/unit/test_work_crm_domain.py`
- `tests/unit/test_zone_manifest.py`
- `tests/unit/test_zones.py`
