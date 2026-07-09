# VeridianAI — Build Provenance & Integrity

**Canonical source:** https://github.com/OmniFoxX

Official VeridianAI builds ship a **signed build manifest** (`build_manifest.json`).
It lists the product, version, build ID, and a SHA-256 of every shipped source
file, and it is signed with the maintainer's (Todd/OmniFoxX) Ed25519 **private key**.
The matching **public key** ships with the app (`backend/build_pubkey.pem`).

At startup — and any time via `GET /api/build/integrity` — VeridianAI verifies the
signature and re-hashes the shipped files, reporting one of:

- **official** — signature valid, all files match, public-key fingerprint matches
  the pinned OmniFoxX fingerprint. This is a genuine, unmodified OmniFoxX build.
- **modified** — signature valid but one or more shipped files differ from the
  manifest (the changed files are listed; changes to the encryption or CRAIID
  modules are flagged as `sensitive_modified`).
- **foreign_key** — signed, but with a key that is **not** OmniFoxX's. A fork that
  re-signs with its own key reads as foreign, never as official.
- **signature_invalid / no_manifest** — unsigned or altered manifest.

## What this does and doesn't do

This is **tamper-evidence and provenance**, not copy protection. Anyone may build
on VeridianAI — but a modified copy cannot present itself as an official OmniFoxX
build, because a fork cannot forge the maintainer's signature, and its public-key
fingerprint will not match the one published at the canonical repo. If a modified
copy misbehaves, its own integrity report identifies it as unofficial.

## Please do not modify (unsupported)

The encryption layer (`backend/atrest.py`, `backend/secret_locator.py`), the
handoff/integrity guard, and the CRAIID modules (`backend/craiid/*`) are
integrity-sensitive. Altering them is unsupported and will mark the build as
modified.

**Official public-key fingerprint:** `a6275345d8a615469f687dcb404d87cf`

> **Revoked:** `486f75266989ccdab2ed8d64eea29297` — this key was exposed on
> 2026-07-05 and is **no longer official**. Any build presenting the revoked
> fingerprint should be treated as untrusted, even with a valid signature.
