/* reasoning-log.js -- read the thinking, and check it has not changed.
 *
 * v2.15.2. The reasoning ledger was written on every reasoning turn from the
 * day it was added and there was NO way to read it: no endpoint, no button,
 * nothing but a Python session against an encrypted file. This is the half
 * that was missing.
 *
 * NOT A SECOND COPY OF THE CHAT PANEL. chat.js already shows a reply's
 * thinking in a collapsible panel underneath it, and that is the right place
 * to read one trace in context. This is the other question: what has this
 * profile's model been thinking across the whole thread, and can any of it be
 * PROVEN to be what was recorded at the time? So the list is ordered by time,
 * carries hashes, and every row can be checked against the memory chain.
 *
 * PREVIEWS FIRST, FULL TEXT ON REQUEST. One trace runs to 200,000 characters.
 * Rendering fifty of them to draw a list would make opening this the most
 * expensive thing in the app.
 *
 * textContent EVERYWHERE, never innerHTML, for the same reason chat.js says so
 * above its reasoning panel: a trace is model output, it frequently contains
 * markup and code, and it is the one string here that never came from us.
 *
 * Pure-ASCII source, matching the other frontend modules.
 */
(function () {
  "use strict";

  var _release = null;
  var _verify = false;
  var _rows = [];

  function human(n) {
    if (n < 1024) return n + " chars";
    if (n < 1048576) return (n / 1024).toFixed(0) + "K chars";
    return (n / 1048576).toFixed(1) + "M chars";
  }

  function when(ts) {
    if (!ts) return "unknown time";
    try {
      return new Date(ts * 1000).toLocaleString();
    } catch (e) {
      return String(ts);
    }
  }

  function closeModal() {
    var b = document.getElementById("rlog-modal-backdrop");
    if (b && b.parentNode) b.parentNode.removeChild(b);
    if (_release) { _release(); _release = null; }
  }

  /* ------------------------------------------------------------ verdicts */
  /* Three states, not two, and the third is the important one. A trace whose
   * witness predates the feature, or whose chain write failed, or that simply
   * sits outside the window the backend searched, is UNVERIFIABLE -- not
   * tampered. Painting that red would teach the reader to ignore red. */
  function verdict(p) {
    if (!p) return { text: "", tone: "" };
    if (p.text_intact === false) {
      return { text: "ALTERED", tone: "bad" };
    }
    if (p.hash_matches === true) {
      return { text: "verified", tone: "good" };
    }
    if (p.hash_matches === false) {
      return { text: "NO MATCH", tone: "bad" };
    }
    return { text: "unverifiable", tone: "muted" };
  }

  function toneColor(tone) {
    if (tone === "good") return "var(--ok, #2e7d32)";
    if (tone === "bad") return "var(--danger, #c62828)";
    return "var(--text-dim, #888)";
  }

  /* --------------------------------------------------------------- render */
  function renderStats(st) {
    var el = document.getElementById("rlog-stats");
    if (!el) return;
    if (!st || !st.entries) {
      el.textContent = "Nothing recorded yet. Traces are stored when a " +
        "reasoning model shows its thinking.";
      return;
    }
    var bits = [st.entries + " entr" + (st.entries === 1 ? "y" : "ies"),
                human(st.total_chars || 0)];
    if (st.oldest_ts) bits.push("since " + when(st.oldest_ts));
    var txt = bits.join(" - ");
    if (st.pruned) {
      /* Said out loud, because the ledger prunes oldest-first and a log that
       * quietly drops what you came looking for is worse than one that admits
       * it. reasoning_ledger counts this on purpose; hiding it here would
       * throw away the reason it counts. */
      txt += ". " + st.pruned + " older entr" +
             (st.pruned === 1 ? "y has" : "ies have") +
             " been pruned to stay within the size limit.";
    }
    el.textContent = txt;
  }

  function renderRows() {
    var box = document.getElementById("rlog-list");
    if (!box) return;
    box.innerHTML = "";
    if (!_rows.length) return;

    _rows.forEach(function (r) {
      var item = document.createElement("details");
      item.className = "rlog-item";
      item.style.cssText =
        "border:1px solid var(--border);border-radius:var(--radius);" +
        "margin:6px 0;padding:6px 8px;background:var(--surface-2, transparent)";

      var sum = document.createElement("summary");
      sum.style.cssText = "cursor:pointer;display:flex;gap:8px;" +
        "align-items:baseline;flex-wrap:wrap";

      var t = document.createElement("span");
      t.style.cssText = "font-weight:600";
      t.textContent = when(r.ts);
      sum.appendChild(t);

      var meta = document.createElement("span");
      meta.className = "voice-extras-note";
      meta.textContent = (r.model ? r.model + " - " : "") +
        human(r.chars || 0) + (r.truncated ? " (stored truncated)" : "");
      sum.appendChild(meta);

      if (r.provenance) {
        var v = verdict(r.provenance);
        var badge = document.createElement("span");
        badge.textContent = v.text;
        badge.title = r.provenance.message || "";
        badge.style.cssText = "font-size:.85em;font-weight:600;color:" +
          toneColor(v.tone);
        sum.appendChild(badge);
      }
      item.appendChild(sum);

      var prev = document.createElement("div");
      prev.className = "voice-extras-note";
      prev.style.cssText = "margin:6px 0 4px 0;white-space:pre-wrap;" +
        "word-break:break-word";
      /* textContent: this is model output. */
      prev.textContent = r.preview + (r.preview_truncated ? "..." : "");
      item.appendChild(prev);

      var hash = document.createElement("div");
      hash.className = "voice-extras-note";
      hash.style.cssText = "font-family:var(--font-mono, monospace);" +
        "font-size:.8em;opacity:.75;word-break:break-all";
      hash.textContent = "sha256 " + String(r.sha256 || "").slice(0, 32) +
        (r.chain_hash ? "  witness " + String(r.chain_hash).slice(0, 16)
                      : "  (no chain witness)");
      item.appendChild(hash);

      if (r.provenance && r.provenance.message) {
        var why = document.createElement("div");
        why.className = "voice-extras-note";
        why.style.cssText = "font-size:.85em;color:" +
          toneColor(verdict(r.provenance).tone);
        why.textContent = r.provenance.message;
        item.appendChild(why);
      }

      var row = document.createElement("div");
      row.className = "voice-extras";
      row.style.cssText = "margin-top:6px";
      var full = document.createElement("button");
      full.type = "button";
      full.className = "voice-extras-btn";
      full.textContent = "Show full trace";
      full.addEventListener("click", function () { showFull(r.sha256, item, full); });
      row.appendChild(full);
      item.appendChild(row);

      box.appendChild(item);
    });
  }

  /* Full text is fetched per entry and cached on the element, so expanding the
   * same one twice does not go back to the server. */
  async function showFull(sha, item, btn) {
    if (item.querySelector(".rlog-full")) {
      var ex = item.querySelector(".rlog-full");
      ex.hidden = !ex.hidden;
      btn.textContent = ex.hidden ? "Show full trace" : "Hide full trace";
      return;
    }
    btn.disabled = true;
    btn.textContent = "Loading...";
    var d;
    try {
      d = await (await fetch("/api/reasoning-ledger/entry?sha=" +
                             encodeURIComponent(sha))).json();
    } catch (e) {
      d = { ok: false, error: "could not reach the backend" };
    }
    btn.disabled = false;
    var pre = document.createElement("pre");
    pre.className = "rlog-full";
    pre.style.cssText =
      "white-space:pre-wrap;word-break:break-word;max-height:40vh;" +
      "overflow:auto;margin:8px 0 0 0;padding:8px;font-size:.9em;" +
      "border:1px solid var(--border);border-radius:var(--radius);" +
      "background:var(--bg, transparent)";
    /* textContent, not innerHTML: see the file header. */
    pre.textContent = d && d.ok
      ? String(d.trace || "")
      : "Could not load this trace. " + ((d && d.error) || "");
    item.appendChild(pre);
    btn.textContent = "Hide full trace";
  }

  /* --------------------------------------------------------------- fetch */
  window.reasoningLogRefresh = async function () {
    var box = document.getElementById("rlog-list");
    var st = document.getElementById("rlog-stats");
    if (st) st.textContent = "Reading...";
    if (box) box.innerHTML = "";
    var d;
    try {
      d = await (await fetch("/api/reasoning-ledger?limit=100" +
                             (_verify ? "&verify=1" : ""))).json();
    } catch (e) {
      if (st) st.textContent = "Could not read the thinking log.";
      return;
    }
    if (!d || !d.ok) {
      if (st) {
        st.textContent = "Could not read the thinking log. " +
          ((d && d.error) || "");
      }
      return;
    }
    _rows = d.entries || [];
    renderStats(d.stats);
    renderRows();
  };

  async function clearLog() {
    if (!window.confirm(
        "Erase this profile's thinking log?\n\n" +
        "- Every stored reasoning trace for this profile is deleted.\n" +
        "- The replies themselves are NOT touched.\n" +
        "- The memory chain's witnesses stay, so this is recorded.\n\n" +
        "This cannot be undone. Export first if you want a copy.")) {
      return;
    }
    var out = document.getElementById("rlog-stats");
    try {
      var d = await (await fetch("/api/reasoning-ledger/clear",
                                 { method: "POST" })).json();
      if (out) {
        out.textContent = d && d.ok
          ? "Cleared " + (d.cleared || 0) + " entr" +
            ((d.cleared === 1) ? "y." : "ies.")
          : "Could not clear it. " + ((d && d.error) || "");
      }
      _rows = [];
      renderRows();
    } catch (e) {
      if (out) out.textContent = "Could not clear it.";
    }
  }

  /* --------------------------------------------------------------- modal */
  function buildModal() {
    var back = document.createElement("div");
    back.id = "rlog-modal-backdrop";
    back.style.cssText =
      "position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9998;" +
      "display:flex;align-items:center;justify-content:center;padding:20px";

    var box = document.createElement("div");
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", "Thinking log");
    box.style.cssText =
      "background:var(--surface);border:1px solid var(--border-hi);" +
      "border-radius:var(--radius);padding:18px 20px;max-width:720px;" +
      "width:100%;max-height:82vh;overflow:auto;font-family:var(--font-body);" +
      "color:var(--text)";

    box.innerHTML =
      '<div style="font-weight:600;margin-bottom:4px">Thinking log</div>' +
      '<div class="voice-extras-note" style="margin-bottom:8px">' +
      '  The model\'s own reasoning for this profile, stored encrypted under ' +
      '  your key and never fed back to the model. Wrong turns and discarded ' +
      '  steps are part of a trace -- it is working, not an answer.' +
      "</div>" +
      '<div id="rlog-stats" class="voice-extras-note" role="status" ' +
      '  aria-live="polite" style="margin-bottom:8px"></div>' +
      '<label class="hw-toggle" style="display:flex;gap:8px;align-items:center;margin:6px 0">' +
      '  <input type="checkbox" id="rlog-verify">' +
      '  <span class="hw-toggle-label">Check each entry against the memory ' +
      '    chain (slower)</span>' +
      "</label>" +
      '<div id="rlog-list" style="margin:6px 0"></div>' +
      '<div class="voice-extras-note" style="margin-top:10px">' +
      '  To keep a copy, use Export and choose "Model reasoning traces".' +
      "</div>" +
      '<div class="voice-extras" style="margin-top:12px">' +
      '  <button type="button" class="voice-extras-btn" id="rlog-refresh-btn">Refresh</button>' +
      '  <button type="button" class="voice-extras-btn" id="rlog-clear-btn"' +
      '    data-tip="Erase every stored trace for this profile">Clear log</button>' +
      '  <button type="button" class="voice-extras-btn" id="rlog-close-btn">Close</button>' +
      "</div>";

    back.appendChild(box);
    document.body.appendChild(back);

    document.getElementById("rlog-close-btn")
      .addEventListener("click", closeModal);
    document.getElementById("rlog-refresh-btn")
      .addEventListener("click", function () { window.reasoningLogRefresh(); });
    document.getElementById("rlog-clear-btn")
      .addEventListener("click", clearLog);
    document.getElementById("rlog-verify")
      .addEventListener("change", function () {
        _verify = this.checked;
        window.reasoningLogRefresh();
      });
    back.addEventListener("click", function (e) {
      if (e.target === back) closeModal();
    });
    if (window.modalA11y) {
      _release = window.modalA11y(box, { onEscape: closeModal });
    }
    return back;
  }

  window.openReasoningLog = function () {
    if (!document.getElementById("rlog-modal-backdrop")) {
      buildModal();
      /* Read on OPEN, not at page load: it decrypts and parses the whole
       * ledger, which is wasted work for every session that never looks. */
      window.reasoningLogRefresh();
    }
  };
})();
