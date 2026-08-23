#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_toggle_truthfulness.py -- a switch shows the state it actually has.

TWO REPORTS, ONE DEFECT (Todd, 2026-08-23)

    "the toggles in the 'Settings' tab persist states between restarts of
     VeridianAI, but the toggles in the 'Plugins' tab do not.. which I don't
     understand."

    "toggling it off in a Non-Owner account does NOT actually turn it off if
     the Owner account has developer mode toggled on."

Different subsystems, same shape: the UI asserting something the system never
agreed to. It is the third form of it this release -- the unlock prompt that
rendered behind the dialog which raised it was the first, and a plugin footer
appended twice was a fourth kind of the same disease: what is shown and what
is true drifting apart with nothing to catch it.

WHY THE PLUGIN TOGGLE DID NOT PERSIST

It wrote the enabled flag back into the plugin's own JSON, inside the INSTALL
directory. On a Store install that is C:\\Program Files\\WindowsApps, which is
read-only. The write raised, the handler printed to a console nobody watches,
and toggle_plugin returned {"status": "ok"}. Todd's Settings toggles persisted
because config.json follows STATE_DIR into sage_data -- the rule this had been
missed by since v2.13.

WHY DEVELOPER MODE IGNORED A NON-OWNER

POST /api/devmode was _owner_gate'd, so a non-owner got the uniform 404 cloak,
settings.js caught it into console.error, and the checkbox stayed where the
browser had already put it.

Developer Mode stays MACHINE-scoped and that is not a concession. There is one
desktop and one set of console windows, and tier_launcher reads the flag in a
daemon with no signed-in user to ask -- ui_prefs says a per-user answer there
"is not merely wrong, it is unanswerable". The fix is not to fake per-person
state. It is that everyone's toggle works, and the UI says what it governs.

    python test_toggle_truthfulness.py
"""
import ast
import io
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


def _read(*p):
    return io.open(os.path.join(*p), encoding="utf-8").read()


MAIN = _read(_HERE, "main.py")
PM_SRC = _read(_HERE, "plugin_manager.py")
SETTINGS = _read(os.path.join(_ROOT, "frontend", "js"), "settings.js")
HTML = _read(os.path.join(_ROOT, "frontend"), "index.html")
UIP = _read(_HERE, "ui_prefs.py")


def _code_only(src, comment="#"):
    out = []
    for line in src.splitlines():
        out.append("" if line.lstrip().startswith(comment)
                   else line.split(comment, 1)[0])
    return "\n".join(out)


def _js_code_only(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(ln.split("//", 1)[0] for ln in src.splitlines())


# =============================================================================
print("=== 1. Plugin state survives a restart, for real ===")
# =============================================================================
from plugin_manager import PluginManager                       # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="vai_plug_"))
try:
    _pdir = _TMP / "plugins"
    _pdir.mkdir()
    (_pdir / "a.json").write_text(json.dumps(
        {"id": "alpha", "name": "Alpha", "enabled": True,
         "hooks": {"append_footer": "x"}}), encoding="utf-8")
    (_pdir / "b.json").write_text(json.dumps(
        {"id": "beta", "name": "Beta", "enabled": False}), encoding="utf-8")
    _state = _TMP / "state" / "plugin_state.json"

    pm = PluginManager(_pdir, state_file=_state)
    _r = pm.toggle_plugin("alpha")
    ok("toggling reports ok", _r.get("status") == "ok", _r)
    ok("...and reports the new state", _r.get("enabled") is False, _r)

    # THE ACTUAL BUG: the shipped file must not be the thing that changed.
    _disk = json.loads((_pdir / "a.json").read_text(encoding="utf-8"))
    ok("the shipped plugin file is untouched", _disk.get("enabled") is True,
       "writing here is what failed on a read-only install, and it is also "
       "what would put package content out of step with what was signed")
    ok("the overlay was written to the state dir", _state.exists(), _state)
    ok("...and records only the id that changed",
       json.loads(_state.read_text(encoding="utf-8")) == {"alpha": False},
       _state.read_text(encoding="utf-8"))

    # RESTART. This is the whole complaint, so it is exercised, not asserted.
    pm2 = PluginManager(_pdir, state_file=_state)
    _by = {p["id"]: p for p in pm2.list_plugins()}
    ok("after a restart the change is still there",
       _by["alpha"]["enabled"] is False,
       "this is the one that was failing between restarts")
    ok("...and a plugin nobody touched keeps its shipped state",
       _by["beta"]["enabled"] is False)

    # A NEWLY SHIPPED PLUGIN must arrive in the state it shipped in, not
    # inherit an absent overlay entry as "off". ai-disclosure ships enabled;
    # an overlay that answered for ids it had never seen would silence it.
    (_pdir / "c.json").write_text(json.dumps(
        {"id": "gamma", "name": "Gamma", "enabled": True}), encoding="utf-8")
    pm3 = PluginManager(_pdir, state_file=_state)
    ok("a newly bundled plugin ships in its own default state",
       {p["id"]: p for p in pm3.list_plugins()}["gamma"]["enabled"] is True)

    # A plugin removed from the install should not leave a preference behind.
    (_pdir / "b.json").unlink()
    pm4 = PluginManager(_pdir, state_file=_state)
    pm4.toggle_plugin("gamma")
    ok("a removed plugin's preference is dropped on the next write",
       "beta" not in json.loads(_state.read_text(encoding="utf-8")),
       _state.read_text(encoding="utf-8"))

    # =========================================================================
    print("\n=== 2. A write that fails is reported as a failure ===")
    # =========================================================================
    # Read-only is awkward to fake portably; an unwritable PATH is not. A file
    # where the state directory should be makes mkdir/write raise exactly as a
    # locked-down install directory does.
    _blocked = _TMP / "blocked"
    _blocked.write_text("not a directory", encoding="utf-8")
    pm5 = PluginManager(_pdir, state_file=_blocked / "plugin_state.json")
    _before = {p["id"]: p["enabled"] for p in pm5.list_plugins()}
    _r5 = pm5.toggle_plugin("alpha")
    ok("a failed save does NOT report ok", _r5.get("status") == "error", _r5)
    ok("...and says so in words a person can read",
       bool(_r5.get("message")), _r5)
    _after = {p["id"]: p["enabled"] for p in pm5.list_plugins()}
    ok("...and the in-memory state is put back",
       _before == _after,
       "otherwise the app believes the plugin changed for the rest of the "
       "session and only a restart disagrees -- which is the original bug "
       "wearing a different hat")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)


# =============================================================================
print("\n=== 3. The endpoints pass the truth through ===")
# =============================================================================
# Located by AST, and the DOCSTRING separated from the statements.
#
# The first version of this sliced the text after the def and stripped only
# `#` comments, then went red on correct code: the docstring quotes the gate it
# removed ("THIS WAS _owner_gate(request)"), by the house rule that a removal
# should explain itself where it happened. Stripping line comments is not the
# same as reading code. The two questions here are genuinely different --
# "does this still RUN the gate" and "does it still SAY why it does not" -- so
# they are asked of the statements and of the whole function separately.
def _func(name):
    for n in ast.walk(ast.parse(MAIN)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _body_src(node):
    """Everything the function executes -- the docstring excluded."""
    if node is None:
        return ""
    body = node.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return "\n".join(ast.get_source_segment(MAIN, s) or "" for s in body)


def _full_src(node):
    return ast.get_source_segment(MAIN, node) if node else ""


_tog_n = _func("api_toggle_plugin_v2")
_toggle = _body_src(_tog_n)
ok("the plugin toggle endpoint was found", bool(_toggle))
ok("...it raises rather than returning 200 on a failed save",
   "HTTPException(" in _code_only(_toggle),
   "fetch() does not throw on a 200 with an error body, so the switch never "
   "went back")
ok("...and only mirrors the flag into sage_engine when the save took",
   'result.get("status") == "ok"' in _toggle,
   "otherwise the engine disagrees with both the manager and the disk")

_dev_n = _func("api_set_devmode")
_devbody = _body_src(_dev_n)
_dev = _full_src(_dev_n)
ok("the devmode endpoint was found", bool(_devbody))
ok("developer mode is no longer owner-only",
   "_owner_gate(" not in _code_only(_devbody),
   "this is the gate that made a non-owner's toggle do nothing at all")
ok("...and the removal is explained where it happened",
   "_owner_gate" in _dev,
   "or the next person restores the gate as a safety improvement")
ok("...it is still localhost-only",
   "_is_local_client(request)" in _code_only(_devbody),
   "opening it to every profile must not also open it to the network")
ok("...and audited",
   'devmode.set"' in _dev or "devmode.set'" in _dev,
   "turning it ON reveals daemon and model-server terminals; who asked is "
   "worth recording")
ok("...and it answers with the STORED value, not the requested one",
   "devmode.is_enabled()" in _code_only(_devbody),
   "echoing the request back is how a write that did not land gets reported "
   "as one that did")


# =============================================================================
print("\n=== 4. Neither switch renders a state the server did not confirm ===")
# =============================================================================
_SC = _js_code_only(SETTINGS)
_tp = _SC.split("async function togglePlugin")[1][:900] \
    if "async function togglePlugin" in _SC else ""
ok("togglePlugin was found", bool(_tp))
ok("...it checks the response status", "r.ok" in _tp,
   "the old catch only caught a NETWORK failure; a 403, a 404 cloak and a 500 "
   "are all successful fetches and left the switch flipped")
ok("...it puts the switch back on a refusal",
   "checkbox.checked = !checkbox.checked" in _tp)
ok("...and tells the person, somewhere they can see it",
   "setStatusError" in _tp,
   "console.error is where the first version of this bug went to hide")

_dm = _SC.split("async function setDevMode")[1][:1200] \
    if "async function setDevMode" in _SC else ""
ok("setDevMode was found", bool(_dm))
ok("...it renders the server's value, not the requested one",
   "d.enabled" in _dm)
ok("...it reverts the checkbox on failure", "box.checked = !enabled" in _dm)
ok("...and reports the failure visibly", "setStatusError" in _dm)


# =============================================================================
print("\n=== 5. What was deliberate stayed deliberate ===")
# =============================================================================
# ui_prefs' two-scope split was itself a fix for a bug Todd found the same way
# (a browser-cookie setting leaking between profiles). Making Developer Mode
# per-profile to satisfy this report would have re-broken it -- and could not
# work anyway, since a daemon reads the flag with nobody signed in.
ok("developer_mode is still declared a MACHINE key",
   '"developer_mode"' in UIP and "MACHINE_KEYS" in UIP,
   "one desktop, one set of console windows, and a daemon with no user to ask")
ok("...and the UI says the setting is machine-wide",
   "whole computer" in HTML,
   "without this, turning it off and watching someone else's windows come "
   "back is inexplicable rather than merely shared")
ok("the plugin overlay sits beside ui_prefs, not in the install dir",
   "plugin_state.json" in PM_SRC and "DATA_DIR" in PM_SRC)
ok("...and the reason is written next to it",
   "STATE_DIR" in PM_SRC,
   "the rule config.json was moved under in v2.13; losing the reason is how "
   "this got missed the first time")


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
