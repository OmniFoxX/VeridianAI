# CodeQL Code-Scanning Triage — 2026-07-13

**Project:** VeridianAI v2.12
**Scanner:** GitHub CodeQL (60 open alerts)
**Companion to:** `SECURITY_REMEDIATION_semgrep_2026-07-13.md`, `SECURITY_CONCEPTS_primer.md`

> **Note:** CodeQL is a different scanner from semgrep, with its **own** suppression
> mechanism. `# nosemgrep` comments do nothing here. Clear CodeQL alerts either by
> **dismissing in the GitHub UI** (Security → Code scanning → tick the box →
> **Dismiss** → pick a reason) or with an inline `# codeql[rule-id]` comment on the
> line *above* the alert (support varies — we're testing that on #62 first).
> The UI bulk-dismisses up to **25 at a time per page**, and you can **filter by
> rule** first to select a whole category at once.

---

## 1. Fixed in code this round — push, then expect these to close

These have real fixes. On the next scan most should drop off automatically. If any
still show (CodeQL can't always "see" a custom guard), dismiss them as **False
positive** with the note *"now guarded — see commit."*

| Alert | Rule | File:line | Fix applied |
|-------|------|-----------|-------------|
| #39 | Clear-text storage | sage_engine.py:2036 | Tavily key now **encrypted at rest** (`atrest.encrypt_bytes`); read via `read_file_auto` |
| #38 | Clear-text logging | browser_tool.py:612 | Removed the password from the signup log line |
| #37 | Polynomial ReDoS | main.py:6049 | `(.*?)(\]|$)` → `([^\]]*)(\]|$)` (linear) |
| #36 | Polynomial ReDoS | main.py:502 | Bounded digit counts `\d{1,4}(?:\.\d{1,3})?` |
| #4  | Overly permissive range | bitchat_guard.py:44 | Bidi/zero-width ranges rewritten as explicit `\u` escapes |
| #1, #2 | Client-side URL redirect | chat.js:761, 776 | Mimetype allowlisted + scheme guard (only `data:image/`, `blob:`, `http(s):`, same-origin) |
| #45, #46 | Path injection | sage_engine.py:1181, 1184 | `[VERIFY_FILE:]` reader now contained to project root / data dir |
| #53, #54 | Path injection | main.py:3398, 3399 | `save_to_downloads` gets a `_within()` containment check |
| #57, #58, #59, #60 | Path injection | skill_store.py:144, 148, 151 | `hid` validated (`_safe_hid`) — rejects separators/`..` before building a path |
| #62 | Full SSRF | skill_api.py:268 | Already validated; added inline `# codeql[py/full-ssrf]` **as a test** |

**Watch #62 specifically:** if it moves to *Closed* after this push, inline
`# codeql[]` comments work in your setup and we can use them elsewhere. If it stays
open, inline suppression isn't honored → dismiss via the UI instead.

---

## 2. Dismiss as **"False positive"** — real guard exists, scanner can't see it

Filter/select these and dismiss with reason **False positive**.

**Server-side request forgery** (filter by rule → dismiss all that remain):

| Alert | File:line | Why it's safe |
|-------|-----------|---------------|
| #7, #64, # | skill_api.py:233 | `_validate_external_url(base)` runs first (blocks private/loopback/link-local/reserved/multicast/unspecified); owner-only route |
| #5, #6 | relay_client.py:22, 32 | `relay` is validated in `skill_api` before `RelayClient` is built. Left the transport unrestricted **on purpose** so local/LAN Aether relays keep working |

**Path injection** (these specific alert numbers — the rest of this rule were fixed above):

| Alert | File:line | Why it's safe |
|-------|-----------|---------------|
| #41, #42, #43, #44 | sage_engine.py:483, 486, 510, 511 | `_safe_archive_name()` reduces to a basename + enforces `archive_*.json` |
| #47, #48, #49 | sage_engine.py:1627, 1630, 1635 | `save_to_downloads` already has a `relative_to()` containment check |
| #50, #51, #52 | main.py:3356, 3370, 3371 | Filename reduced to `Path(filename).name` (basename) before use |
| #40 | atrest.py:165 | Generic file-reader utility; its callers pass basename-guarded / fixed paths |

**Shell command from environment:**

| Alert | File:line | Why it's safe |
|-------|-----------|---------------|
| #3 | electron/main.js:318 | `spawn('cmd.exe', ['/c', resolvedBat, ...])` — args passed as an **array** (no shell string), and `resolvedBat` is verified with `existsSync` right above. Not injectable |

---

## 3. Dismiss as **"Won't fix"** — accepted low risk

**Build-Battle gate paths** (owner-configured, internal feature):

| Alert | File:line | Note |
|-------|-----------|------|
| #55, #56 | main.py:4036, 4204 | Path comes from an **owner-set** `gate_test` config; the owner can run code on their own machine anyway |

**Information exposure through an exception** (~28 alerts — the whole rule group):

> In the UI, **filter by rule "Information exposure through an exception"**, select
> all on the page, and dismiss as **Won't fix**. Repeat per page (25 at a time).

These return exception text to the client (e.g. `return {"reason": "...%s" % e}`).
Real but low-severity for a local app, and there are ~28 of them across `main.py`,
`skill_api.py`, and `bitchat_winrt_gateway.py`. **Your call:** dismiss them now as
above, *or* I can batch-harden them later with one small helper (log the full error
server-side, return a generic message to the client) so they're fixed rather than
dismissed. Say the word and I'll do that pass.

---

## 4. Summary

| Action | Count |
|--------|------:|
| Fixed in code (should auto-close) | 15 + #62 test |
| Dismiss — False positive | 12 |
| Dismiss — Won't fix (incl. ~28 info-exposure) | ~30 |

After you push tonight and CodeQL re-runs, the "fixed" group should shrink the open
count on its own; then the two dismissal passes (filter SSRF → FP, filter
info-exposure → Won't fix, plus the handful of specific path-injection numbers)
clear the rest. That takes you from 60 open to a clean board with a documented
reason on every closed item — which reads as *on top of it*, not hand-wavy.

## 5. Verification
All changed files pass `python3 -m py_compile` / `node --check`, and the rewritten
regexes + the `hid` guard were runtime-tested (guard blocks `../etc`, `a/b`, `..`,
`x\y`; ReDoS patterns still match normal input).

**Files changed this round:** skill_api.py, sage_engine.py, browser_tool.py,
main.py, bitchat_guard.py, skill_store.py, frontend/js/chat.js.
