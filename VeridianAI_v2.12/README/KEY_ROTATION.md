# VeridianAI — Signing-Key Rotation

The build-provenance system signs `build_manifest.json` with an Ed25519 **private
key** held only by the maintainer. This doc is the exact procedure to rotate that
key — either on a schedule or (urgently) after the private key is exposed.

- **Private key:** `sage_data\.oai_signing_key.pem`  — secret, never ships, never synced.
- **Public key:** `backend\build_pubkey.pem`  — ships with the app.
- **Pinned fingerprint:** `OFFICIAL_FINGERPRINT` in `backend\build_integrity.py`.
- **Published fingerprint:** `README\PROVENANCE.md`, `README\BUILD.md`, and github.com/OmniFoxX.

> **Key fact:** a fingerprint is `SHA-256(raw public key)[:32]`. A new keypair =
> a new fingerprint. Rotating **retires the old fingerprint** — anything still
> signed with the old key reads as `foreign_key`, i.e. not official.

---

## When the private key is EXPOSED (leak / accidental sync / commit)

Treat the key as compromised **forever**. Whoever has it can sign manifests that
validate against the *old* fingerprint, so rotating is necessary but not
sufficient — you must also publicly **revoke** the old fingerprint and **purge**
the leaked copy.

### 1. Rotate the keypair
```
cd backend
py build_integrity.py keygen --force
```
`--force` overwrites the burned private key and rewrites `build_pubkey.pem`.
Copy the printed `fingerprint`.

### 2. Pin the new fingerprint in code
`backend\build_integrity.py` → set `OFFICIAL_FINGERPRINT = "<new>"`.

### 3. Update the published fingerprint
- `README\PROVENANCE.md` — new fingerprint + a **Revoked:** line for the old one.
- `README\BUILD.md` — the "Current / Revoked" note.
- ex.- github.com/OmniFoxX — (canonical copy) Publish the new fingerprint and state the old is revoked.

### 4. Re-sign the manifest
```
py build_integrity.py genmanifest
```
Rewrites `build_manifest.json` (new fingerprint + fresh signature) over the
**current** files.

### 5. Verify
```
py build_integrity.py verify      # expect "status": "official", fingerprint_matches: true
py build_integrity.py selftest    # expect SELFTEST: PASS
```

### 6. Purge the leaked copy (the part that isn't re-signing)
- **Cloud sync** (Drive/OneDrive/Dropbox): delete the file **and empty version
  history / trash** — they retain old copies.
- **Git**: deleting the file in a new commit is NOT enough — it stays in history.
  Rewrite history with `git filter-repo` (or BFG), force-push, and assume anyone
  who cloned already has it.
- Rotate anything else that shared the exposed location (Fernet at-rest key, API
  keys) if there's any doubt.

### 7. Prevent recurrence
- Never copy the private key into the repo tree to sign — `genmanifest` reads it
  straight from `sage_data`.
- Keep `sage_data` OUT of any synced folder.
- The root `.gitignore` already excludes `sage_data/` and `.oai_signing_key.pem`
  (but not `build_pubkey.pem`, which must ship).

---

## Routine (non-leak) rotation

Same as above minus the urgency: run steps 1–5, publish the new fingerprint, and
keep the old private key backed up offline only if you have a reason to (usually
you don't — a clean cutover is simpler).

---

## Rotation log

| Date       | Old fingerprint (revoked)          | New fingerprint                    | Reason                 |
|------------|------------------------------------|------------------------------------|------------------------|
| 2026-07-05 | `486f75266989ccdab2ed8d64eea29297` | `a6275345d8a615469f687dcb404d87cf` | Private key exposed (synced online) |
