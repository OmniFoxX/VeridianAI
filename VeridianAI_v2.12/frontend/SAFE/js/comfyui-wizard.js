/**
 * ComfyUI Setup Wizard — VeridianAI v2.9.10
 * =========================================
 * Handles detection, download, install, and verification of ComfyUI portable.
 * Triggered by the image generation button (🎨) when ComfyUI is not set up.
 *
 * Public API:
 *   ComfyUIWizard.check()   — check setup status, show wizard if needed
 *   ComfyUIWizard.show()    — force-show the wizard
 *   ComfyUIWizard.dismiss() — close the wizard
 */

const ComfyUIWizard = (() => {
  'use strict';

  // ── State ────────────────────────────────────────────────────────────────
  let _overlay      = null;
  let _installing   = false;
  let _abortCtrl    = null;
  let _onComplete   = null;   // callback fired when setup succeeds
  let _triggerEl    = null;   // WCAG 2.4.3: focus restore on close
  let _justCompleted = false; // one-shot guard: skip re-check on post-install retry

  // Default install path shown in the wizard input.
  // The backend resolves the real default; this is just a hint.
  const DEFAULT_HINT = '%USERPROFILE%\\VeridianAI\\backend';

  // ── API calls ────────────────────────────────────────────────────────────
  async function _fetchStatus() {
    try {
      const r = await fetch('/api/comfyui/setup-status');
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    } catch (e) {
      return { installed: false, error: e.message };
    }
  }

  async function _runSetup(installDir) {
    const body = installDir ? { install_dir: installDir } : {};
    const r = await fetch('/api/comfyui/run-setup', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  }

  async function _downloadModel(key) {
    const r = await fetch('/api/comfyui/download-model', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ key }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  }

  // ── Progress polling ─────────────────────────────────────────────────────
  // The backend streams progress via SSE on /api/comfyui/setup-progress.
  // We consume it and update the wizard UI in real time.
  function _streamProgress(onMessage, onDone, onError) {
    const es = new EventSource('/api/comfyui/setup-progress');
    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        onMessage(data);
        if (data.done) { es.close(); onDone(data); }
      } catch (_) {}
    };
    es.onerror = (e) => { es.close(); onError(e); };
    return es;
  }

  // ── DOM helpers ──────────────────────────────────────────────────────────
  function _setProgress(pct, message, state) {
    const fill  = document.getElementById('wiz-bar-fill');
    const label = document.getElementById('wiz-bar-label');
    if (fill)  {
      fill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
      fill.className   = 'wizard-progress-bar-fill' +
                         (state === 'error'    ? ' error'    :
                          state === 'complete' ? ' complete' : '');
    }
    if (label && message) label.textContent = message;
  }

  function _appendLog(message, type) {
    const box = document.getElementById('wiz-status-log');
    if (!box) return;
    box.classList.add('visible');
    const line = document.createElement('span');
    line.className = `log-line ${type || ''}`;
    line.textContent = message;
    box.appendChild(line);
    box.appendChild(document.createElement('br'));
    box.scrollTop = box.scrollHeight;
  }

  function _setButtons(installing) {
    const btn    = document.getElementById('wiz-btn-install');
    const cancel = document.getElementById('wiz-btn-cancel');
    if (btn)    btn.disabled    = installing;
    if (cancel) cancel.textContent = installing ? 'Cancel' : 'Skip for now';
  }

  // ── Wizard HTML ──────────────────────────────────────────────────────────
  function _buildHTML(status) {
    const alreadyInstalled = status && status.installed;
    const comfyHome        = (status && status.comfy_home) || '';
    const headless         = status && status.headless_ready;
	const routedToRemote = status && status.routed_to_remote;
	const remoteUrl      = (status && status.remote_node_url) || '';

    return `
<div class="comfyui-wizard-overlay" id="wiz-overlay"
     role="dialog" aria-modal="true"
     aria-labelledby="wiz-title"
     aria-describedby="wiz-desc">
  <div class="comfyui-wizard">

    <div class="wizard-header">
      <h2 class="wizard-title" id="wiz-title">Image Generation Setup</h2>
      <button class="wizard-close"
              onclick="ComfyUIWizard.dismiss()"
              aria-label="Close setup wizard"
              title="Close">✕</button>
    </div>

    <div class="wizard-body">

      ${routedToRemote ? `
	  <div class="wizard-already-installed" role="status"
		   style="background:rgba(201,168,76,0.1);
				  border-color:rgba(201,168,76,0.3);
				  color:var(--gold)">
		<span>🔀</span>
		<span>Image generation is routed to <strong>${remoteUrl}</strong> via Toga Network.
		No local ComfyUI install is needed on this machine.</span>
	  </div>
	  ` : ''}
	  ${alreadyInstalled ? `
      <div class="wizard-already-installed" role="status">
        <span>✓</span>
        <span>ComfyUI is installed and ready at <strong>${comfyHome}</strong></span>
        ${headless
          ? '<span class="wizard-badge headless">✓ Headless</span>'
          : '<span class="wizard-badge windowed">⚠ Windowed fallback</span>'}
      </div>
      ` : `
      <p class="wizard-description" id="wiz-desc">
        <strong>VeridianAI needs ComfyUI</strong> to generate images. The setup
        wizard will download the official portable package from GitHub, install
        it silently in the background, and configure everything automatically.
        <br><br>
        No window will open. ComfyUI runs headlessly — it starts when you
        generate, and stops when you're done.
      </p>

      <div class="wizard-steps" aria-label="Setup progress steps">
		<span class="wizard-step-dot" id="wiz-dot-0" aria-hidden="true"></span>
		<span class="wizard-step-dot" id="wiz-dot-1" aria-hidden="true"></span>
		<span class="wizard-step-dot" id="wiz-dot-2" aria-hidden="true"></span>
		<span class="wizard-step-dot" id="wiz-dot-3" aria-hidden="true"></span>
		<span style="font-family:'Rajdhani',sans-serif;
					font-size:0.8rem;
					color:var(--text-muted);
					margin-left:6px">
			Download → Extract → Dependencies → Verify
		</span>
		</div>

      <div class="setting-group">
        <label class="setting-label"
               for="wiz-install-dir"
               title="Where to install ComfyUI. Leave blank for the default location.">
          Install location
          <span style="color:var(--text-faint);font-size:0.8em">
            (blank = default)
          </span>
        </label>
        <div class="wizard-path-row">
          <input id="wiz-install-dir"
                 class="wizard-path-input"
                 type="text"
                 placeholder="${DEFAULT_HINT}"
                 aria-label="ComfyUI install directory"
                 autocomplete="off"
                 spellcheck="false">
        </div>
        <div style="font-size:0.78em;
                    opacity:0.6;
                    margin-top:3px;
                    font-family:'Rajdhani',sans-serif">
          VeridianAI will install ComfyUI here and remember this location.
          You can change it later in Settings → Image Generation.
        </div>
      </div>

      <div class="wizard-progress-wrap" id="wiz-progress-wrap"
           style="display:none" aria-live="polite">
        <div class="wizard-progress-label"
             id="wiz-bar-label">Preparing…</div>
        <div class="wizard-progress-bar-track"
             role="progressbar"
             aria-valuemin="0"
             aria-valuemax="100"
             aria-valuenow="0"
             aria-labelledby="wiz-bar-label"
             id="wiz-bar-track">
          <div class="wizard-progress-bar-fill"
               id="wiz-bar-fill"></div>
        </div>
      </div>

      <div class="wizard-status"
           id="wiz-status-log"
           aria-live="polite"
           aria-label="Setup log"></div>
      `}

    </div><!-- .wizard-body -->

    <div class="wizard-footer">
      ${alreadyInstalled ? `
        <button class="wizard-btn-primary"
                onclick="ComfyUIWizard.dismiss()">
          Got it
        </button>
      ` : `
        <button class="wizard-btn-secondary"
                id="wiz-btn-cancel"
                onclick="ComfyUIWizard.dismiss()">
          Skip for now
        </button>
        <button class="wizard-btn-primary"
                id="wiz-btn-install"
                onclick="ComfyUIWizard._startInstall()">
          Download &amp; Install
        </button>
      `}
    </div>

  </div><!-- .comfyui-wizard -->
</div><!-- .comfyui-wizard-overlay -->
    `;
  }

  // ── Install flow ─────────────────────────────────────────────────────────
  async function _startInstall() {
    if (_installing) return;
    _installing = true;
    _setButtons(true);

    const dirInput   = document.getElementById('wiz-install-dir');
    const installDir = (dirInput && dirInput.value.trim()) || null;
    const progressWrap = document.getElementById('wiz-progress-wrap');
    if (progressWrap) progressWrap.style.display = 'flex';

    _setProgress(1, 'Connecting to GitHub…', '');
    _appendLog('Starting ComfyUI setup…', 'info');

    // Step dots: map percent ranges to dot indices
    const dotThresholds = [0, 62, 76, 91];

    let es = null;
    try {
      // Fire the setup POST (non-blocking on backend — returns immediately)
      const kickoff = await _runSetup(installDir);
      if (kickoff && kickoff.error) {
        throw new Error(kickoff.error);
      }

      // Stream progress events from the backend SSE endpoint
      await new Promise((resolve, reject) => {
        es = _streamProgress(
          // onMessage
          (data) => {
            const pct = typeof data.percent === 'number' ? data.percent : -1;
            const msg = data.message || '';

            if (pct >= 0) {
              _setProgress(pct, msg, '');
              // Update ARIA
              const track = document.getElementById('wiz-bar-track');
              if (track) track.setAttribute('aria-valuenow', pct);
              // Advance step dots
              dotThresholds.forEach((threshold, i) => {
                const dot = document.getElementById(`wiz-dot-${i}`);
                if (!dot) return;
                if (pct >= threshold && i < dotThresholds.length - 1
                    && pct < (dotThresholds[i + 1] || 101)) {
                  dot.classList.add('active');
                } else if (pct >= (dotThresholds[i + 1] || 101)) {
                  dot.classList.remove('active');
                  dot.classList.add('complete');
                }
              });
            }

            if (msg) _appendLog(msg, pct === 100 ? 'success' : '');

            if (data.done) {
              if (data.success) resolve(data);
              else reject(new Error(data.error || 'Setup failed'));
            }
          },
          // onDone
          (data) => {
            if (data.success) resolve(data);
            else reject(new Error(data.error || 'Setup failed'));
          },
          // onError
          (e) => reject(new Error('Lost connection to setup stream'))
        );
      });

      // ── Success ──────────────────────────────────────────────────────────
      _setProgress(100, 'ComfyUI installed and ready!', 'complete');
      _appendLog('✓ Setup complete. ComfyUI is ready for headless operation.', 'success');
      _setButtons(false);

      // Mark all dots complete
      for (let i = 0; i < 4; i++) {
        const dot = document.getElementById(`wiz-dot-${i}`);
        if (dot) { dot.classList.remove('active'); dot.classList.add('complete'); }
      }

      // Replace footer with a "Start generating" button
      const footer = _overlay && _overlay.querySelector('.wizard-footer');
      if (footer) {
        footer.innerHTML = `
          <span class="wizard-badge headless">✓ Headless ready</span>
          <button class="wizard-btn-primary"
                  onclick="ComfyUIWizard._afterInstall()">
            Next: Choose a Model →
          </button>
        `;
      }

    } catch (err) {
      if (es) try { es.close(); } catch (_) {}
      _setProgress(0, `Setup failed: ${err.message}`, 'error');
      _appendLog(`✗ ${err.message}`, 'error');
      _appendLog('You can retry, or set the ComfyUI path manually in Settings → Image Generation.', '');
      _setButtons(false);
      _installing = false;

      // Swap install button to Retry
      const btn = document.getElementById('wiz-btn-install');
      if (btn) {
        btn.textContent = 'Retry';
        btn.disabled    = false;
      }
    }
  }

  function _onSuccess() {
    _justCompleted = true;   // skip the re-check on the immediate retry
    dismiss();
    if (typeof _onComplete === 'function') _onComplete();
  }

  // ── Model picker (after install, or when ComfyUI has no checkpoint) ─────────
  async function _afterInstall() {
    let status = {};
    try { status = await _fetchStatus(); } catch (_) {}
    if (status && status.has_model) { _onSuccess(); return; }
    showModelPicker(status, _onComplete);
  }

  function _accelNote(gpu) {
    if (!gpu) return '';
    if (gpu.vendor === 'nvidia') return 'NVIDIA GPU · CUDA acceleration';
    if (gpu.vendor === 'amd')    return 'AMD GPU · DirectML if available, else CPU';
    if (gpu.vendor === 'intel')  return 'Intel GPU · DirectML if available, else CPU';
    return 'No GPU detected · CPU mode (slower)';
  }

  function _buildModelPickerHTML(status) {
    const catalog   = (status && status.models_catalog) || [];
    const installed = (status && status.installed_models) || [];
    const active    = (status && status.selected_checkpoint) || '';
    const inSet     = new Set(installed);
    const known     = new Set(catalog.map((m) => m.filename));
    const gpu       = (status && status.gpu) || null;
    const gpuLine   = gpu ? `<div class="wizard-gpu-line">Detected: ${gpu.name} — ${_accelNote(gpu)}</div>` : '';
    const dmlBtn    = (gpu && (gpu.vendor === 'amd' || gpu.vendor === 'intel'))
      ? `<button class="wizard-btn-secondary" type="button" onclick="ComfyUIWizard._enableDirectml()">Enable DirectML (AMD/Intel)</button>`
      : '';

    const cards = catalog.map((m) => {
      const isInstalled = inSet.has(m.filename);
      const isActive    = active === m.filename;
      let action;
      if (isActive) {
        action = `<span class="wizard-badge headless">✓ Active</span>`;
      } else if (isInstalled) {
        action = `<button class="wizard-btn-secondary" type="button" onclick="ComfyUIWizard._useModel('${m.filename}')">Use this model</button>`;
      } else {
        action = `<button class="wizard-btn-primary" type="button" onclick="ComfyUIWizard._pickModel('${m.key}')">Download</button>`;
      }
      const del = isInstalled
        ? `<button class="wizard-model-del" type="button" title="Delete this model file to free disk space" onclick="ComfyUIWizard._deleteModel('${m.filename}')">Delete</button>`
        : '';
      const tag = isInstalled ? ' <span class="wizard-model-tag">installed</span>' : '';
      return `
      <div class="wizard-model-card${isActive ? ' selected' : ''}" id="wiz-model-${m.key}">
        <div class="wizard-model-name">${m.label}${tag}</div>
        <div class="wizard-model-meta">${m.size_label} • ${m.vram_label}</div>
        <div class="wizard-model-blurb">${m.blurb}</div>
        <div class="wizard-model-action">${action}${del}</div>
      </div>`;
    }).join('');

    const extras = installed.filter((f) => !known.has(f)).map((f) => {
      const isActive = active === f;
      const action = isActive
        ? `<span class="wizard-badge headless">✓ Active</span>`
        : `<button class="wizard-btn-secondary" type="button" onclick="ComfyUIWizard._useModel('${f}')">Use this model</button>`;
      const del = `<button class="wizard-model-del" type="button" title="Delete this model file to free disk space" onclick="ComfyUIWizard._deleteModel('${f}')">Delete</button>`;
      return `
      <div class="wizard-model-card${isActive ? ' selected' : ''}">
        <div class="wizard-model-name">${f} <span class="wizard-model-tag">installed</span></div>
        <div class="wizard-model-meta">custom checkpoint</div>
        <div class="wizard-model-action">${action}${del}</div>
      </div>`;
    }).join('');

    return `
<div class="comfyui-wizard-overlay" id="wiz-overlay" role="dialog" aria-modal="true" aria-labelledby="wiz-title">
  <div class="comfyui-wizard">
    <div class="wizard-header">
      <h2 class="wizard-title" id="wiz-title">Image Models</h2>
      <button class="wizard-close" onclick="ComfyUIWizard.dismiss()" aria-label="Close" title="Close">✕</button>
    </div>
    <div class="wizard-body">
      <p class="wizard-description">Choose which model to use, or download another. Your choice is remembered. Add your own in <code>models/checkpoints</code>.</p>
      ${gpuLine}
      <div class="wizard-model-grid">${cards}${extras}</div>
      <div class="wizard-progress-wrap" id="wiz-progress-wrap" style="display:none" aria-live="polite">
        <div class="wizard-progress-label" id="wiz-bar-label">Preparing…</div>
        <div class="wizard-progress-bar-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" id="wiz-bar-track">
          <div class="wizard-progress-bar-fill" id="wiz-bar-fill"></div>
        </div>
      </div>
      <div class="wizard-status" id="wiz-status-log" aria-live="polite"></div>
    </div>
    <div class="wizard-footer">
      ${dmlBtn}
      <button class="wizard-btn-secondary" id="wiz-btn-cancel" onclick="ComfyUIWizard.dismiss()">Close</button>
    </div>
  </div>
</div>`;
  }

  function showModelPicker(status, onComplete) {
    // Generate/install flows pass their callback explicitly; manage() passes
    // null to CLEAR it so switching models from Settings never auto-generates.
    _onComplete = onComplete || null;
    _triggerEl  = document.activeElement;
    if (_overlay) dismiss();
    const root = document.getElementById('modal-root');
    if (!root) return;
    root.insertAdjacentHTML('beforeend', _buildModelPickerHTML(status || {}));
    _overlay = document.getElementById('wiz-overlay');
    _installing = false;
    _overlay.addEventListener('click', (e) => { if (e.target === _overlay && !_installing) dismiss(); });
    _trapFocus(_overlay);
  }

  async function _pickModel(key) {
    if (_installing) return;
    _installing = true;
    const grid = document.querySelector('.wizard-model-grid');
    if (grid) grid.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    const picked = document.getElementById(`wiz-model-${key}`);
    if (picked) picked.classList.add('selected');
    const wrap = document.getElementById('wiz-progress-wrap');
    if (wrap) wrap.style.display = 'flex';
    _setProgress(1, 'Starting download…', '');
    _appendLog('Starting model download…', 'info');
    let es = null;
    try {
      const kickoff = await _downloadModel(key);
      if (kickoff && kickoff.error) throw new Error(kickoff.error);
      await new Promise((resolve, reject) => {
        es = _streamProgress(
          (data) => {
            const pct = typeof data.percent === 'number' ? data.percent : -1;
            const msg = data.message || '';
            if (pct >= 0) _setProgress(pct, msg, '');
            if (msg) _appendLog(msg, pct === 100 ? 'success' : '');
            if (data.done) { if (data.success) resolve(data); else reject(new Error(data.error || 'Download failed')); }
          },
          (data) => { data.success ? resolve(data) : reject(new Error(data.error || 'Download failed')); },
          (e) => reject(new Error('Lost connection to download stream'))
        );
      });
      _setProgress(100, 'Model ready!', 'complete');
      _appendLog('✓ Model installed. You can generate now.', 'success');
      _installing = false;
      const footer = _overlay && _overlay.querySelector('.wizard-footer');
      if (footer) { footer.innerHTML = `<button class="wizard-btn-primary" onclick="ComfyUIWizard._onSuccess()">Start Generating</button>`; }
    } catch (err) {
      if (es) try { es.close(); } catch (_) {}
      _setProgress(0, `Download failed: ${err.message}`, 'error');
      _appendLog(`✗ ${err.message}`, 'error');
      _installing = false;
      const grid2 = document.querySelector('.wizard-model-grid');
      if (grid2) grid2.querySelectorAll('button').forEach((b) => { b.disabled = false; });
    }
  }

  // Switch the active model to an already-installed checkpoint.
  async function _useModel(checkpoint) {
    try {
      const r = await fetch('/api/comfyui/select-model', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ checkpoint }),
      });
      const res = await r.json();
      if (!res || !res.success) throw new Error((res && res.error) || 'Could not select model');
      if (typeof _onComplete === 'function') {
        _onSuccess();                  // came from a generate request -> proceed
      } else {
        let status = {};               // manage mode -> refresh to show new Active
        try { status = await _fetchStatus(); } catch (_) {}
        showModelPicker(status, null);
      }
    } catch (e) {
      _appendLog(`✗ ${e.message}`, 'error');
    }
  }

  // Delete an installed checkpoint to reclaim disk; re-renders the picker.
  async function _deleteModel(checkpoint) {
    if (!(await window.oracleConfirm(`Delete ${checkpoint}? This frees disk space; you'd need to re-download to use it again.`, { title: "Delete model", okLabel: "Delete" }))) return;
    try {
      const r = await fetch('/api/comfyui/delete-model', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ checkpoint }),
      });
      const res = await r.json();
      if (!res || !res.success) throw new Error((res && res.error) || 'Delete failed');
      let status = {};
      try { status = await _fetchStatus(); } catch (_) {}
      showModelPicker(status, _onComplete);   // re-render to reflect removal
    } catch (e) {
      _appendLog(`✗ ${e.message}`, 'error');
    }
  }

  // Opt-in DirectML installer for AMD/Intel GPUs (server refuses on NVIDIA).
  async function _enableDirectml() {
    if (_installing) return;
    if (!(await window.oracleConfirm('Set up the DirectML image engine for AMD/Intel GPUs?\n\n'
        + 'This creates a SEPARATE Python 3.12 + PyTorch-DirectML environment (~1–1.5 GB) '
        + 'that runs ComfyUI on your GPU. It best supports SD 1.5 / SDXL (not Flux), and '
        + 'never touches an NVIDIA/CUDA setup. First-time setup can take several minutes.', { title: "DirectML setup", okLabel: "Set up" }))) return;
    _installing = true;
    const wrap = document.getElementById('wiz-progress-wrap');
    if (wrap) wrap.style.display = 'flex';
    _setProgress(5, 'Installing DirectML…', '');
    _appendLog('Installing torch-directml (this can take several minutes)…', 'info');
    let es = null;
    try {
      const r = await fetch('/api/comfyui/enable-directml', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const kick = await r.json();
      if (kick && kick.error) throw new Error(kick.error);
      await new Promise((resolve, reject) => {
        es = _streamProgress(
          (data) => {
            const pct = typeof data.percent === 'number' ? data.percent : -1;
            if (pct >= 0) _setProgress(pct, data.message || '', '');
            if (data.message) _appendLog(data.message, pct === 100 ? 'success' : '');
            if (data.done) { data.success ? resolve(data) : reject(new Error(data.error || 'Install failed')); }
          },
          (data) => { data.success ? resolve(data) : reject(new Error(data.error || 'Install failed')); },
          (e) => reject(new Error('Lost connection to install stream'))
        );
      });
      _setProgress(100, 'DirectML enabled!', 'complete');
      _appendLog('✓ DirectML installed. Image generation will use your GPU on the next run.', 'success');
      _installing = false;
    } catch (err) {
      if (es) try { es.close(); } catch (_) {}
      _setProgress(0, `DirectML install failed: ${err.message}`, 'error');
      _appendLog(`✗ ${err.message}`, 'error');
      _installing = false;
    }
  }

  // Re-openable model manager (e.g. from Settings -> Image Generation).
  async function manage() {
    let status = {};
    try { status = await _fetchStatus(); } catch (_) {}
    showModelPicker(status, null);
  }

  // ── Public API ────────────────────────────────────────────────────────────
  async function check(onComplete) {
    _onComplete = onComplete || null;

    // If setup just completed in this session, proceed straight to generation
    // on the immediate retry instead of re-prompting (one-shot guard).
    if (_justCompleted) {
      _justCompleted = false;
      return true;
    }

    const status = await _fetchStatus();
	
	// Remote offload is active -- this machine doesn't need local ComfyUI.
    // Don't show the wizard at all.
    if (status.routed_to_remote) {
        return true;
    }
	
    if (!status.installed || status.setup_required) {
      show(status, onComplete);
      return false;
    }
    // Installed, but no checkpoint yet -> let the user pick + download a model.
    if (!status.has_model) {
      showModelPicker(status, onComplete);
      return false;
    }
    return true;
  }

  function show(status, onComplete) {
      _onComplete  = onComplete || null;
      _triggerEl   = document.activeElement; // capture before we move focus
      if (_overlay) dismiss();
      const root = document.getElementById('modal-root');
      if (!root) return;
      root.insertAdjacentHTML('beforeend', _buildHTML(status || {}));
      _overlay = document.getElementById('wiz-overlay');
      _installing = false;

      _overlay.addEventListener('click', (e) => {
          if (e.target === _overlay && !_installing) dismiss();
      });

      // Reset progress bar aria value on fresh open
      const track = document.getElementById('wiz-bar-track');
      if (track) track.setAttribute('aria-valuenow', '0');

      _trapFocus(_overlay);

      const announce = document.getElementById('error-announce');
      if (announce) announce.textContent = 'Image generation setup wizard opened.';
  }

  async function dismiss() {
      if (_installing && _overlay) {
          if (!(await window.oracleConfirm('Setup is in progress. Close anyway?', { title: "Close setup?", okLabel: "Close" }))) return;
          _installing = false;
      }
      if (_overlay) {
          _overlay.remove();
          _overlay = null;
      } 
      _installing = false;

      // Return focus to the element that opened the wizard (WCAG 2.4.3)
      try {
          if (_triggerEl && typeof _triggerEl.focus === 'function') {
              _triggerEl.focus();
          }
      } catch(_) {}
      _triggerEl = null;
  }

  // ── Focus trap (WCAG 2.1 — matches VeridianAI's existing pattern) ──────────
  function _trapFocus(el) {
      const focusableSelectors = [
          'button:not([disabled])',
          'input:not([disabled])',
          'textarea:not([disabled])',
          '[tabindex]:not([tabindex="-1"])'
      ].join(', ');

      const getFocusable = () => Array.from(el.querySelectorAll(focusableSelectors))
          .filter(n => n.offsetParent !== null); // visible only

      const focusable = getFocusable();
      if (!focusable.length) return;

      // Focus the first focusable element
      try { focusable.focus(); } catch(_) {}

      el.addEventListener('keydown', (e) => {
          const current = getFocusable(); // re-query in case DOM changed
          if (!current.length) return;
          const first = current;
          const last  = current[current.length - 1];

          if (e.key === 'Escape' && !_installing) {
              dismiss();
              return;
          }
          if (e.key !== 'Tab') return;
          if (e.shiftKey) {
              if (document.activeElement === first) {
                  e.preventDefault();
                  last.focus();
              }
          } else {
              if (document.activeElement === last) {
                  e.preventDefault();
                  first.focus();
              }
          }
      });
  }

  // Expose _startInstall so the inline onclick can reach it
  return { check, show, dismiss, _startInstall, _onSuccess,
           showModelPicker, _pickModel, _afterInstall, _useModel, manage, _deleteModel,
           _enableDirectml };

})();

window.ComfyUIWizard = ComfyUIWizard;