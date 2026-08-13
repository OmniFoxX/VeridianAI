/* data-folder.js -- give the user a way to reach their own files.
 *
 * WHY
 * On a Microsoft Store install the application lives in
 * C:\Program Files\WindowsApps, which Windows makes unbrowsable -- and taking
 * ownership of it to look inside breaks the AppX subsystem badly enough to
 * cost a Windows reinstall.
 *
 * The user's data was never in there. Archives, uploads, downloads and
 * snapshots are written to sage_data, which IS reachable. But under MSIX that
 * resolves to a redirected LocalCache path that nobody would ever guess, and
 * the app never told anyone where it was.
 *
 * So the data was always accessible and there was simply no door. This is the
 * door.
 *
 * TWO PATHS, ON PURPOSE
 *   Desktop app  -> IPC to main, which calls shell.openPath. The channel
 *                   carries NO payload; main resolves the folder itself, so
 *                   this cannot become "open any folder on the machine".
 *   Browser/PWA  -> no shell access, so show the resolved path as selectable
 *                   text. Being told exactly where your files are is a much
 *                   smaller thing than opening them for you, but it is not
 *                   nothing, and it beats a button that silently does nothing.
 *
 * Pure-ASCII source, matching the other frontend modules.
 */
(function () {
  "use strict";

  var pathEl, btnEl;

  function inElectron() {
    try {
      return !!(window.electronAPI && typeof window.electronAPI.send === "function");
    } catch (e) {
      return false;
    }
  }

  /* Ask the BACKEND where the data actually is.
   *
   * /api/health reports the resolved data_dir -- resolved by the process that
   * does the writing, which is the only answer that counts. Electron computes
   * a path but MSIX redirects the writes elsewhere, so the renderer must not
   * guess and must not trust the computed one. */
  async function resolvedPath() {
    try {
      var r = await fetch("/api/health");
      if (!r.ok) return "";
      var d = await r.json();
      return (d && d.data_dir) || "";
    } catch (e) {
      return "";
    }
  }

  async function refresh() {
    pathEl = document.getElementById("data-folder-path");
    btnEl = document.getElementById("open-data-folder-btn");
    if (!pathEl || !btnEl) return;

    var p = await resolvedPath();

    if (inElectron()) {
      // Keep the sentence short; the path is long and belongs in the tooltip
      // rather than wrapped across three lines of a settings panel.
      pathEl.textContent =
        "Archives, uploads, downloads, snapshots and the documentation are " +
        "stored on this computer.";
      if (p) btnEl.setAttribute("data-tip", p);
      return;
    }

    // Browser / PWA: no shell, so hand over the path itself.
    btnEl.textContent = p ? "Copy Path" : "Unavailable";
    btnEl.disabled = !p;
    if (p) {
      pathEl.textContent = p;
      // user-select:all so one click selects the whole path -- a path you
      // cannot easily select is a path you will retype and get wrong.
      pathEl.style.userSelect = "all";
      pathEl.setAttribute("title", p);
    } else {
      pathEl.textContent =
        "Your data folder location is unavailable until the backend is running.";
    }
  }

  window.openDataFolder = async function () {
    var p = await resolvedPath();

    if (inElectron()) {
      try {
        // No argument. main.js resolves the directory itself.
        window.electronAPI.send("open-data-folder");
        if (window.setStatus) window.setStatus("Opening your data folder...");
      } catch (e) {
        if (window.setStatusError) window.setStatusError("Could not open the folder: " + e.message);
      }
      return;
    }

    if (!p) {
      if (window.setStatusError) window.setStatusError("Data folder location unavailable.");
      return;
    }
    try {
      if (navigator.clipboard) {
        await navigator.clipboard.writeText(p);
        if (window.setStatus) window.setStatus("Path copied: " + p);
      } else if (pathEl) {
        // No clipboard API (insecure context): select it so Ctrl+C works.
        var r = document.createRange();
        r.selectNodeContents(pathEl);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(r);
        if (window.setStatus) window.setStatus("Path selected - press Ctrl+C to copy");
      }
    } catch (e) {
      if (window.setStatusError) window.setStatusError("Could not copy the path: " + e.message);
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", refresh);
  } else {
    refresh();
  }
})();
