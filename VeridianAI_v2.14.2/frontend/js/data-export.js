/* data-export.js -- take your data out.
 *
 * Two modes, because they are different jobs:
 *
 *   Readable  decrypted, plain files, for reading/printing/keeping.
 *             Loses tamper-evidence, deliberately and on request.
 *   Portable  still encrypted, plus the key, for moving machines with the
 *             memory chain still verifiable.
 *
 * The checklist shows SIZES BEFORE the choice. Uploads and snapshots can run
 * to gigabytes; "export everything" should be a decision, not a surprise that
 * arrives as a progress bar which never moves.
 *
 * Pure-ASCII source, matching the other frontend modules.
 */
(function () {
  "use strict";

  var inv = null;

  function human(n) {
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(0) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  }

  function selected() {
    var out = [];
    document.querySelectorAll(".export-sec:checked").forEach(function (c) {
      out.push(c.value);
    });
    return out;
  }

  function selectedBytes() {
    if (!inv) return 0;
    var keys = selected(), total = 0;
    inv.sections.forEach(function (s) {
      if (keys.indexOf(s.key) >= 0) total += s.bytes;
    });
    return total;
  }

  function refreshTotal() {
    var el = document.getElementById("export-total");
    if (el) {
      var n = selected().length;
      el.textContent = n
        ? n + " selected - about " + human(selectedBytes())
        : "Nothing selected.";
    }
  }

  window.exportRefresh = async function () {
    var box = document.getElementById("export-sections");
    if (!box) return;
    box.innerHTML = "<i>Checking...</i>";
    try {
      inv = await (await fetch("/api/export/inventory")).json();
    } catch (e) {
      box.textContent = "Could not read your data folder.";
      return;
    }
    var present = (inv.sections || []).filter(function (s) { return s.present; });
    if (!present.length) {
      box.textContent = "Nothing stored yet.";
      return;
    }
    box.innerHTML = "";
    present.forEach(function (s) {
      var row = document.createElement("label");
      row.className = "hw-toggle";
      row.style.cssText = "display:flex;gap:8px;align-items:center;margin:3px 0";
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "export-sec";
      cb.value = s.key;
      // Conversations and memory on by default; bulk media off. The common
      // case is "my writing", not "every file I ever attached".
      cb.checked = ["chat", "archives", "memory_chain", "procedural", "evidence"]
        .indexOf(s.key) >= 0;
      cb.addEventListener("change", refreshTotal);
      var txt = document.createElement("span");
      txt.className = "hw-toggle-label";
      txt.textContent = s.label + "  (" + s.files + " file" +
        (s.files === 1 ? "" : "s") + ", " + human(s.bytes) + ")";
      row.appendChild(cb);
      row.appendChild(txt);
      box.appendChild(row);
    });

    var pb = document.getElementById("export-portable-btn");
    if (pb && inv.can_portable === false) {
      // The Fernet key is app-wide, so a non-owner taking it would be taking
      // the key to everyone's data. Say why, rather than just greying it out.
      pb.disabled = true;
      pb.setAttribute("data-tip",
        "Portable export includes the shared encryption key, so it is " +
        "available to the profile owner only. A readable export of your own " +
        "data is unaffected.");
    }
    refreshTotal();
  };

  async function run(mode, confirmText) {
    var secs = selected();
    if (!secs.length) {
      if (window.setStatusError) window.setStatusError("Select at least one item to export.");
      return;
    }
    var ok = await oracleConfirm(confirmText, {
      title: mode === "portable" ? "Portable export" : "Readable export",
      okLabel: "Export",
    });
    if (!ok) return;

    var btns = document.querySelectorAll("#export-readable-btn,#export-portable-btn");
    btns.forEach(function (b) { b.disabled = true; });
    if (window.setStatus) window.setStatus("Building export...");
    try {
      var r = await fetch("/api/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: mode, sections: secs }),
      });
      var d = await r.json();
      if (!d.ok) {
        if (window.setStatusError) window.setStatusError("Export failed: " + (d.error || "unknown"));
        return;
      }
      var note = document.getElementById("export-result");
      if (note) {
        note.textContent = d.filename + " (" + human(d.bytes) + ", " +
          d.files + " files) - saved in your data folder under downloads.";
      }
      if (window.setStatus) window.setStatus("Export ready: " + d.filename);
      // Hand it straight over, rather than making them go hunting.
      try { window.open("/api/downloads/" + encodeURIComponent(d.filename) + "?dl=1", "_blank"); }
      catch (e) { /* the file is on disk either way */ }
    } catch (e) {
      if (window.setStatusError) window.setStatusError("Export failed: " + e.message);
    } finally {
      btns.forEach(function (b) { b.disabled = false; });
      var pb = document.getElementById("export-portable-btn");
      if (pb && inv && inv.can_portable === false) pb.disabled = true;
    }
  }

  window.exportReadable = function () {
    run("readable",
      "Create a readable export?\n\n" +
      "- Your data will be DECRYPTED into plain files.\n" +
      "- Anyone who opens the zip can read it.\n" +
      "- The memory chain copy will no longer be tamper-evident.\n\n" +
      "Good for reading, printing and keeping. To move machines with " +
      "integrity intact, use a portable export instead.");
  };

  window.exportPortable = function () {
    run("portable",
      "Create a portable export?\n\n" +
      "- Contents stay ENCRYPTED, exactly as stored.\n" +
      "- The encryption KEY is included so another install can read them.\n" +
      "- Treat the file like a password - key plus data opens everything.\n\n" +
      "This is the one to use when moving to another machine.");
  };

  /* The panel is a modal opened from the chat toolbar, beside Print.
   *
   * It deliberately does NOT sit next to Burn in Settings, where it started.
   * "Send my data out" and "destroy my data forever" are opposite intentions,
   * and putting opposite intentions next to each other is how a mis-tap
   * becomes irreversible. Loud warnings help; distance helps more. Export now
   * lives with Archive / Load / Print -- the other things you do WITH your
   * conversation rather than TO it.
   */
  function buildModal() {
    var back = document.createElement("div");
    back.id = "export-modal-backdrop";
    back.style.cssText =
      "position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9998;" +
      "display:flex;align-items:center;justify-content:center;padding:20px";

    var box = document.createElement("div");
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", "Export your data");
    box.style.cssText =
      "background:var(--surface);border:1px solid var(--border-hi);" +
      "border-radius:var(--radius);padding:18px 20px;max-width:560px;" +
      "width:100%;max-height:80vh;overflow:auto;font-family:var(--font-body);" +
      "color:var(--text)";

    box.innerHTML =
      '<div style="font-weight:600;margin-bottom:8px">Export your data</div>' +
      '<div id="export-sections" style="margin:6px 0"></div>' +
      '<div id="export-total" class="voice-extras-note" role="status" aria-live="polite"></div>' +
      '<div class="voice-extras" style="margin-top:10px">' +
      '  <button type="button" class="voice-extras-btn" id="export-readable-btn"' +
      '    data-tip="Decrypted plain files - for reading, printing and keeping">Readable Export</button>' +
      '  <button type="button" class="voice-extras-btn" id="export-portable-btn"' +
      '    data-tip="Encrypted, with the key - for moving to another machine">Portable Export</button>' +
      '  <button type="button" class="voice-extras-btn" id="export-close-btn">Close</button>' +
      "</div>" +
      '<div id="export-result" class="voice-extras-note" style="margin-top:8px"></div>';

    back.appendChild(box);
    document.body.appendChild(back);

    document.getElementById("export-readable-btn")
      .addEventListener("click", window.exportReadable);
    document.getElementById("export-portable-btn")
      .addEventListener("click", window.exportPortable);
    document.getElementById("export-close-btn")
      .addEventListener("click", closeModal);
    back.addEventListener("click", function (e) { if (e.target === back) closeModal(); });
    // Focus in, Tab contained, focus returned on close. Without this a
    // keyboard user opens the dialog and focus is still on the toolbar
    // button behind it.
    if (window.modalA11y) {
      _release = window.modalA11y(box, { onEscape: closeModal });
    }
    return back;
  }

  var _release = null;

  function closeModal() {
    var b = document.getElementById("export-modal-backdrop");
    if (b && b.parentNode) b.parentNode.removeChild(b);
    if (_release) { _release(); _release = null; }
  }

  window.openExportPanel = function () {
    if (document.getElementById("export-modal-backdrop")) return;
    buildModal();
    // Inventory is fetched on OPEN, not at page load: it stats every file in
    // the data folder, which is wasted work for the many sessions that never
    // export anything.
    window.exportRefresh();
  };
})();
