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
        ? '<input id="auth-confirm" aria-label="Confirm password" type="password" placeholder="Confirm password" autocomplete="new-password" style="' + inputStyle() + '">' +
          // Non-blocking strength meter (WCAG 3.3.8: advisory, never a gate)
          '<div id="auth-meter" aria-live="polite" style="min-height:26px;margin:2px 2px 0"></div>' +
          '<div style="font-size:11px;margin:2px 2px 6px;color:' + V("--text-muted", "#7890b8") +
          '">At least 16 characters. Spaces are fine; a few random words make a great password. Paste works.</div>'
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
    if (needsSetup) {
      attachMeter("auth-password", "auth-meter", function () {
        return (document.getElementById("auth-username") || {}).value || "";
      });
    }
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
      var j = {};
      try { j = await r.json(); } catch (e) {}
      // MFA-enrolled account: password ok, second factor required. The server
      // returned a short-lived challenge token instead of a session.
      if (r.ok && j.mfa_required) { showMfaStep(j); return; }
      if (r.ok) { hideAuthOverlay(); location.reload(); return; }
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
    // shield glyph (U+1F6E1) = two-step verification / security keys
    var secBtn = mkBtn("auth-security-btn", "🛡️",
      "Two-step verification" + (username ? " (" + username + ")" : ""));
    secBtn.onclick = function () { showSecurity(username); };
    var outBtn = mkBtn("auth-logout-btn", "⏻ Sign out",
      "Sign out" + (username ? " (" + username + ")" : ""));
    outBtn.onclick = logout;
    cluster.appendChild(keyBtn);
    cluster.appendChild(secBtn);
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

  function showChangePassword(username, forced) {
    if (document.getElementById("auth-cp-overlay")) return;
    var ov = document.createElement("div");
    ov.id = "auth-cp-overlay";
    if (forced) ov.setAttribute("data-forced", "1");
    ov.setAttribute("role", "dialog");
    ov.setAttribute("aria-modal", "true");
    // Forced mode = the migration gate for legacy weak passwords: opaque
    // backdrop, no cancel, no click-away. The server enforces the same rule,
    // so this is honest UI, not security theater.
    ov.style.cssText = "position:fixed;inset:0;z-index:99999;display:flex;" +
      "align-items:center;justify-content:center;background:" +
      (forced ? V("--bg", "#060a14") : "rgba(2,5,12,0.66)");
    ov.innerHTML =
      '<div style="width:min(92vw,360px);padding:28px 26px;border-radius:16px;background:' +
      V("--surface", "#0a1020") + ';border:1px solid ' + V("--border", "#2a3a5a") +
      ';box-shadow:0 20px 60px rgba(0,0,0,0.5)">' +
      '<div style="font-family:' + V("--font-display", "serif") + ';font-size:19px;' +
      'text-align:center;color:' + V("--text", "#e2e8f8") + '">' +
      (forced ? "Update your password" : "Change password") + '</div>' +
      '<div style="text-align:center;font-size:12px;margin:5px 0 16px;color:' +
      V("--text-muted", "#7890b8") + '">' +
      (forced
        ? "Security upgrade: your current password no longer meets the new " +
          "policy (16+ characters). Pick a new one to continue" +
          (username ? ", " + username : "") + "."
        : (username ? "Signed in as " + username : "")) + '</div>' +
      '<input id="cp-current" type="password" placeholder="Current password" autocomplete="current-password" style="' + inputStyle() + '">' +
      '<input id="cp-new" type="password" placeholder="New password" autocomplete="new-password" style="' + inputStyle() + '">' +
      '<input id="cp-confirm" type="password" placeholder="Confirm new password" autocomplete="new-password" style="' + inputStyle() + '">' +
      '<div id="cp-meter" aria-live="polite" style="min-height:26px;margin:2px 2px 0"></div>' +
      '<div style="font-size:11px;margin:2px 2px 6px;color:' + V("--text-muted", "#7890b8") +
      '">At least 16 characters. Spaces are fine; a few random words make a great password. Paste works.</div>' +
      '<div id="cp-error" role="alert" style="min-height:16px;margin:4px 2px;font-size:12px;color:' +
      V("--error", "#ff6b6b") + '"></div>' +
      '<div style="display:flex;gap:8px;margin-top:6px">' +
      (forced ? "" :
        '<button id="cp-cancel" type="button" style="flex:1;padding:10px;border:1px solid ' +
        V("--border", "#2a3a5a") + ';border-radius:8px;cursor:pointer;font-size:14px;background:' +
        V("--surface-3", "#142036") + ';color:' + V("--text-muted", "#7890b8") + '">Cancel</button>') +
      '<button id="cp-submit" type="button" style="flex:1;padding:10px;border:none;border-radius:8px;' +
      'cursor:pointer;font-size:14px;font-weight:600;background:' + V("--gold", "#f0a500") +
      ';color:#1a1206">Update</button>' +
      '</div></div>';
    document.body.appendChild(ov);
    var close = function () { var o = document.getElementById("auth-cp-overlay"); if (o) o.remove(); };
    var cancelBtn = document.getElementById("cp-cancel");
    if (cancelBtn) cancelBtn.onclick = close;
    if (!forced) ov.addEventListener("mousedown", function (e) { if (e.target === ov) close(); });
    var submit = function () { submitChangePassword(); };
    document.getElementById("cp-submit").onclick = submit;
    ov.querySelectorAll("input").forEach(function (el) {
      el.addEventListener("keydown", function (e) {
        if (e.key === "Enter") submit();
        if (e.key === "Escape" && !forced) close();
      });
    });
    attachMeter("cp-new", "cp-meter", function () { return username || ""; });
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
        // Forced (migration) mode: the old session was confined to the auth
        // surface; the change-password response set a fresh unconfined
        // session cookie, so a reload lands in the app proper.
        if (ov && ov.getAttribute("data-forced")) { location.reload(); return; }
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
      // Legacy weak password detected at login: the backend confines this
      // session to the auth surface, so route straight into a mandatory
      // change-password modal (no cancel) before the app unlocks.
      if (st.must_change) { showChangePassword(st.username, true); return st; }
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
            '<button type="button" class="ua-mfa" data-u="' + encodeURIComponent(u.username) +
            '" style="' + btnStyle + 'color:' + V("--text-muted", "#7890b8") + ';margin-right:6px">Reset MFA</button>' +
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
      box.querySelectorAll(".ua-mfa").forEach(function (b) {
        b.onclick = function () { uaMfaReset(decodeURIComponent(b.getAttribute("data-u"))); };
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
      '<div id="ac-window-note" aria-live="polite" style="font-size:11px;margin:3px 2px 0;color:' +
      V("--text-muted", "#7890b8") + '"></div>' +
      '<label style="' + chk + '"><input id="ac-socials" type="checkbox" style="accent-color:' +
      V("--gold", "#f0a500") + '">Allow socials (Discord, Mastodon, Bluesky, BitChat)</label>' +
      // v2.12.9 delegated admin: grant an assistant-manager profile specific
      // owner-level controls. Maps to access_policy.admin_grants; enforced
      // server-side by _owner_gate(cap) / _owner_guard -- these checkboxes
      // are the owner's pen, not the lock. Node tokens, BitChat verify,
      // AIQNudge and devmode are deliberately NOT delegable (no checkbox).
      '<div style="border-top:1px solid ' + V("--border", "#2a3a5a") + ';margin-top:14px;padding-top:4px">' +
      '<div style="font-size:12px;margin:4px 2px 2px;color:' + V("--text-muted", "#7890b8") +
      '">Delegated admin \u2014 give this profile owner-level controls for:</div>' +
      '<label style="' + chk + '"><input id="ac-cap-models" type="checkbox" style="accent-color:' +
      V("--gold", "#f0a500") + '">Models &amp; tiers (load, unload, restart)</label>' +
      '<label style="' + chk + '"><input id="ac-cap-integrations" type="checkbox" style="accent-color:' +
      V("--gold", "#f0a500") + '">Integrations (web-search key, browser, plugins)</label>' +
      '<label style="' + chk + '"><input id="ac-cap-imagegen" type="checkbox" style="accent-color:' +
      V("--gold", "#f0a500") + '">Image engine (ComfyUI setup &amp; models)</label>' +
      '<label style="' + chk + '"><input id="ac-cap-skills" type="checkbox" style="accent-color:' +
      V("--gold", "#f0a500") + '">Skill sharing &amp; trust store</label>' +
      '</div>' +
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
    var acF = document.getElementById("ac-from"), acT = document.getElementById("ac-to");
    if (acF) acF.addEventListener("input", acWindowNote);
    if (acT) acT.addEventListener("input", acWindowNote);
    acLoad(username);
  }

  function acSetError(m) { var e = document.getElementById("ac-error"); if (e) e.textContent = m || ""; }

  function acClock12(v) {
    var p = String(v).split(":");
    var h = parseInt(p[0], 10), m = parseInt(p[1], 10);
    if (isNaN(h) || isNaN(m)) return null;
    return (h % 12 || 12) + ":" + (m < 10 ? "0" + m : m) + " " + (h < 12 ? "AM" : "PM");
  }

  // Plain-English echo of the sign-in window (v2.12.9, field report: Todd).
  // The native time picker's AM/PM segment makes it easy to save 1:00 PM
  // where 1:00 AM was meant; the stored "HH:MM-HH:MM" then LOOKS right in
  // 24-hour form and the mistake only surfaces as a mystery lockout at the
  // login screen. Echoing the window in words -- with an explicit
  // "crosses midnight" tag for overnight ranges -- catches the slip here,
  // at save time. Backend _fmt_window got the matching 12-hour treatment.
  function acWindowNote() {
    var el = document.getElementById("ac-window-note"); if (!el) return;
    var from = (document.getElementById("ac-from") || {}).value || "";
    var to = (document.getElementById("ac-to") || {}).value || "";
    if (!from || !to) { el.textContent = ""; return; }
    var a = acClock12(from), b = acClock12(to);
    if (!a || !b) { el.textContent = ""; return; }
    if (from === to) { el.textContent = "Start and end match \u2014 clear both for anytime."; return; }
    el.textContent = "Sign-in allowed " + a + " \u2013 " + b +
      (to < from ? " (crosses midnight into the next day)" : "");
  }

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
      acWindowNote();
      el("ac-socials").checked = a.socials_allowed !== false;
      var g = a.admin_grants || [];
      el("ac-cap-models").checked = g.indexOf("models") !== -1;
      el("ac-cap-integrations").checked = g.indexOf("integrations") !== -1;
      el("ac-cap-imagegen").checked = g.indexOf("imagegen") !== -1;
      el("ac-cap-skills").checked = g.indexOf("skills") !== -1;
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
      admin_grants: ["models", "integrations", "imagegen", "skills"].filter(
        function (c) { return !!el("ac-cap-" + c).checked; }),
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

  /* --- v2.13.5 password strength meter (non-blocking, WCAG 3.3.8) --------- */
  /* Renders 5 segments + a label from the SERVER's validator (one source of
   * truth, no logic drift). Advisory only: it never disables submit; the
   * server rejects with real error text if the password fails policy. */
  function attachMeter(inputId, mountId, getUsername) {
    var inp = document.getElementById(inputId);
    var mount = document.getElementById(mountId);
    if (!inp || !mount) return;
    var timer = null;
    var render = function (j) {
      var s = (j && j.strength) || { score: 0, label: "" };
      var cols = [V("--error", "#ff6b6b"), "#e08030", "#d5a020", "#7fb069", "#3fa060"];
      var segs = "";
      for (var i = 0; i < 5; i++) {
        segs += '<span style="flex:1;height:5px;border-radius:3px;background:' +
          (i <= s.score ? cols[s.score] : V("--surface-3", "#142036")) + '"></span>';
      }
      var note = s.label || "";
      if (j && j.errors && j.errors.length) note += " — " + j.errors[0];
      mount.innerHTML =
        '<div style="display:flex;gap:4px;margin:4px 0 3px">' + segs + '</div>' +
        '<div style="font-size:11px;color:' + V("--text-muted", "#7890b8") + '">' +
        note.replace(/[<>&]/g, "") + '</div>';
    };
    inp.addEventListener("input", function () {
      clearTimeout(timer);
      var val = inp.value || "";
      if (!val) { mount.innerHTML = ""; return; }
      timer = setTimeout(async function () {
        try {
          var r = await fetch("/api/auth/password-check", {
            method: "POST", headers: { "Content-Type": "application/json" },
            credentials: "same-origin",
            body: JSON.stringify({ password: val,
                                   username: getUsername ? getUsername() : "" }),
          });
          if (r.ok) render(await r.json());
        } catch (e) { /* meter is advisory; stay quiet offline */ }
      }, 250);
    });
  }

  /* --- v2.13.5 second sign-in step (TOTP / security key / recovery) -------- */
  function showMfaStep(ch) {
    var ov = document.getElementById("auth-overlay");
    if (!ov) return;
    var hasTotp = (ch.methods || []).indexOf("totp") !== -1;
    var hasKey = (ch.methods || []).indexOf("fido2") !== -1;
    var hasRecovery = (ch.methods || []).indexOf("recovery") !== -1;
    // Key-only accounts: the text input is for RECOVERY codes, not TOTP.
    var useRecovery = !hasTotp && hasRecovery;
    ov.innerHTML =
      '<div style="width:min(92vw,380px);padding:32px 28px;border-radius:16px;' +
      'background:' + V("--surface", "#0a1020") + ';border:1px solid ' + V("--border", "#2a3a5a") +
      ';box-shadow:0 20px 60px rgba(0,0,0,0.5)">' +
      '<div style="font-family:' + V("--font-display", "serif") + ';font-size:22px;' +
      'text-align:center;color:' + V("--text", "#e2e8f8") + '">Two-step verification</div>' +
      '<div id="mfa-sub" style="text-align:center;font-size:12px;margin:6px 0 16px;color:' +
      V("--text-muted", "#7890b8") + '">' +
      (hasTotp ? "Enter the 6-digit code from your authenticator app."
               : "Confirm sign-in with your security key" +
                 (hasRecovery ? ", or enter a recovery code." : ".")) + '</div>' +
      (hasTotp || hasRecovery
        ? '<input id="mfa-code" aria-label="Verification code" ' +
          (useRecovery ? 'placeholder="xxxxx-xxxxx"' : 'inputmode="numeric" placeholder="123456"') +
          ' autocomplete="one-time-code" style="' + inputStyle() +
          ';text-align:center;letter-spacing:' + (useRecovery ? '0.05em' : '0.2em') + ';font-size:17px">'
        : "") +
      '<div id="mfa-error" role="alert" style="min-height:16px;margin:4px 2px;font-size:12px;color:' +
      V("--error", "#ff6b6b") + '"></div>' +
      ((hasTotp || hasRecovery)
        ? '<button id="mfa-verify" type="button" style="width:100%;padding:11px;margin-top:4px;' +
          'border:none;border-radius:8px;cursor:pointer;font-size:15px;font-weight:600;' +
          'background:#f0a500;color:#1a1206">Verify</button>'
        : "") +
      (hasKey
        ? '<button id="mfa-key" type="button" style="width:100%;padding:11px;margin-top:8px;' +
          'border:1px solid ' + V("--border", "#2a3a5a") + ';border-radius:8px;cursor:pointer;' +
          'font-size:14px;background:' + V("--surface-3", "#142036") + ';color:' +
          V("--text", "#e2e8f8") + '">🔐 Use security key</button>' +
          '<input id="mfa-pin" type="password" aria-label="Security key PIN" placeholder="Key PIN (only if asked)" ' +
          'autocomplete="off" style="' + inputStyle() + ';display:none">'
        : "") +
      ((hasRecovery && hasTotp)  // key-only accounts are ALREADY in recovery mode
        ? '<button id="mfa-recovery" type="button" style="width:100%;margin-top:10px;background:none;' +
          'border:none;cursor:pointer;font-size:12px;text-decoration:underline;color:' +
          V("--text-muted", "#7890b8") + '">Use a recovery code instead</button>'
        : "") +
      '</div>';
    var err = function (m) {
      var e = document.getElementById("mfa-error");
      if (e) e.textContent = m || "";
    };
    var verify = async function () {
      var code = (document.getElementById("mfa-code") || {}).value || "";
      if (!code) { err("Enter a code."); return; }
      err("");
      try {
        var r = await fetch("/api/auth/mfa/verify", {
          method: "POST", headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ mfa_token: ch.mfa_token, code: code,
                                 method: useRecovery ? "recovery" : "totp" }),
        });
        if (r.ok) { location.reload(); return; }
        var j = {}; try { j = await r.json(); } catch (e2) {}
        err(j.detail || j.error || "That code didn't verify.");
      } catch (e2) { err("Could not reach the server."); }
    };
    var vBtn = document.getElementById("mfa-verify");
    if (vBtn) vBtn.onclick = verify;
    var codeEl = document.getElementById("mfa-code");
    if (codeEl) {
      codeEl.addEventListener("keydown", function (e) { if (e.key === "Enter") verify(); });
      codeEl.focus();
    }
    var kBtn = document.getElementById("mfa-key");
    if (kBtn) {
      kBtn.onclick = async function () {
        err("");
        kBtn.disabled = true;
        kBtn.textContent = "Touch your security key…";
        try {
          var pin = (document.getElementById("mfa-pin") || {}).value || "";
          var r = await fetch("/api/auth/fido2/verify", {
            method: "POST", headers: { "Content-Type": "application/json" },
            credentials: "same-origin",
            body: JSON.stringify({ mfa_token: ch.mfa_token, pin: pin || null }),
          });
          if (r.ok) { location.reload(); return; }
          var j = {}; try { j = await r.json(); } catch (e2) {}
          var msg = j.detail || j.error || "Security key check failed.";
          err(msg);
          if (/pin/i.test(msg)) {
            var p = document.getElementById("mfa-pin");
            if (p) { p.style.display = ""; p.focus(); }
          }
        } catch (e2) { err("Could not reach the server."); }
        kBtn.disabled = false;
        kBtn.textContent = "🔐 Use security key";
      };
    }
    var recBtn = document.getElementById("mfa-recovery");
    if (recBtn) {
      recBtn.onclick = function () {
        useRecovery = !useRecovery;
        var sub = document.getElementById("mfa-sub");
        var inp = document.getElementById("mfa-code");
        if (useRecovery) {
          if (sub) sub.textContent = "Enter one of your single-use recovery codes.";
          if (inp) { inp.placeholder = "xxxxx-xxxxx"; inp.value = ""; inp.style.letterSpacing = "0.05em"; }
          recBtn.textContent = "Use an authenticator code instead";
        } else {
          if (sub) sub.textContent = "Enter the 6-digit code from your authenticator app.";
          if (inp) { inp.placeholder = "123456"; inp.value = ""; inp.style.letterSpacing = "0.2em"; }
          recBtn.textContent = "Use a recovery code instead";
        }
        if (inp) inp.focus();
      };
    }
  }

  /* --- small password re-prompt for destructive MFA ops -------------------- */
  function askPassword(promptText) {
    return new Promise(function (resolve) {
      var ov = document.createElement("div");
      ov.id = "auth-pw-prompt";
      ov.setAttribute("role", "dialog"); ov.setAttribute("aria-modal", "true");
      ov.style.cssText = "position:fixed;inset:0;z-index:100002;display:flex;" +
        "align-items:center;justify-content:center;background:rgba(2,5,12,0.66)";
      ov.innerHTML =
        '<div style="width:min(92vw,340px);padding:24px 22px;border-radius:14px;background:' +
        V("--surface", "#0a1020") + ';border:1px solid ' + V("--border", "#2a3a5a") + '">' +
        '<div style="font-size:14px;margin-bottom:10px;color:' + V("--text", "#e2e8f8") + '">' +
        (promptText || "Confirm your password") + '</div>' +
        '<input id="pwp-input" type="password" aria-label="Password" placeholder="Password" ' +
        'autocomplete="current-password" style="' + inputStyle() + '">' +
        '<div style="display:flex;gap:8px;margin-top:10px">' +
        '<button id="pwp-cancel" type="button" style="flex:1;padding:9px;border:1px solid ' +
        V("--border", "#2a3a5a") + ';border-radius:8px;cursor:pointer;background:' +
        V("--surface-3", "#142036") + ';color:' + V("--text-muted", "#7890b8") + '">Cancel</button>' +
        '<button id="pwp-ok" type="button" style="flex:1;padding:9px;border:none;border-radius:8px;' +
        'cursor:pointer;font-weight:600;background:' + V("--gold", "#f0a500") + ';color:#1a1206">OK</button>' +
        '</div></div>';
      document.body.appendChild(ov);
      var done = function (val) { ov.remove(); resolve(val); };
      document.getElementById("pwp-cancel").onclick = function () { done(null); };
      document.getElementById("pwp-ok").onclick = function () {
        done((document.getElementById("pwp-input") || {}).value || "");
      };
      var inp = document.getElementById("pwp-input");
      inp.addEventListener("keydown", function (e) {
        if (e.key === "Enter") done(inp.value || "");
        if (e.key === "Escape") done(null);
      });
      inp.focus();
    });
  }

  /* --- recovery codes popup (shown ONCE at enrollment / regeneration) ------ */
  function showRecoveryCodes(codes, context) {
    var ov = document.createElement("div");
    ov.id = "auth-rc-overlay";
    ov.setAttribute("role", "dialog"); ov.setAttribute("aria-modal", "true");
    ov.setAttribute("aria-label", "Recovery codes");
    ov.style.cssText = "position:fixed;inset:0;z-index:100003;display:flex;" +
      "align-items:center;justify-content:center;background:rgba(2,5,12,0.8)";
    // Numbered so people can track which codes they've used (cross them off)
    // and count what's left -- calm beats a pile of identical-looking codes.
    var grid = codes.map(function (c, i) {
      return '<code style="padding:5px 8px;border-radius:6px;background:' +
        V("--surface-3", "#142036") + ';color:' + V("--text", "#e2e8f8") +
        ';font-size:13px;letter-spacing:0.05em">' +
        '<span style="display:inline-block;min-width:2em;color:' +
        V("--text-muted", "#7890b8") + '">' + (i + 1) + '.</span>' + c + '</code>';
    }).join("");
    ov.innerHTML =
      '<div style="width:min(94vw,420px);padding:26px 24px;border-radius:16px;background:' +
      V("--surface", "#0a1020") + ';border:1px solid ' + V("--border", "#2a3a5a") + '">' +
      '<div style="font-family:' + V("--font-display", "serif") + ';font-size:19px;text-align:center;color:' +
      V("--text", "#e2e8f8") + '">Your recovery codes</div>' +
      '<div style="font-size:12px;margin:8px 0 12px;color:' + V("--text-muted", "#7890b8") + '">' +
      (context || "") + 'Each code signs you in ONCE if you lose your authenticator or ' +
      'security key. This is the only time they are shown — save them somewhere ' +
      'safe (a password manager, or paper in a drawer).</div>' +
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">' + grid + '</div>' +
      '<div style="display:flex;gap:8px;margin-top:14px">' +
      '<button id="rc-copy" type="button" style="flex:1;padding:10px;border:1px solid ' +
      V("--border", "#2a3a5a") + ';border-radius:8px;cursor:pointer;background:' +
      V("--surface-3", "#142036") + ';color:' + V("--text", "#e2e8f8") + '">Copy all</button>' +
      '<button id="rc-done" type="button" style="flex:1;padding:10px;border:none;border-radius:8px;' +
      'cursor:pointer;font-weight:600;background:' + V("--gold", "#f0a500") + ';color:#1a1206">' +
      'I saved these codes</button>' +
      '</div></div>';
    document.body.appendChild(ov);
    document.getElementById("rc-copy").onclick = function () {
      // Copy WITH the numbering -- the backend's verifier strips a leading
      // "N." / "N)" prefix, so pasting a numbered line straight in still works.
      var text = codes.map(function (c, i) { return (i + 1) + ". " + c; }).join("\n");
      var flash = function () {
        var b = document.getElementById("rc-copy");
        if (b) { b.textContent = "Copied ✓"; setTimeout(function () { if (b) b.textContent = "Copy all"; }, 1400); }
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(flash, flash);
      } else {
        var ta = document.createElement("textarea");
        ta.value = text; document.body.appendChild(ta); ta.select();
        try { document.execCommand("copy"); } catch (e) {}
        ta.remove(); flash();
      }
    };
    document.getElementById("rc-done").onclick = function () { ov.remove(); };
  }

  /* --- security panel: TOTP + security keys + recovery codes --------------- */
  function showSecurity(username) {
    if (document.getElementById("auth-sec-overlay")) return;
    var ov = document.createElement("div");
    ov.id = "auth-sec-overlay";
    ov.setAttribute("role", "dialog"); ov.setAttribute("aria-modal", "true");
    ov.setAttribute("aria-label", "Two-step verification settings");
    ov.style.cssText = "position:fixed;inset:0;z-index:100000;display:flex;" +
      "align-items:center;justify-content:center;background:rgba(2,5,12,0.66)";
    ov.innerHTML =
      '<div style="width:min(94vw,440px);max-height:90vh;overflow:auto;padding:26px 24px;' +
      'border-radius:16px;background:' + V("--surface", "#0a1020") + ';border:1px solid ' +
      V("--border", "#2a3a5a") + ';box-shadow:0 20px 60px rgba(0,0,0,0.5)">' +
      '<div style="font-family:' + V("--font-display", "serif") + ';font-size:19px;text-align:center;color:' +
      V("--text", "#e2e8f8") + '">Two-step verification</div>' +
      '<div style="text-align:center;font-size:12px;margin:5px 0 14px;color:' +
      V("--text-muted", "#7890b8") + '">' + (username || "") + '</div>' +
      '<div id="sec-body" style="font-size:13px;color:' + V("--text", "#e2e8f8") + '">Loading…</div>' +
      '<div id="sec-error" role="alert" style="min-height:16px;margin:8px 2px 0;font-size:12px;color:' +
      V("--error", "#ff6b6b") + '"></div>' +
      '<button id="sec-close" type="button" style="width:100%;padding:10px;margin-top:8px;border:1px solid ' +
      V("--border", "#2a3a5a") + ';border-radius:8px;cursor:pointer;font-size:14px;background:' +
      V("--surface-3", "#142036") + ';color:' + V("--text-muted", "#7890b8") + '">Close</button>' +
      '</div>';
    document.body.appendChild(ov);
    var close = function () { var o = document.getElementById("auth-sec-overlay"); if (o) o.remove(); };
    document.getElementById("sec-close").onclick = close;
    ov.addEventListener("mousedown", function (e) { if (e.target === ov) close(); });
    ov.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
    secLoad(username);
  }

  function secError(m) { var e = document.getElementById("sec-error"); if (e) e.textContent = m || ""; }

  async function secLoad(username) {
    var body = document.getElementById("sec-body");
    if (!body) return;
    var st = null;
    try {
      var r = await fetch("/api/auth/mfa/status", { credentials: "same-origin" });
      if (r.ok) st = await r.json();
    } catch (e) {}
    if (!st) { body.innerHTML = "<i>Could not load security settings.</i>"; return; }
    var sec = 'border-top:1px solid ' + V("--border", "#2a3a5a") + ';margin-top:12px;padding-top:10px';
    var btn = 'padding:6px 12px;font-size:12px;border-radius:6px;cursor:pointer;border:1px solid ' +
      V("--border", "#2a3a5a") + ';background:' + V("--surface-3", "#142036") + ';';
    var h = "";
    // 1) authenticator app (TOTP)
    h += '<div style="font-weight:600;margin-bottom:4px">Authenticator app</div>';
    if (st.totp_enabled) {
      h += '<div style="display:flex;justify-content:space-between;align-items:center">' +
        '<span>Enabled ✓</span><button type="button" id="sec-totp-off" style="' + btn +
        'color:' + V("--error", "#ff6b6b") + '">Disable</button></div>';
    } else {
      h += '<div style="display:flex;justify-content:space-between;align-items:center">' +
        '<span style="color:' + V("--text-muted", "#7890b8") + '">Six-digit codes from any ' +
        'authenticator app. Works fully offline.</span>' +
        '<button type="button" id="sec-totp-on" style="' + btn + 'color:' +
        V("--text", "#e2e8f8") + ';white-space:nowrap;margin-left:8px">Set up</button></div>';
    }
    h += '<div id="sec-totp-enroll"></div>';
    // 2) security keys (FIDO2)
    h += '<div style="' + sec + '"><div style="font-weight:600;margin-bottom:4px">Security keys</div>';
    if (!st.fido2_available) {
      h += '<div style="color:' + V("--text-muted", "#7890b8") + '">python-fido2 is not installed ' +
        'on the backend, so hardware keys are unavailable. <code>pip install fido2</code> and restart.</div>';
    } else {
      if (st.fido2_keys && st.fido2_keys.length) {
        h += st.fido2_keys.map(function (k) {
          return '<div style="display:flex;justify-content:space-between;align-items:center;padding:3px 0">' +
            '<span>🔐 ' + String(k.label || "Security key").replace(/[<>&]/g, "") + '</span>' +
            '<button type="button" class="sec-key-rm" data-id="' + encodeURIComponent(k.id) + '" style="' +
            btn + 'color:' + V("--error", "#ff6b6b") + '">Remove</button></div>';
        }).join("");
      } else {
        h += '<div style="color:' + V("--text-muted", "#7890b8") + '">No keys enrolled yet.</div>';
      }
      h += '<div style="display:flex;gap:6px;margin-top:8px">' +
        '<input id="sec-key-label" placeholder="Key name (e.g. YubiKey 5)" autocomplete="off" style="' +
        inputStyle() + ';margin:0;flex:1">' +
        '<button type="button" id="sec-key-add" style="' + btn + 'color:' + V("--text", "#e2e8f8") +
        ';white-space:nowrap">Add key</button></div>' +
        '<input id="sec-key-pin" type="password" placeholder="Key PIN (only if your key has one)" ' +
        'autocomplete="off" style="' + inputStyle() + '">' +
        '<div id="sec-key-status" aria-live="polite" style="font-size:12px;min-height:14px;color:' +
        V("--text-muted", "#7890b8") + '"></div>';
    }
    h += '</div>';
    // 3) recovery codes
    h += '<div style="' + sec + '"><div style="font-weight:600;margin-bottom:4px">Recovery codes</div>' +
      '<div style="display:flex;justify-content:space-between;align-items:center">' +
      '<span style="color:' + V("--text-muted", "#7890b8") + '">' +
      (st.recovery_remaining > 0
        ? st.recovery_remaining + " single-use codes remaining."
        : "None yet — created with your first MFA method.") + '</span>' +
      ((st.totp_enabled || (st.fido2_keys && st.fido2_keys.length))
        ? '<button type="button" id="sec-rc-regen" style="' + btn + 'color:' +
          V("--text", "#e2e8f8") + ';white-space:nowrap;margin-left:8px">Regenerate</button>'
        : "") +
      '</div></div>';
    body.innerHTML = h;
    secError("");
    // handlers
    var post = async function (url, payload) {
      var r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
        credentials: "same-origin", body: JSON.stringify(payload || {}) });
      var j = {}; try { j = await r.json(); } catch (e) {}
      if (!r.ok) throw new Error(j.detail || j.error || "Request failed.");
      return j;
    };
    var on = function (id, fn) { var el = document.getElementById(id); if (el) el.onclick = fn; };
    on("sec-totp-on", async function () {
      try {
        var j = await post("/api/auth/mfa/totp/begin");
        var slot = document.getElementById("sec-totp-enroll");
        if (!slot) return;
        var secretPretty = (j.secret || "").replace(/(.{4})/g, "$1 ").trim();
        slot.innerHTML =
          '<div style="margin-top:8px;padding:10px;border-radius:8px;background:' +
          V("--surface-3", "#142036") + '">' +
          '<div style="font-size:12px;color:' + V("--text-muted", "#7890b8") + '">1. In your ' +
          'authenticator app choose "enter a setup key" and type:</div>' +
          '<code style="display:block;margin:6px 0;font-size:14px;letter-spacing:0.06em;user-select:all;color:' +
          V("--text", "#e2e8f8") + '">' + secretPretty + '</code>' +
          '<div style="font-size:11px;color:' + V("--text-muted", "#7890b8") + ';word-break:break-all;' +
          'user-select:all">' + (j.otpauth || "").replace(/[<>&]/g, "") + '</div>' +
          '<div style="font-size:12px;margin-top:8px;color:' + V("--text-muted", "#7890b8") +
          '">2. Enter the 6-digit code the app shows:</div>' +
          '<div style="display:flex;gap:6px;margin-top:4px">' +
          '<input id="sec-totp-code" inputmode="numeric" autocomplete="one-time-code" placeholder="123456" ' +
          'style="' + inputStyle() + ';margin:0;flex:1;text-align:center;letter-spacing:0.15em">' +
          '<button type="button" id="sec-totp-confirm" style="' + btn + 'color:' + V("--text", "#e2e8f8") +
          '">Confirm</button></div></div>';
        on("sec-totp-confirm", async function () {
          try {
            var code = (document.getElementById("sec-totp-code") || {}).value || "";
            var res = await post("/api/auth/mfa/totp/confirm", { code: code });
            if (res.recovery_codes) showRecoveryCodes(res.recovery_codes, "Two-step verification is on. ");
            secLoad(username);
          } catch (e) { secError(e.message); }
        });
        var ci = document.getElementById("sec-totp-code");
        if (ci) ci.focus();
      } catch (e) { secError(e.message); }
    });
    on("sec-totp-off", async function () {
      var pw = await askPassword("Disabling the authenticator requires your password.");
      if (pw === null) return;
      try { await post("/api/auth/mfa/totp/disable", { password: pw }); secLoad(username); }
      catch (e) { secError(e.message); }
    });
    on("sec-key-add", async function () {
      var label = (document.getElementById("sec-key-label") || {}).value || "";
      var pin = (document.getElementById("sec-key-pin") || {}).value || "";
      var stat = document.getElementById("sec-key-status");
      if (stat) {
        stat.textContent = "Plug in your key and touch it when it blinks…";
        stat.style.color = V("--text-muted", "#7890b8");  // reset after a failed try
      }
      secError("");
      try {
        var j = await post("/api/auth/fido2/register", { label: label, pin: pin || null });
        if (stat) stat.textContent = "";
        if (j.recovery_codes) showRecoveryCodes(j.recovery_codes, "Security key enrolled. ");
        secLoad(username);
      } catch (e) {
        // Surface the failure right where the eyes are (the status line),
        // not only in the bottom error slot -- a vanishing "touch your key"
        // with no visible reason reads as a UI glitch.
        if (stat) {
          stat.textContent = "Could not add the key: " + (e.message || "unknown error");
          stat.style.color = V("--error", "#ff6b6b");
        }
        secError(e.message);
      }
    });
    document.querySelectorAll(".sec-key-rm").forEach(function (b) {
      b.onclick = async function () {
        var pw = await askPassword("Removing a security key requires your password.");
        if (pw === null) return;
        try {
          await post("/api/auth/fido2/remove",
            { id: decodeURIComponent(b.getAttribute("data-id")), password: pw });
          secLoad(username);
        } catch (e) { secError(e.message); }
      };
    });
    on("sec-rc-regen", async function () {
      var pw = await askPassword("Regenerating recovery codes requires your password. " +
        "Old codes stop working immediately.");
      if (pw === null) return;
      try {
        var j = await post("/api/auth/mfa/recovery/regenerate", { password: pw });
        if (j.recovery_codes) showRecoveryCodes(j.recovery_codes, "Fresh codes — the old ones are dead. ");
        secLoad(username);
      } catch (e) { secError(e.message); }
    });
  }

  /* --- owner: reset a profile's MFA (lost key / lost phone) ---------------- */
  async function uaMfaReset(username) {
    if (!(await window.oracleConfirm(
      'Reset two-step verification for "' + username + '"?\n\nAll their authenticator ' +
      'enrollments, security keys and recovery codes are removed; their password still ' +
      'works and they can re-enroll after signing in.',
      { title: "Reset MFA", okLabel: "Reset" }))) return;
    try {
      var r = await fetch("/api/auth/users/mfa-reset", {
        method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
        body: JSON.stringify({ username: username }),
      });
      var j = {}; try { j = await r.json(); } catch (e) {}
      if (r.ok && j.ok) { uaLoad(); } else { uaSetError(j.detail || j.error || "Could not reset MFA."); }
    } catch (e) { uaSetError("Could not reach the server."); }
  }

  window.OracleAuth = {
    checkAuth: checkAuth,
    logout: logout,
    showAuthOverlay: showAuthOverlay,
    showChangePassword: showChangePassword,
    showUserAdmin: showUserAdmin,
    showSecurity: showSecurity,
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", checkAuth);
  } else {
    checkAuth();
  }
})();
