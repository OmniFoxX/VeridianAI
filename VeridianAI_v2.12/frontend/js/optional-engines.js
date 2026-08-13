/* optional-engines.js -- tell the user about engines we deliberately do NOT ship.
 *
 * WHY THIS EXISTS
 * The Oracle tier runs on Ollama, a separate third-party install. A Store
 * package may not fetch one at runtime, so first_run.js returns immediately for
 * Store builds -- correctly, since everything else it does is a download. The
 * side effect was that a Store user had no Oracle tier, was never told why, and
 * had no idea it was even an option. The app got quieter instead of louder as
 * it lost a capability, which is the exact failure mode this codebase keeps
 * paying for elsewhere.
 *
 * Driven by OBSERVED tier reachability, not by build type: if no model reports
 * tier "Oracle", the engine is not there, whatever kind of install this is. One
 * code path for portable and Store rather than two that drift apart.
 *
 * Offers a LINK and nothing else. Opening a browser is not an install, so this
 * stays inside Store policy. window.open goes through main.js's
 * setWindowOpenHandler -> shell.openExternal, so it lands in the real browser
 * rather than a chromeless Electron window.
 *
 * NO NATIVE DIALOG, deliberately. Native confirm/alert cost a multi-day
 * unclickable-UI bug (the renderer loses pointer focus and never gets it back);
 * all 13 call sites were migrated to in-app modals for that reason. A banner
 * that interrupts nothing is also the right weight for "here is an optional
 * extra" -- this is not a question that needs answering before work continues.
 *
 * Pure-ASCII source on purpose, matching skills.js.
 */
(function () {
  "use strict";

  var DISMISS_URL = "/api/notices/ollama";
  var BANNER_ID = "optional-engine-banner";

  // Tier lists populate asynchronously after the backend probes each port, so a
  // single early check would show the banner to someone who does have Ollama.
  // Re-check a few times before concluding it is absent; err toward silence.
  var CHECKS_MS = [6000, 20000, 45000];

  function styles() {
    if (document.getElementById("optional-engine-styles")) return;
    var css = [
      "#" + BANNER_ID + "{",
      "  display:flex;align-items:flex-start;gap:12px;",
      "  margin:0 0 10px 0;padding:10px 12px;",
      "  background:var(--surface-2);border:1px solid var(--border);",
      "  border-left:3px solid var(--teal);border-radius:var(--radius-sm);",
      "  font-family:var(--font-body);color:var(--text);font-size:0.92rem;",
      "  line-height:1.45;",
      "}",
      "#" + BANNER_ID + " .oeb-body{flex:1 1 auto;min-width:0}",
      "#" + BANNER_ID + " .oeb-title{font-weight:600;margin-bottom:2px}",
      /* Safe on both themes: --text-muted over --surface-2 is 5.71:1 dark /
         5.34:1 parchment. It was 4.40:1 on parchment until the light theme's
         --text-muted was corrected -- it had been tuned against --bg and
         --surface only, and the darker surfaces were never in the matrix. */
      "#" + BANNER_ID + " .oeb-note{color:var(--text-muted)}",
      "#" + BANNER_ID + " .oeb-actions{",
      "  display:flex;gap:8px;flex:0 0 auto;align-items:center;",
      "}",
      /* 24px minimum target: WCAG 2.2 AA (2.5.8 Target Size Minimum). */
      "#" + BANNER_ID + " .oeb-btn{",
      "  min-height:24px;min-width:24px;padding:5px 12px;cursor:pointer;",
      "  border-radius:var(--radius-sm);border:1px solid var(--border-hi);",
      "  background:var(--surface-3);color:var(--text);",
      "  font-family:var(--font-body);font-size:0.88rem;",
      "  text-decoration:none;display:inline-flex;align-items:center;",
      "  transition:background var(--t);",
      "}",
      "#" + BANNER_ID + " .oeb-btn:hover{background:var(--surface)}",
      "#" + BANNER_ID + " .oeb-btn:focus-visible{",
      "  outline:2px solid var(--teal);outline-offset:2px",
      "}",
      "#" + BANNER_ID + " .oeb-btn.primary{",
      "  border-color:var(--teal);color:var(--teal)",
      "}"
    ].join("\n");
    var el = document.createElement("style");
    el.id = "optional-engine-styles";
    el.textContent = css;
    document.head.appendChild(el);
  }

  function dismiss() {
    var b = document.getElementById(BANNER_ID);
    if (b && b.parentNode) b.parentNode.removeChild(b);
    try {
      fetch(DISMISS_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dismissed: true })
      });
    } catch (e) { /* a failed dismiss is a banner next launch, not an error */ }
  }

  function render(url) {
    if (document.getElementById(BANNER_ID)) return;
    styles();

    var bar = document.createElement("div");
    bar.id = BANNER_ID;
    // role=status + polite: announced by a screen reader when it appears,
    // without stealing focus from whatever the user was doing.
    bar.setAttribute("role", "status");
    bar.setAttribute("aria-live", "polite");

    var body = document.createElement("div");
    body.className = "oeb-body";

    var title = document.createElement("div");
    title.className = "oeb-title";
    title.textContent = "Optional: add the Oracle tier";

    var note = document.createElement("div");
    note.className = "oeb-note";
    note.textContent =
      "VeridianAI is running on its built-in engines. The Oracle tier runs on " +
      "Ollama, a separate free install that stays entirely on your machine. " +
      "Install it and restart to gain that tier -- everything already works " +
      "without it.";

    body.appendChild(title);
    body.appendChild(note);

    var actions = document.createElement("div");
    actions.className = "oeb-actions";

    var get = document.createElement("a");
    get.className = "oeb-btn primary";
    get.href = url || "https://ollama.com/download";
    get.target = "_blank";
    get.rel = "noopener noreferrer";     // never hand the opener to a new tab
    get.textContent = "Get Ollama";
    get.setAttribute("data-tip", "Opens ollama.com in your browser");
    get.setAttribute("aria-label", "Get Ollama - opens ollama.com in your browser");

    var no = document.createElement("button");
    no.type = "button";
    no.className = "oeb-btn";
    no.textContent = "No thanks";
    no.setAttribute("data-tip", "Hide this permanently");
    no.setAttribute("aria-label", "Dismiss the Ollama notice permanently");
    no.addEventListener("click", dismiss);

    actions.appendChild(get);
    actions.appendChild(no);
    bar.appendChild(body);
    bar.appendChild(actions);

    // Mount directly above the composer, inside .input-container.
    //
    // It used to go after header.app-header, which LOOKED right and was not:
    // the header is sticky, so the banner rendered underneath it and only the
    // bottom sliver of the text peeked out -- legible enough to make you doubt
    // your eyesight rather than suspect the layout. The top of the UI is also
    // already dense; a one-off notice does not belong competing with it.
    //
    // Inside .input-container it sits in normal flow (nothing to stack under),
    // inherits the composer's width so it lines up with the input box, and
    // appears where the user is already looking.
    var host = document.querySelector(".input-container");
    if (host) {
      host.insertBefore(bar, host.firstChild);
      return;
    }
    var vibe = document.getElementById("vibe-bar");
    if (vibe && vibe.parentNode) {
      vibe.parentNode.insertBefore(bar, vibe.nextSibling);
      return;
    }
    var header = document.querySelector("header.app-header");
    if (header && header.parentNode) {
      header.parentNode.insertBefore(bar, header.nextSibling);
    } else {
      document.body.insertBefore(bar, document.body.firstChild);
    }
  }

  async function oraclePresent() {
    try {
      var r = await fetch("/api/models");
      if (!r.ok) return true;              // unknown -> stay quiet
      var d = await r.json();
      var models = (d && d.models) || [];
      return models.some(function (m) {
        return m && String(m.tier || "").toLowerCase() === "oracle";
      });
    } catch (e) {
      return true;                          // unknown -> stay quiet
    }
  }

  async function check(url, remaining) {
    if (await oraclePresent()) return;      // Ollama is here; nothing to offer
    if (remaining.length) {
      setTimeout(function () { check(url, remaining.slice(1)); }, remaining[0]);
      return;
    }
    render(url);
  }

  async function init() {
    var url = "https://ollama.com/download";
    try {
      var r = await fetch(DISMISS_URL);
      if (r.ok) {
        var d = await r.json();
        if (d && d.dismissed) return;       // user said no; never ask again
        if (d && d.url) url = d.url;
      }
    } catch (e) {
      return;   // cannot read the preference -> do not risk nagging forever
    }
    setTimeout(function () { check(url, CHECKS_MS.slice(1)); }, CHECKS_MS[0]);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
