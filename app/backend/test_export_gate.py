#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_export_gate.py -- signed in is not the same as allowed to take it out.

WHAT WAS WRONG

Print, Export and Burn were reachable by anyone sitting at an unlocked,
signed-in app. Burn asked you to type BURN, which proves INTENT and says
nothing about IDENTITY. Export asked nothing at all -- and its inventory
response lists every section with file counts and sizes, so merely opening the
panel handed over a map of the target.

The threat is not a remote attacker; it is the lent laptop, the borrowed
account, the curious child who does not understand that the button is
permanent. Todd's framing, and it is the right one.

THE RULES, AND WHY EACH IS SHAPED THIS WAY

  * The gate is on the ACTION, not the button. printChat() is reachable from
    the toolbar AND from the command palette; a check on the toolbar's onclick
    would be walked around with Ctrl-K. Section 5 asserts the gate sits inside
    the function every path already calls -- the sixth time this release that
    "right code, wrong coverage" would otherwise have shipped.

  * The prompt comes BEFORE the panel. Not inside it. Counts and sizes are the
    thing being withheld.

  * A second factor ONLY for accounts that have one. mfa.enabled_methods()
    decides. Nobody is asked for a code they cannot produce, and nobody with a
    code can skip it.

  * A growing delay, never a lockout. The abuse guard on /api/auth/login bans
    an IP for 15 minutes after 6 failures, which is right for the front door of
    a network service and wrong for a person re-typing their own password to
    print their own notes.

  * Elevation dies with the session. Not on a timer of its own -- hung off
    session._forget_locked, the single choke point every removal already passes
    through, so "expires on logout" is structural rather than remembered.

    A NOTE ON THE EXPIRY TEST BELOW. It first appeared to fail: 1.2s after a
    1-second session, elevation still read as live. The session did too --
    get_session compares integer seconds with `<`, so at t+1.2s the session was
    genuinely still valid and elevation outliving it was not what happened.
    Verified at t+2.5s, where both are gone and in the right order. Written up
    because a boundary artifact misread as a bug is how real bugs get
    manufactured to fix it.

    python test_export_gate.py
"""
import ast
import io
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


MAIN = io.open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()
SESS = io.open(os.path.join(_HERE, "session.py"), encoding="utf-8").read()
_TREE = ast.parse(MAIN)

_FE = os.path.join(os.path.dirname(_HERE), "frontend")


def _fe(*parts):
    return io.open(os.path.join(_FE, *parts), encoding="utf-8").read()


REAUTH_JS = _fe("js", "reauth.js")
CHAT_JS = _fe("js", "chat.js")
DEX_JS = _fe("js", "data-export.js")
PALETTE_JS = _fe("js", "command-palette.js")
HTML = _fe("index.html")


def _fn(name, tree=None):
    for n in ast.walk(tree or _TREE):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and n.name == name:
            return n
    return None


def _routes():
    out = {}
    for n in ast.walk(_TREE):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in n.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) \
                    and isinstance(d.func.value, ast.Name) \
                    and d.func.value.id == "app" and d.args \
                    and isinstance(d.args[0], ast.Constant):
                out.setdefault(d.args[0].value, []).append((d.func.attr, n))
    return out


ROUTES = _routes()


def _src(node):
    return ast.get_source_segment(MAIN, node) or ""


# =============================================================================
print("=== 1. The endpoints exist and are local-only ===")
# =============================================================================
for _p, _m in (("/api/reauth", "post"),
               ("/api/reauth/status", "get"),
               ("/api/reauth/drop", "post")):
    _hit = [n for (mm, n) in ROUTES.get(_p, []) if mm == _m]
    ok("%s %s is registered" % (_m.upper(), _p), bool(_hit))
    if _hit:
        ok("...local-only", "_is_local_client(request)" in _src(_hit[0]))


# =============================================================================
print("\n=== 2. Elevation is bound to the session, not to a timer ===")
# =============================================================================
ok("elevation lives in session.py", "_ELEVATED" in SESS)
_forget = _fn("_forget_locked", ast.parse(SESS))
ok("_forget_locked drops it", _forget is not None
   and "_ELEVATED.pop" in (ast.get_source_segment(SESS, _forget) or ""),
   "logout, expiry and password change ALL funnel through this one function; "
   "a separate expiry loop would eventually miss one of them")
# "Never persisted" asserted STRUCTURALLY. The first version of this searched
# SESS.lower() for the literal "_ELEVATED" -- which lowercasing had just turned
# into "_elevated", so the split found nothing and the test crashed rather than
# failed. It was a weak proxy even when it worked ("no 'json' within 400
# characters" proves very little). The real property is that session.py does no
# file I/O AT ALL: it says so in its own docstring, and that is what keeps
# sessions, data keys and elevations off the disk together.
_sess_imports = set()
for _n in ast.walk(ast.parse(SESS)):
    if isinstance(_n, ast.Import):
        _sess_imports.update(a.name.split(".")[0] for a in _n.names)
    elif isinstance(_n, ast.ImportFrom) and _n.module:
        _sess_imports.add(_n.module.split(".")[0])
# atrest and access_policy ARE imported (function-locally) and are fine:
# atrest.forget_profile_key REMOVES a key from memory -- the opposite of
# persisting -- and access_policy.admin_granted only reads. Listing them as
# forbidden was a guess about this file rather than a fact about it, and it
# went red on correct code. Forbid the modules that actually serialise.
ok("session.py imports nothing that serialises to disk",
   not (_sess_imports & {"io", "json", "pathlib", "shelve", "pickle",
                         "sqlite3", "csv", "configparser"}),
   sorted(_sess_imports))
ok("...and opens no files",
   not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
           and n.func.id == "open" for n in ast.walk(ast.parse(SESS))),
   "an elevation that survived a restart is one nobody granted -- and the "
   "same rule is what keeps session tokens and data keys off the disk")

import session as S                                             # noqa: E402

ok("the TTL is five minutes", S.ELEVATION_TTL == 300, S.ELEVATION_TTL)
_t = S.create_session({"username": "gate_t", "ns": "gate_ns",
                       "is_owner": False})
ok("a live session can be elevated", S.elevate(_t) is True)
ok("...and reports time remaining, not just a flag",
   S.elevation_remaining(_t) > 250, S.elevation_remaining(_t))
ok("an unknown token cannot be elevated", S.elevate("not-a-token") is False,
   "otherwise elevation could be minted for a session that does not exist")
ok("it can be handed back early", S.drop_elevation(_t) is True)
ok("...and is then gone", S.is_elevated(_t) is False)

S.elevate(_t)
S.destroy_session(_t)
ok("LOGOUT ends it", S.is_elevated(_t) is False,
   "the one Todd called disastrous if missed")

# Session expiry. See the docstring: compared at t+2.5s, not t+1.2s, because
# integer-second truncation means a 1s session is still legitimately alive at
# t+1.2s -- and so is its elevation, correctly.
_e = S.create_session({"username": "gate_t", "ns": "gate_ns",
                       "is_owner": False}, ttl=1)
S.elevate(_e)
time.sleep(2.5)
ok("SESSION EXPIRY ends it too", S.is_elevated(_e) is False)
ok("...and the session is gone as well", S.get_session(_e) is None,
   "elevation must never be the thing keeping a dead session interesting")


# =============================================================================
print("\n=== 3. The gate is applied, and applied EARLY ===")
# =============================================================================
for _path, _method, _label in (
        ("/api/export/inventory", "get", "export inventory"),
        ("/api/export", "post", "export build"),
        ("/api/burn", "post", "burn")):
    _hit = [n for (mm, n) in ROUTES.get(_path, []) if mm == _method]
    ok("%s is gated" % _label,
       bool(_hit) and "_require_elevated(request)" in _src(_hit[0]),
       "a UI-only gate is theatre; this is the half that holds against curl")

_inv = [n for (mm, n) in ROUTES.get("/api/export/inventory", []) if mm == "get"]
if _inv:
    _b = _src(_inv[0])
    ok("the inventory gate runs BEFORE the inventory is read",
       _b.index("_require_elevated") < _b.index("data_export.inventory"),
       "the counts and sizes ARE the sensitive part -- refusing after "
       "computing them protects nothing")
    ok("...and the refusal carries no counts",
       "sections=[], total_bytes=0" in _b)

_burn = [n for (mm, n) in ROUTES.get("/api/burn", []) if mm == "post"]
if _burn:
    _b = _src(_burn[0])
    ok("burn still requires the typed confirmation as well",
       'payload.get("confirm") != "BURN"' in _b,
       "intent AND identity, not one instead of the other")


# =============================================================================
print("\n=== 4. Policy: second factor only if there is one; delay, not lock ===")
# =============================================================================
_ra = [n for (mm, n) in ROUTES.get("/api/reauth", []) if mm == "post"]
_rb = _src(_ra[0]) if _ra else ""
ok("the password is checked against the SESSION's user, not a supplied one",
   "_users.verify_user(_user," in _rb,
   "accepting a username from the body would let a caller re-auth as somebody "
   "else entirely")
ok("a second factor is required only when configured",
   "_mfa.enabled_methods(_user)" in _rb and "if _methods:" in _rb)
ok("recovery codes are accepted as the second factor",
   "verify_recovery" in _rb,
   "a lost phone must not mean lost access to your own export")
ok("a MISSING code is not counted as a failure",
   "_reauth_note_failure" not in _rb.split("if not _code:")[1].split("return")[0]
   if "if not _code:" in _rb else False,
   "otherwise merely opening the dialog throttles the user")
ok("granting an elevation is audited",
   '"reauth.granted"' in _rb)
ok("...and so is every failure", '"reauth.failed"' in _rb)

# The backoff, run for real. Lifted from the shipped source so it cannot drift.
_WANT_NAMES = {"_REAUTH_FAILS", "_REAUTH_MAX_DELAY"}
_WANT_FUNCS = {"_reauth_delay_left", "_reauth_note_failure", "_reauth_clear"}

_ns = {}
_pieces = []
_got = set()
for _n in _TREE.body:
    # BOTH Assign and AnnAssign. `_REAUTH_FAILS: dict = {}` is an AnnAssign,
    # so an Assign-only collector skipped it silently and every extracted
    # function then died on NameError deep inside the run -- a missing piece
    # masquerading as broken code.
    _tgts = []
    if isinstance(_n, ast.Assign):
        _tgts = [t.id for t in _n.targets if isinstance(t, ast.Name)]
    elif isinstance(_n, ast.AnnAssign) and isinstance(_n.target, ast.Name):
        _tgts = [_n.target.id]
    if set(_tgts) & _WANT_NAMES:
        _pieces.append(_src(_n))
        _got.update(set(_tgts) & _WANT_NAMES)
    if isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
            and _n.name in _WANT_FUNCS:
        _pieces.append(_src(_n))
        _got.add(_n.name)

# Checked BEFORE the exec, so an incomplete lift is reported as an incomplete
# lift rather than surfacing as a NameError from inside the extracted code.
ok("every piece of the backoff was lifted out of main.py",
   _got == (_WANT_NAMES | _WANT_FUNCS),
   "missing: %s" % sorted((_WANT_NAMES | _WANT_FUNCS) - _got))
# ONE dict, as globals. Passing separate globals/locals put _REAUTH_FAILS in
# the locals mapping while the functions resolved names against globals, so
# every call raised NameError -- the classic exec() footgun.
_ns["time"] = time
exec(compile("\n\n".join(_pieces), "<main.py extract>", "exec"), _ns)

_waits = [_ns["_reauth_note_failure"]("victim") for _ in range(12)]
ok("the delay grows", _waits[0] < _waits[3] < _waits[6], _waits)
ok("...and is CAPPED, so it never becomes a lockout",
   max(_waits) <= _ns["_REAUTH_MAX_DELAY"], _waits)
ok("...at something a human can wait out",
   _ns["_REAUTH_MAX_DELAY"] <= 60, _ns["_REAUTH_MAX_DELAY"])
ok("a correct password clears the penalty entirely",
   (_ns["_reauth_clear"]("victim"),
    _ns["_reauth_delay_left"]("victim"))[1] == 0,
   "12 fat-fingered attempts must not leave someone locked out of their own "
   "data once they get it right")


# =============================================================================
print("\n=== 5. The gate is in the ACTION, not on the button ===")
# =============================================================================
# THE ASSERTION THAT MATTERS. A check on the toolbar's onclick would pass a
# naive test and be bypassed entirely by the command palette.
ok("the command palette really can reach printChat",
   "safeCall('printChat')" in PALETTE_JS,
   "if this ever stops being true the reasoning below still holds, but this "
   "is the concrete second entry point that made it necessary")
ok("printChat itself is gated",
   "async function printChat()" in CHAT_JS
   and "requireUnlock" in CHAT_JS.split("function printChat()")[1][:600],
   "gated inside the function every caller already goes through")
ok("the export PANEL is gated before it is built",
   "async function openPanel" in DEX_JS
   and DEX_JS.index("requireUnlock") < DEX_JS.index("buildModal();"),
   "the panel lists counts and sizes -- that is the map of the target")
ok("burn is gated after its confirmation, before the fetch",
   "requireUnlock" in CHAT_JS
   and CHAT_JS.index("Burn permanently destroys") < CHAT_JS.index('"/api/burn"'),
   "asked after BURN is typed, so somebody who was never going through with "
   "it is not asked for a password first")
ok("a locked inventory does not read as 'Nothing stored yet'",
   "needs_reauth" in DEX_JS and "Locked." in DEX_JS,
   "an empty list because it is withheld is not an empty list because it is "
   "empty, and saying so would be a plain lie about the user's own data")
ok("an expired unlock mid-panel is retried, not reported as a failure",
   "Your unlock expired" in DEX_JS,
   "five minutes is short on purpose; the timeout must not surface as "
   "'Export failed'")


# =============================================================================
print("\n=== 6. The prompt itself ===")
# =============================================================================
ok("reauth.js is loaded", 'js/reauth.js' in HTML)
ok("requireUnlock is the single entry point",
   "window.requireUnlock" in REAUTH_JS)
ok("it never stacks two prompts", "_open" in REAUTH_JS)
ok("the 2FA field is hidden for accounts without one",
   "code.hidden = !opts.needsCode" in REAUTH_JS)
ok("...and revealed if the server says otherwise mid-flow",
   "d.needs_code" in REAUTH_JS,
   "the status call and the verify call can disagree; believe the one that "
   "actually checked")
ok("a recovery code is offered as an alternative",
   "recovery" in REAUTH_JS)
ok("the wait is counted down rather than shown as a dead button",
   "Try again in" in REAUTH_JS)
ok("it says how long the unlock lasts",
   "minutes" in REAUTH_JS and "sign out" in REAUTH_JS,
   "a lock that appears to open forever is worse than no lock")
# Asserted as an ASSIGNMENT, not as the word. The first version was
# '"innerHTML" not in REAUTH_JS' and went red on the comment that says
# "never innerHTML for copy" -- a check tripping over the note explaining why
# the thing it forbids is absent. That exact shape has now bitten this project
# in test_reasoning_surfaced (which grew a _code_only() helper for it), in the
# at-rest call-site guard, and in the os.replace ordering check.
ok("copy is inserted as text, never markup",
   "e.textContent = text" in REAUTH_JS
   and re.search(r"\.innerHTML\s*=", REAUTH_JS) is None,
   "found: %r" % (re.findall(r".{0,40}\.innerHTML\s*=.{0,20}", REAUTH_JS),))
ok("a status-call failure fails OPEN, and says why",
   "Fail OPEN" in REAUTH_JS,
   "the real boundary is the endpoint; locking someone out of printing "
   "their own notes over a hiccup would add nothing")


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
