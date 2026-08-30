/* ide.js -- the IDE + Display view in the Oracle panel.
 *
 * EVERYTHING IN HERE IS LIVE. It got there in stages, and the staging was the
 * point: the Run button shipped visibly disabled and said so for two releases
 * while the executor it would call was fixed (its timeout was 56000 SECONDS,
 * its docstring claimed a sandbox it did not have, and code_exec_enabled read
 * off in the schema and on in the fallback). A control that looks live and
 * does nothing is worse than one that admits it -- so it admitted it, until it
 * did not have to.
 *
 * Run now posts to /api/ide/run, and the SERVER decides everything that
 * matters: whether code execution is on at all, what Customs makes of the
 * code, how long it may take, and -- from the mode ladder -- whether it runs
 * confined. Beginner and Advanced run confined (no network, no new processes,
 * no reach into the app's data folder, their own scratch directory); Expert
 * runs unconfined, which is the honest thing Expert buys. None of that is
 * decided here. This file shows a button and reports an answer.
 *
 * Stop is real: it kills the child process and anything it started, rather
 * than merely stopping caring about the result.
 *
 * NO EDITOR LIBRARY. Monaco and CodeMirror both mean a CDN, and offline boot
 * is a hard requirement. A textarea with Tab handling is the honest answer at
 * this size; if it ever outgrows that, the replacement has to ship in-tree.
 */
(function () {
  "use strict";

  var EXPANDED_CLASS = "ide-expanded";
  var _menuOpen = false;

  /* The authority ladder. Ordered, and the order is the meaning -- each notch
   * strictly adds what Toga may do with the editor. The SERVER stores and
   * enforces this; everything here is presentation. If the two ever disagree,
   * the server is right, which is why every change re-reads its answer instead
   * of trusting the value we just set. */
  var MODES = ["beginner", "advanced", "expert"];
  var MODE_BLURB = {
    beginner: "Beginner - the editor is yours alone. You write, you Run, you "
      + "Save. Code runs confined.",
    advanced: "Advanced - Toga may read and write the editor. Only you press "
      + "Run. Code runs confined.",
    expert: "Expert - Toga may read, write, AND run code by itself. Code runs "
      + "WITHOUT confinement."
  };
  /* What "confined" means, in one sentence, wherever it has to be said. Kept
   * in one place so the tooltip, the dialog and the blurb cannot drift into
   * three slightly different promises. */
  var CONFINED_MEANS = "no network, no new programs, and no access to the "
    + "app's own data folder";
  var _mode = "beginner";
  /* Separate from the mode on purpose. The mode says what is PERMITTED; this
   * says whether it is switched ON right now. Advanced with the switch off
   * shares nothing -- and the server enforces both, so this is only the UI's
   * copy of an answer it re-reads. */
  var _togaClip = false;
  /* One step, not a stack. "Put back what was there before Toga's last
   * change" is a promise anyone can hold in their head; an N-deep history in
   * a side panel is a feature nobody asked for and a place for stale buffers
   * to live. */
  var _undoBuffer = null;
  var _canExpert = false;
  var _expertNeedsPassword = false;

  function $(id) { return document.getElementById(id); }
  function panel() { return $("oracle-panel"); }

  /* ---- expand / shrink -------------------------------------------------
   * Width is one CSS variable on .oracle-panel, so this is a class toggle and
   * the panel's existing width transition animates it. */
  function setExpanded(on, persist) {
    var p = panel();
    if (!p) return;
    p.classList.toggle(EXPANDED_CLASS, !!on);
    var btn = $("ide-expand");
    if (btn) {
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      var label = on ? "Shrink the panel to normal width"
                     : "Expand the panel to double width";
      btn.setAttribute("aria-label", label);
      btn.setAttribute("data-tip", on ? "Shrink to normal width"
                                      : "Expand to double width");
    }
    if (persist) savePref(!!on);
  }

  function ideToggleExpand() {
    var p = panel();
    setExpanded(!(p && p.classList.contains(EXPANDED_CLASS)), true);
    if (window.Haptic) Haptic.vibrate(Haptic.PATTERNS.toggle);
  }

  /* Per USER, not per install -- ui_prefs, the same split that fixed the
   * browser-cookie switch leaking between profiles. Best effort in both
   * directions: a width preference is never worth an error in front of
   * somebody. */
  function loadPref() {
    fetch("/api/ide/prefs")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        if (d.expanded) setExpanded(true, false);
        _canExpert = !!d.can_expert;
        _expertNeedsPassword = !!d.expert_needs_password;
        _togaClip = !!d.toga_clip;
        applyMode(d.mode || "beginner");
      })
      .catch(function () { /* the safe defaults are already applied */ });
  }

  function savePref(on) {
    fetch("/api/ide/prefs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expanded: !!on })
    }).catch(function () { /* the class is already applied; this is memory only */ });
  }

  /* ---- the authority ladder --------------------------------------------
   * applyMode() paints; ideOnModeChange() negotiates. Keeping them apart
   * matters, because the server can refuse and the control then has to go
   * BACK to what is actually stored rather than to what was clicked. */
  function applyMode(mode) {
    _mode = MODES.indexOf(mode) === -1 ? "beginner" : mode;
    var sel = $("ide-mode");
    if (sel) {
      sel.value = _mode;
      sel.setAttribute("data-mode", _mode);
      sel.setAttribute("data-tip", MODE_BLURB[_mode]);
      var expertOpt = sel.querySelector('option[value="expert"]');
      if (expertOpt) {
        // Do not offer a choice that will be refused. A non-owner sees why.
        expertOpt.disabled = !_canExpert && _mode !== "expert";
        expertOpt.textContent = _canExpert ? "Expert" : "Expert (owner only)";
      }
    }
    // Both of these read the mode, so both get repainted when it changes.
    // Run's tooltip is mode-dependent because CONFINEMENT is: the same button
    // means something materially different in Expert, and the place a person
    // looks to find that out is the button.
    applyTogaClip();
    paintRun();
  }

  /* The switch itself. Beginner disables it AND forces the displayed state
   * off, because "on but not permitted" is not a state anyone should have to
   * reason about. */
  function applyTogaClip() {
    var clip = $("ide-toga-clip");
    var note = $("ide-toga-clip-note");
    if (!clip || !note) return;
    var allowed = _mode !== "beginner";
    clip.disabled = !allowed;
    var on = allowed && _togaClip;
    clip.setAttribute("aria-pressed", on ? "true" : "false");
    if (!allowed) {
      note.textContent = "Advanced only";
      clip.setAttribute("data-tip",
        "Beginner keeps the editor to yourself. Switch to Advanced or " +
        "Expert to let Toga read and write it.");
    } else {
      note.textContent = on ? "on" : "off";
      clip.setAttribute("data-tip", on
        ? "Toga can read the editor and propose changes to it. Your messages "
          + "will include its contents."
        : "Switch on to let Toga read the editor and propose changes.");
    }
  }

  async function ideToggleTogaClip() {
    if (_mode === "beginner") return;
    var want = !_togaClip;
    try {
      var res = await fetch("/api/ide/prefs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ toga_clip: want })
      });
      if (!res.ok) throw new Error("refused");
      var d = await res.json();
      _togaClip = (d && typeof d.toga_clip === "boolean") ? d.toga_clip : want;
    } catch (e) {
      if (window.setStatusError) {
        setStatusError("Could not change that setting.");
      }
      return;
    }
    applyTogaClip();
    setMenu(false);
    if (window.setStatus) {
      setStatus(_togaClip
        ? "Toga can now read your editor, and propose changes to it."
        : "Toga can no longer see your editor.");
    }
    if (window.Haptic) Haptic.vibrate(Haptic.PATTERNS.toggle);
  }

  /* What chat.js asks before sending. Null means "send nothing" -- not an
   * empty string, so the payload key is omitted entirely rather than carrying
   * an empty buffer that says the same thing less clearly. */
  function ideBufferForSend() {
    if (_mode === "beginner" || !_togaClip) return null;
    var ta = $("ide-editor");
    if (!ta || !ta.value) return null;
    return ta.value;
  }

  /* A write arriving from the model. It REPLACES the buffer, so the previous
   * contents are kept and the menu grows an undo. Text only, and set through
   * .value -- there is no path here by which model output becomes markup. */
  function ideApplyWrite(text) {
    var ta = $("ide-editor");
    if (!ta || typeof text !== "string") return false;
    _undoBuffer = ta.value;
    ta.value = text;
    var undo = $("ide-undo-write");
    if (undo) undo.hidden = false;
    // Make it visible that something changed under them.
    if (window.PanelViews && PanelViews.current() !== "ide") {
      var tab = document.querySelector('.game-tab[data-view="ide"]');
      if (tab) tab.click();
    }
    if (window.setStatus) {
      setStatus("Toga changed the editor. Undo is in the panel menu.");
    }
    return true;
  }

  function ideUndoWrite() {
    if (_undoBuffer === null) return;
    var ta = $("ide-editor");
    if (ta) ta.value = _undoBuffer;
    _undoBuffer = null;
    var undo = $("ide-undo-write");
    if (undo) undo.hidden = true;
    setMenu(false);
    if (window.setStatus) setStatus("Put the editor back.");
  }

  async function ideOnModeChange(sel) {
    var want = sel ? sel.value : "beginner";
    if (want === _mode) return;

    // Dropping down is immediate and unconfirmed. Reducing your own authority
    // is always safe, and a confirmation here would put friction in exactly
    // the wrong direction.
    var goingUp = MODES.indexOf(want) > MODES.indexOf(_mode);

    if (goingUp && want === "expert") {
      var confirmed = await confirmExpert();
      if (!confirmed) { applyMode(_mode); return; }   // snap back, no request
      // The password, when there is an account to ask. requireUnlock resolves
      // true without prompting on a single-user install -- there the checkbox
      // in the dialog above WAS the deliberate act.
      if (window.requireUnlock) {
        var unlocked = await window.requireUnlock(
          "Confirm it is you before handing Toga the Run button.");
        if (!unlocked) { applyMode(_mode); return; }
      }
    }

    var prev = _mode;
    var stored = await postMode(want);
    if (stored === null) { applyMode(prev); return; }
    applyMode(stored);

    // Leaving Expert also drops the elevation. Staying unlocked after giving
    // up the privilege it was for is a window nobody asked for.
    if (prev === "expert" && stored !== "expert" && window.reauthDrop) {
      try { window.reauthDrop(); } catch (e) {}
    }
    if (window.setStatus) setStatus(MODE_BLURB[stored]);
    if (window.Haptic) Haptic.vibrate(Haptic.PATTERNS.toggle);
  }

  /* Returns the mode the SERVER now holds, or null if it refused. */
  async function postMode(want) {
    try {
      var res = await fetch("/api/ide/prefs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: want })
      });
      if (!res.ok) {
        var why = "Could not change mode.";
        if (res.status === 404) {
          why = "Expert mode is owner-only on this install.";
        } else {
          try {
            var d = await res.json();
            why = (d && (d.detail || d.error)) || why;
            if (why && typeof why === "object") why = why.error || "Could not change mode.";
          } catch (e) { /* keep the generic message */ }
        }
        if (window.setStatusError) setStatusError(String(why));
        return null;
      }
      var data = await res.json();
      return (data && data.mode) || want;
    } catch (e) {
      if (window.setStatusError) {
        setStatusError("Could not reach the backend to change mode.");
      }
      return null;
    }
  }

  /* The loud one. Cancel is always live, Escape cancels, clicking outside
   * cancels. On a single-user install the confirm button stays disabled until
   * the acknowledgement is ticked -- there is no password to ask for, so the
   * tick IS the deliberate act. */
  function confirmExpert() {
    return new Promise(function (resolve) {
      var root = document.getElementById("modal-root");
      if (!root) { resolve(false); return; }
      var needTick = !_expertNeedsPassword;

      root.innerHTML =
        '<div class="modal-overlay" id="ide-expert-overlay" style="z-index:100001"' +
        ' role="dialog" aria-modal="true" aria-labelledby="ide-expert-title">' +
        '<div class="modal-box" style="max-width:460px">' +
        '<div class="modal-title" id="ide-expert-title">Expert mode</div>' +
        '<div style="font-size:13px;line-height:1.55;color:var(--text)">' +
        "<p style=\"margin:0 0 8px\"><strong>Expert mode does two things, and the " +
        "second one is the one to read twice.</strong></p>" +
        "<p style=\"margin:0 0 8px\"><strong>1.</strong> Toga may write into the " +
        "editor and press Run without asking you first.</p>" +
        "<p style=\"margin:0 0 8px\"><strong>2. It removes the confinement.</strong> " +
        "In Beginner and Advanced your code runs with " + CONFINED_MEANS +
        ". In Expert none of that applies: code runs with your account's full " +
        "access to your files and your network.</p>" +
        "<p style=\"margin:0 0 8px\">So this is not only \u201cToga may press " +
        "Run\u201d. It is \u201cToga may press Run, and Run reaches " +
        "further\u201d.</p>" +
        "<p style=\"margin:0 0 8px\">A model can be wrong, and it can be talked into " +
        "being wrong by something it reads. Use this when you are watching.</p>" +
        "<p style=\"margin:0 0 8px\">You can drop back to Beginner or Advanced at " +
        "any time, with no confirmation. Confinement comes back the moment you " +
        "do.</p>" +
        "<p style=\"margin:0\"><strong>Expert ends when you sign out or close " +
        "VeridianAI.</strong> It is never saved, so nobody inherits it by " +
        "sitting down at this machine after you.</p>" +
        "</div>" +
        (needTick
          ? '<label style="display:flex;gap:8px;align-items:flex-start;' +
            'margin-top:12px;font-size:13px;cursor:pointer">' +
            '<input type="checkbox" id="ide-expert-ack" style="margin-top:3px">' +
            "<span>I understand the risks and want Expert mode.</span></label>"
          : '<p style="margin-top:12px;font-size:12.5px;color:var(--text-muted)">' +
            "You will be asked for your password next.</p>") +
        '<div class="modal-actions">' +
        '<button class="modal-btn" id="ide-expert-cancel">Cancel</button>' +
        '<button class="modal-btn primary" id="ide-expert-ok"' +
        (needTick ? " disabled" : "") + ">I understand</button>" +
        "</div></div></div>";

      var onKey;
      var finish = function (val) {
        document.removeEventListener("keydown", onKey, true);
        root.innerHTML = "";
        resolve(val);
      };
      onKey = function (e) {
        if (e.key === "Escape") { e.preventDefault(); finish(false); }
      };
      document.addEventListener("keydown", onKey, true);

      var ack = document.getElementById("ide-expert-ack");
      var okBtn = document.getElementById("ide-expert-ok");
      if (ack && okBtn) {
        ack.addEventListener("change", function () { okBtn.disabled = !ack.checked; });
      }
      var ov = document.getElementById("ide-expert-overlay");
      ov.addEventListener("click", function (e) { if (e.target === ov) finish(false); });
      document.getElementById("ide-expert-cancel").onclick = function () { finish(false); };
      okBtn.onclick = function () { finish(true); };

      var first = ack || document.getElementById("ide-expert-cancel");
      if (first) first.focus();
    });
  }

  /* What the rest of the app may ask about the ladder. Read-only on purpose:
   * nothing outside this module gets to SET the mode, and the server would not
   * believe it anyway. */
  function ideMode() { return _mode; }
  function ideMayTogaTouchBuffer() { return _mode !== "beginner"; }

  /* ---- the collapsible menu -------------------------------------------- */
  function setMenu(open) {
    var m = $("ide-menu"), b = $("ide-menu-btn");
    if (!m || !b) return;
    _menuOpen = !!open;
    m.hidden = !_menuOpen;
    b.setAttribute("aria-expanded", _menuOpen ? "true" : "false");
  }

  function ideToggleMenu() { setMenu(!_menuOpen); }

  // Click-away and Escape close it. Capture phase so a click on a control
  // inside the menu still reaches that control first.
  document.addEventListener("click", function (e) {
    if (!_menuOpen) return;
    var wrap = document.querySelector(".ide-menu-wrap");
    if (wrap && !wrap.contains(e.target)) setMenu(false);
  });
  document.addEventListener("keydown", function (e) {
    if (_menuOpen && e.key === "Escape") {
      setMenu(false);
      var b = $("ide-menu-btn");
      if (b) b.focus();
    }
  });

  /* ---- the editor ------------------------------------------------------
   * Tab indents instead of moving focus. That traps keyboard users unless
   * there is a way out, so Escape releases the trap: press Escape, then Tab
   * leaves the field normally. Shift+Tab outdents. */
  var _tabEscape = false;

  function onEditorKeydown(e) {
    if (e.key === "Escape") { _tabEscape = true; return; }
    if (e.key !== "Tab") { _tabEscape = false; return; }
    if (_tabEscape) { _tabEscape = false; return; }   // let this Tab move focus

    e.preventDefault();
    var ta = e.target;
    var start = ta.selectionStart, end = ta.selectionEnd;
    var v = ta.value;

    if (!e.shiftKey && start === end) {
      ta.value = v.slice(0, start) + "    " + v.slice(end);
      ta.selectionStart = ta.selectionEnd = start + 4;
      return;
    }

    // Block indent / outdent over the selected lines.
    var ls = v.lastIndexOf("\n", start - 1) + 1;
    var le = v.indexOf("\n", end);
    if (le === -1) le = v.length;
    var block = v.slice(ls, le);
    var lines = block.split("\n");
    var delta = 0, first = 0;
    lines = lines.map(function (line, i) {
      if (e.shiftKey) {
        var m = line.match(/^ {1,4}/);
        var cut = m ? m[0].length : 0;
        if (i === 0) first = -cut;
        delta -= cut;
        return line.slice(cut);
      }
      if (i === 0) first = 4;
      delta += 4;
      return "    " + line;
    });
    ta.value = v.slice(0, ls) + lines.join("\n") + v.slice(le);
    ta.selectionStart = Math.max(ls, start + first);
    ta.selectionEnd = Math.max(ta.selectionStart, end + delta);
  }

  /* ---- output ----------------------------------------------------------
   * Nothing calls this yet. It exists so the display area has ONE way to be
   * written to when execution lands, rather than three call sites inventing
   * their own. textContent, never innerHTML: whatever ends up here is program
   * output, which is exactly the kind of text that must not become markup. */
  function ideShowOutput(text) {
    var idle = $("ide-display-idle"), out = $("ide-output");
    if (!out || !idle) return;
    var s = (text == null) ? "" : String(text);
    if (!s) {
      out.hidden = true; out.textContent = "";
      idle.hidden = false;
      return;
    }
    idle.hidden = true;
    out.hidden = false;
    out.textContent = s;
    out.scrollTop = out.scrollHeight;
  }

  function ideClearOutput() { ideShowOutput(""); }


  /* ---- run / stop -------------------------------------------------------
   * ONE BUTTON. It is Run while nothing is running and Stop while something
   * is, because two buttons where only ever one is usable is two things to
   * look at to learn one fact.
   *
   * Nothing here decides whether the code MAY run, how long it may take, or
   * whether it is confined. All of that is the server's, read from the
   * caller's own stored mode -- a browser that says "I am in Expert" is a
   * browser talking about itself. This shows a state and reports an answer.
   */
  var _running = false;
  var _stopping = false;

  function paintRun() {
    var b = $("ide-run");
    if (!b) return;
    b.textContent = _stopping ? "Stopping…" : (_running ? "■ Stop" : "▶ Run");
    b.disabled = _stopping;
    b.classList.toggle("is-running", _running && !_stopping);
    if (_running) {
      b.setAttribute("aria-label", "Stop the running code");
      b.setAttribute("data-tip", "Stop this run. The program and anything it "
        + "started are ended.");
      return;
    }
    b.setAttribute("aria-label", "Run the code in the editor");
    b.setAttribute("data-tip", _mode === "expert"
      ? "Run the editor's contents. Expert runs WITHOUT confinement: the code "
        + "has your account's access to your files and network."
      : "Run the editor's contents, confined - " + CONFINED_MEANS + ".");
  }

  async function ideRun() {
    if (_stopping) return;
    if (_running) { await ideStop(); return; }

    var ta = $("ide-editor");
    var code = ta ? ta.value : "";
    if (!code.trim()) {
      ideShowOutput("[nothing to run] The editor is empty.");
      return;
    }

    _running = true; paintRun();
    ideShowOutput("Running…");
    try {
      var res = await fetch("/api/ide/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: code })
      });
      if (!res.ok) {
        // The server's refusals name the thing that would fix them -- the
        // consent toggle, or a run already in progress. Repeat them verbatim
        // rather than inventing a friendlier version that is less useful.
        var why = "Could not run.";
        try {
          var d = await res.json();
          why = (d && (d.detail || d.error)) || why;
          if (why && typeof why === "object") why = why.error || "Could not run.";
        } catch (e) { /* keep the generic message */ }
        ideShowOutput("[REFUSED] " + String(why));
        return;
      }
      var data = await res.json();
      ideShowOutput((data && data.output) || "[no output]");
      if (window.Haptic) {
        Haptic.vibrate(data && data.status === "ok"
          ? Haptic.PATTERNS.toggle : Haptic.PATTERNS.error);
      }
    } catch (e) {
      ideShowOutput("[EXECUTION ERROR] Could not reach the backend.");
    } finally {
      _running = false; _stopping = false; paintRun();
    }
  }

  /* Fire-and-forget by design. The kill lands server-side and the RUN request
   * -- still open -- is what comes back and repaints, carrying whatever the
   * program managed to print before it was stopped. Resolving the button here
   * instead would show "Run" while output was still arriving. */
  async function ideStop() {
    _stopping = true; paintRun();
    try {
      await fetch("/api/ide/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      });
    } catch (e) {
      // The run request is the one that reports; if this never landed, it
      // simply finishes normally and says so.
      _stopping = false; paintRun();
    }
  }

  /* ---- save as file ----------------------------------------------------
   * Writes the buffer into the person's own downloads folder through
   * POST /api/downloads/save. The server decides what may be written -- see
   * sage_engine.check_save_filename -- and this reports its answer verbatim
   * rather than guessing at the rule, so the two can never drift apart.
   *
   * The filename is remembered for the session so a second save offers the
   * first name back instead of making you retype it. Not persisted: a
   * filename is a fact about what you are working on right now. */
  var _lastName = "untitled.py";

  async function ideSaveAs() {
    setMenu(false);
    var ta = $("ide-editor");
    if (!ta) return;

    var name = window.oraclePrompt
      ? await window.oraclePrompt(
          "Save the editor contents into your downloads folder as:",
          { title: "Save as file", okLabel: "Save", value: _lastName })
      : window.prompt("Save as:", _lastName);
    if (name == null) return;                 // cancelled, and that is fine
    name = String(name).trim();
    if (!name) {
      if (window.setStatusError) setStatusError("Save cancelled - no filename.");
      return;
    }

    try {
      var res = await fetch("/api/downloads/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: name, content: ta.value })
      });
      var data = null;
      try { data = await res.json(); } catch (e) { data = null; }

      if (!res.ok) {
        // FastAPI puts HTTPException text in `detail`. Show the server's own
        // words: it knows why it refused and this does not.
        var why = (data && (data.detail || data.error)) ||
                  ("save failed (" + res.status + ")");
        if (window.setStatusError) setStatusError(String(why));
        return;
      }

      _lastName = (data && data.filename) || name;
      var msg = "Saved " + _lastName + " to your downloads folder";
      // The server sanitizes; if it had to change the name, say so rather than
      // letting someone go looking for a file under the name they typed.
      if (data && data.filename && data.filename !== name) {
        msg += ' (renamed from "' + name + '")';
      }
      if (data && data.backup) {
        msg += " - the previous version was kept as " + data.backup;
      }
      if (window.setStatus) setStatus(msg);
      if (window.Haptic) Haptic.vibrate(Haptic.PATTERNS.done);
    } catch (e) {
      if (window.setStatusError) {
        setStatusError("Could not reach the backend to save the file.");
      }
    }
  }

  /* ---- registration ---------------------------------------------------- */
  function init() {
    var ta = $("ide-editor");
    if (ta) ta.addEventListener("keydown", onEditorKeydown);

    // Paint the safe end of the ladder before the server has answered. If the
    // fetch never lands, "Beginner" is the state the panel should be showing.
    applyMode("beginner");
    paintRun();

    if (window.PanelViews) {
      window.PanelViews.register("ide", {
        ids: ["ide-view"],
        display: "flex",
        onEnter: function () {
          loadPref();
        },
        // Leaving the IDE drops back to normal width. The stored preference is
        // NOT touched: it describes how the IDE should look when you return,
        // not how the games should look now.
        onLeave: function () {
          setMenu(false);
          var p = panel();
          if (p) p.classList.remove(EXPANDED_CLASS);
        }
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.ideToggleExpand = ideToggleExpand;
  window.ideToggleMenu = ideToggleMenu;
  window.ideOnModeChange = ideOnModeChange;
  window.ideToggleTogaClip = ideToggleTogaClip;
  window.ideBufferForSend = ideBufferForSend;
  window.ideApplyWrite = ideApplyWrite;
  window.ideUndoWrite = ideUndoWrite;
  window.ideMode = ideMode;
  window.ideMayTogaTouchBuffer = ideMayTogaTouchBuffer;
  window.ideRun = ideRun;
  window.ideSaveAs = ideSaveAs;
  window.ideShowOutput = ideShowOutput;
  window.ideClearOutput = ideClearOutput;
})();
