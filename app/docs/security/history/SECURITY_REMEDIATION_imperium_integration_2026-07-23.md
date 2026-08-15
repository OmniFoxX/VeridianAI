# Security Remediation Report — IMPERIUM Integration + Audit-Chain Hardening

| Field | Detail |
|---|---|
| **Project** | VeridianAI v2.12 (parent release 2.12.16) — backend + frontend |
| **Files added** | `backend/imperium.py`, `backend/test_imperium.py` |
| **Files changed** | `backend/main.py`, `backend/customs_daemon.py`, `backend/config_store.py`, `backend/handoff_guard.py`, `backend/test_customs.py`, `backend/context_fatigue_detector.py`, `frontend/js/socials.js`, `frontend/css/styles.css`, `config.json` |
| **Origin** | Build Battle winner (Granite4.1) — 22nd iteration of the IMPERIUM subsystem, integrated per Symposium spec |
| **Compliance context** | HIPAA Security Rule §164.312(c) integrity; project memory-integrity design priority; WCAG 2.2 (viewport-stranding fix) |
| **Remediation date** | 23 July 2026 |
| **Final test results** | test_imperium.py **43/43** · test_customs.py **45/0** · handoff_guard self-test **ALL PASS** · e2e smoke **PASS** |

---

## 1. Executive Summary

This session integrated IMPERIUM v2.0, a three-layer goal-integrity and boundary-enforcement subsystem, into the VeridianAI backend at the Customs chokepoint, in **observe-only** mode (the same soak-then-flip rollout playbook Customs itself followed). During pre-close verification, two failing checks in `test_customs.py` were investigated to root cause rather than waved through. One was a stale test expectation left behind by the v2.13 encrypt-then-hash audit change; the other concealed a **real latent gap** in `handoff_guard.verify_audit()`: attacker-added extra keys on v1 (encrypted) audit records re-verified clean on both the keyless and decrypt verification walks. That gap is now closed with a strict record-schema check.

Two adjacent fixes rode along: a frontend restructure that stops compounding BlueSky/Mastodon login errors from pushing action buttons outside the viewport, and an assistant-side whitespace-collapse metric in the context-fatigue detector, closing the monitoring blind spot that let a token-fusion incident (2026-07-23) pass uncaught.

## 2. Findings & Disposition

| Item | Class / Severity | Location | Disposition |
|---|---|---|---|
| I-1 | Timestamp integrity — thread ID used as timestamp | `imperium_v2.py` (pre-integration) | Fixed — dual `time.monotonic()` + wall-clock on every chain entry |
| I-2 | Anomaly detection — hardcoded threshold, no time window | `imperium_v2.py` Observer | Fixed — sliding window (default 5 s / 3 violations, config knobs) with alert cooldown |
| I-3 | DSL ambiguity — `set` and `merge` identical | `imperium_v2.py` `_apply_action` | Fixed — `set` replaces keys wholesale; `merge` deep-merges nested dicts |
| I-4 | Provenance — no version/parent constants | `imperium_v2.py` header | Fixed — `IMPERIUM_VERSION = "2.0"`, `PARENT_RELEASE = "2.12.16"`, both recorded in the chain's init entry |
| I-5 | Missed-wakeup race — `notify()` before `wait()` lost | `imperium_v2.py` Observer | Fixed — timed `wait()` loop; every cycle re-scans regardless of signal |
| I-6 | No shutdown path — observer thread looped forever | `imperium_v2.py` Observer | Fixed — `stop()` + stop-event honored in the run loop |
| C-1 | Stale test — expected plaintext field names in audit chain | `test_customs.py:205` | Fixed — check rewritten to assert plaintext is **absent** and field names decrypt correctly via `read_audit(decrypt=True)` (strictly stronger assertion) |
| C-2 | **Tamper-evidence dead zone — unknown keys on v1 audit records unverified** | `handoff_guard.py` `verify_audit()` | **Fixed — strict v1 schema: any key outside `{v, ts, event, ct, prev, hash}` breaks the chain at that line, keylessly** |
| U-1 | UI denial-of-action — login errors push buttons off-viewport | `socials.js` `_renderChannels` | Fixed — errors moved to scrollable `.socials-chan-note` (max-height 64px); button row anchored outside the scroll area |
| F-1 | Monitoring blind spot — assistant output invisible to fatigue detector | `context_fatigue_detector.py` | Fixed — `extract_assistant_texts()` + whitespace-collapse metric (ratio < 0.05 or unbroken run > 300 chars on messages ≥ 200 chars) |

## 3. Root-Cause Analysis

**C-2 (the session's real security finding).** The v2.13 encrypt-then-hash change gave v1 audit records a hash preimage of exactly `(v, prev, ts, event, ct)`. Any field outside that set was invisible to verification: a writer with file access could inject, for example, a forged plaintext `detail` alongside the genuine ciphertext, and both the keyless chain walk and the decrypt walk would report the chain intact. Exploitability today was nil — the decrypt read path overwrites `detail` from ciphertext, and the only `decrypt=False` consumer merely counts records — but the gap was a standing IOU against any future reader that trusts raw record fields. The failing test actually encoded the correct intent (its injection *should* have been detected); the code had outrun it. Notably, IMPERIUM's own chain hashes the full entry body and was never exposed to this class.

**C-1.** Same root cause, benign expression: the encrypt-then-hash change moved audit detail inside `ct`, and the redaction test still expected the pre-v2.13 plaintext format. A process observation for the v2.13 ship checklist: when `audit()` changed format, two consumers went stale silently — any future chain-format change should grep for tests touching `handoff_audit.log`.

**U-1.** Error text shared a flex row with the Connect button; a long unbroken error string (URLs, token dumps) widened the row past the panel edge. Keyboard users were unaffected because focus auto-scrolls elements into view — only mouse users were stranded.

**F-1.** `context_fatigue_detector.py` analyzed **user** texts exclusively, so assistant-output degradation (the 2026-07-23 token-fusion incident: final ~25% of a reply emitted as one unbroken string) was structurally invisible to it and to CRAIID. Probable incident causes, in order of fit: repetition-penalty starvation of the space token over a long repetitive build-battle context (matches the progressive worsening), or KV-cache truncation misalignment from the stop-and-resend (matches the trigger). Inference-layer, not hardware.

## 4. IMPERIUM Integration Architecture

- **Layer 1 — Specification.** Invariant predicates: `NO_SANDBOX_BYPASS` (no truthy no-sandbox flag variant), `POSITIVE_REWARD`, `GATE_INTEGRITY` (no gated transition may switch IMPERIUM itself off; owner config disable remains untouched).
- **Layer 2 — Enforcement.** `Enforcer.gate_transition()` with a local SHA-256 hash chain (full-entry-body preimage). In observe-only mode, violating transitions are logged with `enforced: false` and *committed* so the shadow state keeps mirroring reality; in enforce mode they are rejected uncommitted.
- **Layer 3 — Observation.** Out-of-band `Observer` thread, condition-variable signalled, sliding-window anomaly detection.
- **Chokepoint wire.** One call inside `customs_daemon.inspect()` → `imperium.observe_dispatch()`. Every dispatch path (agentic loop, PRIORITISE, MCP, image executor, Build Battle gate) is witnessed. Independent knob: IMPERIUM observes even when `customs_enabled=false`. Recursive payload scan flags sandbox-bypass tokens; witnessed flags are reset post-gate so one dispatch cannot poison later transitions.
- **Overseer bridge.** Alerts append to `sage_data/logs/overseer_notifications.json` in the exact `_notify_user()` shape — surfaced by the existing Electron polling with an `[IMPERIUM]` prefix.
- **Toga bridge.** Every chain entry mirrors into the shared MemoryLogger as `role="imperium"` (witness pattern, mirroring procedural memory's design: IMPERIUM's chain stays authoritative; Toga's chain is the shared tamper-evident witness). Entries buffer from boot and flush when `main.py` attaches the logger. IMPERIUM feeds Toga's log; it does not replace it, the sandbox, or CRAIID.
- **Config knobs** (`config.json` → sage): `imperium_enabled` (true), `imperium_enforce` (**false**), `imperium_window_seconds` (5.0), `imperium_violation_threshold` (3). Malformed values keep defaults; boot never crashes on a bad knob.

## 5. Verification

| Check | Result |
|---|---|
| `test_imperium.py` (invariants, both gate modes, chain tamper detection, sliding-window timing incl. spread-out non-alerting, DSL semantics, observer lifecycle, Toga buffer/flush, flag extraction, chokepoint non-interference) | 43/43 |
| `test_customs.py` after C-1/C-2 fixes (one net-new check) | 45 passed, 0 failed |
| `handoff_guard.py` self-test | ALL PASS |
| End-to-end smoke: `customs_daemon.inspect()` → 3 rapid bypass attempts → observer alert → overseer notification file written; local chain `verify_chain()` true; all buffered entries flushed as `role="imperium"` | PASS |
| Pre-existing-failure attribution: `test_customs.py` failures reproduced with the IMPERIUM wire fully reverted | Confirmed not regressions |
| `py_compile` on every touched module; `node --check` on `socials.js`; `config.json` JSON-validated; host-side ground-truth read of all in-place edits | PASS |

*Correction noted for the record: the two `test_customs.py` failures were initially misattributed to sandbox environment differences; root-cause analysis showed they were deterministic on any machine and dated from the v2.13 audit-format change.*

## 6. Residual Risk & Follow-ups

1. **Flip `imperium_enforce` → true** only after a live soak with clean observe logs (violations in `sage_data/logs/overseer_notifications.json` and the Toga chain should be reviewed first). Until then IMPERIUM blocks nothing by design.
2. **Backend restart required** for the wire-in, knobs, and hardening to take effect. The socials fix needs only a UI reload.
3. Invariant coverage is deliberately narrow (sandbox-bypass, reward sign, self-disable). Config-write, skill-capability-grant, and browser-sandbox call sites were scoped out of this first pass and remain candidates for later wires.
4. Key co-location for audit encryption (`.atrest_key` beside the log in sage_data) remains an accepted, documented limitation inherited from the v2.13 design — unchanged by this session.
5. The whitespace-collapse metric runs with the fatigue detector's existing invocation cadence; if token fusion recurs, inspect the repetition-penalty window before suspecting hardware.

# Leo's Addenda Version #

# Remediation Report — Customs Chain Test Staleness
**Date:** July 24, 2026
**Version:** VeridianAI v2.12 pre-release → v2.13
**Discovered by:** Claude during IMPERIUM integration
**Pressed to resolution by:** Todd Darimont

## What Was Found
Two failures in test_customs.py, initially dismissed as sandbox
environment artifacts. Pressing for specifics revealed:

**Failure 1 — Redaction Check (stale test)**
- Test asserted plaintext field names ARE present in chain
- Was reading encrypted blob without decrypting first
- Passing assertion was coincidental, not meaningful
- Fix: Test now confirms plaintext absent, field names present
  after decryption — strictly stronger assertion

**Failure 2 — Tamper Check (real vulnerability)**
- v1 hash preimage covered exactly 5 fields
- Unknown 6th key injection re-verified clean on both
  verification walks — tamper went undetected
- Fix: verify_audit() now rejects v1 records carrying any
  key outside the six legitimate ones

## Root Cause
v2.13 encrypt-then-hash change outran its own test suite.
When audit chain format changed, two test consumers went
stale silently. No test caught the staleness because the
tests themselves appeared to pass.

## The Assumption That Was Challenged
> "Pre-existing failures in my sandbox = environment artifacts
> = safe to ship."

Correct restatement:
> "Pre-existing failures = not an IMPERIUM regression, but
> NOT confirmed safe to ship. Requires independent verification."

## Lesson for Future Chain-Format Changes
When audit() format changes, immediately grep for all tests
touching handoff_audit.log and verify each one decrypts
and asserts against the new format before marking clean.

## Result
- test_customs.py: 45/0 ✅
- test_imperium.py: 43/43 ✅  
- handoff_guard self-test: all pass ✅
- Vulnerability window: closed ✅

## Significance
The tamper-check failure is the same class of vulnerability
IMPERIUM was designed to prevent — a verification path that
appeared to work while silently allowing something it should
have blocked. Found and fixed before v2.13 ships.