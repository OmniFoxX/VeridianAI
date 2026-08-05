/**
 * VeridianAI — Hardware Panel v2
 * Now shows physical cores vs threads, improved Intel Arc detection display.
 */

async function loadHardware() {
  try {
    const resp = await fetch('/api/hardware');
    const hw = await resp.json();
    renderHardwarePanel(hw);
  } catch {
    document.getElementById('hardware-info').innerHTML =
      '<div class="loading-placeholder">Could not reach backend</div>';
  }
}

function _esc(t) {
  return String(t).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function renderHardwarePanel(hw) {
  const container = document.getElementById('hardware-info');
  const togglesDiv = document.getElementById('hw-toggles');
  if (!container) return;

  const os  = hw.os  || {};
  const cpu = hw.cpu || {};
  const cores = cpu.cores || '?';
  const threads = cpu.threads || cpu.cores || '?';

  // v2.12.19: advisories from /api/hardware. These never change what the app
  // launches (all three tiers always come up by design) — they exist so a
  // constrained machine explains itself instead of just feeling broken.
  const adv = Array.isArray(hw.advisories) ? hw.advisories : [];
  const advHtml = adv.map((a) => {
    const warn = a.level === 'warn';
    return `
      <div class="hw-advisory" role="note" style="
        grid-column: 1 / -1;
        display:flex; gap:10px; align-items:flex-start;
        padding:10px 12px; margin-bottom:10px;
        border-radius:6px; font-size:12px; line-height:1.45;
        border:1px solid ${warn ? '#b8860b' : 'var(--border-hi, #444)'};
        background:${warn ? 'rgba(184,134,11,0.10)' : 'rgba(127,127,127,0.08)'};
        color:var(--text);">
        <span aria-hidden="true" style="flex:0 0 auto;font-size:14px;line-height:1.2">
          ${warn ? '\u26A0' : '\u2139'}
        </span>
        <span>${_esc(a.message || '')}</span>
      </div>`;
  }).join('');

  const mem = hw.memory || {};
  const memCard = mem.total_gb
    ? `<div class="hw-card">
         <div class="hw-card-title">Memory</div>
         <div class="hw-card-value">${mem.total_gb} GB</div>
         <div style="margin-top:4px;font-size:11px;color:var(--text-muted)">
           ${mem.available_gb != null ? mem.available_gb + ' GB available' : ''}
         </div>
       </div>`
    : '';

  container.innerHTML = `
    ${advHtml}
    <div class="hw-card">
      <div class="hw-card-title">Operating System</div>
      <div class="hw-card-value">${os.name || '?'} ${os.release || ''}</div>
    </div>
    <div class="hw-card">
      <div class="hw-card-title">CPU</div>
      <div class="hw-card-value" style="font-size:12px">${cpu.name || 'Unknown'}</div>
      <div style="margin-top:4px;font-size:11px;color:var(--text-muted)">
        ${cores} cores · ${threads} threads
        ${cpu.avx2   ? ' · AVX2'   : ''}
        ${cpu.avx512 ? ' · AVX512' : ''}
      </div>
    </div>
    ${memCard}
    ${renderGpuCard('NVIDIA', hw.nvidia)}
    ${renderGpuCard('AMD',    hw.amd)}
    ${renderGpuCard('Intel',  hw.intel)}
    ${renderNpuCard(hw.npu)}
    <div class="hw-card" style="border-left:3px solid var(--gold)">
      <div class="hw-card-title">Recommended</div>
      <div class="hw-card-value" style="color:var(--gold)">${(hw.recommended_backend || 'cpu').toUpperCase()}</div>
      <div style="margin-top:3px;font-size:11px;color:var(--text-muted)">
        GPU layers: ${hw.recommended_layers === -1 ? 'All' : hw.recommended_layers || 0}
      </div>
    </div>
  `;

  if (togglesDiv) {
    togglesDiv.innerHTML = '';
    const toggles = buildToggles(hw);
    toggles.forEach(t => {
      const row = document.createElement('div');
      row.className = 'hw-toggle-row';
      row.innerHTML = `
        <span class="hw-toggle-label"${t.tip ? ` data-tip="${t.tip}"` : ''}>${t.label}</span>
        <label class="toggle-switch">
          <input type="checkbox" aria-label="${t.label}" ${t.checked ? 'checked' : ''}
                 onchange="${t.onChange}">
          <span class="toggle-track"></span>
        </label>
      `;
      togglesDiv.appendChild(row);
    });
  }
}

function renderGpuCard(brand, info) {
  if (!info) return '';
  const avail = info.available;
  const badge = `<span class="hw-badge ${avail ? 'available' : 'unavailable'}">${avail ? '✓ Detected' : '✗ Not found'}</span>`;
  let details = '';
  if (avail) {
    if (info.gpus && info.gpus.length > 0) {
      details = info.gpus.map(g =>
        `<div style="font-size:12px;color:var(--text-muted);margin-top:3px">${g.name}${g.vram_mb ? ` · ${Math.round(g.vram_mb/1024)}GB VRAM` : ''}</div>`
      ).join('');
    }
    if (info.driver_version) details += `<div style="font-size:11px;color:var(--text-faint);margin-top:2px">Driver: ${info.driver_version}</div>`;
    if (info.driver_info)    details += `<div style="font-size:11px;color:var(--text-faint);margin-top:2px">Driver: ${info.driver_info}</div>`;
    if (info.rocm_version)   details += `<div style="font-size:11px;color:var(--text-faint);margin-top:2px">ROCm: ${info.rocm_version}</div>`;
    if (info.arc_detected)   details += `<div style="font-size:11px;color:var(--teal);margin-top:2px">✦ Arc GPU · Xe Cores · AI Accelerated</div>`;
    if (info.openvino)       details += `<div style="font-size:11px;color:var(--teal);margin-top:2px">✓ OpenVINO available</div>`;
    if (info.level_zero)     details += `<div style="font-size:11px;color:var(--teal);margin-top:2px">✓ Level-Zero / oneAPI</div>`;
  }
  return `
    <div class="hw-card">
      <div class="hw-card-title" style="display:flex;justify-content:space-between;align-items:center">
        ${brand} ${badge}
      </div>
      ${details}
    </div>
  `;
}

// v2.11.12: NPU card (AMD XDNA / Ryzen AI, Intel AI Boost). Previously the
// hardware panel only knew GPUs — an NPU is a ComputeAccelerator PnP device,
// not a video controller, so it was never detected or displayed. Shows the
// runtime status too: Lemonade Server is what actually serves LLMs on the
// NPU, so "detected but no runtime" gets an actionable install hint instead
// of a silent nothing.
function renderNpuCard(info) {
  if (!info) return '';
  const avail = info.available;
  const badge = `<span class="hw-badge ${avail ? 'available' : 'unavailable'}">${avail ? '✓ Detected' : '✗ Not found'}</span>`;
  let details = '';
  if (avail) {
    if (info.name)
      details += `<div style="font-size:12px;color:var(--text-muted);margin-top:3px">${info.name}</div>`;
    if (info.driver_version)
      details += `<div style="font-size:11px;color:var(--text-faint);margin-top:2px">Driver: ${info.driver_version}</div>`;
    if (info.xdna)
      details += `<div style="font-size:11px;color:var(--teal);margin-top:2px">✦ Ryzen AI · XDNA · AI Accelerated</div>`;
    if (info.lemonade)
      details += `<div style="font-size:11px;color:var(--teal);margin-top:2px">✓ Lemonade Server (NPU LLM runtime)</div>`;
    else if (info.xdna)
      details += `<div style="font-size:11px;color:var(--text-faint);margin-top:2px">Runtime missing — install AMD Lemonade Server to run models on the NPU</div>`;
    if (info.vitis_ai)
      details += `<div style="font-size:11px;color:var(--teal);margin-top:2px">✓ VitisAI (ONNX Runtime EP)</div>`;
  }
  return `
    <div class="hw-card">
      <div class="hw-card-title" style="display:flex;justify-content:space-between;align-items:center">
        NPU ${badge}
      </div>
      ${details}
    </div>
  `;
}

// v2.11.12: toggles now read their REAL persisted state from /api/config
// (window._appConfig) instead of a hardcoded checked:true. The keys are
// allowlisted server-side now, so flipping a switch persists and is
// consumed by the inference path (see config_store.InferenceSection).
function _cfgOn(key) {
  return !(window._appConfig && window._appConfig[key] === false);
}

function buildToggles(hw) {
  const toggles = [];
  toggles.push({
    label: 'GPU Acceleration',
    checked: _cfgOn('gpu_acceleration'),
    onChange: "updateSetting('gpu_acceleration', this.checked)",
    tip: 'Use the GPU to accelerate model inference. Off = CPU-only (slower, but works everywhere).',
  });
  if (hw.nvidia && hw.nvidia.available) {
    toggles.push({ label: 'CUDA (NVIDIA)', checked: _cfgOn('cuda_enabled'), onChange: "updateSetting('cuda_enabled', this.checked)", tip: 'Use NVIDIA CUDA to accelerate inference on your NVIDIA GPU.' });
  }
  // v2.11.12d: ROCm toggle only when the ROCm RUNTIME exists (rocm_available),
  // not merely when an AMD GPU is present — ROCm doesn't exist on Windows
  // client machines, so a Radeon iGPU shouldn't summon a dead toggle.
  // (Per Todd: the AMD card just needs to say Detected; the NPU has its own
  // toggle and AMD GPU inference rides the global GPU Acceleration switch.)
  if (hw.amd && hw.amd.rocm_available) {
    toggles.push({ label: 'ROCm (AMD)', checked: _cfgOn('rocm_enabled'), onChange: "updateSetting('rocm_enabled', this.checked)", tip: 'Use AMD ROCm to accelerate inference on your AMD GPU.' });
  }
  // v2.12.19: Vulkan is VENDOR-NEUTRAL and was only offered to Intel users.
  // That left AMD APU laptops with no reachable GPU path at all: ROCm on
  // Windows supports discrete Radeon RX/PRO cards ONLY -- never Ryzen AI
  // integrated graphics -- so hw.amd.rocm_available is false and the ROCm
  // toggle (correctly) never appears, while the toggle that WOULD have worked
  // was gated behind hw.intel. Offer it to any detected GPU vendor.
  if ((hw.amd && hw.amd.available) || (hw.nvidia && hw.nvidia.available)) {
    toggles.push({
      label: 'Vulkan',
      checked: _cfgOn('vulkan_enabled'),
      onChange: "updateSetting('vulkan_enabled', this.checked)",
      tip: 'Vendor-neutral GPU acceleration. On AMD integrated graphics this is the only working GPU path on Windows (ROCm does not support APUs).',
    });
  }
  if (hw.intel && hw.intel.available) {
    toggles.push({ label: 'Vulkan/XPU (Intel)', checked: _cfgOn('vulkan_enabled'), onChange: "updateSetting('vulkan_enabled', this.checked)", tip: 'Use Intel Vulkan/XPU acceleration on your Intel GPU.' });
    if (hw.intel.openvino) {
      toggles.push({ label: 'OpenVINO', checked: _cfgOn('openvino_enabled'), onChange: "updateSetting('openvino_enabled', this.checked)", tip: "Use Intel's OpenVINO runtime for optimized inference." });
    }
    if (hw.intel.arc_detected) {
      toggles.push({ label: 'Arc Xe Cores (AI)', checked: _cfgOn('xe_cores_enabled'), onChange: "updateSetting('xe_cores_enabled', this.checked)", tip: "Use the Arc GPU's Xe-core AI acceleration." });
    }
  }
  // v2.11.12: NPU toggle — AMD's brand feature gets its own switch, same as
  // CUDA for NVIDIA and Arc for Intel. Wired end-to-end: the flag persists
  // via /api/config, model_manager includes/excludes the NPU tier from
  // model listing + routing LIVE, and tier_launcher decides at next boot
  // whether the Lemonade NPU server process runs at all.
  if (hw.npu && hw.npu.available) {
    const label = hw.npu.vendor === 'amd' ? 'Ryzen AI (NPU)' : 'NPU (AI Boost)';
    toggles.push({
      label,
      checked: _cfgOn('npu_enabled'),
      onChange: "updateSetting('npu_enabled', this.checked)",
      tip: hw.npu.lemonade
        ? 'Serve models on the NPU via Lemonade Server. Off = NPU tier hidden and never routed to.'
        : 'NPU detected, but no LLM runtime found. Install AMD Lemonade Server, then this switch controls the NPU tier.',
    });
  }
  return toggles;
}
