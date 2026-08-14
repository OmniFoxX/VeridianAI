# VeridianAI Authentication Policy (v2.13.5)

Design + implementation record for the password policy, MFA, and FIDO2/passkey
support. Written for the WCAG/security audit trail; the HIPAA-relevant angles
(access control, transmission security, integrity) are called out inline.

Everything below works **fully offline**. There is no call to any cloud
service anywhere in the auth path — blocklist checks, TOTP math, and FIDO2
assertion verification all happen on this machine against locally stored
material, consistent with VeridianAI's local-first design.

## 1. Password policy (NIST SP 800-63B Rev.4 aligned)

Implemented in `backend/password_policy.py`, enforced inside
`users.create_user` / `users.set_password` (single choke point — every
endpoint that sets a password inherits it: first-run owner setup, owner
"create user", change-password).

| Rule | Value | NIST rationale |
|---|---|---|
| Minimum length | 16 code points (`password_min_length` knob, default 16, floor 8) | Length is the primary control |
| Maximum length | 256 code points | "64+" honored; bounded so scrypt input can't be a CPU-DoS vector |
| Character set | Full Unicode incl. spaces; length counts code points | Passphrases and non-Latin scripts are first-class |
| Composition rules | **None** | Forced Upper/digit/symbol pushes people to `Password1!` |
| Rotation | **None** | Change only on evidence of compromise (see §3) |
| Reject list | Local blocklist + app-name substring + username substring + algorithmic junk | "Compare against known-compromised values" — done offline |

The reject list has four layers:

1. `backend/password_blocklist.txt` — offline list of common/breached
   passwords, matched lowercase, raw AND with spaces/`-`/`_`/`.` stripped
   (so `correct horse battery staple` and `correct-horse-battery-staple`
   both hit). Deployments can extend it by dropping a
   `password_blocklist.txt` into `sage_data` (survives upgrades; never
   requires touching the project).
2. App-name substring, case-insensitive: `veridianai`, `veridian`,
   `oracleai`. Deliberately NOT `toga`/`sage` — 4-letter dictionary words
   inside innocent passphrases ("photographer", "message") would
   false-positive.
3. Username substring, case-insensitive (usernames ≥ 4 chars; exact match
   rejected at any length).
4. Algorithmic junk that no finite list catches: single repeated character,
   short patterns repeated to fill the string (`abcabcabc…`), and
   alphabet/digit/keyboard walks (`qwertyuiop…`, `1234…`), forward or
   reversed.

**Not done, deliberately:** Unicode normalization (NFKC) before hashing.
Changing it now would invalidate every existing hash. What you type is what
is hashed, byte-for-byte (UTF-8), same as before this change.

## 2. Accessibility (WCAG 2.2 — 3.3.8 Accessible Authentication)

* **Paste always works.** No `onpaste` blocking anywhere; password-manager
  workflows are the *recommended* path given the 16-char minimum.
* **Correct autocomplete attributes** on every field: `username`,
  `current-password`, `new-password`, and `one-time-code` on the TOTP/
  recovery inputs — so password managers and (in PWA mode) browser autofill
  identify each field correctly.
* **No CAPTCHA, no cognitive tests, no memory puzzles** at login. Rate
  limiting is the existing IP-based `AbuseGuard` (machine-side, invisible to
  a legitimate user).
* **The strength meter is advisory, never a gate** (aria-live, non-blocking):
  it renders the server validator's verdict but the only hard failures are
  the policy rules themselves, reported as plain-language error text in the
  existing `role="alert"` slots.
* Existing conventions carried through: `data-tip` tooltips (not `title=`),
  `aria-label` on icon buttons, `window.oracleConfirm` (never native
  `confirm()`), brand-gold buttons keep the 9.3:1 dark-ink pairing.

## 3. Migration: force change at next login

Existing accounts with pre-policy passwords (`1`, `a`, …) are **not** locked
out and **not** silently grandfathered:

1. Login verifies the password as always (constant-time, unchanged hashes).
2. The only moment the backend legitimately holds the plaintext is login —
   so it re-grades the (correct) password against the current policy right
   there. This is NIST's "check at login" pattern, not rotation.
3. A failing password still signs in, but the session is minted
   `must_change=True`. The gate middleware confines such a session to the
   auth surface (`403 password change required` on everything else), and the
   UI routes straight into a mandatory change-password modal — no cancel, no
   click-away. Server-enforced, so a hand-crafted client can't skip it.
4. The change-password response mints a fresh, unconfined session.

## 4. MFA

### TOTP (RFC 6238, pure stdlib)

* 20-byte random secret, base32, SHA-1/30s/6-digit — compatible with every
  authenticator app; enrollment shows the secret + `otpauth://` URI for
  manual entry (works with zero network; QR rendering intentionally skipped
  — no offline QR lib in the stack, revisit if family testers ask).
* Enrollment is two-phase: the secret stays **pending** until the user
  echoes a valid code, only then is it trusted.
* Replay protection: each accepted code burns its time-step counter; the
  same code can never verify twice. Window is ±1 step for clock skew.
* Verified against RFC 6238 Appendix B test vectors in `test_mfa.py`.

### Recovery codes (the lockout answer)

* 10 single-use codes (`xxxxx-xxxxx`, unambiguous alphabet), minted with the
  account's FIRST MFA method and **shown exactly once** in a popup (copy-all
  button; the user confirms "I saved these codes").
* Stored as SHA-256 digests only (high-entropy random input — stretching is
  for low-entropy human secrets), compared constant-time, consumed on use.
* Regeneration requires the password and kills all old codes.
* Codes are cleared automatically when the last MFA method is removed.

### Lockout escape hatches, in order

1. Recovery code at the sign-in second step.
2. Owner resets any profile's MFA: Users panel → "Reset MFA"
   (`/api/auth/users/mfa-reset`, owner-gated; password survives).
3. Owner locked out of MFA with no codes left: physical access to the box =
   `python tools/reset_mfa.py <username>` — the same trust anchor the
   Fernet keys already rely on.

### Login flow with MFA

Password verify → (access-controls gate, unchanged) → if the account has MFA,
**no session is minted**; the server returns a 5-minute single-use challenge
token. The second step (`/api/auth/mfa/verify` for TOTP/recovery,
`/api/auth/fido2/verify` for keys) consumes the token and mints the session —
carrying any access-controls TTL cap and the `must_change` flag across the
hop. MFA failures feed the same `AbuseGuard` as password failures.

## 5. FIDO2 / passkeys — implementation path decision

Two candidate paths were evaluated:

**(a) Browser WebAuthn (`navigator.credentials`)** — requires a secure
context. An Electron renderer on plain `file://` (or plain-http localhost)
does not reliably provide one; making it work means
`protocol.registerSchemesAsPrivileged` with a custom `standard+secure`
scheme, migrating every renderer URL onto it, and then still terminating the
WebAuthn ceremony in the renderer — the OPPOSITE side of the trust boundary
from where VeridianAI keeps every other secret.

**(b) Backend-owned CTAP2 via `python-fido2` (Yubico's own library)** — the
Python backend talks to the authenticator directly and verifies assertions
against public keys stored beside the other auth material.

**Chosen: (b).** Rationale:

* **Fits the architecture.** VeridianAI's backend already owns all crypto
  (scrypt store, Fernet at-rest, Ed25519 build provenance). Passkey
  verification joins the same trust domain instead of splitting auth between
  renderer and backend.
* **No Electron surgery.** The secure-context problem is bypassed entirely,
  not worked around — nothing to maintain when Electron changes scheme
  privilege semantics. (Audit note: **no custom-scheme workaround exists in
  the codebase**; there is nothing extra to review on the Electron side.)
* **Works for the PWA/headless surfaces too** — any client that can reach
  the local API gets key support, not just the Electron shell.
* **Local-only verification falls out naturally**: `fido2.server` verifies
  signatures against OUR stored public keys; attestation is not chased to
  any CA; no Yubico cloud, no internet.

Tradeoffs accepted, and how they're handled:

* **Windows blocks raw HID for FIDO devices** (non-admin, Win10 1903+). The
  sanctioned path is the platform WebAuthn API; `python-fido2`'s
  `WindowsClient` wraps `webauthn.dll` and pops the native Windows Security
  touch/PIN dialog. `mfa._get_client()` prefers it when available, so on
  Windows the UX is the familiar OS dialog — arguably better than an
  in-page ceremony.
* **Linux/macOS** fall back to raw CTAP-over-HID (Linux may need the
  standard udev rules for the key). PIN-protected keys on the raw path get
  the PIN via an optional field in the UI, passed per-operation and never
  stored.
* **Blocking waits** (touch): endpoints run the ceremony in a worker thread
  (`run_in_threadpool`), keeping the event loop free.
* `rp_id` is `localhost` / origin `https://localhost` — a constant of the
  local-first design; credentials enrolled here are for THIS install.
* python-fido2 is an **optional dependency** (in the curated requirements,
  but the backend boots and everything else works without it; the FIDO2
  endpoints report "not installed" cleanly).
* **Version compatibility (post-ship fix, 2026-07-19):** python-fido2 2.x
  moved `WindowsClient` to `fido2.client.windows` and reworked client
  construction around `ClientDataCollector`; the original 1.x-style code
  silently fell through to raw HID (OS-blocked on Windows non-admin) and
  "Add key" failed instantly. `mfa.py` now handles both 1.x and 2.x import
  paths and calling conventions, and `test_mfa.py` drives the REAL
  register/authenticate ceremonies with a software ES256 authenticator
  (only the hardware layer faked) — so a future python-fido2 API change
  breaks the test gate, not a user's enrollment.

## 6. Storage & transmission notes (HIPAA angles)

* MFA material lives in `sage_data/.mfa.json` — OUTSIDE the project like
  `.users.json` — Fernet-encrypted at rest via `atrest.py` (its own
  domain-separated key), 0600. The TOTP secret is necessarily reversible;
  at-rest encryption + sage_data separation is the mitigation. FIDO2 entries
  hold **public** keys only. Recovery codes are digests only.
* Passwords stay scrypt-hashed exactly as before; nothing about hashing or
  verification changed. `password-check` (the meter endpoint) grades in
  memory and never logs or stores the candidate.
* Sessions: unchanged in-memory design; MFA challenge tokens are likewise
  in-memory, 256-bit, 5-minute, single-use.
* Auth cookie flags unchanged (HttpOnly, SameSite=Lax, Secure-on-HTTPS).

## 7. Files & test gates

| File | Role |
|---|---|
| `backend/password_policy.py` | Validator + strength estimate (stdlib-only) |
| `backend/password_blocklist.txt` | Offline reject list (+ optional `sage_data` extension) |
| `backend/mfa.py` | TOTP, recovery codes, FIDO2, challenge tokens |
| `backend/users.py` | Policy enforcement hook (`enforce_policy=True` default) |
| `backend/session.py` | `must_change` flag on sessions |
| `backend/main.py` | Endpoints: `password-check`, `mfa/*`, `fido2/*`, `users/mfa-reset`; login recheck; middleware confinement |
| `frontend/js/auth.js` | Meter, MFA sign-in step, Security panel, recovery popup, forced-change modal, owner Reset MFA |
| `tools/reset_mfa.py` | Offline owner rescue |
| `backend/test_password_policy.py` | 48 checks — gates policy changes |
| `backend/test_mfa.py` | 47 checks incl. RFC 6238 vectors + soft-authenticator FIDO2 ceremonies — gates MFA changes |

Both test files are hermetic (temp-dir stores, stubbed config) and must pass
before touching this surface. Live YubiKey enrollment/verify needs a human
touch and is Todd's manual step, same stance as the NVDA sweep.
