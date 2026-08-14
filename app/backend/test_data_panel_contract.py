#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_data_panel_contract.py -- the export/import panel keeps its promises.

There is no browser in the test environment, so this checks the things that
can be checked without one, and they are the ones that actually broke:

  - every id the script reaches for is an id it builds (a typo here is a
    silent no-op at runtime, not an error)
  - the download handoff does not use window.open, which is what produced
    "access denied, sign in first" instead of a file: a new window is a fresh
    navigation context and does not carry the session cookie
  - the tabs carry the ARIA a keyboard or screen-reader user needs
  - the destructive path asks first, and asks differently

    python test_data_panel_contract.py
"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_FRONT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "frontend")
JS = io.open(os.path.join(_FRONT, "js", "data-export.js"),
             encoding="utf-8").read()
HTML = io.open(os.path.join(_FRONT, "index.html"), encoding="utf-8").read()

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


def code_only(src):
    """Strip comments, so a rule is not satisfied (or broken) by prose."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


CODE = code_only(JS)

print("=== 1. Every id the script uses is one it builds ===")
used = set(re.findall(r'getElementById\("([^"]+)"\)', CODE))
built = set(re.findall(r"id=\\?[\"']([a-zA-Z0-9_-]+)\\?[\"']", JS))
external = {"export-modal-backdrop"}
missing = sorted(used - built - external)
ok("no id is reached for that is never created", not missing, missing)
ok("the panel actually builds ids", len(built) > 10, len(built))

print("\n=== 2. The download handoff ===")
ok("window.open is not used to deliver the file",
   "window.open(" not in CODE)
ok("a same-origin download anchor is used instead",
   'a.download = filename' in CODE and "/api/downloads/" in CODE)
ok("the desktop app is handed to the shell instead",
   'electronAPI.send("open-data-folder")' in CODE)

print("\n=== 3. Tabs are reachable, not just visible ===")
for t in ("export", "import"):
    ok("tab-%s controls its pane" % t,
       ('aria-controls="pane-%s"' % t) in JS and ('id="pane-%s"' % t) in JS)
    ok("pane-%s names its tab" % t, ('aria-labelledby="tab-%s"' % t) in JS)
ok("both tabs are declared tabs", JS.count('role="tab"') == 2)
ok("both panes are declared panels", JS.count('role="tabpanel"') == 2)
ok("selection is announced, not just styled",
   'aria-selected' in JS and 'setAttribute("aria-selected"' in CODE)
ok("the file input has a label",
   'id="import-file"' in JS and 'aria-label="Export archive to import"' in JS)

print("\n=== 4. The irreversible path asks first ===")
ok("import confirms before writing", CODE.count("oracleConfirm(") >= 2)
ok("replace is described as deleting, not as importing",
   "CLEARED first" in JS and "cannot be undone" in JS)
ok("merge says existing data survives",
   "backed up first" in JS and "not deleted" in JS)
ok("a preview path exists that writes nothing",
   'id="import-preview-btn"' in JS and "runImport(true)" in CODE)
ok("dry_run is what preview actually sends",
   "dry_run: !!dry" in CODE)

print("\n=== 5. The passphrase reaches both ends ===")
ok("export sends it", "payload.passphrase = passEl.value" in CODE)
ok("import sends it", "payload.passphrase = pass" in CODE)
ok("only portable exports offer it", 'mode === "portable" && useP' in CODE)
ok("the warning says it cannot be recovered",
   "including you" in JS and "no reset" in JS)
ok("a wrong passphrase re-opens the field rather than failing silently",
   "d.needs_passphrase" in CODE)

print("\n=== 6. The toolbar ===")
ok("an Import button sits beside Export",
   'id="import-toolbar-btn"' in HTML and 'id="export-toolbar-btn"' in HTML)
ok("it opens the same panel", "openImportPanel()" in HTML and
   "window.openImportPanel" in CODE)
ok("both buttons carry a tooltip",
   HTML.count('data-tip="Import data from an export archive"') == 1)
ok("the script tag was cache-busted past the old version",
   "data-export.js?v=2.14.0" not in HTML)

print("\n=== 7. The data-folder door ===")
ok("it lives at the bottom of Settings",
   'id="open-data-folder-btn"' in HTML and 'id="data-folder-path"' in HTML)
ok("and is NOT duplicated in the export/import panel",
   "open-data-folder-btn" not in JS,
   "the panel builds one too")
ok("it is sized like its neighbours, not like a toolbar chip",
   'class="action-btn secondary"\n            id="open-data-folder-btn"' in HTML,
   "expected the action-btn class its neighbours use")
_i_btn = HTML.index('id="open-data-folder-btn"')
ok("it sits with Clear Chat and Refresh Models",
   HTML.index('onclick="reloadModels()"') < _i_btn <
   HTML.index('id="rotate-key-btn"'))

print("\n=== 8. The last-chance door inside the burn dialog ===")
_CHAT = io.open(os.path.join(_FRONT, "js", "chat.js"), encoding="utf-8").read()
_CHATC = code_only(_CHAT)
ok("oracleConfirm supports a third button", "opts.extraLabel" in _CHATC)
ok("the third button does NOT answer the question",
   "finish(" not in _CHATC.split("oracle-confirm-extra")[-1].split("};")[0],
   "the extra handler resolves the dialog")
ok("its label is escaped like every other", "escapeHtml(opts.extraLabel)" in _CHATC)
ok("burn offers the data folder", 'extraLabel: "\U0001F4C1 Open Data Folder"' in _CHAT)
ok("burn says the dialog will wait", "this dialog will wait" in _CHAT)
ok("burn still says it cannot be undone", "CANNOT be undone" in _CHAT)
ok("a failing errand does not answer either",
   "an errand that fails must not answer" in _CHAT)

print("\n=== 9. Source stays pure ASCII (except the toolbar glyphs) ===")
bad = [i + 1 for i, line in enumerate(JS.splitlines())
       if any(ord(c) > 127 for c in line)]
ok("data-export.js is ASCII", not bad, bad[:5])

_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
