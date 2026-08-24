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
import time
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
   "devmode.arm" in _dev and "devmode.disarm" in _dev,
   "turning it ON reveals daemon and model-server terminals; who asked is "
   "worth recording, and arming is now the moment that matters")
ok("...and turning it ON arms a window rather than flipping a switch",
   "devmode.arm(" in _code_only(_devbody),
   "a console spawned windowless has no window to reveal, so ON cannot take "
   "effect where it is thrown")
ok("...and turning it OFF disarms", "devmode.disarm()" in _code_only(_devbody))
ok("...and it records that the arm was placed during THIS session",
   "armed_here" in _code_only(_devbody),
   "or shutdown clears the window on the way out of the very quit the person "
   "was told to perform")

_get = _body_src(_func("api_get_devmode"))
ok("the devmode GET reports active and armed separately",
   "devmode.status(" in _code_only(_get),
   "merging 'terminals are up' with 'the next start will show them' is how "
   "this feature earned its reputation")


# =============================================================================
print("\n=== 3b. The launch owns Developer Mode, start to finish ===")
# =============================================================================
_boot = _body_src(_func("_apply_devmode_on_startup"))
ok("startup asks whether THIS launch is a Developer Mode session",
   "begin_launch()" in _code_only(_boot))
ok("...and hides leftovers when it is not",
   "set_consoles_visible(False)" in _code_only(_boot),
   "a previous session that was killed rather than quit leaves its terminals "
   "on screen -- 'if they didn't shut VeridianAI down, the terminals stay up'")

_shut = _body_src(_func("_end_devmode_on_shutdown"))
ok("quitting ends the Developer Mode session", "end_launch(" in _code_only(_shut))
ok("...unless this session armed the next one",
   "armed_here" in _code_only(_shut),
   "arm-then-quit is the prescribed flow; clearing it here would break the "
   "one person following the instructions")

# The startup path must NOT consume the arm: tier_launcher reads it from a
# different process at line 470 of start.bat, before the backend is up.
ok("startup does not consume the arm",
   "disarm()" not in _code_only(_boot),
   "clearing it here would depend on winning a race against tier_launcher, "
   "and losing means the consoles spawn hidden for the one launch somebody "
   "went to the trouble of arming")

_gate = _body_src(_func("_session_gate"))
ok("the session gate does not touch Developer Mode",
   "devmode" not in _code_only(_gate).lower(),
   "per-profile application was removed with the model it belonged to")


# =============================================================================
print("\n=== 3c. A Developer Mode session cannot end at the login screen ===")
# =============================================================================
# The terminals belong to the LAUNCH. Signing out leaves them running for
# whoever sits down next -- and they may be minimised and forgotten rather than
# obviously on screen, which is how this goes unnoticed. So the two ways of
# leaving are made the same thing.
_out = _body_src(_func("api_auth_logout"))
ok("logout reports whether quitting is required",
   "quit_required" in _code_only(_out),
   "only the backend knows whether this launch is a Developer Mode one")
ok("...decided by the LAUNCH, not by who signed out",
   "_DEVMODE_LAUNCH" in _code_only(_out),
   "a per-session answer would be the model that was already withdrawn")
ok("...and it still hides what it can, for the no-Electron case",
   "set_consoles_visible(False)" in _code_only(_out),
   "a browser pointed at localhost cannot be asked to quit; hiding is the one "
   "direction that works live")

_PRELOAD = _read(os.path.join(os.path.dirname(_HERE), "electron"), "preload.js")
_EMAIN = _read(os.path.join(os.path.dirname(_HERE), "electron"), "main.js")
ok("the quit channel is allowlisted in the preload bridge",
   "veridian-devmode-quit" in _PRELOAD,
   "the bridge is allowlist-only by design; an unlisted channel is silently "
   "dropped and the app would simply never quit")
ok("...and carries no payload",
   "Payload-less" in _PRELOAD or "payload-less" in _PRELOAD,
   "main.js must decide what quitting means; a channel taking an argument "
   "hands web content something it should not have")

# THE FAILURE THAT COST A ROUND OF TESTING, and the reason it was invisible.
#
# preload.js ships inside app.asar; the renderer is served live from frontend/
# by the Python backend. So a new page runs against whatever shell was last
# packaged -- and the first attempt at this feature shipped a renderer that
# asked for a channel the installed shell had never heard of. send() dropped
# it and returned, exactly as designed, with no way for the page to tell.
# Dialog appeared, sign-out happened, app did not quit, nothing said why.
ok("the bridge advertises what it can deliver",
   "supportedChannels" in _PRELOAD,
   "without this, 'refused' and 'delivered' are the same observation from the "
   "renderer's side")
ok("...and send() reports whether it actually sent",
   "return true" in _PRELOAD and "return false" in _PRELOAD)
ok("...and the allowlist is one list, enforced and advertised",
   _PRELOAD.count("ALLOWED_SEND") >= 3,
   "two copies would drift, and the advertised one is the half nobody would "
   "notice was wrong")
ok("main.js quits on it", "veridian-devmode-quit" in _EMAIN
   and "app.quit()" in _EMAIN)
ok("...and quitting is what tears the tiers down",
   "before-quit" in _EMAIN and "stopBackend" in _EMAIN,
   "closing the window alone would leave the consoles running -- they do not "
   "belong to it")

_AUTH_JS = _read(os.path.join(os.path.dirname(_HERE), "frontend", "js"),
                 "auth.js")
_lo = _js_code_only(_AUTH_JS).split("async function logout")[1][:1800] \
    if "async function logout" in _AUTH_JS else ""
ok("the sign-out path was found", bool(_lo))
ok("...it asks before quitting the whole app",
   "oracleConfirm" in _lo,
   "a Sign out button that silently closes VeridianAI is the same defect as "
   "every other control this release had to fix: doing something other than "
   "what it says")
ok("...declining leaves them signed in",
   "if (!go) return" in _lo,
   "the confirmation has to be real; a dialog whose No does nothing is worse "
   "than no dialog")
ok("...and the server's answer overrides the pre-check",
   "body.quit_required" in _lo,
   "the state is read before signing out because it is gone afterwards, but "
   "the authoritative answer arrives with the response")
ok("...it does not reload back into an app that is leaving",
   "send(\"veridian-devmode-quit\")" in _lo and "return;" in _lo)
ok("...it checks the shell can DELIVER the channel before trusting it",
   "_canSendToShell(" in _lo,
   "an older shell drops an unknown channel silently -- sending blind looked "
   "exactly like succeeding, so the fallback never ran")
ok("...and says something useful when it cannot",
   "alert(" in _lo,
   "no Electron, or a shell that predates the channel; silently returning to "
   "the login screen with terminals open is the bug, not the fallback")

_AUTH_CODE = _js_code_only(_AUTH_JS)
ok("the capability check treats a missing list as NO",
   "supportedChannels" in _AUTH_CODE and "return false" in _AUTH_CODE,
   "an older shell cannot answer; assuming yes is what produced a quit that "
   "never happened")


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

_dm = _SC.split("async function setDevMode")[1][:2600] \
    if "async function setDevMode" in _SC else ""
ok("setDevMode was found", bool(_dm))
ok("...it renders the server's answer, not the requested one",
   "renderDevMode(d)" in _dm)
ok("...it reverts the checkbox on failure", "box.checked = !enabled" in _dm)
ok("...and reports the failure visibly", "setStatusError" in _dm)
ok("...it confirms BEFORE starting the clock",
   _dm.find("oracleConfirm") < _dm.find("fetch(\"/api/devmode\""),
   "the window starts when they click OK, so the request must go out on "
   "acceptance -- not before, and not on the toggle moving")
ok("...and a cancelled dialog leaves the switch off",
   "box.checked = false" in _dm,
   "saying no to the dialog while the checkbox stays on is the same lie in a "
   "smaller place")
# Case-insensitive: the point is that the sentence is THERE, not how it is
# capitalised. The first version demanded "Signing out is not enough" and went
# red on "(signing out is not enough)" -- an assertion about prose, failing on
# prose, while the instruction it was checking for was present and correct.
_dml = _dm.lower()
ok("the dialog spells out the steps",
   "quit" in _dml and "5-minute" in _dml
   and "signing out is not enough" in _dml,
   "the whole design depends on the person knowing to quit and restart; if "
   "that is only in a tooltip, it may as well not be anywhere")


# =============================================================================
print("\n=== 5. What was deliberate stayed deliberate ===")
# =============================================================================
# The deadline is read by tier_launcher, overseer_daemon and tier_lifecycle --
# three separate processes, none with a signed-in user. It must therefore stay
# a MACHINE key, and the per-profile preference had to go: it described a model
# that could not be implemented, because the consoles are spawned before
# anybody has signed in.
ok("the deadline is a MACHINE key",
   "developer_mode_until" in UIP and "MACHINE_KEYS" in UIP,
   "tier_launcher reads it at import, in its own process, with no user to ask")
ok("the per-profile preference is gone",
   "developer_mode_pref" not in DEV and "developer_mode_pref" not in UIP,
   "it promised something the spawn model cannot deliver; leaving it in place "
   "would be a stored value nothing consults")
ok("the spawn-time question is still answered by is_enabled()",
   "def is_enabled(" in DEV,
   "tier_launcher.py, overseer_daemon.py and tier_lifecycle.py all call this; "
   "renaming it would silently take Developer Mode away from all three")
ok("...and it is now purely a function of the deadline",
   "armed_until() > _now()" in _code_only(DEV),
   "any stored boolean could be left true by a crash and hand the next person "
   "somebody else's terminals -- an instant in the past cannot")

ok("the UI tells people the actual procedure",
   "quit VeridianAI" in HTML and "Signing out is not enough" in HTML,
   "this design asks for a specific sequence; if it is not written where the "
   "switch is, nobody performs it")
ok("...and no longer claims it is machine-wide or per profile",
   "whole computer" not in HTML and "follows your profile" not in HTML,
   "both were written today and both are now wrong -- it belongs to a LAUNCH")

# The rename sweep that missed _TITLE_HINTS. Todd counted the windows: "the
# live hide/show only affected the 3 llama terminals, not the other 5".
ok("the console title hints know the product's current name",
   '"veridianai"' in DEV,
   "the 2026-08-14 OracleAI -> VeridianAI sweep never reached this tuple, so "
   "the main VeridianAI console matched nothing and was never hidden")
ok("...and still know the old one",
   '"oracleai"' in DEV,
   "installs predating the rename still have consoles titled that way")
ok("hiding may match our own process tree; showing may not",
   "not visible and pid.value in our_pids" in DEV,
   "the python.exe and Ollama consoles carry default command-line titles that "
   "match no hint anyone could write down. Hiding one that is already hidden "
   "does nothing; SHOWING by pid would reveal deliberately-windowless "
   "processes as blank terminals, which is the bug the title rule was for")
ok("the plugin overlay sits beside ui_prefs, not in the install dir",
   "plugin_state.json" in PM_SRC and "DATA_DIR" in PM_SRC)
ok("...and the reason is written next to it",
   "STATE_DIR" in PM_SRC,
   "the rule config.json was moved under in v2.13; losing the reason is how "
   "this got missed the first time")


# =============================================================================
print("\n=== 6. The arm window, exercised against the clock ===")
# =============================================================================
# Everything above reads the source. This runs it, because the whole design is
# a claim about time and a claim about time is worth actually testing.
_T2 = Path(tempfile.mkdtemp(prefix="vai_dev_"))
try:
    import ui_prefs as _uip
    import devmode as _dev
    _orig = _uip._data_dir
    _uip._data_dir = lambda: _T2                      # redirect the store

    ok("it starts off", _dev.is_enabled() is False)

    _dev.arm(seconds=60, by="alice")
    ok("arming turns the spawn-time answer on", _dev.is_enabled() is True,
       "this is what tier_launcher asks as it starts")
    ok("...and records who did it", _dev.armed_by() == "alice")
    ok("...with a real deadline in the future",
       50 <= _dev.seconds_left() <= 60, _dev.seconds_left())

    # THE ONE THAT MATTERS: the deadline is an INSTANT, not a countdown owned
    # by a running process. Nothing here is running; the stored value alone
    # decides, which is why it survives the app being shut down.
    _st = json.loads((_T2 / "ui_prefs.json").read_text(encoding="utf-8"))
    ok("the deadline is stored as an absolute time",
       isinstance(_st.get("developer_mode_until"), (int, float))
       and _st["developer_mode_until"] > time.time(),
       _st)

    # Wind the clock past it, without waiting five minutes.
    _uip.set("developer_mode_until", time.time() - 1)
    ok("a lapsed window is simply off", _dev.is_enabled() is False,
       "nothing has to run for this to expire -- an instant in the past is "
       "in the past whether VeridianAI is open or not")
    ok("...and reports no time left", _dev.seconds_left() == 0)

    # A launch inside the window is a dev session; one outside it is not, and
    # begin_launch must NOT consume the arm on the way past.
    _dev.arm(seconds=60, by="alice")
    ok("a launch inside the window is a Developer session",
       _dev.begin_launch().get("active") is True)
    ok("...and the arm is still there for tier_launcher to read",
       _dev.is_enabled() is True,
       "the backend and tier_launcher are separate processes; consuming here "
       "would race the one thing that actually opens the consoles")

    # Quitting ends it -- unless this session armed the NEXT one.
    _dev.end_launch(arm_placed_this_session=False)
    ok("quitting ends the session", _dev.is_enabled() is False)

    _dev.arm(seconds=60, by="bob")
    _dev.end_launch(arm_placed_this_session=True)
    ok("...but quitting right after arming leaves the window open",
       _dev.is_enabled() is True,
       "arm-then-quit IS the prescribed flow; clearing it here would break "
       "the one person following the instructions exactly")

    _dev.disarm()
    ok("disarming clears it", _dev.is_enabled() is False)
    _st2 = json.loads((_T2 / "ui_prefs.json").read_text(encoding="utf-8"))
    ok("...and leaves nothing behind that could switch it back on",
       not any(v for k, v in _st2.items()
               if k.startswith("developer_mode") and v not in (0, "", None)),
       _st2)
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
