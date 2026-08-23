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
DEV = _read(_HERE, "devmode.py")


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
   "devmode.is_enabled(" in _code_only(_devbody),
   "echoing the request back is how a write that did not land gets reported "
   "as one that did")
ok("...and it writes the preference against the signed-in profile",
   "ns=_ns" in _code_only(_devbody),
   "without an ns the choice lands on the machine and the next person "
   "inherits it -- which is the report")

_get = _body_src(_func("api_get_devmode"))
ok("the devmode GET reports THIS profile's own setting",
   "_session_ns(request)" in _code_only(_get),
   "otherwise the switch shows what the last person to use the machine chose")


# =============================================================================
print("\n=== 3b. Every sign-in path re-points the desktop ===")
# =============================================================================
# Sessions are minted at four separate call sites. Hooking them individually is
# four chances to miss one, and the likeliest miss is the MFA path -- the
# accounts most likely to care. So it is done once, in the middleware every
# request already passes through.
_mints = len(re.findall(r"_session\.create_session\(", _code_only(MAIN)))
_gate = _body_src(_func("_session_gate"))
ok("the session gate was found", bool(_gate))
ok("...and it applies the arriving profile's preference",
   "apply_for(" in _code_only(_gate),
   "found %d create_session call sites; wiring them one by one is how a path "
   "gets missed" % _mints)
ok("...only when the profile actually changed",
   "_DEVMODE_APPLIED" in _code_only(_gate),
   "this runs on every request; re-applying each time would enumerate every "
   "window in the OS on every call")

_out = _body_src(_func("api_auth_logout"))
ok("signing out hides the terminals",
   "set_consoles_visible(False)" in _code_only(_out),
   "one person's daemon and model-server logs must not be left on the login "
   "screen for whoever sits down next")
ok("...without erasing what that profile chose",
   "developer_mode" in _code_only(_out)
   and "developer_mode_pref" not in _code_only(_out),
   "the mirror is what is applied; the preference is theirs and must survive "
   "so signing back in restores it")


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
# Developer Mode is per profile as of v2.16.2, WITHOUT breaking the thing that
# made it machine-scoped in the first place. Two values, two questions:
# developer_mode_pref is whose choice it is; developer_mode is what is applied
# to the desktop right now, which is what a daemon reads at spawn time with
# nobody signed in. The machine key must therefore SURVIVE -- dropping it to
# make the preference per-user is the change that would look like a fix and
# leave tier_launcher asking an unanswerable question.
ok("the applied state is still a MACHINE key",
   '"developer_mode"' in UIP and "MACHINE_KEYS" in UIP,
   "tier_launcher reads this in a daemon where there is no user to ask")
ok("...and the per-profile preference is NOT one",
   "developer_mode_pref" not in UIP,
   "a machine key ignores ns by design, so a preference listed there would "
   "silently go on being shared -- the bug wearing the fix's clothes")
ok("devmode stores the preference per profile",
   "_PREF_KEY" in DEV and "ns=ns" in DEV)
# Asked of the CODE, not of the docstring.
#
# The first version of this searched devmode.py for the sentence "Deliberately
# does not fall back" and went red on correct code, because the sentence wraps
# across a line. It deserved to fail for a better reason than that: a comment
# saying a thing is not the thing. What matters is that the ns branch reads the
# preference and never consults the machine key -- and section 6 then proves
# the behaviour by running it, which is the assertion that actually holds.
_ie = ""
for _n in ast.walk(ast.parse(DEV)):
    if isinstance(_n, ast.FunctionDef) and _n.name == "is_enabled":
        _ie = ast.get_source_segment(DEV, _n) or ""
# Bounded by INDENTATION, not by a line count. A fixed three-line window
# swallowed the statement after the branch -- the machine-value fallback, which
# reads _KEY for exactly the right reason -- and reported the correct code as
# wrong. "The next few lines" is not the same as "this branch".
_nsbranch = ""
_lines = _code_only(_ie).splitlines()
for _i, _l in enumerate(_lines):
    if _l.strip() == "if ns:":
        _ind = len(_l) - len(_l.lstrip())
        _body = []
        for _nxt in _lines[_i + 1:]:
            if _nxt.strip() and (len(_nxt) - len(_nxt.lstrip())) <= _ind:
                break
            _body.append(_nxt)
        _nsbranch = "\n".join(_body)
ok("the per-profile branch reads the preference", "_PREF_KEY" in _nsbranch,
   _nsbranch or "no `if ns:` branch found in is_enabled")
ok("...and never consults the machine value for a profile",
   bool(_nsbranch) and "_KEY" not in _nsbranch.replace("_PREF_KEY", ""),
   "inheriting whatever the owner last chose is the precise bug being fixed, "
   "and ui_prefs refuses the same fallback for the same reason")
ok("...and the UI now describes it as the person's own",
   "follows your profile" in HTML and "whole computer" not in HTML,
   "the machine-wide wording was true for about an hour and is now false")
ok("the plugin overlay sits beside ui_prefs, not in the install dir",
   "plugin_state.json" in PM_SRC and "DATA_DIR" in PM_SRC)
ok("...and the reason is written next to it",
   "STATE_DIR" in PM_SRC,
   "the rule config.json was moved under in v2.13; losing the reason is how "
   "this got missed the first time")


# =============================================================================
print("\n=== 6. Two profiles keep two answers, exercised ===")
# =============================================================================
# Read off the source everything above is asserting ABOUT. This part runs it.
_T2 = Path(tempfile.mkdtemp(prefix="vai_dev_"))
try:
    import ui_prefs as _uip
    import devmode as _dev
    _orig = _uip._data_dir
    _uip._data_dir = lambda: _T2                      # redirect the store

    _dev.set_enabled(True, ns="alice")
    _dev.set_enabled(False, ns="bob")

    ok("alice keeps her own answer", _dev.is_enabled("alice") is True)
    ok("bob keeps his, unaffected by hers", _dev.is_enabled("bob") is False,
       "this is the report: one profile's choice overriding another's")

    # Order matters -- bob wrote last, so the MIRROR is his. The preference
    # must not be.
    ok("...and the machine mirror holds what was last APPLIED",
       _dev.is_enabled() is False,
       "the daemon reads this with nobody signed in and needs a real answer")
    ok("...while alice's preference is still hers",
       _dev.is_enabled("alice") is True,
       "if the mirror overwrote her preference, per-profile would be a "
       "relabelling of the same shared value")

    # A profile that has never touched it gets the DEFAULT, not an inheritance.
    ok("a new profile does not inherit anyone's setting",
       _dev.is_enabled("carol") is False,
       "ui_prefs refuses this fallback for the same reason: inheriting the "
       "owner's choice on first sign-in is the leak the split exists to close")

    # Where the files actually landed. A per-profile key that quietly went to
    # the shared file would pass every check above by accident.
    _shared = json.loads((_T2 / "ui_prefs.json").read_text(encoding="utf-8"))
    ok("the shared file holds ONLY the applied mirror",
       set(_shared) == {"developer_mode"}, _shared)
    _alice = json.loads(
        (_T2 / "users" / "alice" / "ui_prefs.json").read_text(encoding="utf-8"))
    ok("...and the preference is under the profile's own directory",
       _alice.get("developer_mode_pref") is True, _alice)
finally:
    try:
        _uip._data_dir = _orig
    except Exception:
        pass
    shutil.rmtree(_T2, ignore_errors=True)


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
