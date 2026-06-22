# OracleAI Reference

**Version:** v2.11.11
**Updated:** 2026-06-22
**Maintainer:** Todd/"OmniFoxX" (developer/founder)
**Mission:** Dissolve barriers between AI and disabled users. Reliability and memory integrity are load-bearing.

Operational cheat sheet for OracleAI v2.1.5+. Tracks every tool tag, HTTP
endpoint, daemon action, file path, port, and side-channel that exists in
the current install. Written to be useful for both the human operator
(Todd) and Sage herself when this file is uploaded as context.

---

## 1. Architecture Overview

OracleAI runs three inference tiers in parallel plus an overseer daemon a set of supporting daemons. CRAIID concept to become reality, very soon.
All inference is local — no cloud round-trips.

### 1.1 Inference Tiers (typical ports, ports configurable v2.2+)

| Tier       | Port  | Engine       | Role                                                                                              |
| ---------- | ----- | ------------ | ------------------------------------------------------------------------------------------------- |
| **Oracle** | 11434 | Ollama       | Primary user-facing chat. Largest model. (Ollama defaults to this port)                           |
| **Sage**   | 11435 | llama-server | Agentic engine — interprets tool tags, runs SEARCH/WEATHER/BROWSE etc., follows multi-step plans. |
| **Daemon** | 11436 | llama-server | Tiny background inference (digest summarization, KB consolidation). Optional.                     |

### 1.2 Supporting Daemons (typical ports, ports configurable v2.2+)

| Daemon                 | Port | Purpose                                                                                                                                           |
| ---------------------- | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| **sage_daemon.py**     | 9998 | Out-of-band mechanics — chain digest, procedural-KB consolidation, anomaly monitor. No user-facing LLM.                                           |
| **overseer_daemon.py** | —    | Heartbeat monitoring for sage/ipc daemons, loop detection (3 errors in 60s = restart), file-based user notifications. Passive observer in v2.1.9. |
| **ipc_bridge.py**      | 9999 | Primary IPC — browser_tool ↔ visible browser handoff.                                                                                             |
| **ipc_monitor.py**     | 9997 | Web dashboard for IPC events.                                                                                                                     |

### 1.3 File Layout (typical ports, ports configurable v2.2+)

E:\OracleAI_v2.7
├── start.bat ← launcher; launches Ollama + Sage + Daemon + sage_daemon + overseer ├── start.py ← Python entry, called by start.bat ├── config.json ← runtime overrides ├── chat_memory.json ← rolling chat memory ├── archives\ ← saved conversations (timestamped JSONs) ├── downloads\ ← [SAVE_FILE:] outputs land here ├── uploads\ ← user-uploaded files ├── plugins\ ← plugin manifests ├── frontend
│ ├── index.html │ └── js
│ ├── chat.js ← WS handler, stream UI, stall banner, tool-call ⚡ │ └── settings.js ← Settings UI, max_tokens sanitizer ├── electron
│ └── main.js ← Electron orchestration, port-conflict handling └── backend
├── main.py ← FastAPI app + agentic loop dispatcher ├── sage_engine.py ← SAGE_SYSTEM_PROMPT (+ SAGE_SYSTEM_PROMPT_SMALL), tool fns, parser ├── model_manager.py ← tier routing + abort flag, adaptive ctx, generate() ├── task_prioritiser.py ← OAgentP/OAgentD/OSubAgent priority queue ├── overseer_daemon.py ← Systems supervisor/orchestrator (Phase 3, v2.1.8) ├── sage_daemon.py ← out-of-band mechanics daemon ├── sage_daemon_client.py ← TCP client wrapper ├── ipc_bridge.py ← Visible-browser IPC (9999) ├── ipc_monitor.py ← IPC web dashboard (9997) ├── browser_tool.py ← Playwright browser (Sage) ├── memory_logger_surprise.py ← Fernet+SHA3 chain log ├── procedural_memory.py ← KB with chain-witness provenance ├── plugin_manager.py ├── hw_utils.py ├── tier_lifecycle.py ├── time_manager.py ← Unified time source (v2.1.6) ├── config.py ← Paths + constants (PROJECT_DIR, DATA_DIR, LOG_DIR, ...) ├── \_tier_config_reader.py ← read by start.bat for ctx sizes ├── llama-server.exe ├── .fernet_key ← (gitignore-equivalent — back up with log) ├── verify_v214.py ← v2.1.4 regression suite (9 tests) ├── verify_procedural_wiring.py ← v2.1.4 procedural tests (8) ├── verify_v215.py ← v2.1.5 regression suite (6 tests) └── test_browser.py

Outside the project (intentional, daemon-conflict-safe):

E:\sage_data
├── memory_log
│ ├── memory_chain.log ← Fernet-encrypted, SHA3-chained │ └── chain_digest.json ← daemon-written rolling summary ├── procedural_memory
│ └── procedural.json ← KB with chain_hash back-pointers ├── logs
│ ├── sage_daemon.log │ ├── overseer.log │ └── overseer_notifications.json ├── uploads
├── models
│ └── \*.gguf └── snapshots\ ← read-only codebase snapshots
| └── |\_\_Uploads\ OracleAI_v2.7_SageCopy ←Mirror copy for Sage to use for testing, etc.

**Backup set (must snapshot together, The Trinity):**

- `backend\.fernet_key`
- `sage_data\memory_log\memory_chain.log`
- `sage_data\procedural_memory\procedural.json`

Restore one without the others and encrypted entries become
undecryptable (chain still verifies, but content is `[DECRYPT_FAILED]` sentinels).

---

## 2. Tool Tags Sage Emits Inside Her Output

These tags get parsed out of Sage's generation by `parse_agent_actions()`
in `sage_engine.py` and dispatched in the agentic loop in `main.py`.

Sage's instructions live in `sage_engine.py:SAGE_SYSTEM_PROMPT` (full, ~155 lines) or
`SAGE_SYSTEM_PROMPT_SMALL` (~25 lines, for ≤4B models — see §6).

**Universal rule:** tags shown in the system prompt use ANGLE brackets `⟨ ⟩` for
pedagogy only. Real tool invocations from Sage use SQUARE brackets `[ ]`. The parser
intentionally ignores angle-bracket forms.

Anything inside `[ ... ]` here is a tag template — never display these tags in
user-facing chat when they are actually functional. ONLY display them if they are
part of the body/content being used for contextual reference.

### Research / Web

| Tag                       | Purpose                               | Notes                                                                                    |
| ------------------------- | ------------------------------------- | ---------------------------------------------------------------------------------------- |
| `[SEARCH: query]`         | Tavily news search                    | Hard-capped at 5/response, 50/session                                                    |
| `[SEARCH_GENERAL: query]` | Tavily general search                 | Same Tavily budget; good for weather/travel/facts                                        |
| `[SEARCH_MEMORY: topic]`  | Search past conversation archives     | Memory READ — safe path; no external cost                                                |
| `[WEATHER: city]`         | Current conditions                    | Sage corrects obvious misspellings silently; forecast-style queries should use [SEARCH:] |
| `[BROWSE: url]`           | Fetch a specific URL via browser_tool | Browser plugin must be enabled                                                           |
| `[WEB_SEARCH: query]`     | DuckDuckGo via browser_tool           | No Tavily cost; conserves budget                                                         |

### Code & Files

| Tag                              | Purpose                                     | Notes                                                                      |
| -------------------------------- | ------------------------------------------- | -------------------------------------------------------------------------- |
| `[CODE: python]`                 | Run Python in sandbox                       | DOWNLOADS_DIR available as a variable                                      |
| `[SAVE_FILE: name.ext\|content]` | Save any file to downloads                  | Bracket-balanced parser handles code; pipe (`\|`) separates name from body |
| `[VERIFY_FILE: path]`            | Read file + AST-check .py without execution | Canonical verify path; do NOT use [CODE:] to verify. Wired v2.1.6.         |

### Procedural Memory

| Tag                            | Purpose                                       | Notes                                                                   |
| ------------------------------ | --------------------------------------------- | ----------------------------------------------------------------------- |
| `[REMEMBER: key\|description]` | Record a successful insight (chain-witnessed) | Use for the _lesson_, not the bare action sequence — that's auto-logged |
| `[REMEMBER_FAIL: key\|reason]` | Record a dead-end (local only, not chained)   | Saves future cycles                                                     |
| `[RECALL: query]`              | Fuzzy-match against the procedural KB         | Returns top 10 successful matches; memory READ                          |

### Coordination

| Tag                                  | Purpose                               | Notes                                                                                                                                                                                                                                      |
| ------------------------------------ | ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `[PRIORITISE: sub1 \| sub2 \| sub3]` | Batched parallel dispatch via OAgentD | Subtask keywords: `search news <q>`, `search general <q>`, `weather <loc>`, `browse <url>`. Uses deterministic urgency ordering, per-task timeouts (30s), max retries (3), routes to best-performing available sub-agent based on history. |
| `[TASK_DONE]`                        | Mark a multi-step task complete       | Triggers auto-log of the turn's tool sequence as a chain-witnessed successful procedure.                                                                                                                                                   |

**v2.1.9 note:** no new tags were added in this version. The TaskP wiring (Phases 1–2)
and stall detection (#56) operate beneath Sage's view — same tag surface, same emission rules.

### Auto-Logging Behaviour (no tag — happens automatically)

- On `[TASK_DONE]` (not aborted), the full ordered tool-call sequence is saved as a
  chain-witnessed successful procedure keyed by an SHA-1 prefix of the user's request.
- Same `(action_type, content)` failing 3× in one turn auto-logs as an unsuccessful
  procedure (no chain witness).
- Top-5 successful + top-5 unsuccessful procedures are auto-injected into Sage's system
  prompt every turn under a "PROCEDURAL MEMORY" block — Sage must NOT echo that block
  to the user.

---

## 3. Configuration Reference (`config.json`)

**Schema v2 (since #68 Phase E, 2026-05-23).** `config.json` is now a sectioned
nested structure with `"schema_version": 2`. `backend/config_store.py` owns
the schema and is the single source of truth — `DEFAULT_CONFIG` in `main.py`
is a legacy artifact kept only as defense-in-depth for a handful of
back-compat reads. Old v1 (flat) files self-upgrade on boot via
`backend/migrate_config.py`; original file is preserved as
`config.json.v1_backup_<ts>.json` alongside.

Top-level sections (each is a JSON object): `ui`, `electron`, `network`,
`paths`, `inference`, `context_sizing`, `timeouts`, `prompts`, `aiq_nudge`,
`sage`. The tables below list keys by section, with their nested path.

### 3.1 Core inference (`inference`, `ui`)

| Path                         | Default                    | Notes                                       |
| ---------------------------- | -------------------------- | ------------------------------------------- |
| `inference.backend`          | `"ollama"`                 | Inference backend                           |
| `network.ollama_url`         | `"http://localhost:11434"` | Used by Ollama backend                      |
| `paths.models_dir`           | `null`                     | `null` = resolve from `config.MODEL_DIR`    |
| `inference.default_model`    | `null`                     | Pre-selected boot model ("Choose…" if null) |
| `inference.secondary_model`  | `""`                       | Secondary pick for auto-route               |
| `inference.temperature`      | `0.5`                      |                                             |
| `inference.max_tokens`       | `-1`                       | **Canonical "unlimited" sentinel** — see §7 |
| `inference.n_gpu_layers`     | `-1`                       | -1 = all GPU                                |
| `inference.gpu_acceleration` | `true`                     |                                             |
| `ui.theme`                   | `"dark"`                   |                                             |
| `ui.haptic`                  | `true`                     |                                             |

**`system_prompt` is no longer a config key.** As of #68 Phase E Step 6 the
prompt lives in a text file pointed at by `prompts.system_prompt_file`
(default `"../sage_data/prompts/system.txt"`). The settings-UI textarea
binds to `GET`/`POST /api/prompts/system` rather than `/api/config`.
Inference reads the file fresh on every call, so UI edits take effect
without a restart.

### 3.2 Adaptive Context Sizing (`inference`, `context_sizing`)

| Path                                   | Default | Notes                                              |
| -------------------------------------- | ------- | -------------------------------------------------- |
| `context_sizing.hard_cap_ctx`          | `true`  | If true, clamp computed ctx to model's trained max |
| `context_sizing.ctx_min`               | `8192`  | Floor for computed ctx                             |
| `context_sizing.ctx_response_headroom` | `1500`  | Additive tokens reserved for response              |
| `context_sizing.ctx_padding_factor`    | `1.0`   | Multiplicative (legacy; 1.0 = no effect)           |
| `inference.n_ctx`                      | `null`  | Explicit override; `null` = adaptive sizing        |

### 3.3 Timeouts (`timeouts`)

| Path                       | Default | Notes                                                                    |
| -------------------------- | ------- | ------------------------------------------------------------------------ |
| `timeouts.ollama_read_sec` | `1800`  | httpx read timeout. Bump to 10800 (3h) for nemotron-class CPU inference. |
| `timeouts.stall_token_sec` | `300`   | **#56:** between-token watchdog. Aborts run if no token in N sec.        |
| `timeouts.stall_tool_sec`  | `180`   | **#56:** tool-result watchdog. Aborts if tool_call has no tool_result.   |

(Flat-dict back-compat names — `ollama_read_timeout_sec`, `stall_token_timeout_sec`,
`stall_tool_timeout_sec` — are still accepted on `POST /api/config`.)

### 3.4 Prompt Tier (`prompts`)

| Path                         | Default                             | Notes                                                                              |
| ---------------------------- | ----------------------------------- | ---------------------------------------------------------------------------------- |
| `prompts.system_prompt_file` | `"../sage_data/prompts/system.txt"` | Path to system-prompt text file (relative to project root or absolute).            |
| `prompts.force_prompt_tier`  | `null`                              | Auto-detect via model size. Set to `"small"` (≤4B prompt) or `"full"` to override. |

### 3.5 Network & Ports (`network`)

| Path                          | Default                    | Notes                                                 |
| ----------------------------- | -------------------------- | ----------------------------------------------------- |
| `network.host`                | `"127.0.0.1"`              | FastAPI bind host                                     |
| `network.ollama_url`          | `"http://localhost:11434"` | Override if Ollama runs elsewhere                     |
| `network.ports.app`           | `8000`                     | FastAPI port (read by Electron + start.bat + backend) |
| `network.ports.ollama_oracle` | `11434`                    | Ollama Oracle tier                                    |
| `network.ports.llama_sage`    | `11435`                    | Sage llama-server                                     |
| `network.ports.llama_daemon`  | `11436`                    | Daemon llama-server                                   |
| `network.ports.llama_embed`   | `11437`                    | Reserved (embed tier)                                 |
| `network.ports.sage_daemon`   | `9998`                     | Sage Daemon Python TCP service                        |
| `network.ports.ipc_browser`   | `9999`                     | Privacy-browser IPC                                   |

Precedence on read: env var override (e.g., `ORACLE_APP_PORT`) > `network.ports.*` > hardcoded fallback.

### 3.6 Electron (`electron`)

| Path                                                                | Default    | Notes                                             |
| ------------------------------------------------------------------- | ---------- | ------------------------------------------------- |
| `electron.backend_mode`                                             | `"vulkan"` | `"vulkan"` or `"ipex"` — chosen via Electron menu |
| `electron.window.{width,height,min_width,min_height}`               | various    | BrowserWindow dimensions                          |
| `electron.health_probe.{poll_ms,probe_timeout_ms,total_timeout_ms}` | various    | Backend readiness probe tuning                    |

### 3.7 AIQNudge (`aiq_nudge`)

| Path                      | Default         | Notes                                                          |
| ------------------------- | --------------- | -------------------------------------------------------------- |
| `aiq_nudge.enabled`       | `false`         | HMAC-signed mid-run side-channel (#44). Distribution-safe off. |
| `aiq_nudge.watch_pattern` | `"nudge_*.txt"` | Glob pattern for nudge files                                   |

### 3.8 Sage / Plugin Toggles (`sage`)

| Path                      | Default | Notes                             |
| ------------------------- | ------- | --------------------------------- |
| `sage.sage_mode`          | `true`  | Enable agentic Sage layer         |
| `sage.agentic_mode`       | `true`  | Multi-step tool execution         |
| `sage.web_search_enabled` | `true`  | SEARCH/SEARCH_GENERAL tags active |
| `sage.code_exec_enabled`  | `true`  | CODE tag active                   |
| `sage.privacy_mode`       | `false` |                                   |
| `inference.auto_route`    | `false` | Auto-pick model based on query    |

---

## 4. HTTP Endpoints (FastAPI)

Default port `8000`; configurable via `network.ports.app` in `config.json`,
overridable via `ORACLE_APP_PORT` env var or `python start.py --port N`.

Hit from a browser or `curl` while OracleAI is running. (PowerShell users:
`curl` is aliased to `Invoke-WebRequest` with incompatible syntax — use
`curl.exe` explicitly or `Invoke-RestMethod`.)

### 4.1 Chat

- `WS /ws/chat` — main chat WebSocket (events: token, tool_call, tool_result, stall_detected, error, done)
- `POST /api/abort` — out-of-band stop (HTTP, bypasses WS queue)
- `POST /api/route-query` — auto-routing dispatcher
- `GET/POST /api/chat-memory` — chat history

### 4.2 Models & Tiers

- `GET /api/hardware` — VRAM/RAM detection
- `GET /api/models` — list known models
- `POST /api/models/load`
- `POST /api/models/unload`
- `POST /api/models/refresh`
- `GET /api/tiers` — Oracle / Sage / Daemon / Embed status
- `GET /api/tiers/{name}/status`
- `POST /api/tiers/{name}/restart`

### 4.3 Config

- `GET/POST /api/config` — base config (flat-dict back-compat view of the
  nested v2 schema). `POST` validates against an allowlist derived from
  `OracleConfig.to_flat_dict().keys()`; unknown keys return HTTP 400
  with `"unknown config key: '<name>'"`.
- `GET/POST /api/sage/config` — Sage-mode toggles
- `GET/POST /api/prompts/system` — read/write the system-prompt text file.
  `POST` body: `{"system_prompt": "..."}`. Empty/whitespace values are
  accepted (explicit clear).
- `GET /api/plugins` — list plugins
- `POST /api/plugins/{plugin_id}/toggle`
- `GET /api/vibe-prompts`

### 4.4 Files & Archives

- `POST /api/upload` — file upload
- `GET /api/downloads` — list downloads
- `GET /api/downloads/{filename}` — fetch a downloaded file
- `POST /api/downloads/save` — save text content directly
- `GET /api/archives`
- `POST /api/archives/save`
- `POST /api/archives/load`
- `POST /api/archives/delete`

### 4.5 Tavily (Search Budget)

- `GET /api/tavily` — key info, masked
- `POST /api/tavily` — set/clear key

### 4.6 Misc

- `GET /api/health` — liveness
- `GET /api/exercise` — activity log
- `POST /api/exercise` — log activity
- `GET /api/launch-browser` — spawn visible browser (currently launches
  `_browser_test()` in headless mode — see §13 Known Quirks)

### 4.7 Sage Daemon (port 9998)

- `GET /api/daemon/status` — daemon health + periodic-worker state
- `GET /api/daemon/digest` — latest rolling chain digest (or null)
- `POST /api/daemon/digest/refresh` — force regenerate digest now
- `GET /api/daemon/anomaly` — tamper-monitor state
- `POST /api/daemon/consolidate` — force KB consolidation pass now
- `GET /api/daemon/ping` — cheap liveness check

### 4.8 Overseer (v2.1.9 Phase 3)

- `GET /api/overseer/notifications` — pending alerts (file-polled)
- `POST /api/overseer/notifications/clear` — mark all read
- `GET /api/overseer/log?lines=N` — tail overseer.log

---

## 5. Sage Daemon TCP Protocol (Port 9998)

Length-prefixed JSON over localhost TCP. Reachable via `SageDaemonClient`
in `sage_daemon_client.py`. The HTTP `/api/daemon/*` endpoints proxy into these.

| Action              | Purpose                                        |
| ------------------- | ---------------------------------------------- |
| `ping`              | Liveness check                                 |
| `status`            | Daemon health + periodic-worker state + uptime |
| `read_recent`       | N most recent log entries (chronological)      |
| `read_top_surprise` | N highest-surprise log entries                 |
| `verify_chain`      | Full hash-chain integrity walk                 |
| `count_entries`     | How many entries in the log                    |
| `generate_summary`  | Extractive summary from supplied entries       |
| `consolidate_now`   | Force procedural KB consolidation              |
| `read_digest`       | Return the latest written digest               |
| `run_digest_now`    | Force chain-digest regeneration                |
| `anomaly_status`    | Return tamper-monitor state                    |
| `shutdown`          | Graceful daemon stop                           |

Periodic-worker cadences (env-var overridable):

- `SAGE_DAEMON_CONSOLIDATE_SEC` (default 600 = 10 min)
- `SAGE_DAEMON_DIGEST_SEC` (default 300 = 5 min)
- `SAGE_DAEMON_VERIFY_SEC` (default 300 = 5 min)
- `SAGE_DAEMON_STALE_DAYS` (default 14 — age-out cutoff for unsuccessful procedures)

---

## 6. Model-Aware Prompt Tiers (v2.1.9 #55)

| Tier    | Threshold                  | Prompt used                                          |
| ------- | -------------------------- | ---------------------------------------------------- |
| `small` | model name has `≤4b` token | `SAGE_SYSTEM_PROMPT_SMALL` (~25 lines, 3 core tools) |
| `full`  | else (or unknown)          | `SAGE_SYSTEM_PROMPT` (~155 lines, all tools)         |

Helper: `main.py:_model_size_hint(model_id)` — heuristic regex on the model name.
Override via `force_prompt_tier` config key.

Console diagnostic on every turn:
`[PROMPT TIER] model='...' -> tier=small|full (forced=...)`.

---

## 7. The `max_tokens = -1` Sentinel (v2.1.8)

**Canonical:** `-1` means unlimited. The frontend renders this as a blank Max Tokens input.

Sanitized at five layers:

1. HTML input: `min="-1"`, placeholder "blank = unlimited"
2. `settings.js`: blank/zero/non-numeric → -1 on save
3. `chat.js`: only includes `max_tokens` in WS options when user typed a positive int
4. `main.py` `/api/config` POST: sanitizes before merge
5. `model_manager.py`: defensive coercion at both Ollama and llama-server call sites;
   logs `[MAX_TOKENS GUARD]` on anomalies

Any non-positive value other than `-1` (e.g., `0`, `-5`, NaN, `""`) is coerced to `-1`.

---

## 8. Task Prioritiser (v2.1.8 Phase 1–2)

### 8.1 The Classes

`backend/task_prioritiser.py`:

- `OAgentP` — urgency calculator (deterministic, no jitter)
- `OAgentD` — dispatcher (heapq-correct, urgency-ordered)
- `OSubAgent` — worker with per-task timeout + retry

Module constants: `MAX_RETRIES=3`, `TASK_TIMEOUT=30.0`, `STATS_WINDOW=20`, `DISPATCH_SLEEP=0.05`.

### 8.2 Toggle-Gated Wrapping

When `FEATURES_ENABLED["task_prioritiser"]` (plugin toggle) is **ON**:

- 6 SAFE tools route through `main.py:_taskp_run_or_direct`:
  search, weather, search_memory, browse, web_search_browser, recall.
- Their tool_call WS message shows ⚡ before the existing emoji so you can see
  TaskP routed it.

When toggle is **OFF**:

- Direct `run_in_executor` path, byte-identical to pre-Phase-2 behavior.

### 8.3 TaskP Memory Interaction Rules

**SAFE for TaskP dispatch:**

- Memory READS and retrieval
- Prioritizing which memories to surface (urgency scoring)
- Procedural memory step sequencing
- Context retrieval for inference
- Web search, browser, file READ, inference routing

**DIRECT PATH ONLY (never through TaskP):**

- Memory WRITES — must pass through Fernet encryption first
- Hash chain appends — sequential, atomic, single-threaded, no exceptions
- Existing memory modifications — same reason as above

**Why this matters:**

- Fernet encryption must wrap ALL writes before storage
- Hash chain integrity depends on sequential, non-concurrent appends
- ResourceLockManager in `overseer_daemon.py` protects these paths
- As memory history grows, TaskP prioritization becomes increasingly
  powerful for READ operations and procedural sequencing

---

## 9. Stall Detection (v2.1.9 #56)

Two parallel watchdogs run per turn in `main.py`'s WS chat handler:

| Watchdog       | Trigger                                    | Default     |
| -------------- | ------------------------------------------ | ----------- | ---------------------------------------- |
| Token watchdog | No LLM token received in N sec             | 300 (5 min) | _Currently set super high for dev 56000_ |
| Tool watchdog  | tool_call sent but no tool_result in N sec | 180 (3 min) | _Currently set super high for dev 56000_ |

**On stall:** sets `model_manager._abort = True`, sends
`{"type":"stall_detected","reason":"..."}` over WS. Frontend
(`chat.js:handleStallDetected`) renders an amber banner under the in-flight
assistant bubble, preserves any partial response.

**Does NOT** re-prompt Sage. Recovery is user-initiated retry. Re-prompting would
re-engage the AIQNudge self-prompt-injection surface (queued separately as #44).

**Architecture detail:** the watchdog is published via a `contextvars.ContextVar`
so `_taskp_run_or_direct` records tool_call/tool_result automatically without a
function-arg threaded through every call site.

---

## 10. Overseer Daemon (v2.1.9 Phase 3)

Passive observer launched by `start.bat` after sage_daemon.

**Active:**

- TCP heartbeat checks (sage:9998, ipc:9999, ipc:9997). 10s interval,
  30s unresponsive threshold.
- Auto-restart unresponsive daemons via `subprocess.Popen(start_cmd)`.
  Max 3 attempts; escalates to user notification.
- LoopDetector: same (daemon, error) repeated 3× in 60s = interrupt +
  restart + notification.
- File-based notifications in `sage_data/logs/overseer_notifications.json`,
  polled via `/api/overseer/*`.

**NOT yet wired (deferred):**

- ResourceLockManager exists and tracks `chat_memory.json` +
  `hash_chain.log`, but no writer calls `acquire()`/`release()` yet.
  Wiring it requires very careful additive wrappers around the
  Fernet/hash-chain write paths — out of scope for v2.1.9.

---

## 11. Visible-Browser Stack (v2.1.6)

`browser_tool.py` replaced the older `privacy_browser_multi.py` + `sage_browser.py`.
Playwright-based, max_steps now 27, with CAPTCHA handling + IPC handoff to a
visible browser process (port 9999).

The `[BROWSE:]` and `[WEB_SEARCH:]` tags route through this when the browser
plugin is enabled. Default off; enable in Settings or UI toggle switch.

---

## 12. start.bat Options

On launch:

- Choice prompt: `1` = ollama/llama (stable), `2` = Intel Arc backend (experimental)
- Self-locating paths via `%~dp0` so folder renames don't break startup
- Spawns: Ollama Oracle (port 11434), llama-server Sage (port 11435),
  Sage Daemon (port 9998), Daemon llama-server tier (port 11436), Overseer

Tunables at top of `start.bat`:

- `PROBE_TIMEOUT_SEC` — readiness probe timeout per tier
- `LLAMA_SERVER` — path to llama-server.exe (self-locating)
- `MODELS_DIR` — where .gguf models live
- `SAGE_CTX_SIZE` / `DAEMON_CTX_SIZE` — per-tier context window

---

## 13. Feature Flags (`sage_engine.py`)

python
FEATURES_ENABLED = {
"weather": True,
"semantic_search": True,
"exercise_tracker": False,
"complexity_detector":True,
"task_prioritiser": True,
"browser": True,
"daemon": False, # host-side flag, separate from daemon process
}
Toggle via sage_engine.set_feature(name, enabled).

## 14. AIQNudge (Mid-Run Side-Channel) v2.1.10 Addition - Official HMAC signing

HMAC-SHA256 signing on nudge content; consumer verifies before forwarding to Sage. User sends signed QNudge via terminal command, Sage recieves notification on next agentic step. Sage can leave questions at arequested location or at the relative location "sage_data\nudges\AI_QNudge_Protocol_Sage.txt" per User's request.

## 15. Ports (typical ports, configurable v2.2+)

Port Service
8000 FastAPI backend (PORT_APP)
9997 IPC monitor web dashboard
9998 Sage Daemon TCP (PORT_DAEMON)
9999 Browser IPC mirror (PORT_IPC_BROWSER)
11434 Ollama Oracle tier
11435 llama-server Sage tier
11436 llama-server Daemon tier
11437 nomic-embed (reserved, Phase 4)

## 16. Verify Scripts (run from "OracleAI...\backend\")

py verify_v214.py → 9 tests, Fernet + chain integrity
py verify_procedural_wiring.py → 8 tests, REMEMBER/RECALL + auto-capture
py verify_v215.py → 6 tests, TASK_DONE autolog + daemon jobs
All three should be ALL GREEN. Run after any structural edit.

## 17. Quick-Switch Model Configs

See Model_Recommendations.txt for full reasoning.

Profile Oracle Sage Daemon
Minimal qwen2.5:7b llama3.1:8b qwen2.5-coder:1.5b-base
Daily driver gemma4:31b (or qwen3-coder:30b for code) qwen3.5:latest qwen2.5:7b
Nemotron dev nemotron-3-super:120b llama3.1:8b qwen2.5-coder:1.5b-base
For Nemotron dev, bump ollama_read_timeout_sec to 54000 (15 hours) and stall_token_timeout_sec to 36000+ so the stall watchdog doesn't false-fire on slow CPU generation.

## 18. Backups in the Codebase

File What it preserves
backend/task_prioritiser.py.bak_pre_leoupgrade Pre-Phase-1
backend/main.py.bak_pre_taskp_phase2 Pre-Phase-2 (also useful as the pre-#56 restore point)
backend/main.py.bak_pre_stall_detect Pre-#56
| `backend/main.py.bak_pre_prompt_tier` | Pre-#55 |
| `backend/sage_engine.py.bak_pre_prompt_tier` | Pre-#55 |
| `backend/main.py.truncated_state_for_audit` | Phase 2 truncation artifact, kept for forensics |
| `backend/start.bat.bak_diag` | Pre-cmd-/k-revert |

---

## 19. Phase Ledger

### v2.1.4 (April 2026)

- Fernet content encryption in chain log
- Both-sides logging (user + assistant) with `role` field
- Procedural memory CRUD bugs fixed + chain provenance witness
- `[REMEMBER:]` / `[REMEMBER_FAIL:]` / `[RECALL:]` tags
- Auto-capture of repeated tool failures (3× same call + failure-shaped result)
- Auto-injection of recent procedures into system prompt
- Stop button HTTP fix (bypasses WS queue)
- Folder-rename trap defused (`%~dp0` in start.bat)

### v2.1.5 (April 2026)

- TASK_DONE autolog (full tool sequence → chain-witnessed procedure)
- Sage Daemon auto-launched + periodic worker thread
- Daemon-side KB consolidation (prunes stale unsuccessful, dedupes,
  NEVER touches chain-witnessed)
- Daemon-side chain-log digest (rolling extractive summary, READ-ONLY
  against chain)
- Daemon-side anomaly / tamper monitor (verify_chain on cadence)
- `[PRIORITISE:]` tag for batched parallel dispatch via OAgentD
- HTTP `/api/daemon/*` proxy endpoints
- SCRIPT/FILE EMISSION RULE — `[SAVE_FILE:]` is mandatory for any file
  the user asks for; markdown ` `fences are forbidden for file bodies
- Procedural memory pre-seeded with `save_script_use_tag_not_fences` lesson

### v2.1.6 (May 2026)

- `get_browser()` singleton accessor added to `browser_tool.py`
- `[BROWSE:]` and `[WEB_SEARCH:]` tags now fully functional in the
  agentic loop
- `time_manager.py` unified time source added
- `[VERIFY_FILE:]` tag wired

### v2.1.7 (May 2026)

- Bug fixes

### v2.1.8 (May 2026)

- Bug fixes
- WCAG 2.2 accessibility audit pre-emptive work begins
- Overseer daemon (Phase 3) deployed as passive observer
- Adaptive context sizing
- `max_tokens = -1` canonical unlimited sentinel, 5-layer sanitizer
- `stall_token_timeout_sec` / `stall_tool_timeout_sec` config knobs
- TaskP Phase 1: heap ordering fixed, urgency determinism, timeout/retry
- TaskP Phase 2: toggle-gated routing wired into 6 SAFE tool dispatch sites

### v2.1.9 (May 2026)

- **#55** — Model-aware prompt tiers: `SAGE_SYSTEM_PROMPT_SMALL` for ≤4B
  models. `force_prompt_tier` config override.
- **#56** — Stall detection: dual token + tool watchdogs in WS chat
  handler. New config knobs `stall_token_timeout_sec`,
  `stall_tool_timeout_sec`. New WS event `stall_detected`.
- **TaskP Phase 1** — `task_prioritiser.py` upgrade: heap ordering fixed,
  urgency determinism, timeout/retry.
- **TaskP Phase 2** — Toggle-gated TaskP routing wired into 6 SAFE tool
  dispatch sites. ⚡ visual indicator.
- **TaskP Phase 3** — `overseer_daemon.py` deployed as passive observer.
  New `/api/overseer/*` endpoints. Auto-launched from start.bat.
- **Overseer log relocation** — `sage_data/logs/overseer.log` +
  `overseer_notifications.json`, matching other daemons.
- **max_tokens=-1 sanitizer** — End-to-end 5-layer guard.
- **start.bat cleanup** — Reverted cmd /k diagnostic; both Llama-Sage and
  Llama-Daemon back to cmd /c. Added Overseer launch line.
- **Latent bug fixes** — `[WS Error] {e}` NameError on disconnect (Phase 2
  splice leftover); `LoopDetector` deque pruning crash caught in deployment
  audit.

### v2.1.10 (May 2026)

- HMAC signed AI Qnudge Protocol officially implemented.
- WCAG 2.2 Audit Compliance in process.

### v2.2 (May 2026)

= Command-Palette added

- WCAG 2.2 Audit Compliance attained - Pass Complete

---

## 20. Memory Integrity Architecture

Memory writes are **never** routed through TaskP. They go on the direct,
chain-witnessed path.

### 20.1 The Hash Chain

- File: `sage_data/memory_log/<entries>.jsonl`
- Each entry is Fernet-encrypted; the manifest is HMAC-chained.
- Tamper-evident: any prior entry's modification breaks the chain hash.
- Logged sides: `role="user"` (logged before system-prompt injection) and
  `role="assistant"` (logged at full_response completion).

### 20.2 Procedural Memory

- File: `sage_data/procedural_memory/{successful,unsuccessful}.json`
- Auto-captured on `[TASK_DONE]`: the entire tool sequence becomes a
  chain-witnessed successful procedure.
- Auto-captured on 3 identical tool failures in a turn: marked unsuccessful
  (local only, not chain-witnessed).
- Recent successful + unsuccessful procedures pre-loaded into Sage's context
  each turn (silent, never displayed).

### 20.3 Fernet Key

- File: `backend/.fernet_key`
- **Backup pair:** key + manifest + procedural files must be snapshotted
  together; losing the key makes pre-existing encrypted entries unreadable.

---

## 21. Quick Commands

# Run OracleAI

.\start.bat

# Stop a runaway turn (frontend Stop button does this)

curl -X POST http://localhost:8000/api/abort

# Daemon health

curl http://localhost:8000/api/daemon/status

# Force a chain digest now

curl -X POST http://localhost:8000/api/daemon/digest/refresh

# Force KB consolidation now

curl -X POST http://localhost:8000/api/daemon/consolidate

# Verify the chain hasn't been tampered with

curl http://localhost:8000/api/daemon/anomaly

# Overseer notifications

curl http://localhost:8000/api/overseer/notifications

# Run all verifies (regression check after edits)

cd backend
py verify_v214.py
py verify_procedural_wiring.py
py verify_v215.py

## 22. Known Quirks / Loose Ends

Procedural KB grows monotonically on the successful side. Chain-witnessed entries are never pruned (by design — provenance integrity). If the auto-injected PROCEDURAL MEMORY block in the system prompt ever feels bloated, lower the \_recent(succ, 5) count in main.py rather than touching the KB.
Electron wrapper currently requires a "dance" with the backend and frontend to get everything working; even then may be a parser issue with printing angle brackets into chat. (#43 queued for v2.2)
Printing the chat only works in light mode — letters are white in dark mode, invisible on a white printed background. Switch to light mode first. Also, the most recent prompt (user or Sage, depending on timing) may be truncated in the print output.
/api/launch-browser currently launches \_browser_test() in headless mode despite the intent to spawn a visible browser.
ResourceLockManager exists in overseer_daemon.py and tracks chat_memory.json + hash_chain.log, but no writer calls acquire()/release() yet. Intentionally passive in v2.1.9.

## 23. The Compounding Loop

This is the design intent — useful for Sage to know:

Sage emits tool tags → tools run → results feed next step.
On [TASK_DONE], the full sequence is auto-logged as a successful procedure, chain-witnessed for provenance.
Failures get auto-captured after 3 retries; successes via [REMEMBER:] capture insights the bare sequence misses.
Next turn, recent procedures get auto-injected into the system prompt.
The daemon, off-band, consolidates the KB, summarises the chain log, and watches for tamper.
Every turn that completes successfully makes the next one slightly better-informed. Every turn that fails makes future turns slightly less likely to repeat the same mistake. The whole system is built to compound — that's why integrity, provenance, and verifiability are load-bearing in the design.

## 24. Queued for v2.2

-#43 — Electron startup race fix (stretch; needs live observation)
-#68 — Unified config refactor — single source of truth in config.py + config.json (multi-session)
-WCAG 2.2 Passing Audit will cap v2.2. — Todd + Sage + Leo + Claude. - 1st audit results: 28 Passing, 13 Failing, 0 Partial, 19 N/A, 2 Unable To Audit - 9 of 13 fails rectified 5/16/26:  
1 Border contrast 1.4.11 ✅ Done
2 Confirm dialogs 3.3.4 ✅ Done
3 Skip link 2.4.1 ✅ Done
4 Dynamic page title 2.4.2 ✅ Done
5 Form labels 1.3.1 ✅ Done
6 Form Labels 3.3.2 ✅ Done
7 Form Labels 4.1.2 ✅ Done
8 ARIA live regions 4.1.3 ✅ Done
9 Oracle-eye keyboard access 2.1.1 ✅ Done
10 Semantic headings 2.4.6 ✅ Done
11 IPC stall banner E-05 ✅ Done
12 Command-Palette ✅ Done
13 Runtime Scr. Rdr Test/NVDA E-02 ✅ Done
14 Runtime Scr. Rdr Test/NVDA 3.3.3 ✅ Done

## 25. Project Principles (Carried Forward)

Reliability and Integrity as requirements are non-negotiables.
Surgical, targeted edits only — no broad rewrites without explicit approval.
Every change must be easily reversible (backups, additive patterns).
One change verified working before the next begins.
No new dependencies without explicit approval.
Do NOT modify Fernet encryption or hash chain write paths under any circumstances. Resource arbitration (overseer's lock_manager) is currently passive for exactly this reason.
OracleAI is intended for distribution. No user-specific hardcoded paths or keys. Everything optional and accessible by default.
max_tokens=-1 is the canonical unlimited sentinel — UI renders as blank, sanitized at 5 layers.
DO NOT SET SMALL LIMITS FOR ANYTHING DURING DEVELOPMENT! AIM STUPID HIGH, CORRECT LOWER AS NEEDED. STUPID HIGH. SERIOUSLY.
YES, THAT MEANS YOU, TOO.

# End of OracleAI Reference v2.2
