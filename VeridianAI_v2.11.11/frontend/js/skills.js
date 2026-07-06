/* skills.js -- Aether skill-share panel (sharing + bundles + relay + controls).
 * Drives /api/skills/*. Endpoints 404 when skill_share_enabled is off, so we
 * degrade quietly to a "(enable + restart)" state. Pure-ASCII source on purpose. */
(function () {
  "use strict";
  function $(id) { return document.getElementById(id); }
  function esc(s) { var d = document.createElement("div"); d.textContent = (s == null ? "" : String(s)); return d.innerHTML; }
  function toast(m) { try { if (window.setStatus) window.setStatus(m); } catch (e) {} }

  async function jget(url) { var r = await fetch(url); if (!r.ok) throw new Error(String(r.status)); return r.json(); }
  async function jpost(url, body) {
    var r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                               body: JSON.stringify(body || {}) });
    try { return await r.json(); } catch (e) { return {}; }
  }

  var _selfPub = "";
  var _peerUrl = "";
  var _relayCtx = null;   // {relay, target} when the active browse used a relay

  async function skLoadIdentity() {
    try {
      var d = await jget("/api/skills/identity");
      _selfPub = d.pubkey || "";
      if ($("sk-fingerprint")) $("sk-fingerprint").textContent = d.fingerprint || "-";
      if ($("toggle-skill-share")) $("toggle-skill-share").checked = true;
    } catch (e) {
      if ($("sk-fingerprint")) $("sk-fingerprint").textContent = "(enable + restart)";
      if ($("toggle-skill-share")) $("toggle-skill-share").checked = false;
    }
  }

  function skReflectRelayConfig() {
    try {
      var c = window._appConfig || {};
      if ($("toggle-relay-server")) $("toggle-relay-server").checked = !!c.relay_server_enabled;
      if ($("toggle-relay-source")) $("toggle-relay-source").checked = !!c.relay_source_enabled;
      if ($("sk-relay-url") && c.relay_url) $("sk-relay-url").value = c.relay_url;
    } catch (e) {}
  }

  function skCopyIdentity() {
    if (!_selfPub) { toast("Identity not loaded -- enable sharing + restart"); return; }
    if (navigator.clipboard) navigator.clipboard.writeText(_selfPub);
    toast("Public key copied -- hand it to your peer out-of-band");
  }

  async function skLoadTrusted() {
    var box = $("sk-trusted-list"); if (!box) return;
    try {
      var d = await jget("/api/skills/trusted");
      var keys = d.keys || [];
      if (!keys.length) { box.innerHTML = "<i>No trusted authors yet.</i>"; return; }
      box.innerHTML = keys.map(function (k) {
        return '<div style="display:flex;justify-content:space-between;gap:8px;align-items:center;padding:2px 0">'
          + '<span>' + esc(k.label || "(no label)") + ' &middot; <code>' + esc(k.fingerprint) + '</code></span>'
          + '<button class="toolbar-btn danger" onclick="skRemoveKey(\'' + esc(k.pubkey) + '\')">Remove</button></div>';
      }).join("");
    } catch (e) { box.innerHTML = ""; }
  }

  async function skAddKey() {
    var pub = (($("sk-key-pub") || {}).value || "").trim();
    var label = (($("sk-key-label") || {}).value || "").trim();
    if (!pub) { toast("Paste a public key first"); return; }
    var r = await jpost("/api/skills/trusted", { pubkey: pub, label: label });
    if (r.ok) {
      toast("Trusted " + (r.fingerprint || ""));
      if ($("sk-key-pub")) $("sk-key-pub").value = "";
      if ($("sk-key-label")) $("sk-key-label").value = "";
      skLoadTrusted();
    } else { toast(r.reason || "Could not add key"); }
  }

  async function skRemoveKey(pub) {
    var r = await jpost("/api/skills/trusted/remove", { pubkey: pub });
    if (r.ok) { toast("Removed"); skLoadTrusted(); } else { toast(r.reason || "Not found"); }
  }

  function _capLine(caps) { return (caps && caps.length) ? (" &middot; " + esc(caps.join(", "))) : ""; }

  function _renderBrowseItems(box, items) {
    if (!items.length) { box.innerHTML = "<i>Peer shares no skills.</i>"; return; }
    box.innerHTML = items.map(function (it) {
      var m = it.meta || {};
      var have = it.have ? ' <i>(' + esc(it.local_state || "have") + ')</i>' : "";
      var trust = it.author_trusted ? ' &#10003; trusted' : ' &middot; untrusted author';
      return '<div style="padding:3px 0;border-bottom:1px solid var(--border,#2a3a5a)">'
        + '<b>' + esc(m.name || m.id) + '</b> v' + esc(m.version || "?") + _capLine(m.capabilities)
        + '<br><code style="opacity:0.7">' + esc((m.id || "").slice(0, 16)) + '</code>' + trust + have
        + ' <button class="toolbar-btn" onclick="skFetch(\'' + esc(m.id) + '\')">Fetch</button></div>';
    }).join("");
  }

  async function skBrowse() {
    var url = (($("sk-peer-url") || {}).value || "").trim();
    var box = $("sk-browse-result"); if (!box) return;
    if (!url) { toast("Enter a peer URL"); return; }
    _peerUrl = url; _relayCtx = null;
    box.innerHTML = "Browsing...";
    var r = await jpost("/api/skills/browse", { base_url: url });
    if (!r.ok) { box.innerHTML = '<span style="color:var(--error,#ff6b6b)">' + esc(r.reason || "Could not reach peer") + '</span>'; return; }
    _renderBrowseItems(box, r.items || []);
  }

  async function skBrowseRelay() {
    var relay = (($("sk-relay-url") || {}).value || "").trim().replace(/\/+$/, "");
    var target = (($("sk-relay-target") || {}).value || "").trim();
    var box = $("sk-browse-result"); if (!box) return;
    if (!relay || !target) { toast("Enter relay URL + target device name"); return; }
    _relayCtx = { relay: relay, target: target }; _peerUrl = "";
    box.innerHTML = "Browsing via relay...";
    var r = await jpost("/api/skills/browse", { relay: relay, target: target });
    if (!r.ok) { box.innerHTML = '<span style="color:var(--error,#ff6b6b)">' + esc(r.reason || "relay browse failed") + '</span>'; return; }
    _renderBrowseItems(box, r.items || []);
  }

  async function skFetch(id) {
    var body;
    if (_relayCtx) { body = { relay: _relayCtx.relay, target: _relayCtx.target, id: id }; }
    else if (_peerUrl) { body = { base_url: _peerUrl, id: id }; }
    else { toast("Browse a peer first"); return; }
    var r = await jpost("/api/skills/fetch", body);
    if (r.ok) { toast("Fetched -- quarantined for your review"); skLoadLocal(); }
    else { toast(r.reason || "Fetch failed"); }
  }

  async function skLoadLocal() {
    var box = $("sk-local-list"); if (!box) return;
    try {
      var d = await jget("/api/skills/local");
      var skills = d.skills || [];
      if (!skills.length) { box.innerHTML = "<i>No local skills yet.</i>"; return; }
      box.innerHTML = skills.map(function (s) {
        var caps = []; try { caps = JSON.parse(s.capabilities || "[]"); } catch (e) {}
        var actions = "";
        if (s.state === "quarantined") {
          actions = ' <button class="toolbar-btn" onclick="skPromote(\'' + esc(s.id) + '\')">Promote</button>'
                  + ' <button class="toolbar-btn danger" onclick="skReject(\'' + esc(s.id) + '\')">Reject</button>';
        }
        actions += ' <button class="toolbar-btn" onclick="skExport(\'' + esc(s.id) + '\')">Export</button>'
                 + ' <button class="toolbar-btn danger" onclick="skRemove(\'' + esc(s.id) + '\')">Remove</button>';
        var color = s.state === "promoted" ? "var(--success,#56d364)"
                  : (s.state === "rejected" ? "var(--error,#ff6b6b)" : "var(--gold,#f0a500)");
        return '<div style="padding:3px 0;border-bottom:1px solid var(--border,#2a3a5a)">'
          + '<b>' + esc(s.name || s.id) + '</b> v' + esc(s.version || "?") + _capLine(caps)
          + ' <span style="color:' + color + '">' + esc(s.state) + '</span>'
          + '<br><code style="opacity:0.6">' + esc(s.author_fp || "") + '</code>' + actions + '</div>';
      }).join("");
    } catch (e) { box.innerHTML = "<i>Enable skill sharing + restart to manage skills.</i>"; }
  }

  async function skPromote(id) {
    var r = await jpost("/api/skills/promote", { id: id });
    if (r.ok) { toast("Promoted -- skill is now active and shareable"); }
    else { var v = r.verdict || {}; toast("Blocked: " + ((v.reasons || []).join("; ") || "not eligible (import the author's key?)")); }
    skLoadLocal();
  }

  async function skReject(id) {
    var r = await jpost("/api/skills/reject", { id: id });
    toast(r.ok ? "Rejected" : "Could not reject");
    skLoadLocal();
  }

  async function skRemove(id) {
    var r = await jpost("/api/skills/remove", { id: id });
    toast(r.ok ? "Removed (recoverable from removed/)" : "Could not remove");
    skLoadLocal();
  }

  async function skExport(id) {
    try {
      var r = await fetch("/api/skills/export/" + encodeURIComponent(id));
      if (!r.ok) { toast("Export failed"); return; }
      var bundle = await r.json();
      var blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
      var url = URL.createObjectURL(blob);
      var a = document.createElement("a");
      a.href = url; a.download = (String(id).slice(0, 12) || "skill") + ".skill";
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      URL.revokeObjectURL(url);
      toast("Exported -- send this .skill file however you like");
    } catch (e) { toast("Export failed"); }
  }

  async function skImport() {
    var inp = $("sk-import-file");
    if (!inp || !inp.files || !inp.files.length) { toast("Choose a .skill file first"); return; }
    try {
      var text = await inp.files[0].text();
      var bundle = JSON.parse(text);
      var r = await jpost("/api/skills/import", { bundle: bundle });
      if (r.ok) { toast("Imported -- quarantined for your review"); inp.value = ""; skLoadLocal(); }
      else { toast("Import failed: " + (r.reason || "invalid bundle")); }
    } catch (e) { toast("Could not read that file"); }
  }

  async function skKillSwitch() {
    if (!(await window.oracleConfirm("Go dark? This disables skill sharing, relay hosting/serving, and node serving. Restart to fully sever any running services.", { title: "Kill switch", okLabel: "Go dark" }))) return;
    var flags = ["skill_share_enabled", "relay_server_enabled", "relay_source_enabled", "node_server_enabled", "offload_enabled"];
    for (var i = 0; i < flags.length; i++) {
      try { if (window.updateSetting) await window.updateSetting(flags[i], false); } catch (e) {}
    }
    ["toggle-skill-share", "toggle-relay-server", "toggle-relay-source", "toggle-node-server", "toggle-offload"].forEach(function (id) {
      var t = $(id); if (t) t.checked = false;
    });
    toast("Going dark -- sharing / relay / serving disabled. Restart to fully sever.");
  }

  async function skLoadAll() {
    await skLoadIdentity();
    await skLoadTrusted();
    await skLoadLocal();
    skReflectRelayConfig();
    await ipLoad();
  }

  /* --- IP access control (denylist + lockdown allowlist) ------------------- */
  function _ipRender(d) {
    var st = $("ip-lockdown-state");
    if (st) st.textContent = d.lockdown ? "LOCKDOWN (allowlist only)" : "public";
    var tg = $("toggle-ip-lockdown"); if (tg) tg.checked = !!d.lockdown;
    function rows(which, list) {
      if (!list || !list.length) return "none";
      return list.map(function (ip) {
        return '<div style="display:flex;justify-content:space-between;gap:8px;align-items:center;padding:2px 0">'
          + '<code>' + esc(ip) + '</code>'
          + '<button class="toolbar-btn" onclick="ipRemove(\'' + which + '\',\'' + esc(ip) + '\')">Remove</button></div>';
      }).join("");
    }
    if ($("ip-deny-list")) $("ip-deny-list").innerHTML = rows("deny", d.denylist);
    if ($("ip-allow-list")) $("ip-allow-list").innerHTML = rows("allow", d.allowlist);
  }
  async function ipLoad() {
    try { _ipRender(await jget("/api/ip-access")); } catch (e) { /* localhost-only; ignore */ }
  }
  async function ipAdd(which) {
    var ip = (($("ip-entry") || {}).value || "").trim();
    if (!ip) { toast("Enter an IP or CIDR first"); return; }
    var r = await jpost("/api/ip-access", { action: "add", list: which, ip: ip });
    if (r && r.denylist) {
      _ipRender(r);
      if ($("ip-entry")) $("ip-entry").value = "";
      toast((which === "deny" ? "Blocked " : "Allowed ") + ip);
    } else { toast("Could not add -- check the IP/CIDR format"); }
  }
  async function ipRemove(which, ip) {
    var r = await jpost("/api/ip-access", { action: "remove", list: which, ip: ip });
    if (r && r.denylist) { _ipRender(r); toast("Removed " + ip); }
  }
  async function ipLockdown(on) {
    var r = await jpost("/api/ip-access", { action: "lockdown", enabled: !!on });
    if (r && typeof r.lockdown !== "undefined") {
      _ipRender(r);
      toast(on ? "Lockdown ON -- allowlist only" : "Lockdown OFF -- public");
    }
  }

  window.skCopyIdentity = skCopyIdentity; window.skAddKey = skAddKey; window.skRemoveKey = skRemoveKey;
  window.skBrowse = skBrowse; window.skBrowseRelay = skBrowseRelay; window.skFetch = skFetch;
  window.skLoadLocal = skLoadLocal; window.skPromote = skPromote; window.skReject = skReject;
  window.skRemove = skRemove; window.skExport = skExport; window.skImport = skImport;
  window.skKillSwitch = skKillSwitch; window.skLoadAll = skLoadAll;
  window.ipLoad = ipLoad; window.ipAdd = ipAdd; window.ipRemove = ipRemove; window.ipLockdown = ipLockdown;

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", skLoadAll);
  else skLoadAll();
})();
