/* ui-unstick.js -- clears the "UI goes unclickable after a dialog" bug.
 *
 * Symptom (reported during testing): after certain actions the whole UI stops
 * responding to clicks until you open the file picker ("Attach"), at which
 * point clicking works again. Triggers include Archive and other buttons that
 * pop a native confirm()/alert().
 *
 * Root cause: in Electron/Chromium, an in-page modal -- window.confirm/alert/
 * prompt -- blocks the renderer but does NOT blur the OS window, and on some
 * machines the renderer is left without pointer/input focus afterwards. An
 * OS-level dialog (the file picker, the print dialog) DOES blur+refocus the
 * window, which is why "Attach" frees it. We reproduce that recovery on demand.
 *
 * Two layers, both no-ops in a plain browser and safe if the Electron bridge
 * is absent:
 *   1. After every confirm/alert/prompt returns, reclaim focus.
 *   2. Whenever the window regains focus, reclaim it (covers OS dialogs too).
 *
 * The real reclaim happens in the Electron main process via webContents.focus()
 * -- we ask for it over the whitelisted 'oracle-unstick' IPC channel (see
 * preload.js + electron/main.js). window.focus()/blur() are the browser-only
 * fallback. Pure-ASCII, dependency-free.
 */
(function () {
  "use strict";

  var _busy = false;   // guards against any focus -> unstick -> focus re-entry

  function unstick() {
    if (_busy) return;
    _busy = true;
    setTimeout(function () { _busy = false; }, 250);
    // Ask Electron main to reclaim renderer input focus (the reliable path).
    try {
      if (window.electronAPI && typeof window.electronAPI.send === "function") {
        window.electronAPI.send("oracle-unstick");
      }
    } catch (e) {}
    // Browser-only fallback: nudge focus back to the document.
    try { window.focus(); } catch (e) {}
    try {
      var ae = document.activeElement;
      if (ae && ae !== document.body && typeof ae.blur === "function") ae.blur();
    } catch (e) {}
  }

  // 1) Wrap the in-page native modals so focus is reclaimed the instant they
  //    close. The return value is passed through untouched, so existing
  //    `if (confirm(...))` / `prompt(...)` call sites behave exactly as before.
  ["confirm", "alert", "prompt"].forEach(function (name) {
    var orig = window[name];
    if (typeof orig !== "function") return;
    window[name] = function () {
      var result = orig.apply(this, arguments);
      setTimeout(unstick, 0);   // after the modal has fully torn down
      return result;
    };
  });

  // 2) Belt and suspenders: any time the window regains focus (returning from
  //    a file picker, print dialog, alt-tab, etc.), reclaim input focus.
  window.addEventListener("focus", unstick);

  // Exposed so other code can call it directly if a future trigger turns up.
  window.unstickUI = unstick;
})();
