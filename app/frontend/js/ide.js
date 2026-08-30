/* ide.js -- the IDE + Display view in the Oracle panel.
 *
 * PHASE 1: THE SHELL. The display area, the menu bar, the editor, and the
 * expand/shrink control are real and work. RUNNING CODE DOES NOT EXIST YET and
 * the Run button says so rather than pretending: the executor it would call
 * (sage_engine.execute_python) is not sandboxed despite its docstring, its
 * default timeout is 56000 SECONDS, and code_exec_enabled has a
 * schema-says-off / fallback-says-on mismatch. Those get fixed before a button
 * in this panel can reach them.
 *
 * The two menu items exist and are disabled for the same reason: the modes
 * that govern "Allow Toga to Copy/Paste" arrive next, and "Save as file"
 * arrives with the extension allowlist that /api/downloads/save still needs.
 * A control that looks live and does nothing is worse than one that admits it.
 *
 * NO EDITOR LIBRARY. Monaco and CodeMirror both mean a CDN, and offline boot
 * is a hard requirement. A textarea with Tab handling is the honest answer at
 * this size; if it ever outgrows that, the replacement has to ship in-tree.
 */
(function () {
  "use strict";

  var EXPANDED_CLASS = "ide-expanded";
  var _menuOpen = false;

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
      .then(function (d) { if (d && d.expanded) setExpanded(true, false); })
      .catch(function () { /* default width is a fine answer */ });
  }

  function savePref(on) {
    fetch("/api/ide/prefs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expanded: !!on })
    }).catch(function () { /* the class is already applied; this is memory only */ });
  }

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

  /* ---- registration ---------------------------------------------------- */
  function init() {
    var ta = $("ide-editor");
    if (ta) ta.addEventListener("keydown", onEditorKeydown);

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
  window.ideShowOutput = ideShowOutput;
  window.ideClearOutput = ideClearOutput;
})();
