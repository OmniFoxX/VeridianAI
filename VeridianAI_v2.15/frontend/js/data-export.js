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
      // No longer "owner only" -- a profile has a key of its own now, and the
      // only reason this can fail is that the key is not unlocked in this
      // session. Say the actual reason: the old wording sent people looking
      // for a permission they already had.
      pb.disabled = true;
      pb.setAttribute("data-tip",
        "A portable export has to carry the key that opens it, and your " +
        "profile's key is not unlocked in this session. Sign in again, or " +
        "take a readable export of your own data.");
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
      var payload = { mode: mode, sections: secs };
      var useP = document.getElementById("export-usepass");
      var passEl = document.getElementById("export-pass");
      if (mode === "portable" && useP && useP.checked && passEl && passEl.value) {
        payload.passphrase = passEl.value;
      }
      var r = await fetch("/api/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      var d = await r.json();
      if (!d.ok) {
        if (window.setStatusError) window.setStatusError("Export failed: " + (d.error || "unknown"));
        return;
      }
      var where = deliver(d.filename);
      var note = document.getElementById("export-result");
      if (note) {
        note.textContent = d.filename + " (" + human(d.bytes) + ", " +
          d.files + " files) - " + where + "." +
          (d.protected ? "  Protected by your passphrase: without it this " +
                         "archive cannot be opened by anyone, including you." : "");
      }
      if (window.setStatus) window.setStatus("Export ready: " + d.filename);
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
    box.setAttribute("aria-label", "Your data: export and import");
    box.style.cssText =
      "background:var(--surface);border:1px solid var(--border-hi);" +
      "border-radius:var(--radius);padding:18px 20px;max-width:560px;" +
      "width:100%;max-height:80vh;overflow:auto;font-family:var(--font-body);" +
      "color:var(--text)";

    // TABS, not two dialogs. Taking your data out and putting it back are the
    // same subject, and each half needs room to explain itself -- the import
    // side especially, where the consequences are not reversible.
    box.innerHTML =
      '<div style="font-weight:600;margin-bottom:8px">Your data</div>' +
      '<div role="tablist" aria-label="Export or import" ' +
      '     style="display:flex;gap:6px;margin-bottom:10px">' +
      '  <button type="button" role="tab" id="tab-export" aria-controls="pane-export"' +
      '    aria-selected="true"  class="voice-extras-btn">Export</button>' +
      '  <button type="button" role="tab" id="tab-import" aria-controls="pane-import"' +
      '    aria-selected="false" class="voice-extras-btn">Import</button>' +
      "</div>" +

      '<div id="pane-export" role="tabpanel" aria-labelledby="tab-export">' +
      '  <div id="export-sections" style="margin:6px 0"></div>' +
      '  <div id="export-total" class="voice-extras-note" role="status" aria-live="polite"></div>' +
      '  <label class="hw-toggle" style="display:flex;gap:8px;align-items:center;margin:8px 0">' +
      '    <input type="checkbox" id="export-usepass">' +
      '    <span class="hw-toggle-label">Protect a portable export with a passphrase</span>' +
      '  </label>' +
      '  <div id="export-pass-wrap" style="display:none;margin:0 0 8px 0">' +
      '    <input type="password" id="export-pass" autocomplete="new-password"' +
      '      placeholder="Passphrase" style="width:100%;padding:6px">' +
      '    <div class="voice-extras-note">Without this passphrase the archive ' +
      '      cannot be opened -- by anyone, including you. There is no reset.</div>' +
      "  </div>" +
      '  <div class="voice-extras" style="margin-top:10px">' +
      '    <button type="button" class="voice-extras-btn" id="export-readable-btn"' +
      '      data-tip="Decrypted plain files - for reading, printing and keeping">Readable Export</button>' +
      '    <button type="button" class="voice-extras-btn" id="export-portable-btn"' +
      '      data-tip="Encrypted, with the key - for moving to another machine">Portable Export</button>' +
      "  </div>" +
      '  <div id="export-result" class="voice-extras-note" style="margin-top:8px"></div>' +
      "</div>" +

      '<div id="pane-import" role="tabpanel" aria-labelledby="tab-import" hidden>' +
      '  <div class="voice-extras-note" style="margin-bottom:8px">' +
      '    Choose an export archive. It is examined first and nothing is ' +
      '    written until you say so.' +
      "  </div>" +
      '  <input type="file" id="import-file" accept=".zip,application/zip"' +
      '    aria-label="Export archive to import" style="width:100%">' +
      '  <div class="voice-extras" style="margin-top:8px">' +
      '    <button type="button" class="voice-extras-btn" id="import-examine-btn">Examine</button>' +
      "  </div>" +
      '  <div id="import-report" class="voice-extras-note" role="status"' +
      '    aria-live="polite" style="margin-top:8px"></div>' +
      '  <div id="import-choices" hidden>' +
      '    <div id="import-sections" style="margin:6px 0"></div>' +
      '    <div id="import-pass-wrap" style="display:none;margin:6px 0">' +
      '      <input type="password" id="import-pass" autocomplete="off"' +
      '        placeholder="Passphrase for this archive" style="width:100%;padding:6px">' +
      "    </div>" +
      '    <label class="hw-toggle" style="display:flex;gap:8px;align-items:center;margin:6px 0">' +
      '      <input type="checkbox" id="import-replace">' +
      '      <span class="hw-toggle-label">Replace instead of merge (clears each ' +
      '        chosen section first)</span>' +
      "    </label>" +
      '    <label class="hw-toggle" style="display:flex;gap:8px;align-items:center;margin:6px 0">' +
      '      <input type="checkbox" id="import-other">' +
      '      <span class="hw-toggle-label">Import into another profile (owner only)</span>' +
      "    </label>" +
      '    <div id="import-target-wrap" style="display:none;margin:0 0 6px 0">' +
      '      <input type="text" id="import-target" autocomplete="off"' +
      '        placeholder="Profile name" style="width:100%;padding:6px">' +
      '      <div class="voice-extras-note">The profile must already exist and ' +
      '        have no data in it. Importing over someone else\'s history is ' +
      '        refused.</div>' +
      "    </div>" +
      '    <div class="voice-extras" style="margin-top:8px">' +
      '      <button type="button" class="voice-extras-btn" id="import-preview-btn"' +
      '        data-tip="Report what would happen, without writing anything">Preview</button>' +
      '      <button type="button" class="voice-extras-btn" id="import-run-btn">Import</button>' +
      "    </div>" +
      "  </div>" +
      '  <div id="import-result" class="voice-extras-note" style="margin-top:8px"></div>' +
      "</div>" +

      '<div class="voice-extras" style="margin-top:12px">' +
      '  <button type="button" class="voice-extras-btn" id="export-close-btn">Close</button>' +
      "</div>";

    back.appendChild(box);
    document.body.appendChild(back);

    document.getElementById("export-readable-btn")
      .addEventListener("click", window.exportReadable);
    document.getElementById("export-portable-btn")
      .addEventListener("click", window.exportPortable);
    document.getElementById("export-close-btn")
      .addEventListener("click", closeModal);
    document.getElementById("tab-export")
      .addEventListener("click", function () { showTab("export"); });
    document.getElementById("tab-import")
      .addEventListener("click", function () { showTab("import"); });
    document.getElementById("export-usepass")
      .addEventListener("change", function () {
        document.getElementById("export-pass-wrap").style.display =
          this.checked ? "" : "none";
      });
    document.getElementById("import-other")
      .addEventListener("change", function () {
        document.getElementById("import-target-wrap").style.display =
          this.checked ? "" : "none";
      });
    document.getElementById("import-examine-btn")
      .addEventListener("click", examineArchive);
    document.getElementById("import-preview-btn")
      .addEventListener("click", function () { runImport(true); });
    document.getElementById("import-run-btn")
      .addEventListener("click", function () { runImport(false); });
    back.addEventListener("click", function (e) { if (e.target === back) closeModal(); });
    // Focus in, Tab contained, focus returned on close. Without this a
    // keyboard user opens the dialog and focus is still on the toolbar
    // button behind it.
    if (window.modalA11y) {
      _release = window.modalA11y(box, { onEscape: closeModal });
    }
    return back;
  }


  /* ---------------------------------------------------------------- tabs */
  function showTab(which) {
    ["export", "import"].forEach(function (t) {
      var tab = document.getElementById("tab-" + t);
      var pane = document.getElementById("pane-" + t);
      if (!tab || !pane) return;
      var on = (t === which);
      tab.setAttribute("aria-selected", on ? "true" : "false");
      pane.hidden = !on;
    });
  }

  /* ------------------------------------------------------------- deliver */
  /* Hand the finished export over.
   *
   * This used to be window.open("/api/downloads/<file>?dl=1"), which starts a
   * FRESH navigation context. In the desktop app that context does not carry
   * the session cookie, so the session gate answered "not signed in" and the
   * user got an access-denied page instead of their file -- with a URL that
   * looked like a folder path, which made it read like a broken link.
   *
   * Two paths, mirroring data-folder.js:
   *   Desktop  -> ask main to open the data folder. No payload; main resolves
   *               the location itself, so this cannot become "open anything".
   *   Browser  -> a same-origin anchor with the download attribute. Same
   *               document, so the cookie goes with it.
   */
  function deliver(filename) {
    try {
      if (window.electronAPI && typeof window.electronAPI.send === "function") {
        window.electronAPI.send("open-data-folder");
        return "saved in your data folder, under downloads - opening it now";
      }
      var a = document.createElement("a");
      a.href = "/api/downloads/" + encodeURIComponent(filename) + "?dl=1";
      a.download = filename;
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      return "saved in your data folder, under downloads";
    } catch (e) {
      return "saved in your data folder, under downloads";
    }
  }

  /* -------------------------------------------------------------- import */
  var staged = null;   /* the inspect report for the archive on the server */

  function importSelected() {
    var out = [];
    document.querySelectorAll(".import-sec:checked").forEach(function (c) {
      out.push(c.value);
    });
    return out;
  }

  async function examineArchive() {
    var input = document.getElementById("import-file");
    var report = document.getElementById("import-report");
    var choices = document.getElementById("import-choices");
    var f = input && input.files && input.files[0];
    if (!f) {
      report.textContent = "Choose a file first.";
      return;
    }
    staged = null;
    choices.hidden = true;
    report.textContent = "Examining " + f.name + "...";
    var body = new FormData();
    body.append("file", f);
    var d;
    try {
      d = await (await fetch("/api/import/inspect", { method: "POST", body: body })).json();
    } catch (e) {
      report.textContent = "Could not read that file: " + e.message;
      return;
    }
    if (!d || !d.ok) {
      report.textContent = (d && d.error) || "That file could not be read as an export.";
      return;
    }
    staged = d;

    var bits = [];
    bits.push("Mode: " + (d.mode || "unknown"));
    if (d.created) bits.push("Created: " + d.created);
    if (d.key_style === "passphrase") {
      bits.push("Protected by a passphrase.");
    } else if (d.has_key) {
      bits.push("Carries its own key.");
    }
    (d.warnings || []).forEach(function (w) { bits.push("Note: " + w); });
    if ((d.skipped || []).length) {
      bits.push(d.skipped.length + " entr(y/ies) were refused for safety and " +
                "will not be imported.");
    }
    report.textContent = bits.join("  ");

    var box = document.getElementById("import-sections");
    box.innerHTML = "";
    (d.sections || []).forEach(function (s) {
      var row = document.createElement("label");
      row.className = "hw-toggle";
      row.style.cssText = "display:flex;gap:8px;align-items:center;margin:3px 0";
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "import-sec";
      cb.value = s.key;
      cb.checked = true;
      var txt = document.createElement("span");
      txt.className = "hw-toggle-label";
      txt.textContent = s.key + "  (" + (s.files || 0) + " file" +
        ((s.files === 1) ? "" : "s") + ", " + human(s.bytes || 0) + ")" +
        (s.mergeable === false ? "  - cannot be merged; kept as a reference copy" : "");
      row.appendChild(cb);
      row.appendChild(txt);
      box.appendChild(row);
    });

    document.getElementById("import-pass-wrap").style.display =
      d.needs_passphrase ? "" : "none";
    choices.hidden = false;
    document.getElementById("import-result").textContent = "";
  }

  async function runImport(dry) {
    var out = document.getElementById("import-result");
    if (!staged || !staged.stage_id) {
      out.textContent = "Examine an archive first.";
      return;
    }
    var secs = importSelected();
    if (!secs.length) {
      out.textContent = "Select at least one section.";
      return;
    }
    var replace = document.getElementById("import-replace").checked;
    var other = document.getElementById("import-other").checked;
    var target = other
      ? (document.getElementById("import-target").value || "").trim()
      : "self";

    if (!dry) {
      /* The one irreversible thing on this surface. Say plainly what it does,
       * and say it differently for replace, which deletes before it writes. */
      var msg = replace
        ? "REPLACE the chosen sections?\n\n" +
          "- Each chosen section is CLEARED first, then filled from the archive.\n" +
          "- Anything currently in those sections is deleted.\n" +
          "- This cannot be undone."
        : "Import the chosen sections?\n\n" +
          "- Files from the archive are added.\n" +
          "- Where a file already exists, the current one is backed up first.\n" +
          "- Your existing data is not deleted.";
      var okGo = await oracleConfirm(msg, {
        title: replace ? "Replace data" : "Import data",
        okLabel: replace ? "Replace" : "Import",
      });
      if (!okGo) return;
    }

    out.textContent = dry ? "Checking..." : "Importing...";
    var payload = {
      stage_id: staged.stage_id,
      sections: secs,
      mode: replace ? "replace" : "merge",
      dry_run: !!dry,
      target: target,
    };
    var pass = document.getElementById("import-pass").value;
    if (pass) payload.passphrase = pass;

    var d;
    try {
      d = await (await fetch("/api/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      })).json();
    } catch (e) {
      out.textContent = "Import failed: " + e.message;
      return;
    }
    if (!d.ok) {
      out.textContent = d.error || "Import failed.";
      if (d.needs_passphrase) {
        document.getElementById("import-pass-wrap").style.display = "";
        var pf = document.getElementById("import-pass");
        if (pf) pf.focus();
      }
      return;
    }
    var summary = (dry ? "Would restore " : "Restored ") + (d.written || 0) +
      " file(s)" +
      (d.backed_up ? ", backing up " + d.backed_up + " existing" : "") +
      (d.skipped ? ", skipping " + d.skipped + " refused for safety" : "") +
      ((d.errors && d.errors.length) ? ". " + d.errors.length + " could not be written." : ".");
    out.textContent = summary;
    if (!dry) {
      /* The archive is consumed on a real import, so the next run needs a
       * fresh examine rather than a stale id. */
      staged = null;
      document.getElementById("import-choices").hidden = true;
      if (window.setStatus) window.setStatus("Import complete. Reopen your chat to see it.");
    }
  }

  var _release = null;

  function closeModal() {
    var b = document.getElementById("export-modal-backdrop");
    if (b && b.parentNode) b.parentNode.removeChild(b);
    if (_release) { _release(); _release = null; }
  }

  function openPanel(tab) {
    if (!document.getElementById("export-modal-backdrop")) {
      buildModal();
      // Inventory is fetched on OPEN, not at page load: it stats every file in
      // the data folder, which is wasted work for the many sessions that never
      // export anything.
      window.exportRefresh();
    }
    showTab(tab);
  }

  window.openExportPanel = function () { openPanel("export"); };

  /* Two toolbar buttons, one panel. Export and Import are the same subject
   * from opposite ends, and a person who opened the wrong one is one click
   * from the right one instead of closing a dialog and hunting for another. */
  window.openImportPanel = function () { openPanel("import"); };
})();
