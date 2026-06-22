/**
 * OracleAI — first-run setup (Electron main process).
 *
 * Runs ONCE on a fresh machine before the backend launches:
 *   1. Friendly Ollama consent (Agree / download page / skip) — load-bearing,
 *      since the Oracle tier needs it.
 *   2. Auto-installs Python deps from backend/requirements.txt (curated; --user, no admin).
 *   3. Writes a completion marker in sage_data so it never repeats — it re-runs
 *      pip only if requirements change or a previous install failed.
 *
 * Fully defensive: any failure logs and returns so it can never brick launch.
 */
const { app, dialog, BrowserWindow, shell } = require('electron');
const { spawn, execFileSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');

function projectRoot() {
  // Packaged: the install dir next to the exe. Dev: one level up from electron/.
  return app.isPackaged ? path.dirname(app.getPath('exe')) : path.resolve(__dirname, '..');
}

function sageDataDir() {
  // sage_data lives ALONGSIDE the project folder (sibling), never inside it.
  return path.resolve(projectRoot(), '..', 'sage_data');
}

function markerPath() {
  return path.join(sageDataDir(), '.oracle_setup.json');
}

function reqFiles() {
  // Install ONLY the curated backend/requirements.txt (version-ranged — the same
  // file start.py installs). The ROOT requirements.txt is a `pip freeze` artifact
  // with machine-specific CUDA pins (e.g. torch==2.12.1+cu132) that don't resolve
  // on PyPI, so we never install it.
  const root = projectRoot();
  const f = path.join(root, 'backend', 'requirements.txt');
  try { return fs.existsSync(f) ? [f] : []; } catch { return []; }
}

function reqHash() {
  const h = crypto.createHash('sha256');
  for (const f of reqFiles()) {
    try { h.update(fs.readFileSync(f)); } catch { /* ignore */ }
  }
  return h.digest('hex');
}

function findPython() {
  for (const cmd of ['py', 'python', 'python3']) {
    try { execFileSync(cmd, ['--version'], { stdio: 'ignore' }); return cmd; } catch { /* next */ }
  }
  return null;
}

function hasOllama() {
  try { execFileSync('ollama', ['--version'], { stdio: 'ignore' }); return true; } catch { return false; }
}

function readMarker() {
  try { return JSON.parse(fs.readFileSync(markerPath(), 'utf8')); } catch { return null; }
}

function writeMarker(obj) {
  try {
    fs.mkdirSync(sageDataDir(), { recursive: true });
    fs.writeFileSync(markerPath(), JSON.stringify(obj, null, 2), 'utf8');
  } catch (e) {
    console.error('[first-run] could not write marker:', e && e.message);
  }
}

function makeSplash(msg) {
  let win = null;
  try {
    win = new BrowserWindow({
      width: 480, height: 240, frame: false, resizable: false, center: true,
      alwaysOnTop: true, show: true, backgroundColor: '#0b1430',
      webPreferences: { contextIsolation: true },
    });
    const html =
      '<!doctype html><meta charset="utf-8"><body style="margin:0;height:100vh;' +
      'font-family:Segoe UI,system-ui,sans-serif;background:#0b1430;color:#e9edf6;' +
      'display:flex;align-items:center;justify-content:center">' +
      '<div style="text-align:center;padding:26px">' +
      '<div style="font-size:18px;letter-spacing:.16em;color:#f0a500;margin-bottom:14px">O R A C L E&nbsp;&nbsp;A I</div>' +
      '<div style="font-size:13px;opacity:.92;line-height:1.5">' + msg + '</div>' +
      '<div style="margin-top:16px;font-size:11px;opacity:.6">First run only — this can take a few minutes.</div>' +
      '</div></body>';
    win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html));
  } catch (e) {
    console.error('[first-run] splash failed:', e && e.message);
  }
  return win;
}

async function ensureOllama() {
  if (hasOllama()) return 'present';
  const res = dialog.showMessageBoxSync({
    type: 'question',
    title: 'OracleAI — Ollama required',
    message: 'OracleAI needs Ollama to run its main reasoning model.',
    detail:
      'Ollama is a free, local AI runtime. OracleAI uses it for the Oracle tier ' +
      'and cannot fully start without it.\n\nInstall it now?',
    buttons: ['Install Ollama', 'Open download page', 'Skip for now'],
    defaultId: 0,
    cancelId: 2,
  });
  if (res === 2) return 'skipped';
  if (res === 0 && process.platform === 'win32') {
    // Try winget for a one-click install; fall back to the download page.
    try {
      execFileSync('winget', [
        'install', '--id', 'Ollama.Ollama', '-e',
        '--accept-source-agreements', '--accept-package-agreements',
      ], { stdio: 'inherit' });
      return hasOllama() ? 'installed' : 'attempted';
    } catch (e) {
      try { shell.openExternal('https://ollama.com/download'); } catch { /* ignore */ }
      return 'opened_page';
    }
  }
  try { shell.openExternal('https://ollama.com/download'); } catch { /* ignore */ }
  return 'opened_page';
}

function runPip(python) {
  return new Promise((resolve) => {
    const files = reqFiles();
    if (!files.length) return resolve(true);
    let i = 0;
    let allOk = true;
    const next = () => {
      if (i >= files.length) return resolve(allOk);
      const f = files[i++];
      let p;
      try {
        // --no-cache-dir avoids a locked/admin-owned pip wheel cache (Errno 13);
        // --user installs to the per-user site so NO admin elevation is needed.
        p = spawn(python, ['-m', 'pip', 'install', '--user', '--no-cache-dir',
                           '--disable-pip-version-check', '-r', f], {
          cwd: projectRoot(), stdio: 'inherit',
        });
      } catch (e) {
        allOk = false; return next();
      }
      p.on('exit', (code) => { if (code !== 0) allOk = false; next(); });
      p.on('error', () => { allOk = false; next(); });
    };
    next();
  });
}

/**
 * Run first-run setup if needed. Safe to await unconditionally on every boot —
 * it returns immediately once setup has completed (run-once).
 */
async function ensureSetup() {
  let firstRun = true;
  let needDeps = true;
  try {
    const m = readMarker();
    if (m) {
      firstRun = false;
      // Re-run pip only if requirements changed or a prior install failed.
      needDeps = (m.req_hash !== reqHash()) || (m.deps_ok === false);
    }
  } catch { /* treat as first run */ }

  if (!firstRun && !needDeps) return; // already set up — honor run-once

  const python = findPython();

  // Ollama consent is a first-run concern (load-bearing). Don't re-nag later.
  let ollamaState = hasOllama() ? 'present' : 'missing';
  if (firstRun && ollamaState === 'missing') {
    try { ollamaState = await ensureOllama(); } catch (e) {
      console.error('[first-run] ollama step error:', e && e.message);
      ollamaState = 'error';
    }
  }

  let depsOk = true;
  if (needDeps) {
    if (!python) {
      dialog.showMessageBoxSync({
        type: 'warning',
        title: 'OracleAI — Python needed',
        message: 'Python 3.10+ was not found on this machine.',
        detail:
          'OracleAI needs Python to install its dependencies. Please install ' +
          'Python 3.10+ from python.org (tick "Add python.exe to PATH"), then ' +
          'relaunch OracleAI.',
        buttons: ['Open python.org', 'Continue anyway'],
        defaultId: 0,
        cancelId: 1,
      });
      try { shell.openExternal('https://www.python.org/downloads/'); } catch { /* ignore */ }
      depsOk = false;
    } else {
      const splash = makeSplash('Installing Python dependencies&hellip;');
      try { depsOk = await runPip(python); } catch (e) {
        console.error('[first-run] pip error:', e && e.message);
        depsOk = false;
      }
      try { if (splash && !splash.isDestroyed()) splash.close(); } catch { /* ignore */ }
    }
  }

  writeMarker({
    deps_ok: depsOk,
    req_hash: reqHash(),
    python: python || null,
    ollama: ollamaState,
    version: (() => { try { return app.getVersion(); } catch { return null; } })(),
    ts: new Date().toISOString(),
  });
}

module.exports = { ensureSetup, needsSetup: () => {
  try {
    const m = readMarker();
    if (!m) return true;
    return (m.req_hash !== reqHash()) || (m.deps_ok === false);
  } catch { return true; }
}, hasOllama };
