# Release checklist — order matters

Written down because the order is not guessable and gets re-derived every time.

## Both trees, every release

```
1.  edit code
2.  bump_version.bat                     (in EACH tree - they bump separately)
3.  python backend\build_integrity.py genmanifest
4.  package:
      portable  -> zip the tree / tools\make_release.ps1
      store     -> npm run build-store    (runs verify_store_build.js first)
5.  python backend\build_integrity.py verify      -> expect "official"
```

**Step 3 must come before step 4.** `build_manifest.json` is listed in
`extraFiles`, so electron-builder copies whatever exists at build time. Generate
the manifest afterwards and the package ships the previous one, and the app
reports itself `modified` on a build where nothing is actually wrong.

**Step 3 must run once per tree, from that tree's own copy of the script.**
`build_integrity.py` derives the project root from its own location, so running
the Store tree's copy manifests the Store tree only.

## The three commands

| Command | When |
|---|---|
| `genmanifest` | after **every** change to a hashed file, before packaging |
| `verify` | after installing, to confirm `official` |
| `selftest` | only when changing `build_integrity.py` itself |

### Never run `keygen` again

It was run once (2026-07-05). The private key lives at
`sage_data/.oai_signing_key.pem` — outside the project, like the Fernet key —
and `backend/build_pubkey.pem` ships with the app so it can check its own
signature.

Re-running `keygen` rotates that key. Every previously released build then
verifies against the wrong public key and reports **`foreign_key`** — which is
the status meaning "signed by someone who is not the publisher". There is no way
to un-rotate it. It is guarded against overwriting without `--force`; do not
supply `--force`.

**Back up the private key.** Lose it and you cannot sign another release under
the same identity. It belongs with the Fernet key and the memory chain in the
same backup set.

## Which files are hashed

`.py .js .html .css .bat .ps1 .vbs .webmanifest`, minus `EXCLUDE_DIRS` and
`EXCLUDE_FILES` in `build_integrity.py`.

Not hashed, so they never require a re-manifest: `.md`, `.txt`, `.json`,
`.gguf`, models, and everything under `electron/` (its JavaScript is packed into
`app.asar` at build time and is not present loose in an installed package —
hashing it made every build read as `modified`).

**The rule that keeps tripping this:** excluding a file from `extraFiles` and
excluding it from the manifest are two halves of one decision. A file that is
hashed but not shipped is reported missing, and no amount of re-running
`genmanifest` will fix it — the manifest is describing a file the package
cannot contain.

---

# Build supply-chain posture (npm)

Written 2026-08-04, the day the "Shai-Hulud" / `keyv` worm compromised ~434 npm
packages across 1,300+ versions by hijacking one maintainer's GitHub account.

## The one fact that matters most

**VeridianAI ships no npm dependencies at all.**

`resources/app.asar` contains exactly five entries — `main.js`, `first_run.js`,
`preload.js`, `assets/`, `.backend_mode`. There is no `node_modules` in the
packaged application. Every npm package in this project is **build-time only,
on the developer's machine**.

So an npm compromise is a *developer* risk (credential theft, poisoned build
output), never a risk to someone who downloaded VeridianAI. Keep it that way:
if a runtime npm dependency is ever added, this document is wrong and the
threat model changes completely.

## Controls

| Control | Where | What it stops |
|---|---|---|
| `ignore-scripts=true` | `electron/.npmrc` | `preinstall`/`postinstall` execution — the mechanism this entire worm class uses |
| `audit-level=high` | `electron/.npmrc` | Installing over a known high/critical advisory |
| `save-exact=true` | `electron/.npmrc` | `npm i <pkg>` quietly widening a version range |
| Exact pins (no `^`) | `electron/package.json` | Silent minor/patch drift on the next resolve |
| `overrides` | `electron/package.json` | Forcing a patched transitive dep without waiting upstream |
| `npm ci` (never `npm install`) | `npm run deps` | Resolving anything the lockfile did not already pin |
| Committed `package-lock.json` | git | The pin itself, and Dependabot's input |
| Signed build manifest | `backend/build_integrity.py` | A poisoned build tampering with shipped source |

### Why `ignore-scripts` is free here

Verified 2026-08-04 across all 314 installed packages: the **only** package
declaring an install-lifecycle script is `electron-winstaller@5.4.0`
(`node ./script/select-7z-arch.js`), and that exists for **Squirrel**
installers. This project builds **NSIS + MSIX**, so it never runs.

`electron@42.7.1` and `electron-builder@26.15.3` declare no install scripts at
all. Nothing is lost.

If a future dependency genuinely needs its install script, do **not** disable
this globally. Run it for that one package, deliberately:

```
npm rebuild <package>
```

### What was removed

`package.json` previously carried:

```json
"allowScripts": { "electron-winstaller@5.4.0": true }
```

`allowScripts` is a **`@lavamoat/allow-scripts` convention**. That package was
never installed, and npm silently ignores unknown top-level fields — so this
enforced **nothing** while reading like a script allowlist. It was removed;
`.npmrc` now does the job for real. Adding `@lavamoat/allow-scripts` later is
reasonable, but it is a *second* layer, not a replacement for `ignore-scripts`.

## Install workflow

```bash
cd VeridianAI_v2.12/electron

# Normal path — installs the lockfile EXACTLY, runs no scripts.
npm run deps            # = npm ci

# After deliberately changing package.json (new dep, new override):
npm install --ignore-scripts    # regenerates package-lock.json
git add package.json package-lock.json && git commit
```

`npm ci` fails loudly when `package.json` and `package-lock.json` disagree.
That is the desired behaviour: a mismatch means something changed that nobody
reviewed.

Never run a bare `npm install` / `npm update` during an active ecosystem
incident. Those are the only commands that can pull a newly published
malicious version.

## Assessment: `fast-uri` (Dependabot #16, High, Development)

**Not exploitable in VeridianAI. Patched anyway because it was free.**

- **Path:** `ajv@8.20.0 → fast-uri@3.1.4`. `ajv` is electron-builder's build
  **config validator**. Marked `dev: true` in the lockfile.
- **Never ships** — see the top of this document.
- **The vulnerability requires a specific usage pattern we do not have.** It
  bites when `fast-uri` is used to enforce *host-based policy* (allowlist,
  denylist, SSRF/loopback filtering, redirect validation) and the same URL is
  then handed to Node's WHATWG `URL`/`fetch`, which treats `\` as `/` for
  special schemes. The two parsers disagree on the host, and the check is
  bypassed.
- **VeridianAI's host policy is entirely Python-side** — `net_guard.safe_urlopen`,
  `skill_api._pinned_get` (DNS-rebind-safe), `ip_access.py`, and the
  `_safe_ns` / `_within` guards in `main.py`. No JavaScript in this project
  performs host validation, and `ajv` never sees a user-controlled URL — it
  validates the electron-builder config in `package.json`.

Cleared via `"overrides": { "fast-uri": ">=3.1.5" }`.

## What provenance will and will not do

`npm audit signatures` (`npm run deps-verify`) is worth running, but be clear
about its limits: the August 2026 packages were published through **GitHub
Actions with valid provenance**. The attacker pushed to `main` and cut a
release, so the signatures were genuine. Provenance proves *where* a package
was built — not that its contents are trustworthy.

The controls that would actually have helped: `ignore-scripts`, a committed
lockfile, and `npm ci`.

## Blast radius

The build machine also holds `sage_data` — the Fernet key, the Ed25519 build
signing key, and API tokens. A credential-stealing worm there is far worse than
one in a scratch checkout. Longer term, building in a container or dedicated VM
is the control that bounds this properly.
