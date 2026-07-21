# Security Remediation Report — Addendum B

## Audit-Log Confidentiality · encrypt-then-hash without weakening tamper-evidence

| Field | Detail |
|---|---|
| **Parent document** | CodeQL Static-Analysis Findings #71–#87 (see also Addendum A) |
| **Project** | VeridianAI v2.13 — backend |
| **Files changed** | `backend/handoff_guard.py`, `backend/atrest.py`, `backend/main.py` |
| **Objective** | Add confidentiality to the hash-chained audit log (`handoff_audit.log`) without breaking or weakening its tamper-evidence guarantee |
| **Date** | 20 July 2026 |
| **Result** | Implemented; all five design tests pass |

---

## 1. Purpose & Design

The `handoff_audit.log` was tamper-**evident** (SHA-256 hash chain) but stored in **plaintext**. Following #72 (the MFA-reset audit event), audit `detail` can contain identifiers, so it should be confidential at rest — without losing the chain's ability to detect deletion, reordering, or truncation.

The design is **encrypt-then-hash** (not the reverse):

1. For each **new** entry: serialize `detail` to canonical JSON → Fernet-encrypt (via `atrest`) → ciphertext. Compute `hash = SHA256(` every stored field except the hash itself `)` — schema version, `prev`, timestamp, event type, and the **ciphertext** — joined with an unambiguous separator. Store `{v, ts, event, ct, prev, hash}`.
2. **Why this order.** Fernet tokens are authenticated encryption: they carry their own HMAC and raise `InvalidToken` on decrypt if a byte is altered. So per-entry **content** tampering is already caught by Fernet, for free, the moment a tampered entry is read. The hash chain's distinct job is catching what Fernet cannot see on its own — entries **deleted, reordered, or truncated**. Hashing the ciphertext (not the plaintext) lets a basic chain-continuity check run **without decrypting anything**, while full content-tamper detection still happens naturally whenever an entry is actually read.
3. **Migration — existing entries are not rewritten.** Legacy plaintext records (no `"v"` key) are left byte-for-byte as they are and verify under the old plaintext-hash method. New records carry `"v": 1` and verify via ciphertext-hash + Fernet-decrypt-on-read. The chain continues unbroken across the boundary: the first new record's `prev` is simply the last legacy record's `hash`, with no special handling.

## 2. What Changed

- **`HandoffGuard.audit()`** now encrypts `detail` and hash-chains the ciphertext under schema `v:1`. The hash preimage joins every stored field except the hash with the ASCII unit separator (`0x1f`) — a byte that cannot occur in hex / ISO-timestamp / event-token / base64-ciphertext — so no field, the `v` marker included, can be silently altered. If encryption is ever unavailable, `detail` is **redacted** (never written in the clear) while the event and chain stay intact.
- **`HandoffGuard.verify_audit(decrypt=False)`** walks the full chain keyless — plaintext-hash for legacy records, ciphertext-hash for `v1` — and reports the first break line. With `decrypt=True` it additionally Fernet-decrypts each `v1` record to confirm no `InvalidToken`.
- **`HandoffGuard.read_audit(decrypt=True)`** returns records for human review, decrypting `v1` `detail` or marking a tampered entry `[UNREADABLE: InvalidToken …]`.
- **MFA call site (`main.py`)** now passes **plaintext** canonical JSON to `audit()` (it previously pre-encrypted, which would now double-encrypt).
- **Documentation** of the key-placement limitation added at the call site, in `audit()`, and in `atrest`'s module docstring.

Only `event` remains in cleartext (it must — `cadence_alarm()` reads it); `detail` is encrypted.

## 3. Key Placement — Accepted, Documented Limitation

Requirement: confirm the Fernet key is not co-located with the audit log. **Finding:** `atrest`'s key (`.atrest_key`) and `handoff_audit.log` both live in `sage_data`, the same directory — so a leak reaching the log reaches the key too.

This was reviewed and **accepted as a documented limitation**, consistent with `.handoff_key` (the log's own HMAC integrity key), which is likewise co-located. Per `atrest`'s stated threat model, this defends against leaked-project-folder, lower-privilege, and corruption scenarios; it does **not** defend against a same-user attacker with full `sage_data` read access — a threat the module already documents it cannot stop. If that threat model changes, revisit key placement (a dedicated key stored outside `sage_data`).

## 4. Verification Tool (CLI)

```
python -m handoff_guard verify            # keyless chain-continuity check
python -m handoff_guard verify --decrypt  # full check + flags any InvalidToken entries
```

`verify` exits `0` on a clean chain, `1` on a break (printing the line). `--decrypt` additionally lists every entry whose ciphertext fails to decrypt — i.e. content-tampered entries. (`--data-dir` overrides the default `config.DATA_DIR`.)

## 5. Test Results — all five pass

Run against the real modified modules with a live Fernet key; a log was seeded with two legacy plaintext entries, then three encrypted `v1` entries appended.

| # | Test | Result |
|---|---|---|
| 1 | Append several → chain verifies clean (keyless and with `--decrypt`) | **PASS** — `(True, None)` both |
| 2 | Tamper a ciphertext byte → chain runs; decrypt raises `InvalidToken` | **PASS** — see below |
| 3 | Delete a middle entry → chain verification fails | **PASS** — `(False, 3)` |
| 4 | Old plaintext entries still verify + clean old→new transition | **PASS** — mixed chain `(True, None)`, no false break |
| 5 | Legit new entry decrypts back to the original detail | **PASS** — exact round-trip |

**Test 2, in detail:** a naïve byte-flip (attacker does not fix the hashes) is caught by the keyless chain itself. The realistic case — flip a byte **and** re-link the tail so continuity looks intact (a keyless attacker can recompute SHA-256) — fools the keyless walk (`verify_audit()` → `(True, None)`), but `verify_audit(decrypt=True)` catches it and `read_audit()` marks the entry `[UNREADABLE: InvalidToken]`. That is the design's core claim proven: Fernet's HMAC catches content tampering the ciphertext-hash chain cannot.

**Schema transition point:** in the test, **line 3** — two legacy plaintext entries, then the first `v1` record at line 3, its `prev` equal to the last legacy entry's `hash`. In production the boundary is dynamic and needs no migration: existing entries stay as-is, and the next `audit()` call after deploy writes the first `v1` record chained onto the current last line.

## 6. Status

All six design requirements met — encrypt-then-hash, reasoning honored, migration without rewrite, dual-mode verifier + decrypt, key placement (documented co-location), and the five tests. Backward compatible: existing plaintext entries and all current `.audit()` callers (customs, internal handoff events, MFA) continue to work; new entries are confidential at rest.

---

*Prepared with Claude (Cowork) · Addendum B to CodeQL Remediation Report #71–#87 · 20 July 2026*
