/* auth.js -- Phase 2 A3: login / first-run owner-setup gate (frontend).
 *
 * On load, asks /api/auth/status. ONLY when multi-user is enabled AND the caller
 * is not signed in does it draw a blocking overlay: a "create owner account" form
 * on first run, a "sign in" form otherwise. On success it reloads into the app as
 * the signed-in user. When multi-user is off, this file does nothing at all, so a
 * single-user install is unchanged.
 *
 * NOTE: icon glyphs are written as \u escapes (pure-ASCII source) on purpose --
 * literal multi-byte chars get truncated by the editing tooling in this setup.
 */
(function () {
  "use strict";

  var V = function (name, fb) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name);
      // v2.12.2: values are injected into style="..." attributes; a font list
      // like "Cinzel", "Palatino" contains double quotes that would terminate
      // the attribute early (logo lost text-align:center + its display font).
      return String((v && v.trim()) || fb).replace(/"/g, "'");
    } catch (e) { return fb; }
  };

  function inputStyle() {
    return "width:100%;padding:11px 12px;margin:6px 0;border-radius:8px;" +
      "background:" + V("--surface-3", "#142036") + ";color:" + V("--text", "#e2e8f8") +
      ";border:1px solid " + V("--border", "#2a3a5a") + ";font-size:14px;box-sizing:border-box";
  }

  function showAuthOverlay(needsSetup) {
    hideAuthOverlay();
    var subtitle = needsSetup
      ? "First run -- create the owner account."
      : "Sign in to continue.";
    var action = needsSetup ? "Create account" : "Sign in";
    var ov = document.createElement("div");
    ov.id = "auth-overlay";
    ov.setAttribute("role", "dialog");
    ov.setAttribute("aria-modal", "true");
    ov.setAttribute("aria-labelledby", "auth-overlay-title");
    ov.setAttribute("aria-label", needsSetup ? "VeridianAI account setup" : "VeridianAI sign in");
    ov.style.cssText = "position:fixed;inset:0;z-index:99999;display:flex;" +
      "align-items:center;justify-content:center;background:" + V("--bg", "#060a14");
    ov.innerHTML =
      '<div style="width:min(92vw,380px);padding:32px 28px;border-radius:16px;' +
      'background:' + V("--surface", "#0a1020") + ';border:1px solid ' + V("--border", "#2a3a5a") +
      ';box-shadow:0 20px 60px rgba(0,0,0,0.5)">' +
      '<h1 id="auth-overlay-title" style="margin:0 0 12px;font-family:' + V("--font-display", "serif") + ';font-size:42px;line-height:1.05;font-weight:700;' +
      'letter-spacing:0.04em;text-align:center;color:' + V("--text", "#e2e8f8") + '">' +
      '<span style="color:' + V("--text", "#e2e8f8") + '">Veridian</span>' +
      '<span style="color:' + V("--gold", "#f0a500") + '">AI</span></h1>' +
      '<div style="text-align:center;font-size:13px;margin:6px 0 18px;color:' +
      V("--text-muted", "#7890b8") + '">' + subtitle + '</div>' +
      '<input id="auth-username" aria-label="Username" placeholder="Username" autocomplete="username" style="' + inputStyle() + '">' +
      '<input id="auth-password" aria-label="Password" type="password" placeholder="Password" autocomplete="' +
      (needsSetup ? "new-password" : "current-password") + '" style="' + inputStyle() + '">' +
      (needsSetup
        ? '<input id="auth-confirm" aria-label="Confirm password" type="password" placeholder="Confirm password" autocomplete="new-password" style="' + inputStyle() + '">'
        : "") +
      '<div id="auth-error" role="alert" style="min-height:16px;margin:4px 2px;font-size:12px;color:' +
      V("--error", "#ff6b6b") + '"></div>' +
      '<button id="auth-submit" style="width:100%;padding:11px;margin-top:8px;border:none;' +
      // v2.12.7 WCAG: the login button is a branded moment -- pin it to the
      // BRIGHT brand gold with dark ink (9.3:1) regardless of the saved theme.
      // (In the parchment theme V("--gold") resolves to a dark amber, which
      // with dark ink would be dark-on-dark; hardcoding the brand gold here
      // keeps the sign-in button readable in both themes.)
      'border-radius:8px;cursor:pointer;font-size:15px;font-weight:600;background:' +
      '#f0a500;color:#1a1206">' + action + '</button>' +
      '</div>';
    document.body.appendChild(ov);
    var submit = function () { submitAuth(needsSetup); };
    document.getElementById("auth-submit").onclick = submit;
    var onEnter = function (e) { if (e.key === "Enter") submit(); };
    ov.querySelectorAll("input").forEach(function (el) { el.addEventListener("keydown", onEnter); });
    var uname = document.getElementById("auth-username");
    if (uname) uname.focus();
  }

  function hideAuthOverlay() {
    var ov = document.getElementById("auth-overlay");
    if (ov) ov.remove();
  }

  function setError(msg) {
    var e = document.getElementById("auth-error");
    if (e) e.textContent = msg || "";
  }

  async function submitAuth(needsSetup) {
    var u = (document.getElementById("auth-username").value || "").trim();
    var p = document.getElementById("auth-password").value || "";
    if (!u || !p) { setError("Username and password are required."); return; }
    if (needsSetup) {
      var c = (document.getElementById("auth-confirm") || {}).value || "";
      if (p !== c) { setError("Passwords do not match."); return; }
    }
    setError("");
    var endpoint = needsSetup ? "/api/auth/setup" : "/api/auth/login";
    try {
      var r = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ username: u, password: p }),
      });
      if (r.ok) { hideAuthOverlay(); location.reload(); return; }
      var j = {};
      try { j = await r.json(); } catch (e) {}
      setError(j.detail || j.error || (needsSetup ? "Could not create account." : "Invalid credentials."));
    } catch (e) {
      setError("Could not reach the server.");
    }
  }

  function injectLogout(username, isOwner) {
    if (document.getElementById("auth-account-cluster")) return;
    var mkBtn = function (id, label, title) {
      var b = document.createElement("button");
      b.id = id;
      b.type = "button";
      b.textContent = label;
      // Use the a11y-tooltip system (data-tip), not a native title tooltip, so
      // these match the rest of the UI. aria-label also gives the icon-only
      // buttons (e.g. the key glyph) a proper accessible name.
      b.setAttribute("aria-label", title);
      b.setAttribute("data-tip", title);
      b.style.cssText = "padding:4px 9px;font-size:11px;line-height:1.4;border-radius:6px;" +
        "cursor:pointer;white-space:nowrap;background:" + V("--surface-3", "#142036") +
        ";color:" + V("--text-muted", "#7890b8") + ";border:1px solid " + V("--border", "#2a3a5a");
      return b;
    };
    var cluster = document.createElement("span");
    cluster.id = "auth-account-cluster";
    cluster.style.cssText = "display:inline-flex;align-items:center;gap:6px;margin-right:8px";
    // key glyph (U+1F511) = change password ; power glyph (U+23FB) = sign out
    var keyBtn = mkBtn("auth-changepw-btn", "🔑",
      "Change password" + (username ? " (" + username + ")" : ""));
    keyBtn.onclick = function () { showChangePassword(username); };
    var outBtn = mkBtn("auth-logout-btn", "⏻ Sign out",
      "Sign out" + (username ? " (" + username + ")" : ""));
    outBtn.onclick = logout;
    cluster.appendChild(keyBtn);
    cluster.appendChild(outBtn);
    if (isOwner) {
      var usersBtn = mkBtn("auth-users-btn", "Users", "Manage user accounts (owner only)");
      usersBtn.onclick = function () { showUserAdmin(); };
      cluster.appendChild(usersBtn);
    }
    // Preferred home: header controls, to the LEFT of the model dropdown (sits in
    // the gap between the VeridianAI emblem and the model selector).
    var controls = document.querySelector(".header-controls");
    if (controls) { controls.insertBefore(cluster, controls.firstChild); return; }
    // Fallbacks keep it usable if the header markup ever changes.
    var header = document.querySelector(".app-header");
    if (header) { header.appendChild(cluster); return; }
    cluster.style.cssText += ";position:fixed;top:8px;right:10px;z-index:9000";
    document.body.appendChild(cluster);
  }

  function showChangePassword(username) {
    if (document.getElementById("auth-cp-overlay")) return;
    var ov = document.createElement("div");
    ov.id = "auth-cp-overlay";
    ov.setAttribute("role", "dialog");
    ov.setAttribute("aria-modal", "true");
    ov.style.cssText = "position:fixed;inset:0;z-index:99999;display:flex;" +
      "align-items:center;justify-content:center;background:rgba(2,5,12,0.66)";
    ov.innerHTML =
      '<div style="width:min(92vw,360px);padding:28px 26px;border-radius:16px;background:' +
      V("--surface", "#0a1020") + ';border:1px solid ' + V("--border", "#2a3a5a") +
      ';box-shadow:0 20px 60px rgba(0,0,0,0.5)">' +
      '<div style="font-family:' + V("--font-display", "serif") + ';font-size:19px;' +
      'text-align:center;color:' + V("--text", "#e2e8f8") + '">Change password</div>' +
      '<div style="text-align:center;font-size:12px;margin:5px 0 16px;color:' +
      V("--text-muted", "#7890b8") + '">' + (username ? "Signed in as " + username : "") + '</div>' +
      '<input id="cp-current" type="password" placeholder="Current password" autocomplete="current-password" style="' + inputStyle() + '">' +
      '<input id="cp-new" type="password" placeholder="New password" autocomplete="new-password" style="' + inputStyle() + '">' +
      '<input id="cp-confirm" type="password" placeholder="Confirm new password" autocomplete="new-password" style="' + inputStyle() + '">' +
      '<div id="cp-error" role="alert" style="min-height:16px;margin:4px 2px;font-size:12px;color:' +
      V("--error", "#ff6b6b") + '"></div>' +
      '<div style="display:flex;gap:8px;margin-top:6px">' +
      '<button id="cp-cancel" type="button" style="flex:1;padding:10px;border:1px solid ' +
      V("--border", "#2a3a5a") + ';border-radius:8px;cursor:pointer;font-size:14px;background:' +
      V("--surface-3", "#142036") + ';color:' + V("--text-muted", "#7890b8") + '">Cancel</button>' +
      '<button id="cp-submit" type="button" style="flex:1;padding:10px;border:none;border-radius:8px;' +
      'cursor:pointer;font-size:14px;font-weight:600;background:' + V("--gold", "#f0a500") +
      ';color:#1a1206">Update</button>' +
      '</div></div>';
    document.body.appendChild(ov);
    var close = function () { var o = document.getElementById("auth-cp-overlay"); if (o) o.remove(); };
    document.getElementById("cp-cancel").onclick = close;
    ov.addEventListener("mousedown", function (e) { if (e.target === ov) close(); });
    var submit = function () { submitChangePassword(); };
    document.getElementById("cp-submit").onclick = submit;
    ov.querySelectorAll("input").forEach(function (el) {
      el.addEventListener("keydown", function (e) {
        if (e.key === "Enter") submit();
        if (e.key === "Escape") close();
      });
    });
    var cur = document.getElementById("cp-current");
    if (cur) cur.focus();
  }

  function setCPError(msg) {
    var e = document.getElementById("cp-error");
    if (e) e.textContent = msg || "";
  }

  async function submitChangePassword() {
    var cur = (document.getElementById("cp-current") || {}).value || "";
    var nw = (document.getElementById("cp-new") || {}).value || "";
    var cf = (document.getElementById("cp-confirm") || {}).value || "";
    if (!cur || !nw) { setCPError("Fill in all fields."); return; }
    if (nw !== cf) { setCPError("New passwords do not match."); return; }
    if (nw === cur) { setCPError("New password must differ from the current one."); return; }
    setCPError("");
    try {
      var r = await fetch("/api/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ current_password: cur, new_password: nw }),
      });
      if (r.ok) {
        var ov = document.getElementById("auth-cp-overlay");
        if (ov) {
          ov.innerHTML =
            '<div style="width:min(92vw,360px);padding:28px 26px;border-radius:16px;text-align:center;' +
            'background:' + V("--surface", "#0a1020") + ';border:1px solid ' + V("--border", "#2a3a5a") +
            ';color:' + V("--text", "#e2e8f8") + ';font-size:15px">Password updated.</div>';
          setTimeout(function () { var o = document.getElementById("auth-cp-overlay"); if (o) o.remove(); }, 1100);
        }
        return;
      }
      var j = {};
      try { j = await r.json(); } catch (e) {}
      setCPError(j.detail || j.error || "Could not change password.");
    } catch (e) {
      setCPError("Could not reach the server.");
    }
  }

  async function logout() {
    try { await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" }); } catch (e) {}
    location.reload();
  }

  async function checkAuth() {
    var st = { authenticated: false, multiuser: false };
    try {
      var r = await fetch("/api/auth/status", { credentials: "same-origin" });
      st = await r.json();
    } catch (e) {}
    if (!st.multiuser) { hideAuthOverlay(); return st; }   // multi-user off -> no gate
    if (st.authenticated) {
      hideAuthOverlay();
      injectLogout(st.username, st.is_owner);
      armSessionWatch();
    } else {
      showAuthOverlay(!!st.needs_setup);
    }
    return st;
  }

  /* v2.13.1 session watchdog: while signed in, poll /api/auth/status every 30s.
   * This is BOTH halves of Access Controls timing: the poll is the heartbeat
   * that meters daily screen time server-side, AND the detector that notices
   * the session ended (timed session expired, daily budget spent, owner
   * locked the profile or saved new rules) -- on which we reload into the
   * login screen instead of leaving a half-dead UI up. Network hiccups are
   * NOT sign-outs: only an explicit authenticated:false triggers the reload. */
  var _watchTimer = null;
  function armSessionWatch() {
    if (_watchTimer) return;
    _watchTimer = setInterval(async function () {
      try {
        var r = await fetch("/api/auth/status", { credentials: "same-origin" });
        var j = await r.json();
        if (j && j.authenticated === false) {
          clearInterval(_watchTimer); _watchTimer = null;
          location.reload();
        }
      } catch (e) { /* offline blip; try again next tick */ }
    }, 30000);
  }

  /* --- Owner-only user management ---------------------------------------- */
  function showUserAdmin() {
    if (document.getElementById("auth-ua-overlay")) return;
    var ov = document.createElement("div");
    ov.id = "auth-ua-overlay";
    ov.setAttribute("role", "dialog"); ov.setAttribute("aria-modal", "true");
    ov.style.cssText = "position:fixed;inset:0;z-index:99999;display:flex;" +
      "align-items:center;justify-content:center;background:rgba(2,5,12,0.66)";
    ov.innerHTML =
      '<div style="width:min(94vw,440px);padding:26px 24px;border-radius:16px;background:' +
      V("--surface", "#0a1020") + ';border:1px solid ' + V("--border", "#2a3a5a") +
      ';box-shadow:0 20px 60px rgba(0,0,0,0.5)">' +
      '<div style="font-family:' + V("--font-display", "serif") + ';font-size:19px;text-align:center;color:' +
      V("--text", "#e2e8f8") + '">Manage users</div>' +
      '<div id="ua-list" style="margin:14px 0;font-size:13px;color:' + V("--text", "#e2e8f8") + '">Loading...</div>' +
      '<div style="border-top:1px solid ' + V("--border", "#2a3a5a") + ';margin-top:6px;padding-top:12px">' +
      '<div style="font-size:12px;margin-bottom:6px;color:' + V("--text-muted", "#7890b8") + '">Create a new account</div>' +
      '<input id="ua-username" placeholder="Username" autocomplete="off" style="' + inputStyle() + '">' +
      '<input id="ua-password" type="password" placeholder="Password" autocomplete="new-password" style="' + inputStyle() + '">' +
      '<label style="display:flex;align-items:center;gap:7px;font-size:12px;margin:2px 0 4px;cursor:pointer;color:' +
      V("--text-muted", "#7890b8") + '"><input id="ua-restricted" type="checkbox" style="accent-color:' +
      V("--gold", "#f0a500") + '">Restricted profile (socials off; adjust via Access after creating)</label>' +
      '<div id="ua-error" role="alert" style="min-height:16px;margin:4px 2px;font-size:12px;color:' +
      V("--error", "#ff6b6b") + '"></div>' +
      '<div style="display:flex;gap:8px;margin-top:4px">' +
      '<button id="ua-close" type="button" style="flex:1;padding:10px;border:1px solid ' +
      V("--border", "#2a3a5a") + ';border-radius:8px;cursor:pointer;font-size:14px;background:' +
      V("--surface-3", "#142036") + ';color:' + V("--text-muted", "#7890b8") + '">Close</button>' +
      '<button id="ua-create" type="button" style="flex:1;padding:10px;border:none;border-radius:8px;cursor:pointer;' +
      'font-size:14px;font-weight:600;background:' + V("--gold", "#f0a500") + ';color:#1a1206">Create</button>' +
      '</div></div></div>';
    document.body.appendChild(ov);
    var close = function () { var o = document.getElementById("auth-ua-overlay"); if (o) o.remove(); };
    document.getElementById("ua-close").onclick = close;
    ov.addEventListener("mousedown", function (e) { if (e.target === ov) close(); });
    document.getElementById("ua-create").onclick = uaCreate;
    uaLoad();
  }

  function uaSetError(m) { var e = document.getElementById("ua-error"); if (e) e.textContent = m || ""; }

  async function uaLoad() {
    var box = document.getElementById("ua-list"); if (!box) return;
    try {
      var r = await fetch("/api/auth/users", { credentials: "same-origin" });
      var j = await r.json();
      var users = (j && j.users) || [];
      if (!users.length) { box.innerHTML = "<i>No users.</i>"; return; }
      box.innerHTML = users.map(function (u) {
        var tag = u.is_owner ? ' <span style="color:' + V("--gold", "#f0a500") + '">(owner)</span>' : "";
        var name = (u.username || "?").replace(/[<>&]/g, "");
        // v2.13 Access Controls: at-a-glance state badge.
        var a = u.access || {};
        var badge = "";
        if (a.locked) {
          badge = ' <span style="font-size:10px;padding:1px 6px;border-radius:8px;background:' +
            V("--error", "#ff6b6b") + '22;color:' + V("--error", "#ff6b6b") + '">locked</span>';
        } else if ((a.session_minutes | 0) > 0 || (a.daily_minutes | 0) > 0 || a.allowed_hours || a.socials_allowed === false) {
          badge = ' <span style="font-size:10px;padding:1px 6px;border-radius:8px;background:' +
            V("--surface-3", "#142036") + ';color:' + V("--text-muted", "#7890b8") + '">restricted</span>';
        }
        var btnStyle = 'padding:3px 8px;font-size:11px;border-radius:6px;cursor:pointer;background:' +
          V("--surface-3", "#142036") + ';border:1px solid ' + V("--border", "#2a3a5a") + ';';
        var actions = u.is_owner
          ? '<span style="opacity:0.4;font-size:11px">protected</span>'
          : '<button type="button" class="ua-access" data-u="' + encodeURIComponent(u.username) +
            '" style="' + btnStyle + 'color:' + V("--text-muted", "#7890b8") + ';margin-right:6px">Access</button>' +
            '<button type="button" class="ua-del" data-u="' + encodeURIComponent(u.username) +
            '" style="' + btnStyle + 'color:' + V("--error", "#ff6b6b") + '">Delete</button>';
        return '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0">' +
          "<span>" + name + tag + badge + "</span><span>" + actions + "</span></div>";
      }).join("");
      box.querySelectorAll(".ua-del").forEach(function (b) {
        b.onclick = function () { uaDelete(decodeURIComponent(b.getAttribute("data-u"))); };
      });
      box.querySelectorAll(".ua-access").forEach(function (b) {
        b.onclick = function () { uaAccess(decodeURIComponent(b.getAttribute("data-u"))); };
      });
    } catch (e) { box.innerHTML = "<i>Could not load users.</i>"; }
  }

  async function uaCreate() {
    var u = ((document.getElementById("ua-username") || {}).value || "").trim();
    var p = (document.getElementById("ua-password") || {}).value || "";
    if (!u || !p) { uaSetError("Username and password are required."); return; }
    uaSetError("");
    try {
      var restricted = !!((document.getElementById("ua-restricted") || {}).checked);
      var r = await fetch("/api/auth/users", {
        method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
        body: JSON.stringify({ username: u, password: p, restricted: restricted }),
      });
      var j = {}; try { j = await r.json(); } catch (e) {}
      if (r.ok && j.ok) {
        document.getElementById("ua-username").value = "";
        document.getElementById("ua-password").value = "";
        var rc = document.getElementById("ua-restricted"); if (rc) rc.checked = false;
        uaLoad();
      } else { uaSetError(j.detail || j.error || "Could not create user."); }
    } catch (e) { uaSetError("Could not reach the server."); }
  }

  async function uaDelete(username) {
    if (!(await window.oracleConfirm('Delete user "' + username + '"? Their login is removed immediately.', { title: "Delete user", okLabel: "Delete" }))) return;
    var wipe = await window.oracleConfirm('Also ERASE ' + username + "'s stored conversations & archives?\n\n" +
      "OK = erase their data too (cannot be undone).\nCancel = keep their data, remove only the login.", { title: "Erase user data?", okLabel: "Erase data", cancelLabel: "Keep data" });
    try {
      var r = await fetch("/api/auth/users/delete", {
        method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
        body: JSON.stringify({ username: username, wipe_data: wipe }),
      });
      var j = {}; try { j = await r.json(); } catch (e) {}
      if (r.ok && j.ok) { uaLoad(); } else { uaSetError(j.detail || j.error || "Could not delete user."); }
    } catch (e) { uaSetError("Could not reach the server."); }
  }

  /* --- v2.13 Access Controls editor (owner-only, per profile) ------------- */
  function uaAccess(username) {
    if (document.getElementById("auth-ac-overlay")) return;
    var ov = document.createElement("div");
    ov.id = "auth-ac-overlay";
    ov.setAttribute("role", "dialog"); ov.setAttribute("aria-modal", "true");
    ov.setAttribute("aria-label", "Access controls for " + username);
    ov.style.cssText = "position:fixed;inset:0;z-index:100000;display:flex;" +
      "align-items:center;justify-content:center;background:rgba(2,5,12,0.66)";
    var lbl = 'display:block;font-size:12px;margin:10px 0 2px;color:' + V("--text-muted", "#7890b8");
    var chk = 'display:flex;align-items:center;gap:7px;font-size:13px;margin:10px 0 0;cursor:pointer;color:' +
      V("--text", "#e2e8f8");
    ov.innerHTML =
      '<div style="width:min(94vw,420px);max-height:90vh;overflow:auto;padding:26px 24px;border-radius:16px;background:' +
      V("--surface", "#0a1020") + ';border:1px solid ' + V("--border", "#2a3a5a") +
      ';box-shadow:0 20px 60px rgba(0,0,0,0.5)">' +
      '<div style="font-family:' + V("--font-display", "serif") + ';font-size:19px;text-align:center;color:' +
      V("--text", "#e2e8f8") + '">Access controls</div>' +
      '<div style="text-align:center;font-size:12px;margin:5px 0 10px;color:' +
      V("--text-muted", "#7890b8") + '">' + username.replace(/[<>&]/g, "") + '</div>' +
      '<div id="ac-body" style="font-size:13px;color:' + V("--text", "#e2e8f8") + '">' +
      '<label style="' + lbl + '" for="ac-minutes">Session length limit (minutes; 0 = no limit)</label>' +
      '<input id="ac-minutes" type="number" min="0" max="1440" step="5" style="' + inputStyle() + '">' +
      '<label style="' + lbl + '" for="ac-daily">Daily usage cap (total minutes per day; 0 = no cap)</label>' +
      '<input id="ac-daily" type="number" min="0" max="1440" step="5" style="' + inputStyle() + '">' +
      '<div id="ac-usage" style="font-size:11px;margin:2px 2px 0;color:' + V("--text-muted", "#7890b8") + '"></div>' +
      '<label style="' + lbl + '">Allowed sign-in hours (leave both empty = anytime; overnight windows OK)</label>' +
      '<div style="display:flex;gap:8px;align-items:center">' +
      '<input id="ac-from" type="time" aria-label="From" style="' + inputStyle() + ';margin:0">' +
      '<span style="color:' + V("--text-muted", "#7890b8") + '">to</span>' +
      '<input id="ac-to" type="time" aria-label="To" style="' + inputStyle() + ';margin:0"></div>' +
      '<label style="' + chk + '"><input id="ac-socials" type="checkbox" style="accent-color:' +
      V("--gold", "#f0a500") + '">Allow socials (Discord, Mastodon, Bluesky, BitChat)</label>' +
      '<div style="border-top:1px solid ' + V("--border", "#2a3a5a") + ';margin-top:14px;padding-top:4px">' +
      '<label style="' + chk + '"><input id="ac-locked" type="checkbox" style="accent-color:' +
      V("--error", "#ff6b6b") + '">Lock sign-in now (ends their current session)</label>' +
      '<label style="' + lbl + '" for="ac-reason">Message shown at sign-in (e.g. "See Manager" / "Come talk to me first")</label>' +
      '<input id="ac-reason" maxlength="300" placeholder="This account is temporarily unavailable." style="' + inputStyle() + '">' +
      '<label style="' + lbl + '" for="ac-unlock">Auto-unlock after (hours; 0 = until you unlock)</label>' +
      '<input id="ac-unlock" type="number" min="0" max="8784" step="1" value="0" style="' + inputStyle() + '">' +
      '</div></div>' +
      '<div style="font-size:11px;margin-top:10px;color:' + V("--text-muted", "#7890b8") +
      '">Saving applies immediately: this profile is signed out and the new rules take effect at their next sign-in.</div>' +
      '<div id="ac-error" role="alert" style="min-height:16px;margin:6px 2px 0;font-size:12px;color:' +
      V("--error", "#ff6b6b") + '"></div>' +
      '<div style="display:flex;gap:8px;margin-top:6px">' +
      '<button id="ac-cancel" type="button" style="flex:1;padding:10px;border:1px solid ' +
      V("--border", "#2a3a5a") + ';border-radius:8px;cursor:pointer;font-size:14px;background:' +
      V("--surface-3", "#142036") + ';color:' + V("--text-muted", "#7890b8") + '">Cancel</button>' +
      '<button id="ac-save" type="button" style="flex:1;padding:10px;border:none;border-radius:8px;' +
      'cursor:pointer;font-size:14px;font-weight:600;background:' + V("--gold", "#f0a500") +
      ';color:#1a1206">Save</button></div></div>';
    document.body.appendChild(ov);
    var close = function () { var o = document.getElementById("auth-ac-overlay"); if (o) o.remove(); };
    document.getElementById("ac-cancel").onclick = close;
    ov.addEventListener("mousedown", function (e) { if (e.target === ov) close(); });
    ov.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
    document.getElementById("ac-save").onclick = function () { acSave(username, close); };
    acLoad(username);
  }

  function acSetError(m) { var e = document.getElementById("ac-error"); if (e) e.textContent = m || ""; }

  async function acLoad(username) {
    try {
      var r = await fetch("/api/auth/users/access?username=" + encodeURIComponent(username),
        { credentials: "same-origin" });
      if (!r.ok) { acSetError("Could not load access controls."); return; }
      var j = await r.json();
      var a = (j && j.access) || {};
      var el = function (id) { return document.getElementById(id) || {}; };
      el("ac-minutes").value = a.session_minutes || 0;
      el("ac-daily").value = a.daily_minutes || 0;
      var ug = document.getElementById("ac-usage");
      if (ug) {
        ug.textContent = (a.daily_minutes || 0) > 0
          ? "Used today: " + (j.used_today_minutes || 0) + " of " + a.daily_minutes + " min (resets at midnight)"
          : "";
      }
      var w = (a.allowed_hours || "").split("-");
      el("ac-from").value = w.length === 2 ? w[0] : "";
      el("ac-to").value = w.length === 2 ? w[1] : "";
      el("ac-socials").checked = a.socials_allowed !== false;
      el("ac-locked").checked = !!a.locked;
      el("ac-reason").value = a.lock_reason || "";
      // Restore the auto-unlock countdown from the stored deadline. (v2.13
      // bug: this field was never loaded back, so it LOOKED like it reset to
      // 0 on every save.) Ceil so "47 minutes left" reads as 1, not 0.
      var hrs = 0;
      if (a.locked && a.lock_until) {
        hrs = Math.max(1, Math.ceil((a.lock_until * 1000 - Date.now()) / 3600000));
      }
      el("ac-unlock").value = hrs;
    } catch (e) { acSetError("Could not reach the server."); }
  }

  async function acSave(username, close) {
    var el = function (id) { return document.getElementById(id) || {}; };
    var from = el("ac-from").value || "", to = el("ac-to").value || "";
    if ((from && !to) || (!from && to)) { acSetError("Set both hours, or clear both for anytime."); return; }
    if (from && to && from === to) { acSetError("Hours can't start and end at the same time."); return; }
    var access = {
      session_minutes: parseInt(el("ac-minutes").value, 10) || 0,
      daily_minutes: parseInt(el("ac-daily").value, 10) || 0,
      allowed_hours: (from && to) ? (from + "-" + to) : "",
      socials_allowed: !!el("ac-socials").checked,
      locked: !!el("ac-locked").checked,
      lock_reason: (el("ac-reason").value || "").trim(),
    };
    var hrs = parseInt(el("ac-unlock").value, 10) || 0;
    // Locked: always send lock_until (null = "until you unlock") so switching
    // a timed ban to an indefinite one actually clears the old deadline.
    if (access.locked) access.lock_until = hrs > 0 ? Math.floor(Date.now() / 1000) + hrs * 3600 : null;
    acSetError("");
    try {
      var r = await fetch("/api/auth/users/access", {
        method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
        body: JSON.stringify({ username: username, access: access }),
      });
      var j = {}; try { j = await r.json(); } catch (e) {}
      if (r.ok && j.ok) { close(); uaLoad(); return; }
      acSetError(j.detail || j.error || "Could not save access controls.");
    } catch (e) { acSetError("Could not reach the server."); }
  }

  window.OracleAuth = {
    checkAuth: checkAuth,
    logout: logout,
    showAuthOverlay: showAuthOverlay,
    showChangePassword: showChangePassword,
    showUserAdmin: showUserAdmin,
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", checkAuth);
  } else {
    checkAuth();
  }
})();
