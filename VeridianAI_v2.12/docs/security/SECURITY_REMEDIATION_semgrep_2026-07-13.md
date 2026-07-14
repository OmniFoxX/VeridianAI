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
