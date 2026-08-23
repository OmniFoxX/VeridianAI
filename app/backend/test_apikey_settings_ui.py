#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_apikey_settings_ui.py -- the scoped-keys block collapses, and its two
buttons share their row.

WHAT TODD ASKED FOR (2026-08-23)

    "just as the name of 3rd listed API Key says, that should be a collapsible
     list because it could get quite long and that just pushes the rest of the
     settings farther down"

    "the two buttons, Create Key and Refresh should be of equal size and they
     need to equally split that drop down menu above them, from the center and
     reach the edges, so it is esthetically pleasing, isn't smushed together or
     doesn't look like an after thought"

WHY <details> AND NOT A SCRIPTED TOGGLE

It is keyboard-operable, announced as expanded/collapsed by a screen reader,
and findable by in-page search, with no JS and no ARIA to keep in sync. A
hand-rolled toggle has to earn each of those separately and usually earns
none -- and this codebase already has a WCAG posture to hold up
(STANDARDS_ALIGNMENT.md).

WHY THE COUNT IS NOT DECORATION

These are live credentials. Collapsing them must not make a key you did not
issue easier to miss, so the one number that would tell you stays on screen
while the list itself is hidden. It follows that every path out of
loadApiKeys has to set it -- including the failures. A stale "4" above a
collapsed list that could not actually be read is worse than no number, so a
failed read shows "?" rather than falling back to 0. Zero is a claim; "?" is
the truth.

WHY flex: 1 1 0 AND NOT flex: 1

`flex: 1` leaves flex-basis at auto, so the buttons still divide the row in
proportion to their labels and "Create Key" keeps the larger half. Basis 0
divides the SPACE. That is the difference between "equal" and "nearly equal",
and it is the entire ask.

    python test_apikey_settings_ui.py
"""
import io
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_FRONTEND = os.path.join(os.path.dirname(_HERE), "frontend")

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


def _read(*p):
    return io.open(os.path.join(*p), encoding="utf-8").read()


HTML = _read(_FRONTEND, "index.html")
CSS = _read(_FRONTEND, "css", "styles.css")
JS = _read(_FRONTEND, "js", "api-key.js")

# The block this file is about, not the whole settings panel.
_BLOCK = ""
_m = re.search(r"Additional keys, scoped to one purpose(.{0,4000})", HTML, re.S)
if _m:
    _BLOCK = _m.group(1)
ok("the scoped-keys block was found in index.html", bool(_BLOCK),
   "without it every check below passes without reading anything")


# =============================================================================
print("=== 1. The list collapses ===")
# =============================================================================
ok("the key list is inside a <details>",
   "<details" in _BLOCK and 'id="apikeys-details"' in _BLOCK)
ok("...with a <summary> to operate it", "<summary" in _BLOCK)
ok("...and the list itself is inside it",
   _BLOCK.find("<details") < _BLOCK.find('id="apikeys-list"') <
   _BLOCK.find("</details>"),
   "apikeys-list must be the disclosure's content, or it collapses nothing")
ok("it ships COLLAPSED",
   not re.search(r"<details[^>]*\bopen\b", _BLOCK),
   "shipping it open reproduces the exact complaint -- the rest of Settings "
   "pushed down the panel")
ok("no hand-rolled toggle was added alongside it",
   "apikeys-details" not in JS or "classList.toggle" not in JS,
   "<details> already carries the keyboard and screen-reader behaviour; a "
   "second mechanism means two states to keep in agreement")


# =============================================================================
print("\n=== 2. Collapsing does not hide how many keys exist ===")
# =============================================================================
ok("the summary carries a count element", 'id="apikeys-count"' in _BLOCK,
   "the list is live credentials; hiding it must not hide that there are 9")
ok("the count is set from the loaded keys", "setKeyCount(keys.length)" in JS)

# EVERY exit path, because the dangerous one is the failure that leaves the
# last successful number standing above a list nobody could read.
_fn = JS.split("window.loadApiKeys")[1].split("async function revokeKey")[0] \
    if "window.loadApiKeys" in JS else ""
ok("loadApiKeys was located", bool(_fn))
_returns = len(re.findall(r"\breturn;", _fn))
_sets = len(re.findall(r"setKeyCount\(", _fn))
ok("every path out of loadApiKeys sets the count", _sets >= _returns,
   "%d setKeyCount call(s) against %d early return(s) -- one of them leaves a "
   "stale number on screen" % (_sets, _returns))
ok("a failed read shows '?' rather than 0",
   'setKeyCount("?")' in JS,
   "0 asserts you have no keys; the truth after a failed read is that we do "
   "not know, and these are credentials")
ok("creating a key opens the list",
   re.search(r"det\.open\s*=\s*true", JS) is not None,
   "the result of issuing a credential should not land inside a collapsed "
   "section whose only feedback is a number going up by one")


# =============================================================================
print("\n=== 3. The two buttons share their row evenly ===")
# =============================================================================
ok("the buttons are in their own row", 'class="apikeys-actions"' in _BLOCK)
_ai = _BLOCK.find('class="apikeys-actions"')
ok("...containing BOTH buttons",
   _ai != -1 and "Create Key" in _BLOCK[_ai:] and "Refresh" in _BLOCK[_ai:],
   "one of them left outside the row is exactly the ragged look being fixed")
ok("...and nothing else",
   _BLOCK[_ai:].count("<button") == 2 if _ai != -1 else False,
   "a third control in this row breaks the even split")

_rule = re.search(r"\.apikeys-actions\s*\{([^}]*)\}", CSS, re.S)
ok("the row's rule exists in styles.css", _rule is not None)
_body = _rule.group(1) if _rule else ""
ok("...it is a flex row", "display: flex" in _body or "display:flex" in _body)
ok("...spanning the full width", "width: 100%" in _body or "width:100%" in _body,
   "'reach the edges' is this line")

_btn = re.search(r"\.apikeys-actions\s*>\s*\.voice-extras-btn\s*\{([^}]*)\}",
                 CSS, re.S)
ok("the buttons' rule exists", _btn is not None)
_bbody = _btn.group(1) if _btn else ""
ok("...they divide the SPACE, not the text",
   re.search(r"flex:\s*1\s+1\s+0", _bbody) is not None,
   "found %r. flex:1 leaves basis auto, so 'Create Key' keeps the bigger "
   "half and the two are only nearly equal"
   % (_bbody.strip()[:80] or "nothing"))
ok("...with no implicit floor from the longer label",
   "min-width: 0" in _bbody or "min-width:0" in _bbody,
   "without this the longer button refuses to shrink below its content and "
   "the split silently stops being even on a narrow panel")


# =============================================================================
print("\n=== 4. Nothing here reduced what a person can operate ===")
# =============================================================================
ok("Create Key still calls createApiKey", "createApiKey()" in _BLOCK)
ok("Refresh still calls loadApiKeys", "loadApiKeys()" in _BLOCK)
ok("both buttons keep their tooltips",
   _BLOCK[_ai:].count("data-tip") == 2 if _ai != -1 else False,
   "these are the tips that were invisible until the layer fix; losing them "
   "now would be a quiet trade of one bug for another")
ok("the summary has a visible focus indicator",
   ".apikeys-summary:focus-visible" in CSS,
   "a new keyboard-operable control with no focus ring is a 2.4.7 fail, and "
   "STANDARDS_ALIGNMENT.md reports controls with no indicator at all")

# FOUND BY RENDERING IT, not by reading it. display:flex on a <summary> drops
# the browser's default list-item marker, so the disclosure triangle vanished
# and the row read as a plain box with no affordance that it opens. Every
# assertion above still passed: the <details> was valid, keyboard-operable and
# correctly announced. The defect was only ever visible on screen.
ok("the summary still shows a disclosure affordance",
   ".apikeys-summary::before" in CSS,
   "display:flex removes the default marker; without a replacement there is "
   "nothing on the row to say it opens")
ok("...that turns to show the open state",
   re.search(r"\.apikeys-details\[open\][^{]*::before\s*\{[^}]*rotate",
             CSS, re.S) is not None,
   "a static arrow tells you it is a disclosure but not which way it is set")
ok("...and the default markers are cleared in both engines",
   "list-style: none" in CSS and "::-webkit-details-marker" in CSS,
   "Firefox and Chromium hide the built-in marker differently; missing one "
   "leaves a stray triangle beside the drawn one")


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
