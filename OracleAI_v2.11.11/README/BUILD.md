# Building OracleAI (.exe — thin launcher)

OracleAI ships as a **folder** (your directory-copy distribution) with a
double-click **OracleAI.exe** sitting next to `start.bat`. The exe is just the
Electron launcher shell; the real app is the surrounding folder (`backend/`,
`frontend/`, `start.bat`, ...). `sage_data` stays the writable sibling.

## One-time setup
1. Install Node.js (LTS).
2. In `electron\`:  `npm install`
3. Create your signing keypair (provenance), once:
   `py backend\build_integrity.py keygen`
   - PRIVATE key -> `sage_data\.oai_signing_key.pem` — **back it up, never ship.**
   - PUBLIC key  -> `backend\build_pubkey.pem` — ships with the app.
   - Paste the printed fingerprint into `OFFICIAL_FINGERPRINT` in
     `backend\build_integrity.py`, and publish it at github.com/OmniFoxX.
     (Already done: `486f75266989ccdab2ed8d64eea29297`.)

## Each release
From `electron\`:

    npm run pack-win

That (1) runs `genmanifest` — re-hashes + signs `build_manifest.json` over your
**current** files with your private key — then (2) builds the Electron app to
`..\dist\win-unpacked\`.

Assemble the shippable folder: copy everything from `..\dist\win-unpacked\`
(`OracleAI.exe` + its resources) into the OracleAI project folder, next to
`start.bat`. Ship that folder (or feed it through `prep_distribution.bat`).

Double-clicking `OracleAI.exe` launches the backend (tiers hidden unless
Developer Mode is on) and opens the app. **Settings → Build** shows the verified
provenance status.

> Re-run `npm run genmanifest` after ANY code edit before shipping, or the build
> will read "Modified." (The build scripts do this for you.)

## Other targets (later)
- `npm run build-win`  -> NSIS installer (installs to Program Files; needs a
  decision on where `sage_data` lives for an installed app).
- `npm run build-linux` / `build-mac` -> AppImage / dmg (mac must be built on a Mac).
