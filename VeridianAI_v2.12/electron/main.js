/**
 * VeridianAI 2.9.10 — Electron Wrapper
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

// start.bat lives at the project root, one level up from electron/
const PROJECT_ROOT = app.isPackaged
  ? path.join(path.dirname(app.getPath('exe')))
  : path.resolve(__dirname, '..');
const START_BAT = path.join(PROJECT_ROOT, 'start.bat');

// --- Window ----------------------------------------------------
function createWindow() {
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

  mainWindow.loadURL(BACKEND_URL);

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
    const root = app.isPackaged ? path.dirname(app.getPath('exe')) : path.resolve(__dirname, '..');
    const prefs = path.join(root, '..', 'sage_data', 'ui_prefs.json');
    return !!JSON.parse(fs.readFileSync(prefs, 'utf8')).developer_mode;
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

  console.log(`[Electron] spawning ${resolvedBat}`);

  if (process.platform === 'win32') {
    backendProc = spawn('cmd.exe', ['/c', resolvedBat, '--mode', ELECTRON_BACKEND_MODE], {
      cwd:   resolvedRoot,
      // Developer Mode: Dev OFF -> 'ignore' (no console attached/created) +
      // windowsHide so the start.bat window stays hidden for a clean desktop;
      // Dev ON -> 'inherit' so the tier startup logs are visible. (When you run
      // `npm start` from your own terminal, that terminal is the shell you typed
      // in and can't be hidden — only the packaged .exe launch is fully clean.)
      stdio: _devModeEnabled() ? 'inherit' : 'ignore',
      windowsHide: !_devModeEnabled(),
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

  backendProc.on('error', err => {
    console.error('[Electron] Failed to start backend:', err.message);
  });
  backendProc.on('exit', code => {
    console.log(`[Electron] start.bat exited with code ${code}`);
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
  // Case 1: backend already healthy — just reuse it. This is the
  // common case if you ran start.bat manually before launching
  // Electron, or if a prior Electron instance is still alive.
  if (await probeHealth()) {
    console.log('[Electron] backend already healthy — skipping spawn');
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
    runCleanupSync('startup: stale processes detected');
    await new Promise(r => setTimeout(r, 1000));
  }
  if (await isPortBound(APP_PORT)) {
    const pid = await findPidOnPort(APP_PORT);
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
    if (choice === 1) { app.quit(); return false; }
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
      return false;  // user chose "open anyway" but we can't fix it
    }
  }

  // Case 3: port is free — spawn start.bat as before.
  startBackend();
  console.log('[Electron] waiting for backend health…');
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
  const ready = await ensureBackendAvailable();
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
      stopBackend();
      app.quit();
      return;
    }
  } else {
    console.log('[Electron] backend ready, opening window');
  }
  createWindow();

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
  // Security: clear the login cookie on close so reopening requires a fresh
  // sign-in (the auth cookie is also session-scoped server-side now).
  try {
    session.defaultSession.clearStorageData({ storages: ['cookies'] }).catch(() => {});
  } catch (e) { /* ignore */ }
  stopBackend();
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', stopBackend);
