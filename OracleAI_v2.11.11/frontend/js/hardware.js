/**
 * OracleAI — Hardware Panel v2
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

function renderHardwarePanel(hw) {
  const container = document.getElementById('hardware-info');
  const togglesDiv = document.getElementById('hw-toggles');
  if (!container) return;

  const os  = hw.os  || {};
  const cpu = hw.cpu || {};
  const cores = cpu.cores || '?';
  const threads = cpu.threads || cpu.cores || '?';

  container.innerHTML = `
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
    ${renderGpuCard('NVIDIA', hw.nvidia)}
    ${renderGpuCard('AMD',    hw.amd)}
    ${renderGpuCard('Intel',  hw.intel)}
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

function buildToggles(hw) {
  const toggles = [];
  toggles.push({
    label: 'GPU Acceleration',
    checked: window._appConfig && window._appConfig.gpu_acceleration !== false,
    onChange: "updateSetting('gpu_acceleration', this.checked)",
    tip: 'Use the GPU to accelerate model inference. Off = CPU-only (slower, but works everywhere).',
  });
  if (hw.nvidia && hw.nvidia.available) {
    toggles.push({ label: 'CUDA (NVIDIA)', checked: true, onChange: "updateSetting('cuda_enabled', this.checked)", tip: 'Use NVIDIA CUDA to accelerate inference on your NVIDIA GPU.' });
  }
  if (hw.amd && hw.amd.available) {
    toggles.push({ label: 'ROCm (AMD)', checked: true, onChange: "updateSetting('rocm_enabled', this.checked)", tip: 'Use AMD ROCm to accelerate inference on your AMD GPU.' });
  }
  if (hw.intel && hw.intel.available) {
    toggles.push({ label: 'Vulkan/XPU (Intel)', checked: true, onChange: "updateSetting('vulkan_enabled', this.checked)", tip: 'Use Intel Vulkan/XPU acceleration on your Intel GPU.' });
    if (hw.intel.openvino) {
      toggles.push({ label: 'OpenVINO', checked: true, onChange: "updateSetting('openvino_enabled', this.checked)", tip: "Use Intel's OpenVINO runtime for optimized inference." });
    }
    if (hw.intel.arc_detected) {
      toggles.push({ label: 'Arc Xe Cores (AI)', checked: true, onChange: "updateSetting('xe_cores_enabled', this.checked)", tip: "Use the Arc GPU's Xe-core AI acceleration." });
    }
  }
  return toggles;
}
