/* boot-status.js -- say what the app is doing during the cold start.
 *
 * WHY
 * A first launch takes roughly 40 seconds while the local inference servers
 * load their models. During that time the window is up, the UI is drawn, and
 * nothing works: the model dropdown is empty and a message gets no reply.
 * There is no way to tell "loading" from "broken", so a new user -- or a Store
 * reviewer with five minutes -- reasonably concludes the second.
 *
 * The information already exists. The backend knows exactly which tiers are up
 * and when a model becomes available; it simply never told anyone. This is the
 * same principle as the tier logs and the [extras] python line: the app should
 * say what it is doing rather than leave you to infer it from silence.
 *
 * WHAT IT REPORTS
 * Process-running is NOT readiness. llama-server accepts a connection the
 * instant it spawns and cannot answer until the model finishes loading, so
 * "running" would claim ready ~40 seconds early -- worse than saying nothing,
 * because the user would then think a working app was broken.
 *
 * Readiness here means A MODEL CAN ANSWER: /api/models returns entries. That
 * is the same condition the first-run model bootstrap waits for, so this
 * polling also drives that bootstrap on a cold start.
 *
 * Removes itself when ready. A progress indicator that lingers after the thing
 * it tracked has finished is just clutter.
 *
 * Pure-ASCII source on purpose, matching skills.js and optional-engines.js.
 */
(function () {
  "use strict";

  var ID = "boot-status";
  var POLL_MS = 2500;
  var GIVE_UP_MS = 240000;      // 4 min: a very slow disk with a large model
  var started = Date.now();
  var timer = null;

  function el() { return document.getElementById(ID); }

  function ensure() {
    var b = el();
    if (b) return b;
    // Reuses .voice-extras styling, which is already contrast-checked against
    // both themes -- a new bespoke style would need re-verifying for nothing.
    b = document.createElement("div");
    b.id = ID;
    b.className = "voice-extras";
    b.setAttribute("role", "status");
    b.setAttribute("aria-live", "polite");
    var note = document.createElement("span");
    note.className = "voice-extras-note";
    note.id = ID + "-note";
    b.appendChild(note);

    var host = document.querySelector(".input-container");
    if (host) host.insertBefore(b, host.firstChild);
    else document.body.insertBefore(b, document.body.firstChild);
    return b;
  }

  function say(msg) {
    var b = ensure();
    var n = document.getElementById(ID + "-note");
    if (n) n.textContent = msg;
    return b;
  }

  function done(msg) {
    if (timer) { clearInterval(timer); timer = null; }
    if (!msg) { remove(); return; }
    say(msg);
    // Long enough to read, short enough not to become furniture.
    setTimeout(remove, 6000);
  }

  function remove() {
    var b = el();
    if (b && b.parentNode) b.parentNode.removeChild(b);
  }

  async function poll() {
    var elapsed = Date.now() - started;
    try {
      var r = await fetch("/api/models");
      if (r.ok) {
        var d = await r.json();
        var models = (d && d.models) || [];
        if (models.length) {
          var names = models.map(function (m) { return m.tier || m.backend || ""; });
          var uniq = names.filter(function (v, i) { return v && names.indexOf(v) === i; });
          done("Ready" + (uniq.length ? " - " + uniq.join(", ") + " online" : "") + ".");
          return;
        }
      }
    } catch (e) { /* backend still coming up; keep waiting */ }

    // Secondary detail: how many engines have at least spawned. Framed as
    // "starting", never "ready" -- see the note above about process-running.
    var detail = "";
    try {
      var tr = await fetch("/api/tiers");
      if (tr.ok) {
        var td = await tr.json();
        var tiers = (td && td.tiers) || {};
        var keys = Object.keys(tiers);
        var up = keys.filter(function (k) { return tiers[k] && tiers[k].running; });
        if (keys.length) detail = " (" + up.length + " of " + keys.length + " engines started)";
      }
    } catch (e) { /* optional */ }

    if (elapsed > GIVE_UP_MS) {
      done("Local engines are taking longer than expected. The app still " +
           "works - see Settings for model status.");
      return;
    }
    say("Starting local inference" + detail +
        " - first launch takes about a minute while models load.");
  }

  function init() {
    // Give the backend a moment; if everything is already warm (a restart
    // rather than a cold start) the first poll finds models and this banner
    // never appears at all.
    setTimeout(function () {
      poll();
      timer = setInterval(poll, POLL_MS);
    }, 1200);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
