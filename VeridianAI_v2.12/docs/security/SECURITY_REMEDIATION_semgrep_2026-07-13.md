# Security Remediation Log — Semgrep High-Severity / High-Confidence Findings

**Project:** VeridianAI v2.12
**Date:** 2026-07-13
**Scanner:** semgrep.dev (free tier report, manually compiled)
**Remediation by:** Claude (in-codebase context)
**Threat-model note:** Handled with a lean toward *real fixes* rather than
suppression wherever a finding could plausibly touch **HIPAA** scope
(ePHI confidentiality/integrity, transmission security, access control),
per your near-term compliance goal. Findings that are genuine scanner noise are
annotated with a justified `# nosemgrep` so the scan goes green **with a paper
trail**, not silenced blindly.

---

## 1. Summary

| # | Finding class | File(s) | Verdict | Action taken |
|---|---------------|---------|---------|--------------|
| 1 | Path Traversal (FastAPI) | `backend/main.py` (10 sites) | **Mixed** — 1 genuinely exploitable, rest guarded-at-source | Added shared `_safe_ns()` + `_within()` guards; real sanitization of remote-supplied filename; annotated the rest |
| 2 | SSRF (FastAPI) | `backend/skill_api.py` (2 sites) | Already guarded | Strengthened `_validate_external_url` (block `0.0.0.0`/multicast); URL-encoded `hid`; annotated |
| 3 | Unsafe pickle load | `check_model.py`, `infer_plm_daemon_v2.py`, `audit_archives_personal_v2.py` | **Real (low prob., high impact)** | `weights_only=True` on every `torch.load` |
| 3b | Unsafe pickle load | `train_plm_daemon_v2.py` | **False positive** | Line is `torch.save` (not a load/RCE vector) — annotated |
| 4 | child_process injection | `electron/first_run.js` (all sites) | **False positive** | Safe usage (no `shell:true`, array args, internal cmd) — annotated |
| 5 | Insecure WebSocket | `frontend/js/chat.js` + `frontend/SAFE/js/chat.js` | **Real** | Derive `wss://` under TLS from page protocol |
| 5b | Insecure WebSocket | `bless_ble_daemon.py`, `bitchat_winrt_gateway.py` | **False positive (log string)** — but surfaced a real bind issue | Flipped `bless` default bind `0.0.0.0`→`127.0.0.1`; startup warning; annotated log lines |
| 6 | subprocess `shell=True` | `backend/comfyui_launcher.py` | **Low real risk (owner-only)** | Converted to platform-aware `shell=False` |

**Net:** 12 files changed. 3 genuinely exploitable issues fixed for real
(remote-filename traversal, plaintext WS on a `0.0.0.0` bind, `wss` upgrade),
plus defense-in-depth on the rest. All files pass `py_compile` / `node --check`.

---

## 2. Detail by finding

### 2.1 Path Traversal — `backend/main.py`
Reported lines: 2049, 2051, 2406, 2421, 2789, 2790, 2802, 2805, 2862, 3552.

The 10 sites reduce to **two root causes**:

**(a) Namespace-derived paths** (2049/2051 burn, 2406/2421 user settings).
These build `sage_data/users/<ns>/…`. `ns` is *not* raw request input — it comes
from the authenticated session, and namespaces are already sanitized at account
creation (`users._ns_for` → `[A-Za-z0-9_-]`, ≤40 chars). So these are
**guarded at the source**, but the safety lives 3 layers away from the call site.

*Fix:* added two small shared helpers to `main.py` and routed the sites through
them (defense-in-depth + makes the invariant explicit at the filesystem boundary,
which is what a HIPAA auditor wants to see):

```python
_NS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
def _safe_ns(ns):   # re-validate namespace token at each FS boundary; None passes through
def _within(child, parent) -> bool:   # resolved-path containment (symlink / '..' safe)
```

- Burn: `ns = _safe_ns(_session_ns(request))` + `_within(base, DATA_DIR/users)` check before wipe.
- Settings: `_user_settings_file()` now routes `ns` through `_safe_ns`.
- The `shutil.rmtree`/`os.remove`/read/write lines carry justified `# nosemgrep`.

**(b) Filename from an untrusted source** — **this is the one genuinely
exploitable site.** Line 2862 (`_generate_image_routed`) wrote an image using
`res.get("filename")` **supplied by a remote Aether node**. A hostile or
compromised node could return `"../../config.json"` and overwrite files outside
`downloads/`. Even though the node is token-authenticated, it is a **separate
trust domain**.

*Fix (real):*
```python
_rawfn = os.path.basename(str(res.get("filename") or ""))       # strip any path
fn = re.sub(r"[^A-Za-z0-9._-]", "_", _rawfn).lstrip(".") or "<generated>.png"
_outp = _dl / fn
if not _within(_outp, _dl):        # belt-and-suspenders containment
    _outp = _dl / "<generated>.png"
```

**3552** (account-deletion wipe) already had a containment check
(`str(d.resolve()).startswith(users_root)`). Replaced with `_within()` — same
intent but separator-safe (a bare `startswith` would treat `.../users_evil` as
inside `.../users`).

### 2.2 SSRF — `backend/skill_api.py` (lines 225, 258)
**Already guarded** — both outbound `httpx` calls are preceded by
`_validate_external_url()`, which resolves the host and rejects
loopback/private/link-local/reserved (so the cloud-metadata IP `169.254.169.254`
was already blocked).

*Hardening applied:*
- Added `ip.is_multicast or ip.is_unspecified` to the denylist — closes
  `0.0.0.0` / `::` (which route to localhost on many stacks) and `224.0.0.0/4`.
- URL-encoded the `hid` path segment (`quote(hid, safe="")`) so it can't inject
  structure into the request URL.
- Both request lines annotated.

*Residual (documented, not fixed — see §4):* the validator resolves DNS once,
so a determined attacker could attempt **DNS rebinding** between the check and
httpx's own lookup. Acceptable here because these endpoints are **owner-only**.

### 2.3 Unsafe pickle — `torch.load`
`check_model.py:36`, `infer_plm_daemon_v2.py:55`,
`audit_archives_personal_v2.py:151` all `torch.load()` a checkpoint dict.

*Why it matters despite loading "our own" models:* **Aether can share model
artifacts between nodes**, so a checkpoint is not always locally-produced.
`torch.load` unpickles arbitrary Python by default → RCE on a malicious file.

*Fix (real):* added `weights_only=True` to every load. This restricts
deserialization to tensors/basic types and blocks code execution. All three sites
only consume `checkpoint['model_state_dict']` (tensors), so this is behavior-safe.

`train_plm_daemon_v2.py:156` is `torch.save` — **serialization, not a
deserialization/RCE vector.** False positive; annotated.

### 2.4 child_process — `electron/first_run.js`
All `child_process` usage is the **safe form**:
- `execFileSync(cmd, ['--version'], …)` — no shell, fixed args, `cmd` is an
  internal literal (`py`/`python`/`python3`/`ollama`/`winget`).
- `spawn(cmd, args, …)` — **no `shell:true`**, so args are an argv array and are
  never parsed by a shell; `cmd`/`args` come from internal tool discovery.

The rule (`detect-child-process`) fires on *any* variable command. **False
positives**; annotated at both sites with the reasoning.

> Note: the report also listed `first_run.py:267` ("python") — there is no
> `first_run.py` in the tree; this is a transcription of one of the `first_run.js`
> sites above and is covered by the annotations.

### 2.5 Insecure WebSocket
**`frontend/js/chat.js:55` (+ `frontend/SAFE/js/chat.js`) — real.**
The browser hard-coded `ws://`. If the app is ever served over HTTPS (which HIPAA
requires for any network-facing deployment), the socket must be `wss://`.

*Fix:*
```js
const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
const url = `${proto}//${location.host}/ws/chat`;
```
Now the socket inherits the page's transport security automatically. Both the
live and `SAFE` copies were fixed.

**`bless_ble_daemon.py:316` / `bitchat_winrt_gateway.py:467` — log strings.**
The flagged lines are `logger.info("… ws://%s …")` — text, not live sockets.
**But** reading the surrounding code surfaced a real divergence the scanner only
pointed at sideways:

- `bitchat_winrt_gateway.py` binds WS to `127.0.0.1` (loopback) by default. ✅
- `bless_ble_daemon.py` bound WS to **`0.0.0.0`** (all interfaces) by default,
  serving **plaintext `ws://` on the LAN**. ⚠️ (Both read the same
  `BITCHAT_WS_HOST` env var — looked like a copy-paste oversight.)

*Fix (real):* flipped `bless` default to `127.0.0.1` (matches its sibling; the WS
is a same-machine bridge to the UI, so loopback is correct), added a **startup
warning** if an operator overrides it to a non-loopback host without TLS, and
annotated the log lines. See §3 for the behavior change.

### 2.6 subprocess `shell=True` — `comfyui_launcher.py:365`
Only reached for an **owner-set** `comfyui_launch_cmd` config string
(auto-detected launches return a list/filepath and take earlier branches). Low
real exploitability (owner → owner, on their own machine), but `shell=True` is
easy to remove:

```python
import shlex
proc_cmd = command if os.name == "nt" else shlex.split(command)
subprocess.Popen(proc_cmd, shell=False, …)
```
- Windows: the raw string goes to `CreateProcess` (parses quoted paths/args) but
  `cmd.exe` never runs, so `&`/`|`/`>` are not interpreted.
- POSIX: `shlex.split` into an argv list.

---

## 3. Behavior changes to be aware of (please test)

1. **`bless_ble_daemon.py` default bind is now `127.0.0.1`** (was `0.0.0.0`).
   If your topology ever relied on reaching that daemon's WebSocket from *another
   device* over the LAN, set `BITCHAT_WS_HOST=0.0.0.0` explicitly — and front it
   with TLS. For the normal same-machine setup, no change is visible.
2. **ComfyUI launch** now runs without a shell. If your `comfyui_launch_cmd` used
   shell features (pipes, `&&`, `%VAR%`/`$VAR` expansion), wrap it in a
   `.bat`/`.cmd`/`.sh` and point the config at that file instead. A plain
   `python … main.py --args` command works unchanged.
3. **`torch.load(weights_only=True)`** — if a checkpoint ever legitimately needs a
   non-tensor global, the load will raise `UnpicklingError: Weights only load
   failed…`. **Do not revert to `weights_only=False`** — allowlist the specific
   global with `torch.serialization.add_safe_globals([...])`.
4. **chat.js** now uses `wss://` when the page is HTTPS. No change when served
   over plain HTTP / localhost.

---

## 4. Residual / known limitations
- **DNS rebinding** on `_validate_external_url` (skill_api.py): the host is
  resolved once for validation; httpx re-resolves at request time. Fully closing
  this means pinning the validated IP into the httpx transport. Deferred —
  endpoints are owner-only. Worth revisiting before any multi-tenant exposure.
- **`# nosemgrep` rule scoping:** annotations use bare `# nosemgrep` (+ a written
  justification) so they reliably suppress regardless of exact rule ID. If you
  prefer rule-scoped ignores, replace each with `# nosemgrep: <rule-id>` using the
  exact IDs from your semgrep dashboard.

---

## 5. Docs-housecleaning note (bonus)
The scan tree contains **stale duplicate copies** that will keep flagging even
after these fixes:
- `uploads/main.py` (duplicate of `backend/main.py`)
- `uploads/audit_archives_personal_v2.py` (duplicate of the `backend/craiid/` one)

I fixed the **canonical `backend/` copies** only. To get a fully green scan,
either delete these stale copies or add an `.semgrepignore` for `uploads/`.

---

## 6. Files changed
```
backend/main.py                          (path-traversal helpers + 10 sites)
backend/skill_api.py                     (SSRF validator + hid encoding)
backend/check_model.py                   (weights_only=True)
backend/infer_plm_daemon_v2.py           (weights_only=True)
backend/craiid/audit_archives_personal_v2.py  (weights_only=True)
backend/train_plm_daemon_v2.py           (annotated torch.save)
backend/bless_ble_daemon.py              (bind 0.0.0.0->127.0.0.1 + warning)
backend/bitchat_winrt_gateway.py         (annotated log line)
backend/comfyui_launcher.py              (shell=True -> shell=False)
electron/first_run.js                    (annotated child_process)
frontend/js/chat.js                      (ws:// -> protocol-aware wss://)
frontend/SAFE/js/chat.js                 (ws:// -> protocol-aware wss://)
```
All pass `python3 -m py_compile` / `node --check`.

---

## 7. Round 2 — additional semgrep.dev findings (2026-07-13, later)

| Finding | File:line | Verdict | Action |
|---------|-----------|---------|--------|
| Risky cryptographic algorithm | `bitchat-python/bitchat/encryption.py:357` | **False positive** | Dismiss |
| Automatic memory pinning | `train_plm_daemon_v2.py:98` | Perf advisory (not security) | Fixed |
| Missing `Secure` cookie attribute ×3 | `main.py` auth `set_cookie` | **Real (HIPAA)** | Fixed |

**Crypto (encryption.py:357) — false positive.** The line is
`b'\x00\x00\x00\x00' + counter.to_bytes(8, "little")` — a **counter-based**
ChaCha20-Poly1305 nonce (the correct Noise-transport construction, matching the
Swift client), *not* random-module output. Deterministic counter nonces are the
right choice for AEAD (no collision risk); using `os.urandom` here would be wrong
and would break Noise/Swift interop. The file already uses `os.urandom(12)` +
`secrets` for real random material and the `cryptography` library's `.generate()`
for keys. Also: this is **vendored** BitChat library code — don't fork it. Dismiss.

**Pinning (train_plm_daemon_v2.py:98).** Not a security issue (semgrep over-rates
it "high") — it's a host→GPU transfer optimization. Applied
`pin_memory=(device == "cuda")` so it helps on CUDA and is a no-op on CPU.

**Cookies (main.py auth setup/login/change-password).** The three auth
`set_cookie` calls had `httponly=True, samesite="lax"` but no `Secure`. Added a
`_cookie_secure(request)` helper returning True when the request is HTTPS (direct
or via `X-Forwarded-Proto`), False on plain-http localhost — so the auth cookie is
never sent in cleartext on a real HTTPS deployment (HIPAA §164.312(e)) while local
sign-in still works. Applied to all 3 `set_cookie` + the logout `delete_cookie`
(so a Secure cookie clears cleanly); added `request: Request` to `api_auth_setup`,
which lacked it. Same adaptive pattern as the `wss://` fix.

> **Behavior note:** behind a proxy that doesn't forward `X-Forwarded-Proto`, the
> cookie won't get `Secure` (the app sees http). Standard proxies forward it; if
> yours doesn't, add a `cookie_secure` config override.

**Verify:** `train_plm_daemon_v2.py` compiles clean; the `main.py` cookie edits are
host-verified (the Linux sandbox mount was lagging — confirm with
`python -m py_compile backend\main.py` on Windows).

*(The GitHub CodeQL round is tracked separately in `CODEQL_TRIAGE_2026-07-13.md`.)*

---

## 8. Round 3 — 2026-07-14 (semgrep.dev 25 + CodeQL 20 remaining)

### Fixed
| Finding | File:line | Action |
|---------|-----------|--------|
| wildcard-cors (Medium) | `main.py:691` | `allow_origins=["*"]` + credentials → **loopback `allow_origin_regex`** + `VERIDIAN_CORS_ORIGINS` env override. Frontend is same-origin (StaticFiles) so unaffected; blocks external drive-by credentialed requests. |
| insecure-hash-sha1 (Medium) ×2 | `main.py:6008`, `verify_v215.py:82` | SHA1 → **SHA256**. Both are non-crypto request identifiers (`.hexdigest()[:8]`), so zero behavior change. |
| Info exposure (CodeQL) ×~7 | `skill_api.py:98` | `raise HTTPException(503, "skill service unavailable")` — dropped the `_service_err` (str(e)) leak. Every skill endpoint routed through `_guard()`, so this one line clears ~7 alerts. |

**CORS behavior note:** if you ever open the UI from a **LAN browser** (e.g. laptop → PC over Wi-Fi), add that origin via `VERIDIAN_CORS_ORIGINS=http://192.168.x.x:PORT`. Local/Electron use is unaffected. Please test login once after this.

### False positives — dismiss
| Finding | File:line | Why |
|---------|-----------|-----|
| Risky crypto (nonce) | `bitchat-python/.../encryption.py:357` | Counter nonce for ChaCha20-Poly1305 (correct AEAD/Noise construction); vendored lib. |
| contains-bidirectional-characters | `bitchat_guard.py:45` | The bidi chars ARE the detection targets — it's the control-char *sanitizer*. Intentional. |
| Info exposure (CodeQL) ×~13 | `main.py` (return helper()), `bitchat_winrt_gateway.py:487,498` | Diffuse: exception detail bubbles up *inside* helpers (`detect_hardware()`, `build_integrity.verify()`, etc.) then a route returns the dict. Fixing each = auditing many internal helpers; Medium severity, local-first, and the detail is genuinely useful to the owner debugging their own instance. **Recommend dismiss as "Won't fix."** |

### Not security / low — your call
- **automatic-memory-pinning** `train_plm_daemon_v2.py:98` — already fixed last round (`pin_memory=(device=="cuda")`); perf, not security.
- **dynamic-urllib** ×15 (`comfyui_*`, `bitchat_drift.py`, `craiid/journalist.py`) — Low. URLs are internal (ComfyUI localhost API, model downloads). Real risk (file://) needs an attacker-controlled URL. Dismiss as low, or I can add `http(s)`-only scheme guards.
- **react-unsanitized-method** `comfyui-wizard.js:466,642` — `insertAdjacentHTML` of `_buildHTML(status)`. Real-ish XSS *if* ComfyUI status (model names/errors) carries markup. Fix = escape the dynamic values inside the two builder functions. **Offered:** a focused escape pass.
- **using-http-server** `electron/main.js:445` — likely the loopback IPC/visible-browser server; loopback http is fine. Verify it's `127.0.0.1`, then dismiss.
- **missing-integrity** `index.html:16` — add SRI hash if it's a CDN asset; FP if local. Low.
- **unsafe-formatstring** `command-palette.js:31` — `console.log` concatenation. Low, likely FP.

**Verify:** `verify_v215.py` + `skill_api.py` compile clean; the `main.py` CORS/SHA256 edits are host-verified (mount lagging — confirm with `python -m py_compile backend\main.py` on Windows).

---

## 9. Round 4 — 2026-07-14 (production-grade pass; HIPAA-aligned)

Rules this round: real fixes over green-dashboard, no half-measures, no dormant
code, severity vs remediation-risk evaluated separately. Each item is **Fixed**,
**False Positive**, or **Deferred (with reason)**.

### Item 1 — DNS-rebinding pin (`skill_api.py`) — ✅ FIXED
Old `_validate_external_url` resolved once (`gethostbyname`, first record) and httpx
re-resolved separately at request time — a TOCTOU a crafted DNS answer could ride.
Replaced with `_resolve_validated()` (validates **every** `getaddrinfo` address —
closes the multi-A-record bypass — and returns the exact IP) + `_pinned_get()`
(connects to that **validated IP**, so httpx never re-resolves; preserves the `Host`
header and, for https, the `sni_hostname` extension so **TLS cert verification still
runs against the hostname**). Both browse + fetch route through it; the redundant
pre-validate calls were removed so there's exactly one resolution.
**Live-tested (httpx 0.28):** http pin ✓, https pin w/ self-signed SNI ✓ (no-SNI
control correctly rejected → cert-check is real), private IP blocked ✓, public-1st/
private-2nd multi-record blocked ✓, loopback blocked ✓. Relay path left at
boundary-validation (pinning RelayClient out of scope to avoid destabilising Aether).

### Item 2 — dynamic-urllib ×15 — ✅ FIXED
New `backend/net_guard.py::safe_urlopen()` rejects any non-http(s) scheme
(`file://`/`ftp://`/`data:`). Routed **all 15** sites across 9 files through it
(comfyui_client ×4, comfyui_models ×2, comfyui_setup ×2, comfyui_directml,
bitchat_drift, node_client, sage_engine, craiid/journalist ×2, hw_utils).
Transparent for http/https — no behavior change for ComfyUI/model-downloads/node
calls. **Tested:** allows http/https, blocks file/ftp/data + `Request(file://)`; all
9 compile; 0 raw `urlopen` remain.

### Item 3 — react-unsanitized (`comfyui-wizard.js`) — ✅ FIXED
Added `_esc()` (HTML escape) + `_escJs()` (hex-escape for the `onclick="…('${…}')"`
context — a `'` can't break out even after HTML-decode). Wrapped **all 13** dynamic
values (9 HTML-text, 5 onclick args). `_accelNote`/`DEFAULT_HINT` are static.
`node --check` passes; normal names render unchanged, markup neutralised.

### Item 4 — using-http (`electron/main.js:445`) — ⚪ FALSE POSITIVE
It's an http **client** probe, not a server: `http.get(HEALTH_URL)` with
`HEALTH_URL = http://127.0.0.1:${APP_PORT}/api/health` — loopback to the app's own
backend, not MITM-exposed. Dismiss with that note.

### Item 5 — missing-integrity (`index.html:16`) — ✅ FIXED (better than SRI)
The flagged asset was a jsdelivr **CDN** theme CSS (the hljs *library* is already
local; only the theme was remote, and `settings.js` swaps two CDN themes). Rather
than add SRI (still an external fetch), **self-hosted both themes** to
`frontend/css/hljs-github.css` + `hljs-github-dark-dimmed.css` and repointed
`index.html` + `settings.js`. Zero external theme fetches remain (offline-safe, no
third-party call). `settings.js` compiles.

### Item 6 — unsafe-formatstring (`command-palette.js:31`) — ⚪ FALSE POSITIVE
`console.warn(\`…${fnName}…\`, e)` — `fnName` is an internal function-name key, not
user input; dev-console warn, log-forging at worst, no executed path. Dismiss.

### Item 7 — diffuse info-exposure — 🟡 PARTIAL (bitchat_gw FIXED · main.py DEFERRED)
**bitchat_winrt_gateway.py:487,498 — FIXED.** Both leak `_ble_error` (was
`str(exc)`). Chokepoint = one variable, and the full exception is **already logged**
(line 458) → genericised to `"peripheral advertising failed"`, clearing both alerts
with **zero diagnostic loss** + defense-in-depth (gateway bind is env-overridable to
non-loopback). Compiles clean.

**main.py ×11 — DEFERRED (this is the rule-2 case).** `return <helper>()` boundaries
(`detect_hardware()`, `build_integrity.verify()`, …) where exception detail is
embedded *inside* each helper. On both axes:
- **No central chokepoint** — ~11 distinct helpers → scattered edits across modules +
  `main.py` (mount-fragile here; two truncation incidents this week).
- **Exploit impact ≈ nil today** — own API, same-origin frontend, owner-only,
  localhost (CORS lockdown + owner gates + WAN hardening). No external party hits them.
- **Not ePHI** — hardware/build/BLE error strings; low direct HIPAA relevance.
- **Rule #2:** genericising removes owner-useful diagnostics *without a real
  confidentiality gain* in the local-first model — satisfies the scanner, not the
  principle. Flagging per your rule #2 rather than closing with an empty change.
- Remediation risk > exploit impact → **defer.**

**Revisit trigger:** fix the moment any of these endpoints (or a gateway) becomes
**network-exposed / multi-tenant**. The fix is then the standard pattern: log full
error server-side, return a generic message. Available on command.

### No-half-measures / no-dormant-code confirmation
- `safe_urlopen`, `_pinned_get`, `_resolve_validated`, `_esc`, `_escJs` are all wired
  into the **real** call paths (every urllib site, both skill-fetch sites, every
  dynamic interpolation) — not scaffolding.
- Self-hosted CSS is referenced by live `index.html` + `settings.js` (both themes,
  both switch directions); no orphan files, no CDN ref left.
- Nothing happy-path-only: pin validates+rejects on every path (tested), urllib guard
  rejects bad schemes (tested), escapers handle null/undefined.

### 🔧 Test these manually
1. **Windows compile:** `python -m py_compile backend\skill_api.py` (host-verified;
   sandbox mount phantom-lags it). `main.py` unchanged this round.
2. **Skill browse/fetch** (if used): peer `base_url` over http, and https to a
   valid-cert peer (SNI-pin).
3. **ComfyUI:** generation, model download/install, and the **wizard + model picker**
   (renders + Use/Download/Delete buttons).
4. **Aether node:** a remote-node inference request (node_client → safe_urlopen).
5. **Code highlighting:** chat code blocks highlighted; Settings light/dark toggle
   still swaps the (now local) theme.
6. **BitChat:** normal; on BLE failure `/api/info` shows generic text (detail in log).

### Files changed (Round 4)
```
backend/net_guard.py                     (NEW — safe_urlopen scheme guard)
backend/skill_api.py                     (DNS-rebinding pin: _resolve_validated + _pinned_get)
backend/{bitchat_drift,comfyui_client,comfyui_models,comfyui_directml,
         comfyui_setup,node_client,sage_engine,hw_utils}.py  (urlopen -> safe_urlopen)
backend/craiid/journalist.py             (urlopen -> safe_urlopen)
backend/bitchat_winrt_gateway.py         (_ble_error genericised; detail stays in log)
frontend/js/comfyui-wizard.js            (_esc/_escJs on all dynamic values)
frontend/js/settings.js                  (hljs theme -> local)
frontend/index.html                      (hljs theme -> local)
frontend/css/hljs-github.css             (NEW — self-hosted light theme)
frontend/css/hljs-github-dark-dimmed.css (NEW — self-hosted dark theme)
```

---

## 10. Round 5 — 2026-07-14 (GitHub alert reconciliation)

9 alerts open after the 11 main.py info-exposure were dismissed.

**Group 2 — skill_api.py ×7 "info exposure" (254-409): FIXED.**
Correction: Round 3's "line-98 `_guard()` fix clears ~7" was **wrong** — line 98
fixed a *separate* alert (`_service_err`, since closed). These 7 are
`return s.<SkillService method>()` where the exception detail is embedded deeper.
All 7 trace to **three** `str(exc)` chokepoints (not 7 scattered edits):
- `skill_service.py:85` (fetch_object) → alerts #31(307), #33(324), #35(409 import)
- `skill_service.py:103` (browse) → #28(273), #30(290)
- `skill_trust.py:192` (verify_artifact) → #34(335 promote), #27(254 publish via store.put)

Fix: each now logs the full exception server-side (`logging.getLogger("veridian")`)
and returns a generic reason (`"fetch failed"` / `"browse failed"` /
`"verification error"`). Owner diagnostics preserved in the log; no exception text in
the response. Both files `ast.parse` clean. Should auto-close on rescan. Fixed (not
deferred like main.py) because it's a contained 3-line chokepoint AND the Aether
skill-share surface is the most likely to become network-exposed.

**Group 3 — chat.js ×2 "Client-side URL redirect": FALSE POSITIVE (dismiss).**
#69/#70 (scan lines 707/721) are the `img.src = imgUrl` (~769) and
`save.href = imgUrl` (784) sinks in `appendImageResult`, both **below** the scheme
allowlist at lines 756-758 (`data:image/`, `blob:`, `http(s):`, same-origin only;
`javascript:` etc. `return` early). Not a wss regression (that was line 55); same
guarded sinks as the old #1/#2. CodeQL can't trace the regex barrier → dismiss FP.

**Files changed (Round 5):** skill_service.py, skill_trust.py.
