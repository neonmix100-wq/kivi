---
tags: [kivi, code, moc]
updated: 2026-11-11
generated: true
---

# Карта кода Kivi

> Сгенерирована из docstring'ов модулей. Не редактируй вручную.

## (root)

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.__init__` | 3 | Kivi - local-first AI assistant. |
| `src.kivi.__main__` | 8 | Allow ``python -m kivi``. |
| `src.kivi.config` | 220 | Typed configuration for Kivi. |
| `src.kivi.doctor` | 208 | Environment diagnostics for ``kivi doctor``. |
| `src.kivi.paths` | 68 | Filesystem layout for Kivi runtime data. |

## brain

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.brain.__init__` | 1 | Brain: request routing, planning, execution and verification. |

## cloud

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.cloud.__init__` | 61 | Cloud Broker: policy, quotas and local handoff. |
| `src.kivi.cloud.broker` | 520 | Cloud Broker: policy, quotas, audited calls and local handoff. |
| `src.kivi.cloud.providers` | 121 | Cloud providers: metadata only, never secrets. |
| `src.kivi.cloud.scout` | 545 | Cloud Scout: free-provider discovery, cards and manual confirmation. |

## interfaces

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.interfaces.__init__` | 1 | User-facing interfaces: CLI, API, voice, tray, avatar. |
| `src.kivi.interfaces.cli` | 1064 | Command-line interface (stage 1). |

## kernel

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.kernel.__init__` | 1 | Kernel primitives: storage, events and audit. |
| `src.kivi.kernel.db` | 343 | SQLite storage with forward-only migrations. |
| `src.kivi.kernel.events` | 75 | In-process event bus. |
| `src.kivi.kernel.logs` | 43 | Append-only JSONL logs. |

## memory

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.memory.__init__` | 1 | Memory: working, episodic, semantic, profile and artifacts. |

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

## safety

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.safety.__init__` | 51 | Safety layer: policy, protected zones, approvals and trust labels. |
| `src.kivi.safety.killswitch` | 157 | Kill switch: one durable flag that stops tools and cloud calls. |
| `src.kivi.safety.permissions` | 224 | Permission Engine: task grants, risk levels and allow/confirm/deny. |
| `src.kivi.safety.policy` | 227 | A4-Bounded policy checks. |
| `src.kivi.safety.sanitize` | 257 | Sanitizer for untrusted content. |
| `src.kivi.safety.trust` | 214 | Trust labels and envelope rendering for external content. |
| `src.kivi.safety.zone_manifest` | 237 | Signed Protected Zones manifest. |
| `src.kivi.safety.zones` | 233 | Protected Zones: hard-deny paths and escape checks. |

## senses

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.senses.__init__` | 1 | Senses: voice, hearing, vision and text input. |

## tools

| Модуль | Строк | Ответственность |
| --- | ---: | --- |
| `src.kivi.tools.__init__` | 29 | Tools: files, shell, processes, browser, code, media and OS control. |
| `src.kivi.tools.base` | 143 | Tool contract: typed arguments, previews and structured results. |
| `src.kivi.tools.clipboard` | 205 | Clipboard tools: explicit, confirmed access to the Windows clipboard. |
| `src.kivi.tools.executor` | 288 | Capability Broker: the only path from a proposed tool call to the OS. |
| `src.kivi.tools.fs` | 221 | Filesystem tools: read, list and write inside the workspace. |
| `src.kivi.tools.net` | 187 | HTTP GET with an SSRF guard and an explicit ``untrusted:web`` label. |
| `src.kivi.tools.proc` | 192 | Process list tool: names and memory only, never command lines. |
| `src.kivi.tools.registry` | 84 | Tool registry: one place that knows which tools exist. |
| `src.kivi.tools.screen` | 306 | Screen capture tool: save the desktop as a BMP inside the workspace. |
| `src.kivi.tools.shell` | 132 | Shell tool: one approved process, no shell expansion, minimal environment. |

## Тесты

Всего файлов: **27**.

- `tests/security/test_injection.py`
- `tests/unit/test_bench_gateway.py`
- `tests/unit/test_cli.py`
- `tests/unit/test_clipboard.py`
- `tests/unit/test_cloud.py`
- `tests/unit/test_cloud_providers.py`
- `tests/unit/test_cloud_scout.py`
- `tests/unit/test_config.py`
- `tests/unit/test_db.py`
- `tests/unit/test_doctor.py`
- `tests/unit/test_events.py`
- `tests/unit/test_gateway.py`
- `tests/unit/test_http_tool.py`
- `tests/unit/test_killswitch.py`
- `tests/unit/test_knowledge.py`
- `tests/unit/test_ollama.py`
- `tests/unit/test_paths.py`
- `tests/unit/test_permissions.py`
- `tests/unit/test_policy.py`
- `tests/unit/test_proc.py`
- `tests/unit/test_registry.py`
- `tests/unit/test_screen.py`
- `tests/unit/test_tools.py`
- `tests/unit/test_vram.py`
- `tests/unit/test_watchdog.py`
- `tests/unit/test_zone_manifest.py`
- `tests/unit/test_zones.py`
