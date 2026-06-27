/* socials.js -- Socials channel tab (BitChat experimental + Discord + more).
 * Full-area view in the Oracle (games) panel, shown via the 📡 tab. Talks to
 * /api/socials/*. The feed only polls while the tab is open. Pure-ASCII source. */
(function () {
  "use strict";
  function $(id) { return document.getElementById(id); }
  function esc(s) { var d = document.createElement("div"); d.textContent = (s == null ? "" : String(s)); return d.innerHTML; }
  function toast(m) { try { if (window.setStatus) window.setStatus(m); } catch (e) {} }
  async function jget(u) { var r = await fetch(u); return r.json(); }
  async function jpost(u, b) {
    var r = await fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) });
    try { return await r.json(); } catch (e) { return {}; }
  }

  var _feedTimer = null;
  var _last = null;          // last /api/socials/status (so peers polling knows what's connected)
  var _msgs = [];            // last /api/socials/recent messages (every platform, newest last)
  var _activeThread = "all"; // which channel thread the feed shows ("all" = merged view)

  // Swap the game canvas/scoreboard/controls for the full Socials view.
  function socialsOnTab(show) {
    var els = [$("game-canvas"), $("scoreboard-container"), $("game-controls")];
    var info = document.querySelector("#oracle-panel .game-info");
    if (info) els.push(info);
    els.forEach(function (el) { if (el) el.style.display = show ? "none" : ""; });
    var v = $("socials-view"); if (v) v.style.display = show ? "flex" : "none";
    if (show) { socialsRefresh(); _startFeed(); } else { _stopFeed(); }
  }

  function _renderChannels(d) {
    _last = d;
    var box = $("socials-channels");
    if (box) {
      var chans = (d && d.channels) || {};
      var names = Object.keys(chans);
      box.innerHTML = names.length ? names.map(function (n) {
        var c = chans[n];
        var dot = c.connected ? "🟢" : (c.available ? "⚪" : "🔴");
        var label = n + (c.experimental ? " (experimental)" : "");
        var note = c.note ? (' <span style="opacity:0.7">— ' + esc(c.note) + "</span>") : "";
        var btn = c.connected
          ? '<button class="toolbar-btn" aria-label="Disconnect ' + esc(n) + '" data-tip="Disconnect from ' + esc(n) + '" onclick="socialsConnect(\'' + n + '\',false)">Disconnect</button>'
          : '<button class="toolbar-btn"' + (c.available ? "" : " disabled") + ' aria-label="Connect ' + esc(n) + '" data-tip="Connect to ' + esc(n) + '" onclick="socialsConnect(\'' + n + '\',true)">Connect</button>';
        return '<div style="display:flex;justify-content:space-between;align-items:center;gap:6px;padding:3px 0">'
          + "<span>" + dot + " " + esc(label) + note + "</span>" + btn + "</div>";
      }).join("") : "<i>No channels.</i>";
    }
    var st = $("socials-status"); if (st) st.textContent = (d && d.available === false) ? "unavailable" : "ready";
    // Install hint: which interpreter + exact pip line (fixes "installed but not detected").
    var hint = $("socials-setup-hint");
    if (hint) {
      var needs = [];
      var chans2 = (d && d.channels) || {};
      Object.keys(chans2).forEach(function (n) {
        var c = chans2[n];
        if (!c.available && c.note && c.note.indexOf("pip install") === 0) needs.push(c.note.replace("pip install", "").trim());
      });
      if (needs.length && d && d.python) {
        hint.textContent = 'To enable, install into THIS interpreter, then restart:\n"'
          + d.python + '" -m pip install ' + needs.join(" ")
          + (d.python_version ? ("\n(running Python " + d.python_version + ")") : "");
        hint.style.display = "block";
      } else { hint.style.display = "none"; hint.textContent = ""; }
    }
    var sel = $("socials-target");
    if (sel) {
      var prev = sel.value;
      var nm = Object.keys((d && d.channels) || {});
      sel.innerHTML = nm.map(function (n) { return '<option value="' + n + '">' + esc(n) + "</option>"; }).join("");
      if (prev) sel.value = prev;
    }
    var tg = $("toggle-socials-autoreply"); if (tg) tg.checked = !!(d && d.auto_reply);
    _renderConfig((d && d.config) || {});
    _renderThreads();   // channel list drives the thread tabs
  }

  function _renderConfig(cfg) {
    var box = $("socials-config"); if (!box) return;
    var d = cfg.discord || {}, b = cfg.bitchat || {}, m = cfg.mastodon || {}, k = cfg.bluesky || {};
    function tok(has) { return has ? '<span style="opacity:0.7">(saved)</span>' : '<span style="opacity:0.7">(none)</span>'; }
    box.innerHTML =
      // --- Discord ---
      '<div style="margin-bottom:10px"><div><b>Discord</b> ' + tok(d.has_token) + "</div>"
      + '<input id="cfg-discord-token" class="setting-select" type="password" placeholder="bot token" style="width:100%;margin-top:2px">'
      + '<input id="cfg-discord-channels" class="setting-select" type="text" placeholder="watched channels (comma-sep, optional)" value="' + esc((d.watched_channels || []).join(", ")) + '" style="width:100%;margin-top:2px">'
      + '<div style="display:flex;gap:4px;margin-top:3px"><button class="toolbar-btn" onclick="socialsSaveDiscord()">Save</button>'
      + '<button class="toolbar-btn" onclick="socialsClearToken(\'discord\')">Remove token</button></div></div>'
      // --- Mastodon ---
      + '<div style="margin-bottom:10px"><div><b>Mastodon</b> ' + tok(m.has_token) + "</div>"
      + '<input id="cfg-masto-instance" class="setting-select" type="text" placeholder="https://mastodon.social" value="' + esc(m.instance || "") + '" style="width:100%;margin-top:2px">'
      + '<input id="cfg-masto-token" class="setting-select" type="password" placeholder="access token" style="width:100%;margin-top:2px">'
      + '<div style="display:flex;gap:4px;margin-top:3px"><button class="toolbar-btn" onclick="socialsSaveMastodon()">Save</button>'
      + '<button class="toolbar-btn" onclick="socialsClearToken(\'mastodon\')">Remove token</button></div></div>'
      // --- BlueSky ---
      + '<div style="margin-bottom:10px"><div><b>BlueSky</b> ' + tok(k.has_app_password) + "</div>"
      + '<input id="cfg-bsky-handle" class="setting-select" type="text" placeholder="you.bsky.social" value="' + esc(k.handle || "") + '" style="width:100%;margin-top:2px">'
      + '<input id="cfg-bsky-pass" class="setting-select" type="password" placeholder="app password" style="width:100%;margin-top:2px">'
      + '<input id="cfg-bsky-service" class="setting-select" type="text" placeholder="service (default https://bsky.social)" value="' + esc(k.service || "") + '" style="width:100%;margin-top:2px">'
      + '<div style="display:flex;gap:4px;margin-top:3px"><button class="toolbar-btn" onclick="socialsSaveBluesky()">Save</button>'
      + '<button class="toolbar-btn" onclick="socialsClearToken(\'bluesky\',\'app_password\')">Remove password</button></div></div>'
      // --- BitChat (experimental) ---
      + '<div><div><b>BitChat</b> <span style="opacity:0.7">(experimental)</span></div>'
      + '<div style="display:flex;gap:4px;margin-top:2px">'
      + '<input id="cfg-bitchat-host" class="setting-select" type="text" placeholder="host" value="' + esc(b.host || "localhost") + '" style="flex:1">'
      + '<input id="cfg-bitchat-port" class="setting-select" type="number" placeholder="port" value="' + esc(b.port || 8080) + '" style="width:80px"></div>'
      + '<button class="toolbar-btn" style="margin-top:3px" onclick="socialsSaveBitchat()">Save</button></div>';
  }

  async function socialsRefresh() {
    try {
      var d = await jget("/api/socials/status");
      _renderChannels((d && d.available === false) ? { available: false } : d);
      socialsPeers();
    } catch (e) { var b = $("socials-channels"); if (b) b.innerHTML = "<i>Socials unavailable.</i>"; }
  }

  async function socialsPeers() {
    var box = $("socials-peers"); if (!box) return;
    var chans = (_last && _last.channels) || {};
    var connected = Object.keys(chans).filter(function (n) { return chans[n].connected; });
    if (!connected.length) { box.innerHTML = ""; return; }
    var parts = [];
    for (var i = 0; i < connected.length; i++) {
      var n = connected[i];
      try {
        var d = await jget("/api/socials/peers?channel=" + encodeURIComponent(n));
        var ps = (d && d.peers) || [];
        var names = ps.length ? ps.map(function (p) { return esc(p); }).join(", ") : "(none visible yet)";
        parts.push("<div><b>" + esc(n) + ":</b> " + names + "</div>");
      } catch (e) {}
    }
    box.innerHTML = parts.length ? ("👥 Peers<br>" + parts.join("")) : "";
  }

  async function socialsConnect(name, connect) {
    toast(connect ? ("Connecting " + name + "…") : ("Disconnecting " + name + "…"));
    var d = await jpost("/api/socials/connect", { channel: name, connect: connect });
    if (d && d.channels) { _renderChannels(d); toast((d.ok ? (connect ? "Connected " : "Disconnected ") : "Could not reach ") + name); }
    else { toast("Socials error"); }
    socialsFeed();
  }

  async function socialsSaveDiscord() {
    var tok = (($("cfg-discord-token") || {}).value || "").trim();
    var chs = (($("cfg-discord-channels") || {}).value || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean);
    var settings = { watched_channels: chs };
    if (tok) settings.token = tok;
    var d = await jpost("/api/socials/config", { channel: "discord", settings: settings });
    if (d && d.ok) { if ($("cfg-discord-token")) $("cfg-discord-token").value = ""; toast("Discord settings saved"); socialsRefresh(); }
    else { toast("Save failed"); }
  }

  async function socialsSaveBitchat() {
    var host = (($("cfg-bitchat-host") || {}).value || "localhost").trim();
    var port = parseInt((($("cfg-bitchat-port") || {}).value || "8080"), 10) || 8080;
    var d = await jpost("/api/socials/config", { channel: "bitchat", settings: { host: host, port: port } });
    if (d && d.ok) { toast("BitChat settings saved"); socialsRefresh(); }
    else { toast("Save failed"); }
  }

  async function socialsClearToken(channel, key) {
    key = key || "token";
    var label = key.replace("_", " ");
    if (!window.confirm("Remove the saved " + channel + " " + label + "?")) return;
    var d = await jpost("/api/socials/config", { channel: channel, clear: true, keys: [key] });
    if (d && d.ok) { toast(channel + " " + label + " removed"); socialsRefresh(); }
  }

  async function socialsSaveMastodon() {
    var instance = (($("cfg-masto-instance") || {}).value || "").trim();
    var token = (($("cfg-masto-token") || {}).value || "").trim();
    var settings = { instance: instance };
    if (token) settings.token = token;
    var d = await jpost("/api/socials/config", { channel: "mastodon", settings: settings });
    if (d && d.ok) { if ($("cfg-masto-token")) $("cfg-masto-token").value = ""; toast("Mastodon settings saved"); socialsRefresh(); }
    else { toast("Save failed"); }
  }

  async function socialsSaveBluesky() {
    var handle = (($("cfg-bsky-handle") || {}).value || "").trim();
    var pass = (($("cfg-bsky-pass") || {}).value || "").trim();
    var service = (($("cfg-bsky-service") || {}).value || "").trim();
    var settings = { handle: handle };
    if (service) settings.service = service;
    if (pass) settings.app_password = pass;
    var d = await jpost("/api/socials/config", { channel: "bluesky", settings: settings });
    if (d && d.ok) { if ($("cfg-bsky-pass")) $("cfg-bsky-pass").value = ""; toast("BlueSky settings saved"); socialsRefresh(); }
    else { toast("Save failed"); }
  }

  async function socialsSend() {
    var sel = $("socials-target"); var txt = (($("socials-text") || {}).value || "").trim();
    if (!sel || !sel.value) { toast("No channel selected"); return; }
    if (!txt) { toast("Type a message first"); return; }
    var d = await jpost("/api/socials/send", { channel: sel.value, text: txt });
    if (d && d.ok) { if ($("socials-text")) $("socials-text").value = ""; toast("Sent to " + sel.value); socialsFeed(); }
    else { toast("Send failed — is " + sel.value + " connected?"); }
  }

  async function socialsAutoReply(on) {
    if (on && !window.confirm(
        "Let Sage auto-reply on connected channels?\n\n" +
        "When ON, Sage generates and POSTS a reply to any message that mentions the wake word on a CONNECTED channel. Off by default.")) {
      var t = $("toggle-socials-autoreply"); if (t) t.checked = false; return;
    }
    var d = await jpost("/api/socials/auto-reply", { enabled: !!on });
    toast(d && d.auto_reply ? "Sage auto-reply ON" : "Sage auto-reply OFF");
  }

  // --- Per-channel threads -------------------------------------------------
  // Each Social channel gets its own thread tab; the feed shows only the active
  // one. "all" is a merged convenience view. Counts come from the buffered feed.
  function _threadCounts() {
    var counts = {};
    for (var i = 0; i < _msgs.length; i++) {
      var p = (_msgs[i].platform || "").toLowerCase();
      counts[p] = (counts[p] || 0) + 1;
    }
    return counts;
  }

  function _renderThreads() {
    var box = $("socials-threads"); if (!box) return;
    var chans = (_last && _last.channels) || {};
    var names = Object.keys(chans);
    var counts = _threadCounts();
    function tab(id, label, count) {
      var on = (_activeThread === id);
      var badge = count ? (' <span class="socials-thread-count">' + count + "</span>") : "";
      var tip = (id === "all")
        ? "Show messages from every channel together"
        : ("Show only the " + id + " thread");
      return '<button class="socials-thread' + (on ? " active" : "") + '" role="tab"'
        + ' aria-selected="' + (on ? "true" : "false") + '"'
        + ' data-tip="' + esc(tip) + '"'
        + ' onclick="socialsSelectThread(\'' + esc(id) + '\')">' + esc(label) + badge + "</button>";
    }
    var html = [tab("all", "All", _msgs.length)];
    for (var i = 0; i < names.length; i++) html.push(tab(names[i], names[i], counts[names[i]] || 0));
    box.innerHTML = html.join("");
    // Keep the per-thread Clear button in sync with the active thread.
    var cb = $("socials-clear-thread");
    if (cb) {
      var isAll = (_activeThread === "all");
      cb.disabled = isAll;
      cb.textContent = isAll ? "Clear this thread" : ("Clear " + _activeThread);
      cb.setAttribute("data-tip", isAll
        ? "Pick a channel thread to clear its messages (or use Delete all)"
        : ("Remove the messages buffered for the " + _activeThread + " thread"));
    }
  }

  function _renderFeed() {
    var box = $("socials-feed"); if (!box) return;
    var msgs = (_activeThread === "all")
      ? _msgs
      : _msgs.filter(function (m) { return (m.platform || "").toLowerCase() === _activeThread; });
    if (!msgs.length) {
      box.innerHTML = (_activeThread === "all")
        ? "<i>No messages yet.</i>"
        : "<i>No messages in this thread yet.</i>";
    } else {
      box.innerHTML = msgs.slice(-50).map(function (m) {
        // In a single-channel thread the platform is implied, so show just the sender.
        var who = (_activeThread === "all" ? ((m.platform || "") + "/") : "") + (m.sender || "?");
        return "<div><b>" + esc(who) + ":</b> " + esc(m.content || "") + "</div>";
      }).join("");
    }
    box.scrollTop = box.scrollHeight;
  }

  function socialsSelectThread(name) {
    _activeThread = name || "all";
    // Convenience: selecting a channel thread also points the Send box at it.
    if (_activeThread !== "all") {
      var sel = $("socials-target");
      if (sel) for (var i = 0; i < sel.options.length; i++) {
        if (sel.options[i].value === _activeThread) { sel.value = _activeThread; break; }
      }
    }
    _renderThreads();
    _renderFeed();
  }

  async function socialsClearThread() {
    if (_activeThread === "all") return;            // button is disabled in this state anyway
    var ch = _activeThread;
    var d = await jpost("/api/socials/clear", { channel: ch });
    if (d && d.ok) {
      _msgs = _msgs.filter(function (m) { return (m.platform || "").toLowerCase() !== ch; });
      _renderThreads(); _renderFeed();
      toast("Cleared the " + ch + " thread (" + (d.removed || 0) + " message" + (d.removed === 1 ? "" : "s") + ")");
    } else { toast("Clear failed"); }
  }

  function socialsArmDeleteAll(on) {
    var warn = $("socials-deleteall-warn"); if (warn) warn.style.display = on ? "block" : "none";
    var btn = $("socials-delete-all"); if (btn) btn.disabled = !on;
  }

  async function socialsDeleteAll() {
    var arm = $("socials-deleteall-arm");
    if (!arm || !arm.checked) { toast("Tick the box first to delete every channel"); return; }
    if (!window.confirm("Delete recent messages from ALL Social channels?\n\n"
        + "This clears every thread, not just the one you are viewing. It cannot be undone.\n"
        + "(Nothing is saved to disk or shared across user profiles.)")) return;
    var d = await jpost("/api/socials/clear", { all: true });
    if (d && d.ok) {
      _msgs = [];
      arm.checked = false; socialsArmDeleteAll(false);   // disarm after firing
      _renderThreads(); _renderFeed();
      toast("Cleared all channels (" + (d.removed || 0) + " message" + (d.removed === 1 ? "" : "s") + ")");
    } else { toast("Delete failed"); }
  }

  async function socialsFeed() {
    if (!$("socials-feed")) return;
    try {
      var d = await jget("/api/socials/recent");
      _msgs = (d && d.messages) || [];
    } catch (e) { return; /* transient */ }
    _renderThreads();
    _renderFeed();
  }
  function _startFeed() { _stopFeed(); _feedTimer = setInterval(function () { socialsFeed(); socialsPeers(); }, 5000); socialsFeed(); socialsPeers(); }
  function _stopFeed() { if (_feedTimer) { clearInterval(_feedTimer); _feedTimer = null; } }

  window.socialsOnTab = socialsOnTab; window.socialsRefresh = socialsRefresh; window.socialsConnect = socialsConnect;
  window.socialsPeers = socialsPeers;
  window.socialsSend = socialsSend; window.socialsAutoReply = socialsAutoReply; window.socialsFeed = socialsFeed;
  window.socialsSaveDiscord = socialsSaveDiscord; window.socialsSaveBitchat = socialsSaveBitchat; window.socialsClearToken = socialsClearToken;
  window.socialsSaveMastodon = socialsSaveMastodon; window.socialsSaveBluesky = socialsSaveBluesky;
  window.socialsSelectThread = socialsSelectThread; window.socialsClearThread = socialsClearThread;
  window.socialsArmDeleteAll = socialsArmDeleteAll; window.socialsDeleteAll = socialsDeleteAll;
})();
