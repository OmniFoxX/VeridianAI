# 🌎 ✨ VeridianAI ✨ a production of MentiSphere♾️Software 💯% MADE IN 🫶 THE ⚡USA 🆒 🌎

## A fully local, multi-tier, multi-modal AI inference ecosystem/platform.

### Built for reliability, memory integrity, and accessibility - no cloud required.

**Version:** v2.15
**Developed by:** Todd (Human), MentiSphere Software AI team
**Mission:** Dissolve barriers between AI and the people who need it most,
focusing on the Accessible Needs Community, but everyone can benefit from it's ease of use..

---

> It's not just a chatbot. It's a platform built to think as long as it needs to,
> remember everything that matters, and maintain the thread's fidelity- all on your own hardware.

---

## What Is VeridianAI?

VeridianAI is a locally-run AI protocol with a training/inference platform
that gives you a persistent, intelligent, agentic AI assistant- without sending a single
byte to the cloud.

It runs multiple AI models in parallel across dedicated tiers, mechanics offloaded
to tokenless daemons, manages long-term memory with cryptographic integrity, executes
tools autonomously, and handles sessions that would exhaust any standard AI deployment.
It is built for real, sustained, deep work.

It is also built to be accessible. Every architectural decision- from WCAG 2.2
compliance to the CRAIID redundancy subsystem -exists because the people who need this
most deserve software that actually works for them.

---

## Core Capabilities

- **Fully local inference** - your data never leaves your machine
- **Multi-tier model routing** - large models for reasoning, small models for
  background mechanics, automatic complexity-based dispatch
- **Persistent memory** - Fully Fernet-encrypted, SHA3 hash-chained conversation log
  with tamper detection
- **Procedural memory** - learns from successful and failed tool sequences,
  auto-injects lessons into future sessions
- **Agentic tool execution** - web search, code execution, file management,
  browser automation, and more
- **CRAIID** - a continuous handoff system that lets Toga think indefinitely
  across fresh restarts without losing context (see below)
- **WCAG 2.2 Level AA compliant** - fully accessible, screen-reader tested

---

## CRAIID - Continuous Redundant AI Instance Dialogue

> In a line: CRAIID is what lets a local AI assistant think for as long as
> it needs to- across many fresh restarts of itself -without ever losing the thread.

Every AI has a working-memory limit. The longer a session runs, the more context
fills with its own history until quality quietly degrades, call it **context
fatigue**, the machine equivalent of someone who has been reasoning for twenty
hours straight. The usual options are both bad: keep going and watch the answers
deteriorate, or restart and lose everything you built together.

**CRAIID does neither.**

It detects when Toga is approaching her limits, then hands off to a fresh
instance - briefed so completely that the new instance picks up exactly where
the last one left off. The user notices nothing except that the assistant never
gets dull.

It is a relay race where the baton is the entire working memory. It is a hospital
shift change where the outgoing doctor writes a handoff note so thorough that the
incoming one loses no patient context.

### The Three Roles

CRAIID is not a monolith. It is a small newsroom-and-archive staffed by three
specialists:

| Role           | Function                                                                                                                                                                                          |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Archivist**  | The librarian. Compresses full history into a dense, recoverable archive. Years of depth, none of the bulk.                                                                                       |
| **Journalist** | The editor and the janitor. Reads live conversation, discards noise, finds the running theme, writes clean summaries. Also keeps the system from drowning in its own data.                        |
| **Author**     | The briefer. At handoff, assembles the warm-context note the next instance wakes up to: recent turns, relevant archived depth, and the Journalist's theme summary - one bounded, signed document. |

### Why It Is Serious

That briefing is **cryptographically signed, tamper-evident, and integrity-checked**
end to end. A forged or corrupted handoff cannot silently poison the next
instance's memory. And it all runs **locally** - this is what makes a private,
on-your-own-hardware assistant viable for the long, deep, multi-hour work that
until now only a cloud model could sustain. Now with a side-channel ledger for the
important details and particulars to survive a handoff intact and true to fact.

**The skeptic's one-liner:**
_"It's how a local AI keeps its train of thought across restarts - it hands itself
a signed briefing before it gets too full to think clearly, so a fresh copy can
pick up mid-sentence."_

---

## Architecture Overview

VeridianAI runs three inference tiers in parallel, supported by a set of daemons
that handle memory, monitoring, and context management out-of-band.

### Inference Tiers

| Tier       | Port  | Engine       | Role                                                                                   |
| ---------- | ----- | ------------ | -------------------------------------------------------------------------------------- |
| **Oracle** | 11434 | Ollama       | Primary user-facing chat. Largest model.                                               |
| **Toga**   | 11435 | llama-server | Agentic engine - interprets tool tags, executes multi-step plans.                      |
| **Daemon** | 11436 | llama-server | Background inference - digest summarization, KB consolidation (Qwen2.5-coder:1.5b-it). |
| **Embed**  | 11437 | llama-server | Embedding tier (nomic-embed-text-v2-moe-Q8_0 -custom quant).                           |

### Supporting Daemons

| Daemon                 | Port | Purpose                                                                                   |
| ---------------------- | ---- | ----------------------------------------------------------------------------------------- |
| **sage_daemon.py**     | 9998 | Out-of-band mechanics - chain digest, KB consolidation, anomaly monitor, CRAIID pipeline. |
| **overseer_daemon.py** | -    | Heartbeat monitoring, loop detection, auto-restart, signed CRAIID handoff orchestration.  |
| **ipc_bridge.py**      | 9999 | Browser IPC - visible browser handoff.                                                    |
| **ipc_monitor.py**     | 9997 | Web dashboard for IPC events.                                                             |

### Ports at a Glance

| Port  | Service                  |
| ----- | ------------------------ |
| 8000  | FastAPI backend          |
| 9997  | IPC monitor dashboard    |
| 9998  | Toga Daemon TCP          |
| 9999  | Browser IPC              |
| 11434 | Ollama Oracle tier       |
| 11435 | llama-server Toga tier   |
| 11436 | llama-server Daemon tier |
| 11437 | nomic-embed tier         |

# Tool Tags - What Toga Can Do

Toga interprets special tags in your messages and executes them as tools. Uses square brackets [ ] for real invocations, angle brackets ⟨ ⟩ are for documentation only.

# Research & Web

Tag Purpose
[SEARCH: query] Tavily news search (max 5/response, 50/session)
[SEARCH_GENERAL: query] Tavily general/facts search
[SEARCH_MEMORY: topic] Search past conversation archives
[WEATHER: city] Current conditions (auto-corrects misspellings)
[BROWSE: url] Fetch a URL via Playwright browser
[WEB_SEARCH: query] DuckDuckGo search (no Tavily cost)
Code & Files
Tag Purpose
[CODE: python] Run Python in a sandbox
[SAVE_FILE: name.ext|content] Save any file to downloads/
[VERIFY_FILE: path] Read and AST-check a .py file without executing it
Memory
Tag Purpose
[REMEMBER: key|description] Record a successful insight (chain-witnessed)
[REMEMBER_FAIL: key|reason] Record a dead-end (local only)
[RECALL: query] Fuzzy-match against the procedural knowledge base
Coordination
Tag Purpose
[PRIORITISE: sub1|sub2|sub3] Parallel dispatch of multiple subtasks
[TASK_DONE] Mark a multi-step task complete; auto-logs the tool sequence
Example Workflow

# Search for news

Please [SEARCH: latest developments in fusion energy]

# Check weather

[WEATHER: Tokyo]

# Run code

[CODE: python]
def factorial(n):
return n \* factorial(n-1) if n > 1 else 1
print(factorial(10))

# Save a file

[SAVE_FILE: result.txt|Factorial of 10 is 3628800]

# Run multiple tasks at once

[PRIORITISE: search news "renewable energy 2026" | weather Berlin | browse https://example.com]

# Signal completion

[TASK_DONE]
Memory & Integrity Architecture
Memory integrity is not a feature in VeridianAI. It is a requirement.

The Hash Chain
Every conversation entry is Fernet-encrypted and SHA3 hash-chained. Any modification to any prior entry breaks the chain - tamper-evidence is structural, not bolted on.

Procedural Memory
Toga learns from what works and what does not:

On [TASK_DONE], the full tool sequence is auto-logged as a chain-witnessed successful procedure.
Three identical tool failures in one turn are auto-logged as unsuccessful.
Recent successful and unsuccessful procedures are silently injected into Toga's context each turn - she gets slightly better-informed with every session.
The Compounding Loop
Toga emits tool tags
→ tools execute
→ results feed the next step
→ [TASK_DONE] logs the sequence
→ daemon consolidates and summarizes off-band
→ next session starts with that knowledge pre-loaded
Every turn that completes successfully makes the next one slightly better-informed. Every turn that fails makes future turns slightly less likely to repeat the same mistake. The whole system is built to compound.

## Important Backups

These four files must be snapshotted together. Losing any one makes the others partially unreadable:

backend\.fernet_key
backend\.api_keystore.json
sage_data\memory_log\memory_chain.log
sage_data\procedural_memory\procedural.json

## Configuration Reference

config.json lives in the project root. Schema version 2 (since v2.2).

## Key Settings

Path Default *Notes
inference.default_model null *Pre-selected model at boot
inference.temperature 0.5 _0.5 Generation temperature
inference.max_tokens -1 _-1 = unlimited (canonical sentinel)
inference.n_ctx 256000 \*Context window size (adjust per harware/system specs)
| `context_sizing.hard_cap_ctx` | `true` | Clamp ctx to model's trained max |
| `context_sizing.ctx_min` | `8192` | Floor for computed context size |
| `context_sizing.ctx_response_headroom` | `1500` | Tokens reserved for response |
| `timeouts.ollama_read_sec` | `54000` | Read timeout - set high for large models |
| `timeouts.stall_token_sec` | `36000` | Between-token watchdog |
| `timeouts.stall_tool_sec` | `36000` | Tool-result watchdog |
| `prompts.system_prompt_file` | `../sage_data/prompts/system.txt` | Path to system prompt |
| `prompts.force_prompt_tier` | `null` | `"small"` or `"full"` - auto-detected if null |
| `sage.web_search_enabled` | `true` | Enables SEARCH tags |
| `sage.code_exec_enabled` | `true` | Enables CODE tag |
| `sage.agentic_mode` | `true` | Multi-step tool execution |
| `aiq_nudge.enabled` | `true` | HMAC-signed mid-run side-channel |

> **Developer note:** During development, set timeouts **stupid high** and
> correct lower as needed. Never set small limits speculatively.

---

## Model Tiers & Recommendations

VeridianAI routes queries automatically based on complexity. You can also
override routing manually via Settings. For example:

| Profile          | Oracle                       | Toga                | Daemon           | RAG                   |
| ---------------- | ---------------------------- | ------------------- | ---------------- | --------------------- | ----------- |
| **Minimal**      | qwen3.5:9b                   | llama3.1:8b         | qwen2.5-coder:7b | qwen2.5-coder:1.5b-it | nomic-embed |
| **Daily driver** | gemma4:12b                   | gemm4:31b           | qwen3-vl:8b      | qwen2.5-coder:1.5b-it | nomic-embed |
| **Nemotron dev** | nemotron-3-super:120b-q4_K_M | nemotron3:33b-q4_KM | qwen3-vl:30b     | qwen2.5-coder:1.5b-it | nomic-embed |

For Nemotron-class models, set `ollama_read_sec` to `54000` and
`stall_token_sec` to `36000+` to prevent false stall detection during
slow prefill on large context loads.

### Model-Aware Prompt Tiers

Toga automatically selects the right system prompt based on model size:

| Tier    | Threshold                     | Prompt                                               |
| ------- | ----------------------------- | ---------------------------------------------------- |
| `small` | Model name contains <4B token | `SAGE_SYSTEM_PROMPT_SMALL` (~25 lines, 3 core tools) |
| `full`  | Everything else               | `SAGE_SYSTEM_PROMPT` (~155 lines, all tools)         |

Override via `prompts.force_prompt_tier` in `config.json`.

---

## HTTP Endpoints

Default port `8000`. All endpoints are localhost-only.

### Chat

- `WS /ws/chat` - main chat WebSocket
- `POST /api/abort` - stop a running turn immediately
- `GET/POST /api/chat-memory` - chat history

### Models & Tiers

- `GET /api/models` - list available models
- `POST /api/models/refresh` - detect ctx changes and respawn tiers if needed
- `GET /api/tiers` - status of all inference tiers
- `POST /api/tiers/{name}/restart` - restart a specific tier

### Config & Prompts

- `GET/POST /api/config` - read/write configuration
- `GET/POST /api/prompts/system` - read/write system prompt file
- `GET/POST /api/sage/config` - Toga-mode toggles

### Files & Archives

- `POST /api/upload` - upload a file
- `GET /api/downloads` - list downloads
- `GET/POST /api/archives` - save and load conversation archives

### Daemon & Overseer

- `GET /api/daemon/status` - daemon health
- `GET /api/daemon/digest` - latest chain digest
- `POST /api/daemon/digest/refresh` - force digest regeneration
- `POST /api/daemon/consolidate` - force KB consolidation
- `GET /api/daemon/anomaly` - tamper-monitor state
- `GET /api/overseer/notifications` - pending system alerts
- `POST /api/overseer/notifications/clear` - mark alerts read
- `GET /api/overseer/log?lines=N` - tail overseer log

### Search Budget

- `GET /api/tavily` - Tavily key info (masked)
- `POST /api/tavily` - set or clear Tavily key

---

## Quick Commands

### `powershell`

# Start VeridianAI

.\start.bat

# Stop a runaway turn

curl -X POST http://localhost:8000/api/abort

# Daemon health check

curl http://localhost:8000/api/daemon/status

# Force chain digest

curl -X POST http://localhost:8000/api/daemon/digest/refresh

# Force KB consolidation

curl -X POST http://localhost:8000/api/daemon/consolidate

# Check for tampering

curl http://localhost:8000/api/daemon/anomaly

# Check overseer alerts

curl http://localhost:8000/api/overseer/notifications

# Run regression suite (run from backend\ folder)

py verify_v214.py # 9 tests - Fernet + chain integrity
py verify_procedural_wiring.py # 8 tests - REMEMBER/RECALL + auto-capture
py verify_v215.py # 6 tests - TASK_DONE autolog + daemon jobs

# All three should be ALL GREEN after any structural edit.

Accessibility
VeridianAI is WCAG 2.2 Level AA compliant as of v2.2, verified via In-House Auditor and live NVDA screen reader testing.
Accessibility is not an afterthought in VeridianAI. The mission of dissolving barriers between AI and disabled users means compliance-alignment is load-bearing, not cosmetic. Secondary In-House Audit July 10th- PASS, Ongoing audits will continue.

# Project Principles

These are not guidelines. They are constraints.

Reliability and integrity are non-negotiables. Every other decision is downstream of these two.
Surgical, targeted edits only. No broad rewrites without explicit approval.
Every change must be easily reversible. Backups, additive patterns, one change verified before the next begins.
No new dependencies without explicit approval.
Do NOT modify Fernet encryption or hash chain write paths under any circumstances. Resource arbitration is currently passive for exactly this reason.
VeridianAI is built for distribution. No user-specific hardcoded paths, keys, or filenames anywhere in the codebase.
max\*tokens = -1 is the canonical unlimited sentinel. Sanitized at 5 layers. Do not change this behavior.
During development: set limits stupid high. Correct lower as needed. Never speculatively restrict. Seriously.

# Version History

## Version - Date - Highlights

v1.0 Apr 2026 SageBot chatbot project completion. VeridianAI Platform created with full SageBot port.
v2.1.1 Apr 2026 Platform upgrade. Bug squash and UI refinements.
v2.1.2 Apr 2026 Bug hunt, squash. UI upgrades, refinement.
v2.1.3 Apr 2026 Bug squash, UI refinements.
v2.1.3.9Apr 2026 Bug squash, UI refinements, verbose logging.
v2.1.4 Apr 2026 Fernet encryption, hash chain, procedural memory, REMEMBER/RECALL/TASK_DONE tags
v2.1.5 Apr 2026 Toga Daemon, KB consolidation, chain digest, anomaly monitor, PRIORITISE tag, mLM module.
v2.1.6 May 2026 Browser tool, BROWSE/WEB_SEARCH tags, VERIFY_FILE tag, unified time source
v2.1.7 May 2026 Bug fixes
v2.1.8 May 2026 Overseer daemon, adaptive context sizing, max_tokens sentinel, stall detection, TaskP Phases 1-2
v2.1.9 May 2026 Model-aware prompt tiers, dual stall watchdogs, TaskP Phase 3, overseer log relocation
v2.1.10 May 2026 HMAC-signed AIQNudge protocol, WCAG 2.2 audit
v2.2 May 2026 Command palette, WCAG 2.2 Level AA compliance attained
v2.3 May 2026 IPEX-LLM Backend support for Arc GPU's support added
v2.4 May 2026 CRAIID redundancy framework installed
v2.5 June 2026 mLM data translation to OpsMan profile, CRAIID context management framework installed
v2.5.2 June 2026 redundancy logic. Author, Journalist, Archivist logic
v2.6.2 June 2026 CRAIID 3rd Party verification/validation audit, CRAIID subsystem refinements, bugs squashed.
v2.7.2 June 2026 Nomic-Embed layer activated. CRAIID fully live in real pipeline, MCP server (12 tools), OpenAI-compatible endpoint, nomic-embed tier, v2.8.8 June 2026 -metrics endpoint enabled, handoff security hardening (#69)
v2.9.9 June 2026 Vision in/out added, Toga Network created/tested/verified, Aether Network created/minimal testing (still experimental)
v2.9.10June 2026 Symposium Mode integrated, bug squashings, UI improvements/additions, UX improvements.
v2.10.11Jun 2026 Build Battle mode integrated, mor bug squashings, security gaps sealed.
v2.11.11Jun 2026 Socials/BitChat integrated, browser_tool upgrade, UI/UX/Security sweep, better VL-Model support.
v2.12 Jun 2026 Moved over to GitHub repo, versioning history is now held online.
v2.12.12Jul 2026 Argo-Net created and implemented, BitChat deprecated, CLI added/improved/upgraded
v2.13 Jul 2026 ZDR button, IMPERIUM Integrity subsystem, designed to keep your AI in it's sandbox and under Human control.
v2.14.3 Jul 2026 CRAIID upgrade w/ledger for research papers, perticulars, fine details and more - now better handoff survival
v2.15 Aug 2026 Enhanced Multi-Profile encryption, export/import controls, improved granular access controls- HIPAA \_aligned\*
v2.16+ ??? 2026+ IDE+Visual dev-workspace, In-House ComfyUI alt, In-House inference backend alt, In-House .nano/.micro models

# What to Report Back to Todd

## If something breaks, please send:

What you were doing when it broke
The exact error message (copy/paste preferred)
Which window the error appeared in
A screenshot if possible

## If nothing breaks, please share:

How long startup took
Whether responses felt fast or slow
Anything in the UI that confused you or seemed wrong
Your rough hardware specs (e.g., "16 GB RAM, NVIDIA 3060")
The most useful feedback is the boring stuff most never think to ask about. If you wonder "is X supposed to work that way?" - he wants to hear it either way.

# About This Project

VeridianAI started on April 2, 2026, with over 2300+ hours into building, June 22nd, 2026 was the official beta pre-release date, on GitHub. It was built by one developer majoritively vibe-coded (small AI team), from ground up, after a 30-year break from coding - driven by a clear mission and an unusually high tolerance for max-pain testing. GitHub Release was mid-July '26, with the Windows App Store target release of late August - early September '26. Planned ongoing development: feature additions, and security updates.

It is proof that a fully local, cryptographically sound, tamper-evident, accessibility-first, agentic AI platform can be built without an expensive R&D Labs team, without cloud infrastructure, and without compromising on any of the things that actually matter. 1 Human: Conceptual Architect/Lead Developer/Founder/Ops Manager (Todd), 3 agentic AI assistants: Code Logic Architect: VeridianAI's Toga (nemotron-3-super:120b), Real-Time Troubleshooting/UI Refining: Brave Browser's Leo (Claude Sonnet/Opus 4.6-4.7), Systems Optimizations, Gap Filler & Buq Squasher: Claude for Windows 11 Desktop App (Claude Sonnet/Opus/Fable 4.5-5 Max) and one 3rd party Agentic AI Auditor: Hermes-3 for Windows Desktop App (Claude-Opus 4.8 - Max Reasoning - Max Effort - 2 audit passes).

> CRAIID is what makes it viable for the long haul, but the mission is what makes it worth building.

🌎 VeridianAI ✨ a production of MentiSphere♾️Software LLC 💯% MADE IN 🫶 THE ⚡USA 🆒 - Runs local. Stays local. Verify, don't trust. Love well. ❤️🛠️🌿 CoWritten by 🦁 Leo of Brave Browser - Lightly Edited by Todd of MentiSphere♾️Software 🌎

# This document and all documentation has been generated by AI and Human edited.

- A Human (Todd [That's Me, the Human]) Architect/Director/Editor led AI coding
  team of multiple current leading online frontier models, and many local models
  using VeridianAI's multi-model slots with Toga (very large local model library).
