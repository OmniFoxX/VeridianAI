#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ide_panel.py -- the IDE + Display panel says what it can actually do.

WHY THIS EXISTS

Phase 1 of this panel shipped a Run button that did not run anything, and two
menu items that were not wired yet. That was a deliberate, staged decision --
the executor they would call had three defects that got fixed first -- but it
is also exactly the shape that rots: someone tidies the "temporary" disabled
attribute, or wires one item and forgets the label, and now the app has a
control that lies.

So the honesty is the CONTRACT, and it is tested. Every one of the assertions
below has now been through the cycle it was written for: Save as file went live
in 2a, Toga copy/paste in 2b-ii, and RUN in 3b-ii -- and each time the
assertion that said otherwise was rewritten in the same commit that made it
untrue, never deleted and never silenced. A red test here means "you changed
what this panel promises", which is the moment to check that the promise still
matches the code.

What the contract says NOW is the harder version of the same idea: the panel
must not merely avoid lying about what it can do, it must be accurate about
what running actually MEANS. Run is live, so the claims under test moved from
"this button admits it does nothing" to "this button says whether your code is
confined, and Expert admits that it is not".

The rest is structure the other pieces depend on: the panel registry needs the
view registered, the command palette needs data-view, and the cache-bust tool
needs ide.js referenced the way it references everything else.
"""
import io
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

_fails = []


def ok(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n            -> " + str(detail)) if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def _read(*p):
    return io.open(os.path.join(*p), encoding="utf-8").read()


def _js_code_only(src):
    """ide.js with its comments removed.

    The first draft of this file checked for the strings "monaco",
    "codemirror" and "innerHTML" in ide.js and failed -- on the header comment
    that explains why none of them are used. A test that cannot tell prose
    from code will fail hardest on the best-documented file, which is exactly
    backwards. Same fix as _code_only() in test_report_mechanism.py.

    Deliberately simple: strips /* */ and //, and is string-literal aware only
    enough not to eat a `//` inside a quoted string. It is looking for a
    library reference, not parsing JavaScript.
    """
    out = []
    i, n = 0, len(src)
    quote = None
    while i < n:
        c = src[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1]); i += 2; continue
            if c == quote:
                quote = None
            i += 1; continue
        if c in "\"'`":
            quote = c; out.append(c); i += 1; continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = (j + 2) if j != -1 else n
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = j if j != -1 else n
            continue
        out.append(c); i += 1
    return "".join(out)


HTML = _read(_ROOT, "frontend", "index.html")
IDEJS_RAW = _read(_ROOT, "frontend", "js", "ide.js")
IDEJS = _js_code_only(IDEJS_RAW)
GAMES = _read(_ROOT, "frontend", "js", "games.js")
CSS = _read(_ROOT, "frontend", "css", "styles.css")
MAIN = _read(_HERE, "main.py")

print("\n=== 1. The tab exists and is addressable ===")
ok("there is a fifth game-tab for the IDE",
   HTML.count('class="game-tab"') + HTML.count('class="game-tab active"') >= 5,
   "the panel should have 5 tabs: 3 games, socials, IDE")
ok('the IDE tab carries data-view="ide"', 'data-view="ide"' in HTML,
   "command-palette.js selects tabs by data-view, not by onclick text")
ok("every game-tab carries a data-view",
   HTML.count('class="game-tab"') + HTML.count('class="game-tab active"')
   == HTML.count('data-view="'),
   "a tab without data-view is unreachable from the command palette")
ok("the IDE tab has an aria-label", 'aria-label="\U0001F6E0 IDE and display"' in HTML,
   "icon-only buttons need an accessible name")
ok("...and a data-tip, not a title attribute", 'data-tip="IDE + Display"' in HTML,
   "house convention: data-tip; title= is not exposed consistently")

print("\n=== 2. The three rows are present ===")
for el in ("ide-view", "ide-display", "ide-editor",
           "ide-expand", "ide-run", "ide-menu", "ide-menu-btn"):
    ok("#%s exists" % el, ('id="%s"' % el) in HTML)
ok(".ide-bar exists", 'class="ide-bar"' in HTML,
   "the menu bar is a layout container, addressed by class")
ok("the output element is a <pre>, so whitespace survives",
   re.search(r'<pre[^>]*id="ide-output"', HTML) is not None)
ok("the editor is a textarea",
   re.search(r'<textarea[^>]*id="ide-editor"', HTML, re.S) is not None)
ok("the editor has a label", 'for="ide-editor"' in HTML)

print("\n=== 3. THE HONESTY CONTRACT ===")
run_tag = re.search(r'<button[^>]*id="ide-run".*?</button>', HTML, re.S)
ok("the Run button was found", run_tag is not None)
if run_tag:
    tag = run_tag.group(0)
    # Phase 3b-ii: Run went LIVE. The three assertions that used to live here
    # required it to be disabled, to say "not available" and to promise a
    # "later build". All three were true when written and are false now, so
    # they were rewritten here rather than removed -- the contract did not go
    # away when the button started working, it got a harder job.
    ok("Run is ENABLED", "disabled" not in tag,
       "the executor defects are fixed and /api/ide/run exists")
    ok("...and calls ideRun()", "ideRun()" in tag)
    ok("...and no longer promises a later build",
       "later build" not in tag.lower() and "not available" not in tag.lower())
    ok("...and its tooltip says what running actually does to your machine",
       "confined" in tag.lower(),
       "the default modes confine the run; a Run button that does not say so "
       "is the same class of omission as the old sandbox claim")
    ok("...naming the three limits rather than just the word",
       all(w in tag.lower() for w in ("network", "programs", "data folder")),
       '"confined" on its own is a word, not a promise anyone can check')

print("\n=== 3b. WHAT EXPERT MODE ADMITS ===")
# The Expert dialog could only start saying this once it was TRUE -- which is
# 3b-ii, not before. Until the confined runner existed there was nothing for
# Expert to remove, and a dialog claiming otherwise would have been the exact
# false-isolation problem the sandbox wording had.
_dlg = IDEJS.split("function confirmExpert")[1].split("\n  /*")[0] \
    if "function confirmExpert" in IDEJS else ""
ok("the Expert dialog was found", bool(_dlg))
ok("it says Expert REMOVES the confinement",
   "removes the confinement" in _dlg.lower(),
   "Expert's real cost is not just 'Toga may press Run' -- it is that Run "
   "then reaches further")
ok("...and says what the other modes give you instead",
   "CONFINED_MEANS" in _dlg,
   "the same sentence as the button and the blurb, from one variable, so the "
   "three cannot drift into slightly different promises")
ok("...and says confinement comes back on the way down",
   "comes back" in _dlg.lower())
ok("the ladder blurbs name confinement too",
   IDEJS.count("confined") >= 2 or IDEJS.count("Confined") >= 2)
ok("Cancel is never disabled",
   'id="ide-expert-cancel">Cancel<' in _dlg
   and "ide-expert-cancel\").disabled" not in _dlg,
   "the way out of a scary dialog is never the thing you have to earn")

menu = re.search(r'<div class="ide-menu"[^>]*>.*?</div>\s*</div>', HTML, re.S)
ok("the menu was found", menu is not None)
if menu:
    m = menu.group(0)
    ok("the menu offers Toga copy/paste", "Allow Toga to Copy/Paste" in m)
    ok("the menu offers Save as file", "Save as file" in m)

    # Phase 2a: Save as file went LIVE, so the assertion that said otherwise
    # changed in the same commit that made it untrue. That is the whole point
    # of writing the promise down as a test -- see this file's docstring.
    save_item = re.search(r'<button[^>]*id="ide-save".*?</button>', m, re.S)
    ok("the Save item was found", save_item is not None)
    if save_item:
        si = save_item.group(0)
        ok("Save as file is ENABLED", "disabled" not in si,
           "it is wired to /api/downloads/save, which now has an allowlist")
        ok("...and calls ideSaveAs()", "ideSaveAs()" in si)
        ok("...and no longer claims to arrive later", "next build" not in si)

    toga_item = [b for b in re.findall(r'<button[^>]*>.*?</button>', m, re.S)
                 if "Allow Toga" in b]
    ok("the Toga item was found", len(toga_item) == 1)
    if toga_item:
        ti = toga_item[0]
        # Phase 2b-ii WIRED this. The markup still ships disabled because
        # Beginner is the default mode, but ide.js enables it the moment the
        # mode allows -- so the note is a STATE now ("on"/"off"), not an
        # excuse. These assertions changed in the commit that made the old
        # ones untrue, which is the whole point of writing them down.
        ok("it is a real toggle now", "ideToggleTogaClip()" in ti,
           "it was inert through Phase 1 and 2b-i")
        ok("...with a pressed state for screen readers",
           'aria-pressed="false"' in ti)
        ok("...shipping disabled because Beginner is the default",
           "disabled" in ti)
        ok("...and labelled with the mode that would allow it",
           "Advanced only" in ti)
        ok("ide.js swaps the label for the real state",
           'note.textContent = on ? "on" : "off"' in IDEJS,
           "a mode-dependent excuse became an on/off state")
        ok("Beginner both disables it and forces it off",
           "var allowed = _mode !== \"beginner\"" in IDEJS
           and "var on = allowed && _togaClip" in IDEJS)

print("\n=== 3c. Save as file is wired end to end ===")
ok("ide.js defines ideSaveAs", "function ideSaveAs" in IDEJS)
ok("...and exports it", "window.ideSaveAs" in IDEJS)
ok("...posting to /api/downloads/save", "/api/downloads/save" in IDEJS)
ok("...sending the editor contents", "ta.value" in IDEJS)
ok("it asks for the filename with the in-app prompt, not window.prompt alone",
   "oraclePrompt" in IDEJS,
   "a native dialog costs the Electron window its focus -- see the "
   "unclickable-UI fix")
ok("it reports the server's own refusal text",
   "detail" in IDEJS,
   "the allowlist lives on the server; restating the rule here would let the "
   "two drift apart")
ok("it says so when the server renamed the file",
   "renamed from" in IDEJS,
   "otherwise someone goes looking for a name that is not on disk")

print("\n=== 4. It plugs into the panel registry, not around it ===")
ok("ide.js registers a view named 'ide'",
   re.search(r'register\(\s*["\']ide["\']', IDEJS) is not None,
   "PanelViews.register is how a view is hidden when another is shown")
ok("...owning #ide-view", '"ide-view"' in IDEJS)
ok("...and displayed as flex", '"flex"' in IDEJS)
ok("it does NOT set style.display on the game chrome itself",
   "game-canvas" not in IDEJS,
   "that cross-module reach is exactly what PanelViews replaced")
ok("games.js still owns the registry", "PanelViews" in GAMES)

print("\n=== 5. Offline boot: no editor library from a CDN ===")
for bad in ("monaco", "codemirror", "ace.js", "cdnjs", "unpkg", "jsdelivr"):
    ok("ide.js does not reach for %s" % bad, bad not in IDEJS.lower(),
       "offline boot is a hard requirement; the editor must ship in-tree")
ok("the editor handles Tab itself", "Tab" in IDEJS_RAW,
   "a textarea without Tab handling loses the key to focus movement")
ok("...and Escape releases the tab trap", "Escape" in IDEJS_RAW,
   "trapping Tab with no way out strands keyboard users")

print("\n=== 6. Output is text, never markup ===")
# This used to assert that innerHTML appears NOWHERE in ide.js. That was a fine
# proxy while the file had no dialogs, and it broke the moment confirmExpert()
# built one -- which is the wrong reason for this test to go red. The property
# that actually matters is narrower and permanent: whatever the display area
# receives is PROGRAM OUTPUT, and program output must never become markup.
_show = IDEJS[IDEJS.index("function ideShowOutput"):]
_show = _show[:_show.index("\n  }") + 4]
ok("ideShowOutput writes with textContent", "textContent" in _show,
   "program output is precisely the text that must not become markup")
ok("...and never with innerHTML", "innerHTML" not in _show)

# The one place that DOES build markup may only build constants.
_dlg = IDEJS[IDEJS.index("function confirmExpert"):]
_dlg = _dlg[:_dlg.index("\n  }") + 4]
ok("the Expert dialog interpolates no variables into its HTML",
   ("+ _" not in _dlg) and ("${" not in _dlg),
   "a dialog built from static strings cannot carry someone else's markup")

print("\n=== 7. Double width is one variable ===")
ok("--oracle-w-x2 is defined", "--oracle-w-x2" in CSS)
ok("the expanded rule keys off .ide-expanded",
   ".oracle-panel.visible.ide-expanded" in CSS)
ok("leaving the IDE drops back to normal width",
   "classList.remove(EXPANDED_CLASS)" in IDEJS,
   "a 600px panel around a 300px game canvas is empty space")

print("\n=== 8. The preference is per-person and allowlisted ===")
ok("GET /api/ide/prefs exists", '@app.get("/api/ide/prefs")' in MAIN)
ok("POST /api/ide/prefs exists", '@app.post("/api/ide/prefs")' in MAIN)
ok("the write is namespaced", re.search(
   r'api_set_ide_prefs.*?ui_prefs\.set\([^)]*ns=_ns', MAIN, re.S) is not None,
   "without ns= one person's width becomes everybody's")
ok("the write is allowlisted, not a passthrough",
   "_IDE_PREF_DEFAULTS" in MAIN and "for key, default in _IDE_PREF_DEFAULTS" in MAIN,
   "the payload is web content; unknown keys must not reach ui_prefs")

import ui_prefs
ok("ide_expanded is NOT a machine key",
   "ide_expanded" not in ui_prefs.MACHINE_KEYS,
   "MACHINE_KEYS is for facts a daemon needs with nobody signed in")

print("\n=== 8b. The authority ladder ===")
ok("the mode select exists", 'id="ide-mode"' in HTML)
ok("...with all three notches",
   all(('value="%s"' % m) in HTML for m in ("beginner", "advanced", "expert")))
ok("...and a label for it", 'for="ide-mode"' in HTML,
   "a bare select is unnamed to a screen reader")
ok("ide.js orders the modes", 'MODES = ["beginner", "advanced", "expert"]' in IDEJS,
   "the order IS the meaning; index comparison is the whole rule")
ok("the ladder is monotone in the UI copy",
   "read and write" in IDEJS and "AND run" in IDEJS)

ok("main.py declares the same order",
   'IDE_MODES = ("beginner", "advanced", "expert")' in MAIN,
   "two orderings would be two ladders")
ok("there is a server-side reader", "def _ide_mode(" in MAIN)
ok("...and a comparison helper", "def _ide_mode_at_least(" in MAIN,
   "every privileged path asks this instead of believing the payload")
ok("an unknown mode is rejected", 'raise HTTPException(400, "unknown mode")' in MAIN)

_post = MAIN[MAIN.index('@app.post("/api/ide/prefs")'):]
_post = _post[:_post.index("@app.", 10)]
ok("Expert is owner-gated", "_owner_gate(request)" in _post,
   "a model gaining the Run button is a system decision, not a preference")
ok("Expert demands elevation", "_demand_elevation(request)" in _post,
   "being signed in is not the same as choosing to hand a model the Run button")
ok("...and both are inside the expert branch only",
   _post.index('want == "expert"') < _post.index("_owner_gate(request)"),
   "gating the whole route would gate de-escalation too")
ok("dropping down is NOT gated",
   _post.index("_demand_elevation(request)") < _post.index('ui_prefs.set("ide_mode"'),
   "reducing your own authority must never need a ceremony")
ok("the change is audited", '"ide.mode"' in _post)
ok("GET reports whether Expert is even offerable", '"can_expert"' in MAIN)
ok("GET reports whether a password applies", '"expert_needs_password"' in MAIN,
   "single-user has no account to re-verify; the panel shows a checkbox there")

import ui_prefs as _up
ok("ide_mode is NOT a machine key", "ide_mode" not in _up.MACHINE_KEYS,
   "one mode for the whole install is the bug the cookie switch had")

print("\n=== 8c. The escalation dialog ===")
ok("there is a confirm step for Expert", "function confirmExpert" in IDEJS)
ok("...built on the in-app modal, not a native dialog", "modal-root" in IDEJS,
   "a native dialog costs the Electron window its focus")
ok("...Escape cancels", 'e.key === "Escape"' in IDEJS)
ok("...clicking outside cancels", "e.target === ov" in IDEJS)
ok("...and the checkbox path exists for single-user",
   "ide-expert-ack" in IDEJS)
ok("the password is asked through requireUnlock",
   "window.requireUnlock" in IDEJS,
   "reauth.js already handles 2FA-if-configured and single-user")
ok("leaving Expert drops the elevation", "reauthDrop" in IDEJS,
   "staying unlocked after giving up the privilege is a window nobody asked for")
ok("a refusal snaps the control back to the stored mode",
   "applyMode(prev)" in IDEJS,
   "the dropdown must show what the SERVER holds, not what was clicked")

print("\n=== 9. Cache-busting ===")
ok("ide.js is referenced with a ?v= bust",
   re.search(r'/static/js/ide\.js\?v=[0-9a-f]{10}', HTML) is not None,
   "run _bust_cache.py; test_cache_busts.py checks the value independently")

print("")
if _fails:
    print("%d CHECK(S) FAILED" % len(_fails))
    for f in _fails:
        print("   - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED - IDE + Display panel")
sys.exit(0)
