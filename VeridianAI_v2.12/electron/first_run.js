 /**
 * VeridianAI — Setup Assistant (first-run bootstrap, Electron main process).
 *
 * v2.11.15: rebuilt from the old silent first_run into a visible, guided
 * installer. Goal (Todd, 2026-07-04): download → unzip → double-click the
 * exe → watch a friendly progress window → UI pops up → ask Toga questions.
 *
 * Steps (each independent, resumable, and recorded in the marker):
 *   1. Python       — detect; if missing, install via winget (user consent),
 *                     then locate the fresh install DIRECTLY (PATH is stale
 *                     in this session) and adopt it.
 *   2. Dependencies — pip install backend/requirements.txt with LIVE output
 *                     streamed into the window. Re-runs only when
 *                     requirements.txt changes (hash) or a prior run failed.
 *   3. Ollama       — consent → winget install → locate exe directly and
 *                     prepend its dir to THIS session's PATH so the very
 *                     first backend launch finds it (no logout needed).
 *   4. Starter model— if Ollama is present but has NO models, offer a small
 *                     starter pull (llama3.2:3b, ~2 GB) with live progress.
 *                     Skippable; power users with model libraries never see it.
 *   5. Data folders — ensure the sage_data layout exists.
 *
 * Fully defensive: any failure logs, marks the step, and continues — setup
 * can never brick the launch. Everything is also written to
 * sage_data/setup.log for debugging.
 */
const { app, dialog, BrowserWindow, shell } = require('electron');
const { spawn, execFileSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');

// --- paths -------------------------------------------------------------
function projectRoot() {
  return app.isPackaged ? path.dirname(app.getPath('exe')) : path.resolve(__dirname, '..');
}
function sageDataDir() {
  return path.resolve(projectRoot(), '..', 'sage_data');
}
function markerPath() {
  return path.join(sageDataDir(), '.oracle_setup.json');
}
function setupLogPath() {
  return path.join(sageDataDir(), 'setup.log');
}

// --- setup log (file + console) -----------------------------------------
function slog(line) {
  const msg = `[setup ${new Date().toISOString()}] ${line}`;
  console.log(msg);
  try {
    fs.mkdirSync(sageDataDir(), { recursive: true });
    fs.appendFileSync(setupLogPath(), msg + '\n', 'utf8');
  } catch { /* best effort */ }
}

// --- requirements hash (re-run pip only when the manifest changes) -------
function reqFile() {
  const f = path.join(projectRoot(), 'backend', 'requirements.txt');
  try { return fs.existsSync(f) ? f : null; } catch { return null; }
}
function reqHash() {
  const h = crypto.createHash('sha256');
  const f = reqFile();
  if (f) { try { h.update(fs.readFileSync(f)); } catch { /* ignore */ } }
  return h.digest('hex');
}

// --- marker ---------------------------------------------------------------
function readMarker() {
  try { return JSON.parse(fs.readFileSync(markerPath(), 'utf8')); } catch { return null; }
}
function writeMarker(obj) {
  try {
    fs.mkdirSync(sageDataDir(), { recursive: true });
    fs.writeFileSync(markerPath(), JSON.stringify(obj, null, 2), 'utf8');
  } catch (e) {
    slog('could not write marker: ' + (e && e.message));
  }
}

// --- session PATH (the same-session trap) ----------------------------------
// winget updates the PATH *registry*, but this already-running process — and
// every child it spawns (start.bat, tier_launcher, pip) — inherits the OLD
// environment. Prepending the freshly installed tool's directory here makes
// the whole first session work without a logout/relogin.
function addToSessionPath(dir) {
  try {
    if (!dir) return;
    const cur = process.env.PATH || '';
    if (!cur.toLowerCase().split(';').includes(dir.toLowerCase())) {
      process.env.PATH = dir + ';' + cur;
      slog('session PATH += ' + dir);
    }
  } catch { /* ignore */ }
}

// --- tool discovery ----------------------------------------------------
function tryVersion(cmd) {
  try { execFileSync(cmd, ['--version'], { stdio: 'ignore', timeout: 15000 }); return true; }
  catch { return false; }
}
function findPython() {
  for (const cmd of ['py', 'python', 'python3']) {
    if (tryVersion(cmd)) return cmd;
  }
  // PATH may be stale (just installed): look where the python.org/winget
  // installers actually put it, newest version first.
  const local = process.env.LOCALAPPDATA || '';
  if (local) {
    const base = path.join(local, 'Programs', 'Python');
    try {
      const dirs = fs.readdirSync(base).filter((d) => /^Python3\d+$/i.test(d)).sort().reverse();
      for (const d of dirs) {
        const exe = path.join(base, d, 'python.exe');
        if (fs.existsSync(exe)) { addToSessionPath(path.dirname(exe)); return exe; }
      }
    } catch { /* no such dir */ }
  }
  return null;
}
function findOllama() {
  if (tryVersion('ollama')) return 'ollama';
  const cands = [];
  if (process.env.LOCALAPPDATA) {
    cands.push(path.join(process.env.LOCALAPPDATA, 'Programs', 'Ollama', 'ollama.exe'));
  }
  cands.push(path.join(process.env.ProgramFiles || 'C:\\Program Files', 'Ollama', 'ollama.exe'));
  for (const c of cands) {
    try { if (fs.existsSync(c)) { addToSessionPath(path.dirname(c)); return c; } } catch { /* next */ }
  }
  return null;
}
function hasWinget() { return tryVersion('winget'); }

// --- child process with live output ------------------------------------
function run(cmd, args, onLine, opts) {
  return new Promise((resolve) => {
    let p;
    try {
      p = spawn(cmd, args, {
        cwd: projectRoot(), windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'], ...(opts || {}),
      });
    } catch (e) {
      slog(`spawn failed: ${cmd}: ${e && e.message}`);
      return resolve(-1);
    }
    let buf = '';
    const feed = (chunk) => {
      buf += chunk.toString();
      let idx;
      while ((idx = buf.search(/\r|\n/)) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (line && onLine) onLine(line);
      }
    };
    p.stdout.on('data', feed);
    p.stderr.on('data', feed);
    p.on('error', () => resolve(-1));
    p.on('exit', (code) => resolve(code == null ? -1 : code));
  });
}

// --- the Setup Assistant window --------------------------------------------
const STEPS = [
  ['python', 'Python runtime'],
  ['deps', 'VeridianAI components'],
  ['ollama', 'Ollama (local AI engine)'],
  ['model', 'Starter model'],
  ['data', 'Data folders'],
];

function makeSetupWindow() {
  let win = null;
  try {
    win = new BrowserWindow({
      width: 560, height: 460, frame: false, resizable: false, center: true,
      alwaysOnTop: true, show: true, backgroundColor: '#0b1430',
      webPreferences: { contextIsolation: true },
    });
    const rows = STEPS.map(([id, label]) =>
      `<div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid #182448">
         <span id="ic-${id}" style="width:20px;text-align:center;opacity:.55">•</span>
         <span style="flex:1">${label}</span>
         <span id="st-${id}" style="font-size:11px;opacity:.7;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">waiting</span>
       </div>`).join('');
    const html =
      '<!doctype html><meta charset="utf-8"><body style="margin:0;height:100vh;box-sizing:border-box;' +
      'font-family:Segoe UI,system-ui,sans-serif;background:#0b1430;color:#e9edf6;padding:26px 30px;overflow:hidden">' +
      '<div style="font-size:18px;letter-spacing:.16em;color:#f0a500;text-align:center;margin-bottom:4px">V E R I D I A N&nbsp;&nbsp;A I</div>' +
      '<div style="font-size:12px;opacity:.75;text-align:center;margin-bottom:16px">Setting things up — first run only</div>' +
      `<div style="font-size:13px">${rows}</div>` +
      '<div id="live" style="margin-top:12px;font-size:11px;opacity:.65;height:30px;line-height:1.4;overflow:hidden"></div>' +
      '<div style="position:fixed;bottom:12px;left:0;right:0;text-align:center;font-size:10px;opacity:.45">' +
      'You can keep using your computer — VeridianAI will open when everything is ready.</div>' +
      '<script>function setStep(id,icon,txt,color){' +
      'var i=document.getElementById("ic-"+id),s=document.getElementById("st-"+id);' +
      'if(i){i.textContent=icon;i.style.opacity=1;i.style.color=color||"";}' +
      'if(s&&txt!==undefined){s.textContent=txt;}}' +
      'function live(t){var e=document.getElementById("live");if(e)e.textContent=t;}</script></body>';
    win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html));
  } catch (e) {
    slog('setup window failed: ' + (e && e.message));
  }
  return win;
}

function uiCall(win, js) {
  try {
    if (win && !win.isDestroyed()) win.webContents.executeJavaScript(js).catch(() => {});
  } catch { /* ignore */ }
}
function stepState(win, id, state, txt) {
  const icons = { run: ['⟳', '#f0a500'], ok: ['✓', '#3fbf6f'], warn: ['⚠', '#f0a500'], skip: ['—', '#99a0ad'] };
  const [icon, color] = icons[state] || ['•', ''];
  uiCall(win, `setStep(${JSON.stringify(id)},${JSON.stringify(icon)},${JSON.stringify(txt || '')},${JSON.stringify(color)})`);
}
function liveLine(win, txt) {
  uiCall(win, `live(${JSON.stringify(String(txt).slice(0, 160))})`);
}

// --- steps -----------------------------------------------------------------
async function stepPython(win) {
  stepState(win, 'python', 'run', 'checking…');
  let python = findPython();
  if (python) { stepState(win, 'python', 'ok', 'found'); return python; }

  if (!hasWinget()) {
    stepState(win, 'python', 'warn', 'not found — see python.org');
    dialog.showMessageBoxSync({
      type: 'warning', title: 'VeridianAI — Python needed',
      message: 'Python 3.10+ was not found and automatic install is unavailable.',
      detail: 'Please install Python from python.org (tick "Add python.exe to PATH"), then relaunch VeridianAI.',
      buttons: ['Open python.org', 'Continue anyway'], defaultId: 0, cancelId: 1,
    });
    try { shell.openExternal('https://www.python.org/downloads/'); } catch { /* ignore */ }
    return null;
  }

  const res = dialog.showMessageBoxSync({
    type: 'question', title: 'VeridianAI — install Python?',
    message: 'VeridianAI needs Python (free, from the Python Software Foundation).',
    detail: 'It can be installed automatically now — no clicks needed, no admin required.',
    buttons: ['Install automatically', 'I\'ll install it myself'], defaultId: 0, cancelId: 1,
  });
  if (res !== 0) {
    stepState(win, 'python', 'warn', 'skipped by user');
    try { shell.openExternal('https://www.python.org/downloads/'); } catch { /* ignore */ }
    return null;
  }

  stepState(win, 'python', 'run', 'installing via winget…');
  slog('installing Python via winget');
  const code = await run('winget', [
    'install', '--id', 'Python.Python.3.12', '-e', '--scope', 'user',
    '--accept-source-agreements', '--accept-package-agreements', '--silent',
  ], (l) => liveLine(win, l));
  slog('winget python exit ' + code);
  python = findPython();   // locates the fresh install + fixes session PATH
  stepState(win, 'python', python ? 'ok' : 'warn',
            python ? 'installed' : 'install did not complete');
  return python;
}

async function stepDeps(win, python, needDeps) {
  if (!needDeps) { stepState(win, 'deps', 'ok', 'up to date'); return true; }
  if (!python) { stepState(win, 'deps', 'warn', 'needs Python'); return false; }
  const f = reqFile();
  if (!f) { stepState(win, 'deps', 'ok', 'nothing to install'); return true; }
  stepState(win, 'deps', 'run', 'installing components…');
  // --user: per-user site, no admin. --no-cache-dir: avoids locked caches.
  const code = await run(python, ['-m', 'pip', 'install', '--user', '--no-cache-dir',
                                  '--disable-pip-version-check', '-r', f],
                         (l) => liveLine(win, l));
  const ok = code === 0;
  slog('pip exit ' + code);
  stepState(win, 'deps', ok ? 'ok' : 'warn', ok ? 'installed' : 'some components failed (see setup.log)');
  return ok;
}

async function stepOllama(win, firstRun) {
  stepState(win, 'ollama', 'run', 'checking…');
  let ollama = findOllama();
  if (ollama) { stepState(win, 'ollama', 'ok', 'found'); return ollama; }
  if (!firstRun) { stepState(win, 'ollama', 'skip', 'not installed'); return null; }

  const res = dialog.showMessageBoxSync({
    type: 'question', title: 'VeridianAI — Ollama required',
    message: 'VeridianAI needs Ollama to run its main reasoning model.',
    detail: 'Ollama is a free, local AI runtime — nothing leaves your machine. Install it now?',
    buttons: ['Install automatically', 'Open download page', 'Skip for now'],
    defaultId: 0, cancelId: 2,
  });
  if (res === 2) { stepState(win, 'ollama', 'skip', 'skipped'); return null; }
  if (res === 1) {
    try { shell.openExternal('https://ollama.com/download'); } catch { /* ignore */ }
    stepState(win, 'ollama', 'warn', 'manual install — relaunch after');
    return null;
  }
  if (!hasWinget()) {
    try { shell.openExternal('https://ollama.com/download'); } catch { /* ignore */ }
    stepState(win, 'ollama', 'warn', 'winget unavailable — download page opened');
    return null;
  }
  stepState(win, 'ollama', 'run', 'installing via winget…');
  slog('installing Ollama via winget');
  const code = await run('winget', [
    'install', '--id', 'Ollama.Ollama', '-e',
    '--accept-source-agreements', '--accept-package-agreements', '--silent',
  ], (l) => liveLine(win, l));
  slog('winget ollama exit ' + code);
  ollama = findOllama();   // locates it + fixes session PATH for start.bat
  stepState(win, 'ollama', ollama ? 'ok' : 'warn',
            ollama ? 'installed' : 'install did not complete');
  return ollama;
}

async function stepStarterModel(win, ollama) {
  if (!ollama) { stepState(win, 'model', 'skip', 'needs Ollama'); return 'skipped'; }
  stepState(win, 'model', 'run', 'checking your models…');
  // Any model already present (power users / upgrades) -> nothing to do.
  let hasModels = false;
  await run(ollama, ['list'], (l) => {
    // Header line is "NAME  ID  SIZE  MODIFIED"; any other line = a model.
    if (l && !/^NAME\s+/i.test(l)) hasModels = true;
  });
  if (hasModels) { stepState(win, 'model', 'ok', 'models found'); return 'present'; }

  const res = dialog.showMessageBoxSync({
    type: 'question', title: 'VeridianAI — download a starter model?',
    message: 'No AI models are installed yet.',
    detail: 'VeridianAI can download a small starter model now (Llama 3.2 3B, ' +
            'about 2 GB) so you can chat immediately. You can add bigger or ' +
            'different models any time from the Models menu.',
    buttons: ['Download starter model (~2 GB)', 'Skip — I\'ll add models myself'],
    defaultId: 0, cancelId: 1,
  });
  if (res !== 0) { stepState(win, 'model', 'skip', 'skipped'); return 'skipped'; }

  stepState(win, 'model', 'run', 'downloading llama3.2:3b…');
  slog('pulling starter model llama3.2:3b');
  const code = await run(ollama, ['pull', 'llama3.2:3b'], (l) => {
    liveLine(win, l);
    const m = l.match(/(\d+)%/);
    if (m) stepState(win, 'model', 'run', `downloading… ${m[1]}%`);
  });
  const ok = code === 0;
  slog('ollama pull exit ' + code);
  stepState(win, 'model', ok ? 'ok' : 'warn', ok ? 'ready' : 'download failed (add one later)');
  return ok ? 'pulled' : 'failed';
}

function stepDataDirs(win) {
  stepState(win, 'data', 'run', 'creating…');
  try {
    for (const d of ['', 'models', 'logs', 'users', 'nudges']) {
      fs.mkdirSync(path.join(sageDataDir(), d), { recursive: true });
    }
    stepState(win, 'data', 'ok', 'ready');
    return true;
  } catch (e) {
    slog('data dirs failed: ' + (e && e.message));
    stepState(win, 'data', 'warn', 'could not create sage_data');
    return false;
  }
}

/**
 * Run first-run setup if needed. Safe to await unconditionally on every boot —
 * returns immediately once setup has completed (run-once; pip re-runs only
 * when requirements.txt changes or a prior install failed).
 */
async function ensureSetup() {
  let firstRun = true;
  let needDeps = true;
  try {
    const m = readMarker();
    if (m) {
      firstRun = false;
      needDeps = (m.req_hash !== reqHash()) || (m.deps_ok === false);
      // Re-adopt previously discovered tool dirs into this session's PATH
      // (covers the launch right after an earlier partial setup).
      if (m.python_dir) addToSessionPath(m.python_dir);
      if (m.ollama_dir) addToSessionPath(m.ollama_dir);
    }
  } catch { /* treat as first run */ }

  if (!firstRun && !needDeps) return;   // already set up — honor run-once

  slog(`setup starting (firstRun=${firstRun} needDeps=${needDeps})`);
  const win = firstRun ? makeSetupWindow() : null;   // update runs: quiet pip only

  let python = null, ollama = null, depsOk = false, modelState = 'n/a';
  try {
    python = await stepPython(win);
    depsOk = await stepDeps(win, python, needDeps);
    if (firstRun) {
      ollama = await stepOllama(win, firstRun);
      modelState = await stepStarterModel(win, ollama);
      stepDataDirs(win);
    } else {
      ollama = findOllama();
    }
  } catch (e) {
    slog('setup error: ' + (e && e.message));
  }

  writeMarker({
    deps_ok: depsOk,
    req_hash: reqHash(),
    python: python || null,
    python_dir: (python && path.isAbsolute(python)) ? path.dirname(python) : null,
    ollama: ollama ? 'present' : 'missing',
    ollama_dir: (ollama && path.isAbsolute(ollama)) ? path.dirname(ollama) : null,
    starter_model: modelState,
    version: (() => { try { return app.getVersion(); } catch { return null; } })(),
    ts: new Date().toISOString(),
  });
  slog('setup finished');

  try { if (win && !win.isDestroyed()) win.close(); } catch { /* ignore */ }
}

function hasOllama() { return !!findOllama(); }

module.exports = {
  ensureSetup,
  needsSetup: () => {
    try {
      const m = readMarker();
      if (!m) return true;
      return (m.req_hash !== reqHash()) || (m.deps_ok === false);
    } catch { return true; }
  },
  hasOllama,
};
