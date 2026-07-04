/**
 * OracleAI — Settings Module v2
 * Added: Tavily key, Sage config, multi-model, vibe prompts
 */

window._appConfig = {};

async function loadSettings() {
  try {
    // #68 Phase E Step 6: system prompt now lives in its own file/endpoint,
    // not inside /api/config. Fetch both in parallel and inject the prompt
    // text into the cfg dict so applySettingsToUI (which still reads
    // cfg.system_prompt for the textarea) keeps working unchanged.
    const [cfgResp, promptResp] = await Promise.all([
      fetch("/api/config"),
      fetch("/api/prompts/system"),
    ]);
    const cfg = await cfgResp.json();
    try {
      const promptData = await promptResp.json();
      cfg.system_prompt = promptData.system_prompt || "";
    } catch {
      // Prompt endpoint failure shouldn't block settings load — fall
      // back to empty so the textarea renders blank instead of broken.
      cfg.system_prompt = "";
    }
    window._appConfig = cfg;
    applySettingsToUI(cfg);
    loadDevAndBrowserToggles();
  } catch (e) {
    console.error("[Settings] Failed to load config", e);
  }
}

function applySettingsToUI(cfg) {
  setVal("setting-backend", cfg.backend || "ollama");
  setVal("setting-ollama-url", cfg.ollama_url || "http://localhost:11434");
  // #68 Phase E: cfg.temperature is now guaranteed present from
  // OracleConfig.to_flat_dict (default 0.5). The old `?? 0.7` fallback
  // was dead and caused drift when the network was briefly unreachable
  // (audit Bug 3). Keep `?? 0.5` only as a defensive read in case the
  // fetch ever returns a partial dict — it now matches the backend.
  setVal("setting-temperature", cfg.temperature ?? 0.5);
  // v2.1.8 max_tokens=-1 trap fix:
  // Old code did `cfg.max_tokens ?? -1` which forced the literal string "-1"
  // into a number input that confused users (Todd hit a runtime breakage
  // when -1 was sitting in this field). The backend sentinel for unlimited
  // is -1, but the *UI* should render that as blank so it reads naturally
  // ("blank = unlimited") and matches the n_ctx pattern from earlier.
  // The save path (sanitizeMaxTokensInput) re-encodes blank → -1 for the
  // backend, so the on-disk config stays canonical.
  setVal(
    "setting-max-tokens",
    cfg.max_tokens && cfg.max_tokens > 0 ? cfg.max_tokens : "",
  );
  // v2.1.8 fix: previously fell back to 8190 when cfg.n_ctx was missing,
  // which the user would then save into config.json (or send via options)
  // and effectively defeat the adaptive ctx sizing. Empty string means
  // "use adaptive" — the input is blank, and updateSetting will skip
  // sending n_ctx unless the user explicitly types a value.
  setVal("setting-n-ctx", cfg.n_ctx ?? "");
  setVal("setting-gpu-layers", cfg.n_gpu_layers ?? -1);
  setVal("setting-system-prompt", cfg.system_prompt || "");
  // Image Generation (ComfyUI) autostart -- config-driven (see comfyui_launcher.py).
  var _caxEl = document.getElementById("toggle-comfyui-autostart");
  if (_caxEl) _caxEl.checked = !!cfg.comfyui_autostart_enabled;
  setVal("setting-comfyui-launch-cmd", cfg.comfyui_launch_cmd || "");

  const td = document.getElementById("temp-display");
  if (td) td.textContent = cfg.temperature ?? 0.5;
  onBackendChange(cfg.backend || "ollama");

  // Haptic
  if (typeof Haptic !== "undefined") {
    Haptic.setEnabled(cfg.haptic !== false);
    const btn = document.getElementById("haptic-btn");
    if (btn) btn.classList.toggle("active", Haptic.isEnabled());
  }

  // Theme
  const theme = cfg.theme || "dark";
  document.documentElement.setAttribute("data-theme", theme);
  syncThemeButton(theme);

  // Sage toggles -- previously read `cfg.X !== true` which inverted every
  // toggle (true config -> unchecked UI, false config -> checked UI). Now
  // reads the config value directly with a default-on fallback matching
  // DEFAULT_CONFIG in main.py, and uses `=== false` only for auto_route
  // which defaults off.
  setChecked("toggle-sage", cfg.sage_mode !== false);
  setChecked("toggle-agentic", cfg.agentic_mode !== false);
  setChecked("toggle-websearch", cfg.web_search_enabled !== false);
  setChecked("toggle-codeexec", cfg.code_exec_enabled !== false);
  setChecked("toggle-autoroute", cfg.auto_route === true);

  // Load tavily status
  loadTavilyStatus();

  // Load vibe prompts
  loadVibePrompts();
}

function setVal(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = val;
}

function setChecked(id, val) {
  const el = document.getElementById(id);
  if (el) el.checked = val;
}

// --- Developer Mode + browser cookie toggles --------------------------------
// These use their own endpoints (sage_data-backed), NOT /api/config, so they
// bypass the allowlist validator and survive OracleConfig saves.
async function loadDevAndBrowserToggles() {
  try {
    const r = await fetch("/api/devmode");
    const d = await r.json();
    setChecked("toggle-devmode", !!d.enabled);
  } catch (e) { /* leave default unchecked */ }
  try {
    const r = await fetch("/api/browser/config");
    const d = await r.json();
    setChecked("toggle-browser-cookies", !!d.persist_cookies);
  } catch (e) { /* leave default unchecked */ }
  try {
    const r = await fetch("/api/build/integrity");
    renderBuildStatus(await r.json());
  } catch (e) { /* leave default text */ }
  loadMultiProfileToggle();
}

// --- Multi-Profile toggle (v2.11.13) ----------------------------------------
// Owner-only: the Profiles section stays hidden for child profiles. Visible
// when multi-user is OFF (single user = owner) or the session is the owner.
// The backend enforces this too (POST /api/config rejects multiuser_enabled
// from non-owners) — hiding it is UX, not the security boundary.
async function loadMultiProfileToggle() {
  try {
    const r = await fetch("/api/auth/status");
    const s = await r.json();
    const isOwner = s.multiuser === false || s.is_owner === true;
    const section = document.getElementById("multiprofile-section");
    if (section) section.style.display = isOwner ? "" : "none";
    if (isOwner) setChecked("toggle-multiprofile", !!s.multiuser);
  } catch (e) { /* leave hidden */ }
}

async function setMultiProfile(enabled) {
  await updateSetting("multiuser_enabled", !!enabled);
  setStatus(enabled
    ? "Multi-Profile enabled — each person now signs in to their own profile"
    : "Multi-Profile disabled — back to single-user mode");
}
window.setMultiProfile = setMultiProfile;

function renderBuildStatus(d) {
  const el = document.getElementById("build-integrity-status");
  if (!el) return;
  const s = (d && d.status) || "unknown";
  const map = {
    official:          ["✓ Official build — OmniFoxX, verified", "#3fbf6f"],
    modified:          ["⚠ Modified build — differs from the signed manifest", "#f0a500"],
    foreign_key:       ["⚠ Signed with a non-official key (not OmniFoxX)", "#f0a500"],
    signature_invalid: ["⚠ Manifest signature invalid", "#e0533d"],
    no_manifest:       ["• Unsigned build (no manifest yet)", "#99a0ad"],
    no_pubkey:         ["• No public key present", "#99a0ad"],
    error:             ["• Build check unavailable", "#99a0ad"],
  };
  const pair = map[s] || ["• Build status: " + s, "#99a0ad"];
  let txt = pair[0];
  if (d && d.version) txt += "  (v" + d.version + ")";
  if (s === "modified" && d.sensitive_modified && d.sensitive_modified.length) {
    txt += " — incl. " + d.sensitive_modified.join(", ");
  }
  el.textContent = txt;
  el.style.color = pair[1];
}

async function setDevMode(enabled) {
  try {
    await fetch("/api/devmode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !!enabled }),
    });
  } catch (e) {
    console.error("[Settings] devmode save failed", e);
  }
}

async function setBrowserCookies(enabled) {
  if (enabled) {
    const ok = await window.oracleConfirm(
      "Let Sage's browser keep cookies between sessions?\n\n" +
      "Bookmarks and history already persist. Cookies can also hold personal " +
      "or session data (logins). They're stored only in this machine's " +
      "per-user browser profile, and are not encrypted at rest. Enable?",
      { title: "Browser cookies", okLabel: "Enable" }
    );
    if (!ok) {
      setChecked("toggle-browser-cookies", false);
      return;
    }
  }
  try {
    await fetch("/api/browser/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ persist_cookies: !!enabled }),
    });
  } catch (e) {
    console.error("[Settings] browser cookie save failed", e);
  }
}

window.loadDevAndBrowserToggles = loadDevAndBrowserToggles;
window.setDevMode = setDevMode;
window.setBrowserCookies = setBrowserCookies;

async function updateSetting(key, value) {
  window._appConfig[key] = value;
  try {
    if (key === "system_prompt") {
      // #68 Phase E Step 6: system_prompt has its own endpoint backed by
      // a real file. POSTing it via /api/config would now 400 because
      // the validator's allowlist no longer includes the key.
      await fetch("/api/prompts/system", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ system_prompt: value }),
      });
    } else {
      await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [key]: value }),
      });
    }
  } catch (e) {
    console.error("[Settings] Save failed", e);
  }
}

// #68 Phase E loose-end fix: the system-prompt textarea originally used
// onchange, which only fires when focus leaves the element. If the user
// typed in the textarea and immediately restarted/closed without ever
// clicking outside, the save never happened. Debounced auto-save while
// typing (1.5s after the last keystroke) makes edits durable regardless
// of focus behavior. index.html binds the textarea's oninput to this.
let _systemPromptSaveTimer = null;
function updateSystemPromptDebounced(value) {
  // Keep _appConfig in sync immediately so the next loadSettings doesn't
  // race against the in-flight POST.
  window._appConfig.system_prompt = value;
  if (_systemPromptSaveTimer) clearTimeout(_systemPromptSaveTimer);
  _systemPromptSaveTimer = setTimeout(() => {
    updateSetting("system_prompt", value);
    _systemPromptSaveTimer = null;
  }, 1500);
}

/**
 * v2.1.8 max_tokens=-1 trap fix.
 * Re-encodes whatever the user typed into the Max Tokens input into the
 * canonical backend sentinel:
 *   - blank / NaN / non-positive  -> -1   (unlimited)
 *   - positive integer            -> that integer
 * The backend independently coerces invalid values too (defense in depth),
 * but doing it here keeps /api/config payloads clean and avoids round-trip
 * surprises where the UI shows one thing and the server stores another.
 */
function sanitizeMaxTokensInput(raw) {
  if (raw === null || raw === undefined || raw === "") return -1;
  const n = parseInt(raw, 10);
  if (!Number.isFinite(n) || n <= 0) return -1;
  return n;
}

function onBackendChange(val) {
  const ollamaRow = document.getElementById("ollama-url-row");
  if (ollamaRow) ollamaRow.style.display = val === "ollama" ? "flex" : "none";
}

/* --- Models -------------------------------------------------- */
/**
 * Refresh the model picker AND apply any pending tier ctx_size changes.
 *
 * Calls POST /api/models/refresh, which:
 *   1. Detects whether Sage or Daemon llama-server tiers need to restart
 *      (because the user changed the global n_ctx in Settings).
 *   2. Restarts any tier whose cached ctx_size differs from the desired.
 *   3. Returns the fresh model list + list of restarted tiers + warnings.
 *
 * If any tier actually restarts, this call blocks ~5-15 seconds per tier
 * while the new llama-server loads its model. We show a status message
 * during the wait so the user knows why the button feels slow. If no
 * restart is needed, the call is cheap and completes in <1s.
 */
async function reloadModels() {
  const sel     = document.getElementById("model-select");
  const secSel  = document.getElementById("setting-secondary-model");
  const terSel  = document.getElementById("setting-tertiary-model");
  const priSel  = document.getElementById("setting-primary-model");
  const status  = document.getElementById("status-text");
  const refreshBtn = document.querySelector(".action-btn.secondary");
  if (!sel) return;

  // Disable button, show loading state
  const origLabel = refreshBtn ? refreshBtn.textContent : null;
  if (refreshBtn) {
    refreshBtn.disabled = true;
    refreshBtn.textContent = "Refreshing…";
  }
  if (status) status.textContent = "Refreshing models…";
  sel.innerHTML = '<option value="">Loading models…</option>';

  try {
    const resp = await fetch("/api/models/refresh", { method: "POST" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const { models, restarted_tiers, warnings } = await resp.json();

    // Populate dropdowns
    sel.innerHTML = '<option value="">— Select Model —</option>';
    if (secSel) secSel.innerHTML = '<option value="">— None —</option>';
    if (terSel) terSel.innerHTML = '<option value="">— None —</option>';
    if (priSel) priSel.innerHTML = '<option value="">— Select Model —</option>';

    if (!models || models.length === 0) {
      sel.innerHTML += '<option value="" disabled>No models found</option>';
    } else {
      models.forEach((m) => {
        const opt = document.createElement("option");
        opt.value = m.id;
        // Show tier as a small suffix if present, so user can tell which
        // tier a model lives on. Falls back to just name if no tier info.
        const tierLabel = m.tier ? `  [${m.tier}]` : "";
        const sizeLabel = m.size ? "  (" + formatBytes(m.size) + ")" : "";
        opt.textContent = `${m.name}${tierLabel}${sizeLabel}`;
        sel.appendChild(opt);

        if (secSel) {
          const opt2 = opt.cloneNode(true);
          secSel.appendChild(opt2);
        }
        if (terSel) {
          const opt3 = opt.cloneNode(true);
          terSel.appendChild(opt3);
        }
        if (priSel) {
          const opt0 = opt.cloneNode(true);
          priSel.appendChild(opt0);
        }
      });

      if (window._appConfig.default_model) {
        sel.value = window._appConfig.default_model;
        if (!sel.value) sel.value = "";
      }
      if (secSel && window._appConfig.secondary_model) {
        secSel.value = window._appConfig.secondary_model;
      }
      if (terSel && window._appConfig.tertiary_model) {
        terSel.value = window._appConfig.tertiary_model;
      }
      if (priSel && window._appConfig.default_model) {
        priSel.value = window._appConfig.default_model;
      }
    }

    // Surface tier restart feedback
    if (restarted_tiers && restarted_tiers.length > 0) {
      const names = restarted_tiers.map((t) => `${t.tier} (ctx=${t.ctx_size})`).join(", ");
      if (status) status.textContent = `Restarted: ${names}`;
      console.log("[Settings] Tiers restarted:", restarted_tiers);
    } else if (status) {
      status.textContent = "Ready";
    }

    if (warnings && warnings.length > 0) {
      console.warn("[Settings] Tier warnings:", warnings);
      if (status) status.textContent = "⚠ " + warnings[0];
    }
  } catch (e) {
    console.error("[Settings] Refresh failed", e);
    sel.innerHTML = '<option value="">— Could not load models —</option>';
    if (status) status.textContent = "Refresh failed";
  } finally {
    if (refreshBtn) {
      refreshBtn.disabled = false;
      refreshBtn.textContent = origLabel;
    }
  }
}

function onModelChange(modelId) {
  if (modelId) updateSetting("default_model", modelId);
  // keep the header picker and the routing-section Primary picker in sync
  const hdr = document.getElementById("model-select");
  if (hdr && hdr.value !== modelId) hdr.value = modelId;
  const pri = document.getElementById("setting-primary-model");
  if (pri && pri.value !== modelId) pri.value = modelId;
}

/* --- Plugins ------------------------------------------------- */
/* --- Sage Network ------------------------------------------- */
async function snLoadStatus() {
  try {
    const r = await fetch("/api/sage-network/status");
    if (!r.ok) return;
    const s = await r.json();
    const fp = document.getElementById("sn-fingerprint");
    if (fp) fp.textContent = s.fingerprint || "-";
    const tog = document.getElementById("toggle-node-server");
    if (tog) tog.checked = !!s.node_server_enabled;
    const nm = document.getElementById("setting-node-name");
    if (nm) nm.value = s.node_name || "";
    const ru = document.getElementById("setting-remote-node-url");
    if (ru) ru.value = s.remote_node_url || "";
    const bind = document.getElementById("toggle-bind-lan");
    if (bind) bind.checked = !!(s.host && s.host !== "127.0.0.1" && s.host !== "localhost");
    const off = document.getElementById("toggle-offload");
    if (off) off.checked = !!s.offload_enabled;
    const addr = document.getElementById("sn-lan-addr");
    if (addr) addr.textContent = (s.lan_ip || "?") + ":" + (s.app_port || 8000);
  } catch (e) { /* Sage Network section is optional */ }
}

async function snRevealToken() {
  try {
    const r = await fetch("/api/sage-network/token");
    if (!r.ok) return;
    const data = await r.json();
    const inp = document.getElementById("sn-token");
    if (inp) inp.value = data.token || "";
    const box = document.getElementById("sn-token-box");
    if (box) box.style.display = "block";
  } catch (e) { setStatus("Could not reveal token"); }
}

function snSetBind(toLan) {
  updateSetting("host", toLan ? "0.0.0.0" : "127.0.0.1");
  setStatus(toLan ? "Bind set to LAN - RESTART OracleAI to apply"
                  : "Bind set to localhost - restart to apply");
}

function snCopyToken() {
  const inp = document.getElementById("sn-token");
  if (!inp || !inp.value) return;
  if (navigator.clipboard) navigator.clipboard.writeText(inp.value);
  setStatus("Token copied");
}

async function snSetToken() {
  const inp = document.getElementById("sn-paste-token");
  const tok = ((inp && inp.value) || "").trim();
  if (!tok) { setStatus("Paste a token first"); return; }
  try {
    const r = await fetch("/api/sage-network/token", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: tok }),
    });
    const res = await r.json();
    if (res.ok) {
      const fp = document.getElementById("sn-fingerprint");
      if (fp) fp.textContent = res.fingerprint || "-";
      if (inp) inp.value = "";
      setStatus("Token set - fingerprint " + (res.fingerprint || ""));
    } else { setStatus("Could not set token"); }
  } catch (e) { setStatus("Could not set token"); }
}

async function snResetToken() {
  if (!(await window.oracleConfirm("Reset to a brand-new token? This BREAKS any existing pairing - every node will then need this new token.", { title: "Reset token", okLabel: "Reset" }))) return;
  try {
    const r = await fetch("/api/sage-network/token/reset", { method: "POST" });
    const res = await r.json();
    if (res.ok) {
      const fp = document.getElementById("sn-fingerprint");
      if (fp) fp.textContent = res.fingerprint || "-";
      const inp = document.getElementById("sn-token");
      if (inp) inp.value = res.token || "";
      const box = document.getElementById("sn-token-box");
      if (box) box.style.display = "block";
      setStatus("New token generated - share it with your other nodes");
    } else { setStatus("Could not reset token"); }
  } catch (e) { setStatus("Could not reset token"); }
}

async function snPairTest() {
  const url = ((document.getElementById("setting-remote-node-url") || {}).value || "").trim();
  const out = document.getElementById("sn-pair-result");
  if (!url) { if (out) out.textContent = "Enter a remote node URL first."; return; }
  if (out) out.textContent = "Testing...";
  try {
    const r = await fetch("/api/sage-network/pair-test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const res = await r.json();
    if (res.ok) {
      const m = res.remote || {};
      const match = res.fingerprint_match ? "fingerprints MATCH" : "fingerprint MISMATCH (different token!)";
      const models = (m.models || []).length;
      if (out) out.textContent = "Reached \"" + (m.node_name || "node") + "\": " + match + ", " + models + " models, comfyui: " + (m.has_comfyui ? "yes" : "no");
    } else {
      if (out) out.textContent = "Pairing failed: " + (res.error || "unknown");
    }
  } catch (e) { if (out) out.textContent = "Pairing error: " + e.message; }
}

async function loadPlugins() {
  const container = document.getElementById("plugins-list");
  if (!container) return;
  try {
    const resp = await fetch("/api/plugins", { cache: "no-store" });
    const { plugins } = await resp.json();
    if (!plugins || plugins.length === 0) {
      container.innerHTML =
        '<div class="loading-placeholder">No plugins installed.<br>Drop JSON files into /plugins</div>';
      return;
    }
    container.innerHTML = plugins
      .map(
        (p) => `
      <div class="plugin-card">
        <div class="plugin-header">
          <span class="plugin-name">${p.name}</span>
          <label class="toggle-switch">
            <input type="checkbox" ${p.enabled ? "checked" : ""}
                   onchange="togglePlugin('${p.id}', this)">
            <span class="toggle-track"></span>
          </label>
        </div>
        <div class="plugin-desc">${p.description || ""}</div>
        <div class="plugin-meta">v${p.version} · ${p.author}${p.hooks && p.hooks.length ? " · hooks: " + p.hooks.join(", ") : ""}</div>
      </div>
    `,
      )
      .join("");
  } catch {
    container.innerHTML =
      '<div class="loading-placeholder">Could not load plugins</div>';
  }
}

async function togglePlugin(pluginId, checkbox) {
  try {
    await fetch(`/api/plugins/${pluginId}/toggle`, { method: "POST" });
    Haptic.vibrate(Haptic.PATTERNS.toggle);
  } catch {
    checkbox.checked = !checkbox.checked;
  }
}

/* --- Theme --------------------------------------------------- */
function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme") || "dark";
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  syncThemeButton(next);
  updateSetting("theme", next);

  const hlTheme = document.getElementById("hljs-theme");
  if (hlTheme) {
    hlTheme.href =
      next === "light"
        ? "https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/styles/github.min.css"
        : "https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/styles/github-dark-dimmed.min.css";
  }
  Haptic.vibrate(Haptic.PATTERNS.toggle);
}

function syncThemeButton(theme) {
  const btn = document.getElementById("theme-btn");
  if (btn) btn.textContent = theme === "dark" ? "○" : "●";
}

/* --- Sidebar -------------------------------------------------- */
function toggleSidebar() {
  const sidebar = document.getElementById("sidebar");
  if (sidebar) sidebar.classList.toggle("collapsed");
}

function switchPanel(name, btn) {
  document.title = `${name.charAt(0).toUpperCase() + name.slice(1)} - OracleAI`;
  document
    .querySelectorAll(".nav-tab")
    .forEach((b) => b.classList.remove("active"));
  document
    .querySelectorAll(".panel")
    .forEach((p) => p.classList.remove("active"));
  if (btn) btn.classList.add("active");
  const panel = document.getElementById(`panel-${name}`);
  if (panel) panel.classList.add("active");
}

/* --- Tavily Key ----------------------------------------------- */
async function loadTavilyStatus() {
  try {
    const resp = await fetch("/api/tavily");
    const data = await resp.json();
    const el = document.getElementById("tavily-status");
    if (el) {
      if (data.has_key) {
        el.innerHTML = `<span class="key-status">✓ Key set</span> <span style="font-size:11px;color:var(--text-faint)">${data.masked}</span>`;
      } else {
        el.innerHTML = '<span class="key-status none">No key set</span>';
      }
    }
  } catch {}
}

async function saveTavilyKey() {
  const inp = document.getElementById("tavily-input");
  if (!inp || !inp.value.trim()) return;
  try {
    await fetch("/api/tavily", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: inp.value.trim() }),
    });
    inp.value = "";
    loadTavilyStatus();
  } catch {}
}

async function deleteTavilyKey() {
  if (!(await window.oracleConfirm("Are you sure you want to delete your Tavily API key? This cannot be undone.", { title: "Delete Tavily key", okLabel: "Delete" }))) return;
  try {
    await fetch("/api/tavily", { method: "DELETE" });
    loadTavilyStatus();
  } catch {}
}

/* --- Vibe Coding Prompts -------------------------------------- */
let vibePrompts = {};

async function loadVibePrompts() {
  try {
    const resp = await fetch("/api/vibe-prompts");
    vibePrompts = await resp.json();
    renderVibeBar();
  } catch {}
}

function renderVibeBar() {
  const bar = document.getElementById("vibe-bar");
  if (!bar) return;
  bar.innerHTML = Object.entries(vibePrompts)
    .map(
      ([key, vp]) =>
        `<button class="vibe-btn" onclick="useVibePrompt('${key}')" title="${vp.prompt.substring(0, 60)}…">${vp.label}</button>`,
    )
    .join("");
}

function toggleVibeBar() {
  const bar = document.getElementById("vibe-bar");
  if (bar) bar.classList.toggle("open");
}

function useVibePrompt(key) {
  const vp = vibePrompts[key];
  if (!vp) return;
  const input = document.getElementById("user-input");
  if (input) {
    const existing = input.value.trim();
    input.value = vp.prompt + existing;
    input.focus();
    autoResize(input);
  }
  // Close vibe bar after selection
  const bar = document.getElementById("vibe-bar");
  if (bar) bar.classList.remove("open");
}

/* --- Utils --------------------------------------------------- */
function formatBytes(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  if (bytes < 1024 ** 3) return (bytes / 1024 / 1024).toFixed(1) + " MB";
  return (bytes / 1024 ** 3).toFixed(2) + " GB";
}
