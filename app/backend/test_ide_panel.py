#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ide_panel.py -- the IDE + Display panel says what it can actually do.

WHY THIS EXISTS

Phase 1 of this panel ships a Run button that does not run anything, and two
menu items that are not wired yet. That is a deliberate, staged decision -- the
executor they would call has three defects that get fixed first -- but it is
also exactly the shape that rots: someone tidies the "temporary" disabled
attribute, or wires one item and forgets the label, and now the app has a
control that lies.

So the honesty is the CONTRACT, and it is tested. When execution lands, these
assertions should FAIL and be updated in the same commit that makes them
untrue. A red test here means "you changed what this panel promises" -- which
is the moment to check that the promise still matches the code.

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

print("\n=== 3. THE HONESTY CONTRACT (update these when Run lands) ===")
run_tag = re.search(r'<button[^>]*id="ide-run".*?</button>', HTML, re.S)
ok("the Run button was found", run_tag is not None)
if run_tag:
    tag = run_tag.group(0)
    ok("Run is disabled", "disabled" in tag,
       "Phase 1 ships no executor; an enabled Run would be a lie")
    ok("...and its accessible name says so",
       "not available" in tag.lower(),
       'aria-label should tell a screen-reader user why it cannot be pressed')
    ok("...and its tooltip explains when", "later build" in tag.lower(),
       "a disabled control should say what would make it work")

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
        ok("Toga copy/paste is still disabled", "disabled" in ti,
           "the Beginner/Advanced/Expert modes that govern it do not exist yet")
        ok("...and still says when it arrives", "next build" in ti)

print("\n=== 3b. Save as file is wired end to end ===")
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
ok("the output element is written with textContent",
   "textContent" in IDEJS and "innerHTML" not in IDEJS,
   "program output is precisely the text that must not become markup")

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
print("ALL CHECKS PASSED - IDE + Display panel (Phase 1 shell)")
sys.exit(0)
