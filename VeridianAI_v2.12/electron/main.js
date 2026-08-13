/**
 * VeridianAI - Electron Wrapper
 *
 * NO VERSION NUMBER IN THIS COMMENT, DELIBERATELY.
 *
 * It used to read "VeridianAI 2.9.10", and nothing ever updated it. On
 * 2026-08-13 that line was used as the test for whether a tree held current
 * code -- reading the first 80 bytes of each main.js found on the drive and
 * comparing. Every copy answered "2.9.10", including the fully current
 * 52,800-byte file, because the comment was stale in all of them. The
 * conclusion drawn was that no tree had the current code and the files
 * needed rewriting from scratch. They did not; only the comment was old.
 *
 * A version string in a comment is a second source of truth that nothing
 * enforces, and it is worse than no version at all: absent, you go looking
 * for the real one; stale, you believe it.
 *
 * The version lives in electron/package.json, which _bump_version.py owns
 * and which becomes the MSIX package version. At runtime: app.getVersion().
 * To identify a build's actual code, compare content or size -- see
 * tools/verify_electron_payload.js.
 * ===========================
 *
 * v2.1.6 fix: previously this file spawned `python main.py` directly,
 * which started ONLY the FastAPI backend — NOT Ollama, NOT the Toga
 * llama-server tier, and NOT the Toga Daemon. That's why launching
 * Electron alone left Toga and the daemon dead, and why the manual
 * dance was needed (open Ollama, run start.bat, close popup, then
 * Electron). Now we spawn start.bat which is the canonical launcher
 * for the entire stack — Electron just becomes a thin wrapper around
 * the same boot sequence the user gets from double-clicking start.bat.
 *
 * Usage:
 *   cd electron
 *   npm install        (one-time)
 *   npm start
 *
 * No prior dance required. Electron handles the full startup.
 */

const { app, BrowserWindow, Menu, shell, dialog, session, ipcMain } = require('electron');
const { spawn, spawnSync } = require('child_process');
const path       = require('path');
const fs         = require('fs');
const http       = require('http');
const os         = require('os');

// ---------------------------------------------------------------------------
// BOOT LOG (v2.13). A packaged GUI app has nowhere to print: stdout is not
// attached to anything, and when Developer Mode is off start.bat is spawned
// with stdio:'ignore'. Five debugging rounds were lost to that silence, so
// main.js now keeps its own account of the boot on disk, from the first line
// of execution, in a location that is writable no matter how the app was
// installed. Read it with:  notepad %TEMP%\VeridianAI-boot.log
// ---------------------------------------------------------------------------
const BOOT_LOG    = path.join(os.tmpdir(), 'VeridianAI-boot.log');
// start.bat's own stdout/stderr. Previously this went to stdio:'ignore'
// whenever Developer Mode was off, which is why a backend that failed on
// startup produced literally no evidence anywhere. Now it is always
// captured; Developer Mode only controls whether a CONSOLE also appears.
const BACKEND_LOG = path.join(os.tmpdir(), 'VeridianAI-backend.log');
// v2.14.1: the boot log is written to TWO places.
//
// %TEMP% alone was not good enough. Two portable runs on 2026-08-12 -- one
// that failed to start and one that succeeded -- BOTH reported
// "not present: ...\Temp\VeridianAI-boot.log", while the app was demonstrably
// running with four VeridianAI processes and a live backend. Whatever the
// cause (an elevated launch resolving TEMP elsewhere, a cleaner, a redirected
// environment), the outcome is what matters: the diagnostic written
// specifically so that a failed boot leaves evidence left none, on the one
// occasion it was needed.
//
// So it is also written beside sage_data, which the app resolves anyway, the
// user can open from Settings, and no cleaner touches. TEMP is kept because
// early lines are logged before app.getPath() is safe to call.
let _logDir2 = null;         // resolved once the app is ready
let _logBuf  = [];           // lines emitted before that

function _bootLog2Path() {
  return _logDir2 ? path.join(_logDir2, 'VeridianAI-boot.log') : null;
}

function blog(msg) {
  const line = `[${new Date().toISOString()}] ${msg}`;
  try { fs.appendFileSync(BOOT_LOG, line + '\n'); } catch { /* never fatal */ }
  const p2 = _bootLog2Path();
  if (p2) {
    try { fs.appendFileSync(p2, line + '\n'); } catch { /* never fatal */ }
  } else {
    if (_logBuf.length < 500) _logBuf.push(line);   // bounded; boot is short
  }
  try { console.log(line); } catch { /* no console when packaged */ }
}

/** Point the second log at sage_data and flush anything buffered. */
function openDurableBootLog() {
  try {
    const dir = resolveDataDir();
    fs.mkdirSync(dir, { recursive: true });
    _logDir2 = dir;
    const p2 = _bootLog2Path();
    fs.writeFileSync(p2, '');                       // fresh file per launch
    if (_logBuf.length) {
      fs.appendFileSync(p2, _logBuf.join('\n') + '\n');
      _logBuf = [];
    }
    blog(`durable boot log -> ${p2}`);
    blog(`os.tmpdir()=${os.tmpdir()}  (copy also written there)`);
  } catch (e) {
    blog('could not open durable boot log: ' + e.message);
  }
}
try { fs.writeFileSync(BOOT_LOG, ''); } catch { /* fresh file per launch */ }
blog('=== VeridianAI boot ===');
blog(`electron=${process.versions.electron} node=${process.versions.node} ` +
     `platform=${process.platform} windowsStore=${process.windowsStore === true}`);
blog(`execPath=${process.execPath}`);

// v2.2 #74 — backend mode picker.
//
// IPEX-LLM trades a small bit of LLM speed for substantially better whole-PC
// responsiveness vs Vulkan; Todd uses both depending on what else his
// machine is doing. Picker UX is "saved preference file" per his choice:
// electron/.backend_mode is a one-line text file containing either
// "vulkan" or "ipex". Read on launch; the Backend Mode submenu writes
// it and offers to relaunch Electron so the change takes effect.
//
// Default 'vulkan' so installs without the file get the previous
// behavior. The file is ignored at the OS level (no .gitignore needed
// since Todd doesn't use git per project rules); it's user-machine
// state, not project state.
const BACKEND_MODE_FILE = path.join(__dirname, '.backend_mode');
// Resolve project root (one level up from electron/) so we can find
// config.json. Same pattern startBackend() uses to locate start.bat.
const PROJECT_CONFIG_FILE = path.join(__dirname, '..', 'config.json');
const VALID_MODES = ['vulkan', 'ipex'];

// Shared config.json reader. Returns parsed object or null on any failure.
// Electron reads this synchronously at boot before spawning the backend, so
// it MUST tolerate a missing or malformed file without throwing.
function _readConfigJson() {
  try {
    if (fs.existsSync(PROJECT_CONFIG_FILE)) {
      return JSON.parse(fs.readFileSync(PROJECT_CONFIG_FILE, 'utf8'));
    }
  } catch (e) {
    console.warn('[Electron] could not parse config.json:', e.message);
  }
  return null;
}

// v2.2 #68 Phase E Step 4: config.json is the single source of truth.
// Read priority is intentional:
//   1. config.json (v2 nested: cfg.electron.backend_mode)
//   2. config.json (v1 flat: cfg.backend_mode) — Electron may run before
//      the backend's boot-time migration on the first launch post-deploy,
//      so the v1 form still needs to be readable for one boot cycle.
//   3. electron/.backend_mode (legacy dedicated file)
//   4. 'vulkan' default — preserves pre-#74 behavior
//
// Writes still go ONLY to .backend_mode for now. main.py owns config.json,
// and racing Electron writes against main.py's load/save_config would
// require coordination that's out of scope. Once settings.js gets a UI
// for backend_mode and writes via /api/config, the .backend_mode write
// path can be retired and the legacy file deleted.
function readBackendMode() {
  const cfg = _readConfigJson();
  if (cfg) {
    // v2 nested form (post-migration)
    const v2 = cfg.electron && cfg.electron.backend_mode;
    if (typeof v2 === 'string') {
      const m = v2.trim().toLowerCase();
      if (VALID_MODES.includes(m)) {
        console.log(`[Electron] backend_mode from config.json (v2): ${m}`);
        return m;
      }
    }
    // v1 flat form (first boot after deploying #68 before backend migrates)
    if (typeof cfg.backend_mode === 'string') {
      const m = cfg.backend_mode.trim().toLowerCase();
      if (VALID_MODES.includes(m)) {
        console.log(`[Electron] backend_mode from config.json (v1): ${m}`);
        return m;
      }
    }
  }

  // Fall back to dedicated .backend_mode file
  try {
    if (fs.existsSync(BACKEND_MODE_FILE)) {
      const raw = fs.readFileSync(BACKEND_MODE_FILE, 'utf8').trim().toLowerCase();
      if (VALID_MODES.includes(raw)) return raw;
    }
  } catch (e) {
    console.warn('[Electron] could not read backend mode file:', e.message);
  }
  return 'vulkan';
}

// v2.2 #68 Phase E Step 4: read the FastAPI app port from config.json so
// the user's port setting is honored without env-var gymnastics. Mirrors
// readBackendMode's resilience: v2 nested first, defensive parse, then
// fall back to the conventional default. Same value the backend's
// config.PORT_APP uses when env var ORACLE_APP_PORT is unset.
function readAppPort() {
  const cfg = _readConfigJson();
  if (cfg) {
    const v2 = cfg.network && cfg.network.ports && cfg.network.ports.app;
    if (Number.isInteger(v2) && v2 >= 1 && v2 <= 65535) {
      console.log(`[Electron] app port from config.json: ${v2}`);
      return v2;
    }
  }
  // Conventional fallback. NOT a recommendation to the user — just what we
  // bind to if no config value is present. The user is free to change
  // network.ports.app in config.json (or via the settings UI once it
  // exposes it) and Electron will follow on the next launch.
  return 8000;
}

function writeBackendMode(mode) {
  if (!VALID_MODES.includes(mode)) {
    throw new Error(`Invalid backend mode: ${mode}`);
  }
  fs.writeFileSync(BACKEND_MODE_FILE, mode + '\n', 'utf8');
}

// Resolved once at boot. The menu shows this as the "currently selected"
// radio item; switching writes the file and prompts for relaunch, which
// re-reads the file on the next boot.
const ELECTRON_BACKEND_MODE = readBackendMode();
console.log(`[Electron] backend mode: ${ELECTRON_BACKEND_MODE} (from ${BACKEND_MODE_FILE})`);

let mainWindow;
let backendProc;     // spawned start.bat process tree (was: pythonProcess)

// v2.2 #68 Phase E Step 4: app port driven by config.json. Resolved once
// at module load (same lifecycle as ELECTRON_BACKEND_MODE) so all later
// uses — BrowserWindow.loadURL, health probe, port-conflict detection —
// see a single consistent value.
const APP_PORT       = readAppPort();
const BACKEND_URL    = `http://127.0.0.1:${APP_PORT}`;
const HEALTH_URL     = `${BACKEND_URL}/api/health`;
console.log(`[Electron] backend URL: ${BACKEND_URL}`);

// v2.1.11 health-probe tuning. Originals: POLL=500ms / per-probe-timeout=1500ms.
// That combo broke on Todd's Arc B580 + new Vulkan driver:
//   1. /api/health internally does a SYNCHRONOUS requests.get() to Ollama's
//      /api/tags inside check_ollama_health(). The sync call blocks FastAPI's
//      event loop. With the new Arc driver, that round-trip can land near
//      ~1s — fine in isolation, but right at the 1500ms probe timeout, so
//      probes consistently fail even though /api/health works (~1s in Brave).
//   2. The 500ms poll cadence kicks off a new probe before the previous one
//      finishes, and FastAPI serializes around the sync block, so probes
//      stack up and queue, making each subsequent one slower. Death spiral.
//
// New values:
//   - PROBE_TIMEOUT_MS=5000  — gives /api/health ~5x its measured response
//     time, so it succeeds even with some load.
//   - HEALTH_POLL_MS=1500    — probes never overlap, no queueing on the
//     sync Ollama call.
// Happy-case wall-clock to detect a healthy backend is still ~1-2s.
const HEALTH_POLL_MS    = 1500;
const PROBE_TIMEOUT_MS  = 5000;
const HEALTH_TIMEOUT_MS = 240_000;  // v2.11.12: was 90s. The 90s budget had
                                    // to cover start.bat's per-tier probes
                                    // (up to 90s EACH on cold Ollama) PLUS
                                    // uvicorn boot — a cold morning start
                                    // legitimately exceeds it, and the
                                    // "backend slow" dialog was firing on
                                    // healthy-but-cold boots. 240s covers
                                    // the realistic worst case; a healthy
                                    // warm boot still connects in seconds
                                    // (polling, not waiting).
const PRELOAD_PATH = path.join(__dirname, 'preload.js');

// ---------------------------------------------------------------------------
// v2.13 STORE BUILD DETECTION (MSIX / Microsoft Store)
//
// Electron sets process.windowsStore = true when the app is running from an
// APPX/MSIX package. That single flag drives three behavioural differences,
// all of them required by Store policy or by how MSIX works:
//
//   1. NO RUNTIME INSTALLS. Store apps must be self-contained and serviced
//      only through the Store, so first_run.js must not winget-install Python
//      or Ollama, and must not pip-install anything. The Store package ships a
//      bundled Python instead (see tools/bundle_python.ps1).
//   2. WRITABLE DATA LOCATION. The install directory under
//      C:\\Program Files\\WindowsApps is READ-ONLY at runtime, and so is its
//      parent -- so the usual sibling sage_data cannot exist. We point the
//      backend at the package's LocalAppData folder via VERIDIAN_DATA_DIR.
//   3. NO BROAD FILESYSTEM ACCESS. The Store manifest deliberately omits the
//      broadFileSystemAccess restricted capability, so anything outside our
//      own data folder must come from an explicit user folder grant.
//
// Everything here is a no-op for the portable/NSIS build.
// ---------------------------------------------------------------------------
const IS_STORE_BUILD = process.windowsStore === true;

function resolveDataDir() {
  // Explicit override always wins (power users, portable installs on a stick).
  const envDir = (process.env.VERIDIAN_DATA_DIR || '').trim();
  if (envDir) return envDir;
  if (IS_STORE_BUILD) {
    // app.getPath('userData') resolves inside the package's virtualized
    // LocalAppData, which IS writable and survives updates.
    try { return path.join(app.getPath('userData'), 'sage_data'); }
    catch { /* fall through */ }
  }
  // Portable/NSIS: unchanged historical layout — sibling of the project.
  const root = app.isPackaged
    ? path.dirname(app.getPath('exe'))
    : path.resolve(__dirname, '..');
  return path.join(root, '..', 'sage_data');
}

// start.bat lives at the project root, one level up from electron/
const PROJECT_ROOT = app.isPackaged
  ? path.join(path.dirname(app.getPath('exe')))
  : path.resolve(__dirname, '..');
const START_BAT = path.join(PROJECT_ROOT, 'start.bat');

// --- Window ----------------------------------------------------
// v2.14.1: the window now exists from the first moment, showing boot.html,
// and is navigated to the app once the backend answers. Previously
// createWindow() ran only AFTER the health check passed, so launching the app
// put nothing on screen for 12-40 seconds. Two consequences, both reported
// from the field: users saw a dead launch, and -- because no window existed --
// a transient first-run setup window closing could fire 'window-all-closed'
// and quit the app before it ever appeared.
//
// A window that exists throughout fixes the visible symptom and removes the
// structural cause of the second one, rather than guarding against it.
let _bootWindowShown = false;

function showBootWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) return mainWindow;
  createWindow({ boot: true });
  _bootWindowShown = !!(mainWindow && !mainWindow.isDestroyed());
  return mainWindow;
}

function createWindow(opts) {
  opts = opts || {};
  if (mainWindow && !mainWindow.isDestroyed()) {
    // Already up (the boot window). Just point it at the app.
    if (!opts.boot) {
      blog('navigating boot window -> app');
      try { mainWindow.loadURL(BACKEND_URL); } catch (e) {
        blog('navigate failed: ' + e.message);
      }
    }
    return;
  }
  mainWindow = new BrowserWindow({
    width:  1280,
    height: 820,
    minWidth:  900,
    minHeight: 600,
    title:     'VeridianAI',
    backgroundColor: '#070b14',
    webPreferences: {
      nodeIntegration:    false,
      contextIsolation:   true,
      preload: PRELOAD_PATH,
    },
    // Frameless option (optional — remove titleBarStyle for standard chrome)
    titleBarStyle: 'hiddenInset',
  });

  if (opts.boot) {
    // No network, no backend: a local file, so it paints instantly.
    mainWindow.loadFile(path.join(__dirname, 'boot.html'))
      .catch((e) => blog('boot.html failed to load: ' + e.message));
  } else {
    mainWindow.loadURL(BACKEND_URL);
  }

  mainWindow.on('closed', () => { mainWindow = null; });

  // --- Unclickable-UI fix ---------------------------------------
  // In Electron, an in-page modal (window.confirm/alert/prompt) blocks the
  // renderer but does NOT blur the OS window, and on some machines the renderer
  // is left without pointer/input focus afterwards -- so the UI stops
  // responding to clicks until the window is re-focused. An OS dialog (the file
  // picker, print dialog) blurs+refocuses the window, which is exactly why
  // opening "Attach" frees it. Reclaiming webContents focus restores clicks.
  // We do it on every window focus (covers OS dialogs) and on explicit request
  // from the renderer after an in-page dialog (the 'oracle-unstick' channel,
  // sent from frontend/js/ui-unstick.js via the preload whitelist).
  const reclaimFocus = () => {
    try { if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.focus(); } catch (e) {}
  };
  mainWindow.on('focus', reclaimFocus);
  ipcMain.removeAllListeners('oracle-unstick');   // idempotent if createWindow runs again
  ipcMain.on('oracle-unstick', reclaimFocus);

  // --- Open the user's data folder ------------------------------------
  // Under MSIX the install directory lives in C:\Program Files\WindowsApps,
  // which the user cannot browse -- and their archives, uploads, downloads
  // and snapshots are NOT there anyway. They are in sage_data, which IS
  // reachable but sits at a redirected LocalCache path nobody would guess.
  // The data was always accessible; there was simply no door.
  //
  // The payload is IGNORED. resolveDataDir() is the only path this can ever
  // open, so the renderer cannot turn this into "open any folder".
  ipcMain.removeAllListeners('open-data-folder');
  ipcMain.on('open-data-folder', () => {
    try {
      const dir = resolveDataDir();
      fs.mkdirSync(dir, { recursive: true });
      // realpath so MSIX redirection lands the user in the folder the app
      // actually writes to, not the virtual one it computes.
      let target = dir;
      try { target = fs.realpathSync.native(dir); } catch { /* use computed */ }
      blog(`open-data-folder -> ${target}`);
      shell.openPath(target).then((err) => {
        if (err) blog(`open-data-folder failed: ${err}`);
      });
    } catch (e) {
      blog(`open-data-folder error: ${e.message}`);
    }
  });

  // External links in system browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // v2.11.15: right-click context menu. Electron ships NO native context
  // menu, so copy/paste via mouse silently did nothing — a papercut for
  // everyone who doesn't reach for Ctrl+C. Menu adapts to the target:
  // text field -> Cut/Copy/Paste/Select All; selection -> Copy;
  // link -> Copy Link Address. Roles act on the focused webContents.
  mainWindow.webContents.on('context-menu', (_event, params) => {
    const template = [];
    if (params.isEditable) {
      template.push(
        { label: 'Cut', role: 'cut', enabled: params.selectionText.length > 0 },
        { label: 'Copy', role: 'copy', enabled: params.selectionText.length > 0 },
        { label: 'Paste', role: 'paste' },
        { type: 'separator' },
        { label: 'Select All', role: 'selectAll' },
      );
    } else if (params.selectionText && params.selectionText.trim()) {
      template.push(
        { label: 'Copy', role: 'copy' },
        { type: 'separator' },
        { label: 'Select All', role: 'selectAll' },
      );
    }
    if (params.linkURL) {
      if (template.length) template.push({ type: 'separator' });
      template.push({
        label: 'Copy Link Address',
        click: () => { try { require('electron').clipboard.writeText(params.linkURL); } catch { /* ignore */ } },
      });
    }
    if (!template.length) return;   // nothing useful to offer — no empty menu
    try {
      Menu.buildFromTemplate(template).popup({ window: mainWindow });
    } catch (e) {
      console.error('[Electron] context menu failed:', e && e.message);
    }
  });

  buildMenu();
}

// --- Backend (full stack via start.bat) ------------------------
// v2.1.6: replaces the old `python main.py` spawn. start.bat is the
// canonical launcher and starts Ollama (Oracle tier), llama-server
// (Toga tier), Toga Daemon, AND FastAPI in the right order. Spawning
// start.bat means Electron's launch behaves identically to the
// double-click-start.bat flow.
function _devModeEnabled() {
  // Read the Developer Mode flag from sage_data/ui_prefs.json (the same store
  // the backend uses). Default false = hidden. Best-effort, never throws.
  try {
    // v2.13: MUST go through resolveDataDir(). This used to compute
    // root/../sage_data directly, which under MSIX points at
    // C:\Program Files\WindowsApps\sage_data -- a path that does not
    // exist and cannot be created. Developer Mode was therefore
    // UNREACHABLE in the Store build: the one switch that makes
    // start.bat's console visible was disabled by the same read-only
    // path bug it would have helped diagnose.
    const prefs = path.join(resolveDataDir(), 'ui_prefs.json');
    // Strip a UTF-8 BOM before parsing. PowerShell 5.1's
    // `Set-Content -Encoding utf8` writes one, and JSON.parse throws on a
    // leading \uFEFF -- which the catch below then swallowed, silently
    // reporting Developer Mode as off. Cost a full debugging round.
    const raw = fs.readFileSync(prefs, 'utf8').replace(/^\uFEFF/, '');
    return !!JSON.parse(raw).developer_mode;
  } catch (e) {
    return false;
  }
}

function startBackend() {
  const fs = require('fs');

  // v2.1.6 fix: __dirname resolves inside the asar bundle when packaged,
  // so we use app.getPath('exe') to find the actual install directory
  // where start.bat lives alongside the exe. Falls back to the dev-time
  // path when running unpackaged via npm start.
  const resolvedRoot = app.isPackaged
    ? path.dirname(app.getPath('exe'))
    : path.resolve(__dirname, '..');
  const resolvedBat = path.join(resolvedRoot, 'start.bat');

  if (!fs.existsSync(resolvedBat)) {
    dialog.showErrorBox(
      'VeridianAI — startup error',
      `Cannot find start.bat at:\n${resolvedBat}\n\nElectron cannot launch the backend.\n\nCheck that start.bat is in the same folder as VeridianAI.exe`
    );
    return;
  }

  blog(`isPackaged=${app.isPackaged} resolvedRoot=${resolvedRoot}`);
  blog(`start.bat exists=${fs.existsSync(resolvedBat)} at ${resolvedBat}`);
  try { blog('root contents: ' + fs.readdirSync(resolvedRoot).join(', ')); }
  catch (e) { blog('root unreadable: ' + e.message); }

  if (process.platform === 'win32') {
    // v2.13: hand the backend its data location and the Store flag. config.py
    // reads VERIDIAN_DATA_DIR; the rest of the stack inherits both from here,
    // so there is exactly one place that decides where data lives.
    const _dataDir = resolveDataDir();
    try { require('fs').mkdirSync(_dataDir, { recursive: true }); } catch { /* best effort */ }
    blog(`dataDir=${_dataDir} (store=${IS_STORE_BUILD}) exists=${fs.existsSync(_dataDir)}`);
    // An MSIX package redirects writes to %APPDATA% into its own LocalCache,
    // so the path computed above is NOT where the backend's files land. This
    // log line used to name a directory that stays empty forever while the
    // real sage_data -- API key, config.json, memory chain -- lived elsewhere
    // entirely. realpath follows the redirection and prints the truth.
    try {
      const _real = fs.realpathSync.native(_dataDir);
      if (_real && _real !== _dataDir) {
        blog(`dataDir REDIRECTED by the package container -> ${_real}`);
        blog('  (that resolved path is the one to back up, not the one above)');
      }
    } catch (e) { blog(`dataDir realpath unavailable: ${e.message}`); }

    const _dev = _devModeEnabled();
    let _outFd = null;
    if (!_dev) {
      try { fs.writeFileSync(BACKEND_LOG, ''); _outFd = fs.openSync(BACKEND_LOG, 'a'); }
      catch (e) { blog('could not open backend log: ' + e.message); }
    }
    blog(`devMode=${_dev} backendLog=${_outFd !== null ? BACKEND_LOG : '(console)'}`);
    // -----------------------------------------------------------------------
    // v2.13 STORE BUILD: launch Python DIRECTLY, never through cmd.exe.
    //
    // Windows only propagates PACKAGE IDENTITY to child processes that live
    // INSIDE the package. VeridianAI.exe is inside, so it may execute the
    // bundled python.exe. cmd.exe lives in System32 -- outside -- so it
    // starts as an ordinary process with no package identity, and the
    // WindowsApps ACL then refuses it execute access to anything in there.
    // Every python invocation from start.bat died with 'Access is denied'
    // and the launcher exited 5 (ERROR_ACCESS_DENIED).
    //
    // Proven by an exec probe: spawning python.exe from Electron returned
    // 'Python 3.12.8' status=0, while the cmd.exe route was refused.
    //
    // So a .bat launcher is structurally unusable in MSIX: it can only run
    // inside cmd.exe, which is exactly the process that lacks the rights it
    // needs. Python is spawned straight from here instead.
    //
    // store_launch.py is that Python bootstrap: it sets up the tier
    // environment, runs tier_launcher.py, then hands off to start.py.
    // Identity flows the whole way down because every link lives inside the
    // package: VeridianAI.exe -> python.exe -> llama-server.exe.
    // Ollama is absent by design (not installable at runtime); Toga and
    // Daemon run on the BUNDLED llama-server.exe.
    // -----------------------------------------------------------------------
    const _spawnCmd = IS_STORE_BUILD
      ? path.join(resolvedRoot, 'python', 'python.exe')
      : 'cmd.exe';
    // -u is NOT optional here. Python block-buffers stdout whenever it is not
    // a terminal, and we redirect it to a file -- so on a v2.13 run the whole
    // startup transcript sat in an 8 KB buffer and was LOST when the process
    // was killed, leaving a log with a banner and nothing else. Unbuffered
    // output costs nothing here and is the difference between a diagnosable
    // failure and another blind round.
    const _spawnArgs = IS_STORE_BUILD
      ? ['-u', path.join(resolvedRoot, 'store_launch.py'), '--port', String(APP_PORT)]
      : ['/c', resolvedBat, '--mode', ELECTRON_BACKEND_MODE];

    if (IS_STORE_BUILD && !fs.existsSync(_spawnCmd)) {
      blog(`FATAL: bundled python missing at ${_spawnCmd}`);
    }
    blog(`spawning: ${_spawnCmd} ${_spawnArgs.join(' ')}`);
    backendProc = spawn(_spawnCmd, _spawnArgs, {
      cwd:   resolvedRoot,
      env: {
        ...process.env,
        VERIDIAN_DATA_DIR: _dataDir,
        VERIDIAN_STORE_BUILD: IS_STORE_BUILD ? '1' : '0',
        PYTHONUNBUFFERED: '1',        // belt to -u's braces
        PYTHONFAULTHANDLER: '1',      // dump a stack if it hard-crashes
      },
      // Developer Mode: Dev OFF -> 'ignore' (no console attached/created) +
      // windowsHide so the start.bat window stays hidden for a clean desktop;
      // Dev ON -> 'inherit' so the tier startup logs are visible. (When you run
      // `npm start` from your own terminal, that terminal is the shell you typed
      // in and can't be hidden — only the packaged .exe launch is fully clean.)
      stdio: _dev ? 'inherit' : (_outFd !== null ? ['ignore', _outFd, _outFd] : 'ignore'),
      windowsHide: !_dev,
    });
  } else {
    // Non-Windows: there's no start.bat equivalent yet, so fall back
    // to the old python main.py spawn. (VeridianAI is Windows-first
    // for now.)
    const backendDir = path.join(__dirname, '..', 'backend');
    backendProc = spawn('python3', ['main.py'], {
      cwd:   backendDir,
      stdio: 'inherit',
    });
  }

  blog(`spawned pid=${backendProc && backendProc.pid}`);
  backendProc.on('error', err => {
    blog('SPAWN ERROR: ' + err.message);
  });
  backendProc.on('exit', (code, signal) => {
    // An immediate exit here is the single most diagnostic event in the
    // whole boot: it means start.bat ran and gave up, and the code says why.
    blog(`backend process EXITED code=${code} signal=${signal}`);
    backendProc = null;
  });
}

// v2.11.12 zombie-process fix: resolve a Python launcher once so we can run
// backend\shutdown_cleanup.py. Mirrors start.bat's py -> python -> python3
// detection. Cached after first call; null if no Python found (cleanup is
// then skipped — taskkill /T still runs, same behavior as before this fix).
let _pythonCmd;   // undefined = not probed yet, null = none found
function findPython() {
  if (_pythonCmd !== undefined) return _pythonCmd;
  for (const cand of ['py', 'python', 'python3']) {
    try {
      const r = spawnSync(cand, ['--version'], { timeout: 5000, windowsHide: true });
      if (r.status === 0) { _pythonCmd = cand; return cand; }
    } catch (e) { /* keep trying */ }
  }
  _pythonCmd = null;
  return null;
}

// v2.11.12: run backend\shutdown_cleanup.py SYNCHRONOUSLY. This is the fix
// for the zombie python/llama-server/ollama processes: tier_launcher.py
// spawns the tiers and exits, orphaning them, so taskkill /T on the
// start.bat tree can NEVER reach them. The cleanup script kills exactly
// the PIDs recorded in .oracle_pids.json (identity-verified) plus any
// stack process running from backend\ — and nothing else (a user-launched
// Ollama survives). Synchronous on purpose: quit must not race the reaper.
function runCleanupSync(reason) {
  const py = findPython();
  if (!py) {
    console.warn('[Electron] no Python found — skipping process cleanup');
    return;
  }
  const script = path.join(PROJECT_ROOT, 'backend', 'shutdown_cleanup.py');
  if (!fs.existsSync(script)) return;
  console.log(`[Electron] running process cleanup (${reason})…`);
  try {
    const r = spawnSync(py, [script, '--quiet'], {
      cwd: PROJECT_ROOT,
      timeout: 25000,
      windowsHide: true,
      env: { ...process.env, OAI_ROOT: PROJECT_ROOT },
    });
    console.log(`[Electron] cleanup exit code ${r.status}`);
  } catch (e) {
    console.error('[Electron] cleanup failed:', e.message);
  }
}

let _shutdownDone = false;   // stopBackend runs from window-all-closed AND
                             // before-quit; only do the work once.
function stopBackend() {
  if (_shutdownDone) return;
  _shutdownDone = true;

  // Step 1 — kill the spawned start.bat tree (start.bat's cmd, start.py,
  // FastAPI uvicorn). taskkill /T /F, but SYNCHRONOUS now (v2.11.12): the
  // old async spawn raced app exit, and on slow quits the kill never
  // happened — one of the two sources of zombies.
  if (backendProc && !backendProc.killed) {
    if (process.platform === 'win32') {
      try {
        const r = spawnSync('taskkill',
          ['/PID', String(backendProc.pid), '/T', '/F'],
          { timeout: 15000, windowsHide: true });
        console.log(`[Electron] taskkill exit code ${r.status}`);
      } catch (e) {
        console.error('[Electron] taskkill failed:', e);
      }
    } else {
      try { backendProc.kill('SIGTERM'); } catch { /* ignore */ }
    }
    backendProc = null;
  }

  // Step 2 — reap the orphaned tier processes via the PID ledger. Runs
  // even when backendProc is null: if Electron reused an already-running
  // backend (the "already healthy — skipping spawn" path), quitting
  // VeridianAI should still take the whole stack down.
  if (process.platform === 'win32') runCleanupSync('quit');

  // Step 3 — failsafe against zombie VeridianAI windows: if anything keeps
  // the quit from completing (stuck renderer, pending dialog, hung IPC),
  // hard-exit after a grace period. A live timer does not block Electron
  // from exiting normally, so the happy path is unaffected.
  setTimeout(() => {
    console.warn('[Electron] quit did not complete in 8s — forcing exit');
    try { process.exit(0); } catch { /* unreachable */ }
  }, 8000);
}

// --- Health probe (single attempt) -----------------------------
// Promise-based single GET to /api/health. Resolves true on 2xx,
// false on anything else (including connection refused). Kept
// separate from waitForBackend so we can reuse it for the
// "is backend already up?" pre-check before spawning.
function probeHealth() {
  return new Promise((resolve) => {
    // v2.1.11: was hardcoded 1500ms; now driven by PROBE_TIMEOUT_MS so the
    // value lives in one place with the rest of the health-probe tuning.
    const req = http.get(HEALTH_URL, { timeout: PROBE_TIMEOUT_MS }, (res) => {
      const ok = res.statusCode >= 200 && res.statusCode < 300;
      res.resume();
      resolve(ok);
    });
    req.on('error',   () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

// --- Wait for backend ------------------------------------------
// v2.1.6: poll /api/health (strict 200) instead of the bare URL.
// Longer total timeout since Ollama can take 30-60s on first model
// load. We don't fail hard if the backend's slow — we surface a
// dialog asking whether to open the window anyway.
async function waitForBackend() {
  const deadline = Date.now() + HEALTH_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await probeHealth()) return true;
    await new Promise(r => setTimeout(r, HEALTH_POLL_MS));
  }
  return false;
}

// --- Port-conflict detection (v2.1.6, port-aware since #68 Phase E) ------
// Three failure modes we need to distinguish:
//   1. Backend already healthy on APP_PORT  -> reuse, skip spawn.
//   2. Port APP_PORT bound but not healthy  -> orphan process from
//      a prior crash/incomplete shutdown; offer to kill it.
//   3. Port APP_PORT free                   -> spawn start.bat.
//
// On Windows we use `netstat -ano` to find the PID listening on the
// configured port, then `taskkill /F /PID <pid>` to clean it up. We never
// kill without explicit user confirmation — surprising the user by
// terminating processes is worse than the orphan itself.

function isPortBound(port) {
  // Try a TCP connect; if it succeeds the port is bound.
  return new Promise((resolve) => {
    const net = require('net');
    const sock = new net.Socket();
    sock.setTimeout(1000);
    sock.once('connect', () => { sock.destroy(); resolve(true); });
    sock.once('timeout', () => { sock.destroy(); resolve(false); });
    sock.once('error',   () => { sock.destroy(); resolve(false); });
    sock.connect(port, '127.0.0.1');
  });
}

function findPidOnPort(port) {
  // Windows-only — uses netstat -ano. On other platforms we just
  // skip the orphan-kill flow and let the user resolve manually.
  if (process.platform !== 'win32') return null;
  return new Promise((resolve) => {
    const ns = spawn('netstat', ['-ano']);
    let buf = '';
    ns.stdout.on('data', (d) => { buf += d.toString(); });
    ns.on('close', () => {
      // Parse lines like: "  TCP    0.0.0.0:8000   0.0.0.0:0   LISTENING   1234"
      const lines = buf.split(/\r?\n/);
      const target = `:${port}`;
      for (const line of lines) {
        if (!line.includes('LISTENING')) continue;
        if (!line.includes(target)) continue;
        const parts = line.trim().split(/\s+/);
        const pid = parts[parts.length - 1];
        if (/^\d+$/.test(pid)) return resolve(parseInt(pid, 10));
      }
      resolve(null);
    });
    ns.on('error', () => resolve(null));
  });
}

function killPid(pid) {
  return new Promise((resolve) => {
    if (process.platform !== 'win32') {
      try { process.kill(pid, 'SIGTERM'); resolve(true); }
      catch { resolve(false); }
      return;
    }
    const tk = spawn('taskkill', ['/F', '/PID', String(pid)]);
    tk.on('exit', (code) => resolve(code === 0));
    tk.on('error', () => resolve(false));
  });
}

// Orchestrator: decides whether to reuse, kill+spawn, or just spawn.
// Returns true if the backend is (or will shortly be) reachable.
async function ensureBackendAvailable() {
  blog(`ensureBackendAvailable: APP_PORT=${APP_PORT} HEALTH_URL=${HEALTH_URL}`);
  // Case 1: backend already healthy — just reuse it. This is the
  // common case if you ran start.bat manually before launching
  // Electron, or if a prior Electron instance is still alive.
  if (await probeHealth()) {
    blog('CASE 1: backend already healthy - skipping spawn');
    return true;
  }

  // Case 2: port bound but not healthy. Likely an orphan process
  // from a prior incomplete shutdown (the old Electron used
  // kill('SIGTERM') which doesn't always reap children on Windows).
  //
  // v2.11.12: before bothering the user with a dialog, run the ledger
  // cleanup — if the port-holder is one of OUR orphans (it almost always
  // is), this reaps it and the whole stale tier family silently, and the
  // launch proceeds first try. The dialog below now only appears when a
  // FOREIGN process owns the port, where user confirmation is right.
  if (await isPortBound(APP_PORT)) {
    blog('port is BOUND -> running stale-process cleanup');
    runCleanupSync('startup: stale processes detected');
    blog('cleanup finished');
    await new Promise(r => setTimeout(r, 1000));
  }
  if (await isPortBound(APP_PORT)) {
    blog('port STILL bound after cleanup - identifying holder');
    const pid = await findPidOnPort(APP_PORT);
    blog(`port holder pid=${pid}`);
    const detail = pid
      ? `Found PID ${pid} listening on port ${APP_PORT} but it's not ` +
        `responding to /api/health. This is usually an orphan ` +
        `process from a prior VeridianAI launch that didn't shut ` +
        `down cleanly. Killing it is safe if you're not running ` +
        `another tool on port ${APP_PORT}.`
      : `Something is bound to port ${APP_PORT} but couldn't identify ` +
        `which process. You may need to investigate manually ` +
        `(netstat -ano | findstr :${APP_PORT}).`;
    const choice = dialog.showMessageBoxSync({
      type: 'warning',
      title: `VeridianAI — port ${APP_PORT} in use`,
      message: `Port ${APP_PORT} is occupied by another process.`,
      detail,
      buttons: pid
        ? ['Kill that process and continue', 'Quit']
        : ['Open anyway (will fail)', 'Quit'],
      defaultId: 0,
      cancelId: 1,
    });
    blog(`port-in-use dialog choice=${choice}`);
    if (choice === 1) { blog('user chose QUIT'); _bootingUp = false; app.quit(); return false; }
    if (pid) {
      const killed = await killPid(pid);
      if (!killed) {
        dialog.showErrorBox(
          'taskkill failed',
          `Could not terminate PID ${pid}. You may need to do it ` +
          `manually: taskkill /F /PID ${pid}`,
        );
        return false;
      }
      // Give Windows a beat to release the port before bind.
      await new Promise(r => setTimeout(r, 500));
    } else {
      // NOTE: returns WITHOUT spawning. If the boot log shows this line and
      // then jumps straight to 'backend healthy=false', that is the whole
      // story: nothing was ever launched.
      blog('CASE 2b: port bound, holder UNKNOWN, user opted to continue - NOT spawning');
      return false;
    }
  }

  // Case 3: port is free — spawn start.bat as before.
  blog('CASE 3: port free -> startBackend()');
  startBackend();
  blog('waiting for backend health...');
  return await waitForBackend();
}

// --- Menu ------------------------------------------------------
// v2.2 #74: handler for the Backend Mode submenu. Writes the user's
// choice to .backend_mode and offers to relaunch Electron so start.bat
// picks up the new mode on its next spawn. We can't hot-swap the
// backend mid-run because start.bat would have to tear down Ollama +
// llama-server + the daemons and respawn them — cleaner to relaunch.
function onBackendModeSelected(mode) {
  if (mode === ELECTRON_BACKEND_MODE) {
    // Already on this mode — nothing to do, no nagging dialog.
    return;
  }
  try {
    writeBackendMode(mode);
  } catch (e) {
    dialog.showErrorBox(
      'VeridianAI — backend mode',
      `Could not save backend mode preference:\n${e.message}\n\n` +
      `The file ${BACKEND_MODE_FILE} may not be writable.`,
    );
    return;
  }
  const choice = dialog.showMessageBoxSync({
    type: 'question',
    title: 'VeridianAI — Backend mode changed',
    message:
      `Backend mode set to "${mode}". ` +
      `Restart VeridianAI now to apply it?`,
    detail:
      `Vulkan = current default, fastest LLM. ` +
      `IPEX-LLM = slightly slower LLM but the rest of your PC stays ` +
      `more responsive. The change applies on the next start.bat ` +
      `spawn, so an Electron relaunch is required.`,
    buttons: ['Restart Now', 'Later'],
    defaultId: 0,
    cancelId: 1,
  });
  if (choice === 0) {
    // app.relaunch + app.exit gives us a clean Electron restart that
    // also reaps the spawned start.bat process tree via the existing
    // before-quit / window-all-closed handlers.
    app.relaunch();
    app.exit(0);
  }
}

function buildMenu() {
  const template = [
    {
      label: 'VeridianAI',
      submenu: [
        { label: 'About VeridianAI', role: 'about' },
		{ label: 'Command Palette', accelerator: 'CmdOrCtrl+K', click: () => mainWindow?.webContents.send('open-command-palette') },
        { type: 'separator' },
        // v2.2 #74: Backend Mode submenu — radio items so the current
        // mode is visually obvious. Selection writes .backend_mode and
        // offers a relaunch. Read happens on next boot.
        {
          label: 'Backend Mode',
          submenu: [
            {
              label:   'Vulkan (default, fastest LLM)',
              type:    'radio',
              checked: ELECTRON_BACKEND_MODE === 'vulkan',
              click:   () => onBackendModeSelected('vulkan'),
            },
            {
              label:   'IPEX-LLM (better PC responsiveness)',
              type:    'radio',
              checked: ELECTRON_BACKEND_MODE === 'ipex',
              click:   () => onBackendModeSelected('ipex'),
            },
          ],
        },
        { type: 'separator' },
        { label: 'Reload', accelerator: 'CmdOrCtrl+R', click: () => mainWindow?.reload() },
        { label: 'DevTools', accelerator: 'F12', click: () => mainWindow?.webContents.toggleDevTools() },
        { type: 'separator' },
        { label: 'Quit', accelerator: 'CmdOrCtrl+Q', click: () => app.quit() },
      ],
    },
    {
      label: 'View',
      submenu: [
        { label: 'Zoom In',   accelerator: 'CmdOrCtrl+=', role: 'zoomIn'  },
        { label: 'Zoom Out',  accelerator: 'CmdOrCtrl+-', role: 'zoomOut' },
        { label: 'Reset Zoom', accelerator: 'CmdOrCtrl+0', role: 'resetZoom' },
        { type: 'separator' },
        { label: 'Toggle Fullscreen', accelerator: 'F11', role: 'togglefullscreen' },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// --- Lifecycle -------------------------------------------------
// v2.11.12: single-instance lock. When a half-dead previous instance is
// still around (the zombie-window failure mode), launching again used to
// stack a second broken instance on top — part of why restarting took
// 3-5 tries. Now the second launch either focuses the live window or, if
// the first instance is truly hung, the user kills one process and the
// next launch is clean (the startup cleanup reaps the rest).
const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  console.log('[Electron] another VeridianAI instance is running — exiting');
  app.exit(0);
}
app.on('second-instance', () => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  }
});

// v2.12.18 — THE "FIRST CLICK INSTALLS, SECOND CLICK RUNS" BUG.
//
// On a clean machine first_run.js opens a Setup Assistant BrowserWindow.
// When setup finished it closed that window — and because it was the only
// window open (the main window is not created until AFTER the backend is
// healthy, further down this handler), Electron fired 'window-all-closed',
// which calls stopBackend() + app.quit(). The app therefore installed
// everything perfectly and then exited before it ever showed itself. The
// second launch skipped setup, so no transient window existed, and it ran.
//
// This flag marks the window between app start and createWindow(). While it
// is true, 'window-all-closed' is a normal part of booting and must NOT quit.
let _bootingUp = true;

app.whenReady().then(async () => {
  // v2.12.0 rebrand: the About dialog (menu role:'about') reads these —
  // without them it falls back to the exe's embedded package name/version.
  try {
    app.setAboutPanelOptions({
      applicationName: 'VeridianAI',
      applicationVersion: app.getVersion(),
      copyright: '© 2026 MentiSphere Software',
    });
  } catch (e) { /* older Electron on non-Windows — ignore */ }

  // Window FIRST, before first-run setup or the backend spawn -- both of which
  // can take a minute on a cold machine. The user sees the app open
  // immediately and watches it start, instead of watching their desktop.
  // Do this first: everything after it is a candidate for going wrong, and
  // this is what makes the going-wrong legible.
  openDurableBootLog();

  blog('showBootWindow()');
  showBootWindow();

  // First-run setup (Python deps + Ollama consent) BEFORE the backend launches,
  // so a fresh machine installs what start.bat needs. Run-once via a marker in
  // sage_data; fully defensive so a hiccup never blocks launch.
  try {
    await require('./first_run').ensureSetup();
  } catch (e) {
    console.error('[Electron] first-run setup failed (continuing):', e && e.message);
  }

  // v2.1.6: ensureBackendAvailable handles the three cases (reuse,
  // orphan-kill, fresh spawn) so we don't blindly spawn start.bat
  // when APP_PORT is already taken. See helper docstring above.
  blog('calling ensureBackendAvailable()...');
  const ready = await ensureBackendAvailable();
  blog(`backend healthy=${ready}`);

  // v2.13 LOG HYGIENE. The backend transcript is captured to a temp file so
  // a failed boot leaves evidence. But that transcript includes anything the
  // backend prints -- and auth.py prints the raw bearer token on first boot.
  // Keeping a plaintext credential in %TEMP% indefinitely is not acceptable,
  // least of all for a HIPAA-facing deployment.
  //
  // So: if the backend came up, the diagnostics served no purpose -- replace
  // them with a one-line note. If it FAILED, keep everything, because that is
  // exactly when we need it.
  // v2.13: REDACT, don't discard. The previous version wiped the whole
  // transcript on a successful boot, which threw away the startup detail we
  // actually need -- which tiers launched, which models were found. The only
  // thing that genuinely must not persist is the first-boot bearer token, so
  // scrub that and keep everything else.
  if (ready) {
    try {
      let txt = fs.readFileSync(BACKEND_LOG, 'utf8');
      // auth.py prints the token with an 'ora_' prefix; also catch any
      // Bearer header that might be echoed.
      const before = txt;
      txt = txt.replace(/\bora_[A-Za-z0-9_\-]{8,}/g, 'ora_<REDACTED>')
               .replace(/(Bearer\s+)[A-Za-z0-9._\-]{8,}/gi, '$1<REDACTED>');
      if (txt !== before) {
        fs.writeFileSync(BACKEND_LOG, txt);
        blog('backend log retained (credentials redacted)');
      } else {
        blog('backend log retained (nothing to redact)');
      }
    } catch (e) { blog('could not redact backend log: ' + e.message); }
  } else {
    blog('backend FAILED - backend log retained for diagnosis');
  }
  // Copy the backend transcript next to the durable boot log, for the same
  // reason and with the same redaction already applied above.
  try {
    if (_logDir2 && fs.existsSync(BACKEND_LOG)) {
      fs.copyFileSync(BACKEND_LOG,
                      path.join(_logDir2, 'VeridianAI-backend.log'));
      blog('backend log copied beside sage_data');
    }
  } catch (e) { blog('could not copy backend log: ' + e.message); }
  if (!ready) {
    const choice = dialog.showMessageBoxSync({
      type: 'warning',
      title: 'VeridianAI startup',
      message: 'Backend is slow to come up.',
      detail:
        `Tried to reach ${HEALTH_URL} for ${HEALTH_TIMEOUT_MS}ms ` +
        `with no successful response. The start.bat console window ` +
        `(if visible) should show what's stalling — usually Ollama ` +
        `loading a large model on cold cache, or a tier failing to bind.`,
      buttons: ['Open anyway (auto-retry)', 'Quit'],
      defaultId: 0,
      cancelId: 1,
    });
    if (choice === 1) {
      // User chose Quit at the "backend is slow" dialog. Release the boot
      // guard first, or window-all-closed would decline to quit and we
      // would linger as a headless process.
      _bootingUp = false;
      stopBackend();
      app.quit();
      return;
    }
  } else {
    console.log('[Electron] backend ready, opening window');
  }
  blog('createWindow()');
  createWindow();
  // Boot finished: from here a window-all-closed really does mean "the user
  // closed the app", so the handler may quit normally again.
  _bootingUp = false;

  // v2.1.6: if the window loaded with no working backend (user
  // chose "Open anyway" past the timeout), auto-retry the load
  // every 3s until the page actually has DOM content. This rescues
  // the "blue background forever" failure mode where the backend
  // came up just AFTER the timeout dialog fired and the user is
  // now staring at an empty BrowserWindow with no way back.
  let reloadTimer = null;
  if (!ready) {
    reloadTimer = setInterval(async () => {
      if (await probeHealth()) {
        console.log('[Electron] backend now healthy — reloading window');
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.loadURL(BACKEND_URL);
        }
        clearInterval(reloadTimer);
        reloadTimer = null;
      }
    }, 3000);
    // Stop retrying after 5 minutes regardless — at that point the
    // user has bigger problems and we don't want a forever-loop.
    setTimeout(() => {
      if (reloadTimer) {
        clearInterval(reloadTimer);
        reloadTimer = null;
      }
    }, 5 * 60 * 1000);
  }

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

// v2.1.6: stopBackend() now does taskkill /T /F so the WHOLE process
// tree dies — Ollama, llama-server, FastAPI, Toga Daemon, all the
// children spawned by start.bat. Previously kill('SIGTERM') only
// touched the start.bat shell and left orphans behind.
app.on('window-all-closed', () => {
  // v2.12.18: during boot the only window may be first_run.js's Setup
  // Assistant. Closing it is the normal end of setup, NOT the user closing
  // the app — quitting here is what made a fresh install need two launches.
  // See the _bootingUp comment above app.whenReady.
  // v2.14.1: the bypass now applies ONLY when no boot window is up.
  //
  // The original problem was that during boot the sole window could be
  // first_run.js's transient Setup Assistant, so its closing looked like the
  // user quitting. Now a boot window is opened before setup runs and stays up
  // until the app loads -- so while it is present, the setup window closing
  // does NOT empty the window list, and 'window-all-closed' during boot can
  // only mean the user closed the boot window deliberately. Honour that:
  // refusing would leave a backend running with no window, which is worse
  // than the bug this guard was written for.
  //
  // The bypass is kept for the case where the boot window failed to open, so
  // a broken boot.html cannot resurrect the two-launch bug.
  if (_bootingUp && !_bootWindowShown) {
    console.log('[Electron] window-all-closed during boot, no boot window — not quitting');
    return;
  }
  if (_bootingUp) {
    console.log('[Electron] boot window closed by user — shutting down');
  }

  // Security: clear the login cookie on close so reopening requires a fresh
  // sign-in (the auth cookie is also session-scoped server-side now).
  try {
    session.defaultSession.clearStorageData({ storages: ['cookies'] }).catch(() => {});
  } catch (e) { /* ignore */ }
  stopBackend();
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', stopBackend);
