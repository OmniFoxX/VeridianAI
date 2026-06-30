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
      return (v && v.trim()) || fb;
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
    ov.setAttribute("aria-label", needsSetup ? "OracleAI account setup" : "OracleAI sign in");
    ov.style.cssText = "position:fixed;inset:0;z-index:99999;display:flex;" +
      "align-items:center;justify-content:center;background:" + V("--bg", "#060a14");
    ov.innerHTML =
      '<div style="width:min(92vw,380px);padding:32px 28px;border-radius:16px;' +
      'background:' + V("--surface", "#0a1020") + ';border:1px solid ' + V("--border", "#2a3a5a") +
      ';box-shadow:0 20px 60px rgba(0,0,0,0.5)">' +
      '<div style="font-family:' + V("--font-display", "serif") + ';font-size:22px;' +
      'letter-spacing:0.04em;text-align:center">' +
      '<span style="color:' + V("--text", "#e2e8f8") + '">Oracle</span>' +
      '<span style="color:' + V("--gold", "#f0a500") + '">AI</span></div>' +
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
      'border-radius:8px;cursor:pointer;font-size:15px;font-weight:600;background:' +
      V("--gold", "#f0a500") + ';color:#1a1206">' + action + '</button>' +
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
    // the gap between the OracleAI emblem and the model selector).
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
    } else {
      showAuthOverlay(!!st.needs_setup);
    }
    return st;
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
        var del = u.is_owner
          ? '<span style="opacity:0.4;font-size:11px">protected</span>'
          : '<button type="button" class="ua-del" data-u="' + encodeURIComponent(u.username) +
            '" style="padding:3px 8px;font-size:11px;border-radius:6px;cursor:pointer;background:' +
            V("--surface-3", "#142036") + ';color:' + V("--error", "#ff6b6b") + ';border:1px solid ' +
            V("--border", "#2a3a5a") + '">Delete</button>';
        return '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0">' +
          "<span>" + name + tag + "</span>" + del + "</div>";
      }).join("");
      box.querySelectorAll(".ua-del").forEach(function (b) {
        b.onclick = function () { uaDelete(decodeURIComponent(b.getAttribute("data-u"))); };
      });
    } catch (e) { box.innerHTML = "<i>Could not load users.</i>"; }
  }

  async function uaCreate() {
    var u = ((document.getElementById("ua-username") || {}).value || "").trim();
    var p = (document.getElementById("ua-password") || {}).value || "";
    if (!u || !p) { uaSetError("Username and password are required."); return; }
    uaSetError("");
    try {
      var r = await fetch("/api/auth/users", {
        method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
        body: JSON.stringify({ username: u, password: p }),
      });
      var j = {}; try { j = await r.json(); } catch (e) {}
      if (r.ok && j.ok) {
        document.getElementById("ua-username").value = "";
        document.getElementById("ua-password").value = "";
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
