#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_auth_elevation.py -- the second factor guards the locks, not just the door.

THE OBSERVATION BEHIND THIS (Todd, 2026-08-22)

    "It being called Two-Factor Authentication implies that in order for one
     to be authenticated for entry to a profile that two separate factors be
     satisfied, and we're only doing one while allowing for the other that
     only protects after the fact."

The trigger turned out to be a stale entry in an authenticator app, left over
from a deleted profile -- login gating was working the whole time, and this
file proves it rather than assuming it. But the question it prompted found
something real:

    Turning TOTP OFF required only the password.

Which is backwards. The password is the factor an attacker is most likely to
already have; the second factor exists for exactly that case. A second factor
that guards the front door but not the lock on the front door is half a factor.

Worse, one row undercut the export gate shipped hours earlier: minting an API
token needed only a session, and API tokens are deliberately exempt from that
gate. Creating one from an unlocked session was a way straight around it.

WHAT IS GATED, AND WHAT IS DELIBERATELY NOT

Gated: anything that GRANTS access, WEAKENS authentication, or acts on another
account. Not gated: adding a factor (making yourself safer must never need a
ceremony), revoking a token (giving up a privilege is unilateral -- the same
reasoning as profile_keys.disable_recovery), and every read-only status call.

SECTION 2 IS THE ONE THAT MATTERS. It does not check a list of endpoints
against itself; it requires EVERY /api/auth route to be classified, so an
endpoint added later lands in neither list and fails. Six times this release a
correct mechanism shipped with a gap in its coverage. A list that only checks
what someone remembered to add to it is that same bug wearing a test's clothes.

    python test_auth_elevation.py
"""
import ast
import io
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


MAIN = io.open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()
_TREE = ast.parse(MAIN)
_FE = os.path.join(os.path.dirname(_HERE), "frontend")
REAUTH_JS = io.open(os.path.join(_FE, "js", "reauth.js"), encoding="utf-8").read()


def _src(n):
    return ast.get_source_segment(MAIN, n) or ""


def _routes():
    out = []
    for n in ast.walk(_TREE):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in n.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) \
                    and isinstance(d.func.value, ast.Name) \
                    and d.func.value.id == "app" and d.args \
                    and isinstance(d.args[0], ast.Constant):
                out.append((d.func.attr.upper(), d.args[0].value, n))
    return out


ROUTES = _routes()


# =============================================================================
print("=== 1. Login still gates on the second factor ===")
# =============================================================================
# Proved empirically against a throwaway enrolled account before any of this
# was written; pinned here so a refactor cannot quietly undo it.
_login = [n for (m, p, n) in ROUTES
          if p == "/api/auth/login" and m == "POST"]
ok("there is exactly ONE login endpoint", len(_login) == 1,
   "a second way in is a second place to forget the second factor")
_lb = _src(_login[0]) if _login else ""
ok("it asks whether the account has a second factor",
   "_mfa.enabled_methods(" in _lb)
ok("...and returns a CHALLENGE instead of a session",
   '"mfa_required": True' in _lb and "create_session" in _lb.split('"mfa_required"')[1],
   "the session must be minted after the challenge, never before it")
_before = _lb.index('"mfa_required": True')
_after = _lb.index("_session.create_session")
ok("...with the challenge return BEFORE any session is minted", _before < _after)
ok("the challenge path sets no cookie",
   "set_cookie" not in _lb[:_before],
   "a cookie handed out at the password step is a session in all but name")


# =============================================================================
print("\n=== 2. EVERY auth route is classified (the coverage assertion) ===")
# =============================================================================
# Grants access, weakens authentication, or acts on another account.
MUST_GATE = {
    ("POST", "/api/auth/mfa/totp/disable"),
    ("POST", "/api/auth/fido2/remove"),
    ("POST", "/api/auth/mfa/recovery/regenerate"),
    ("POST", "/api/auth/keys"),
    ("POST", "/api/auth/rotate-key"),
    ("POST", "/api/auth/users"),
    ("POST", "/api/auth/users/delete"),
    ("POST", "/api/auth/users/reset-password"),
    ("POST", "/api/auth/users/mfa-reset"),
    ("POST", "/api/auth/users/access"),
    ("POST", "/api/auth/users/{username}/recovery"),
}

# Each with the reason it is exempt. A reason nobody can state is a gap.
MUST_NOT_GATE = {
    ("POST", "/api/auth/login"): "the gate itself; MFA handled inline",
    ("POST", "/api/auth/logout"): "ending a session needs no proof",
    ("POST", "/api/auth/setup"): "first run; no account exists yet",
    ("POST", "/api/auth/mfa/verify"): "IS the second factor",
    ("POST", "/api/auth/fido2/verify"): "IS the second factor",
    ("POST", "/api/auth/mfa/totp/begin"): "ADDING a factor; strengthens",
    ("POST", "/api/auth/mfa/totp/confirm"): "ADDING a factor; strengthens",
    ("POST", "/api/auth/fido2/register"): "ADDING a factor; strengthens",
    ("POST", "/api/auth/keys/revoke"): "giving up access is unilateral",
    ("POST", "/api/auth/change-password"): "requires the OLD password",
    ("POST", "/api/auth/password-check"): "policy check; touches nothing",
    ("GET", "/api/auth/status"): "read-only",
    ("GET", "/api/auth/keys"): "read-only; prefixes, never secrets",
    ("GET", "/api/auth/users"): "read-only; the UI needs it to render",
    ("GET", "/api/auth/users/access"): "read-only",
    ("GET", "/api/auth/mfa/status"): "read-only",
    ("GET", "/api/auth/users/{username}/recovery"): "read-only booleans, no codes",
}

_auth_routes = {(m, p) for (m, p, n) in ROUTES if p.startswith("/api/auth")}
_unclassified = _auth_routes - MUST_GATE - set(MUST_NOT_GATE)
ok("every /api/auth route is classified", not _unclassified,
   "%r -- a new auth endpoint must be a deliberate decision, not a default. "
   "Add it to MUST_GATE, or to MUST_NOT_GATE with the reason." % (sorted(_unclassified),))

_by_route = {}
for m, p, n in ROUTES:
    _by_route.setdefault((m, p), []).append(n)


def _is_gated(key):
    return any("_demand_elevation" in _src(n) or "_require_elevated" in _src(n)
               for n in _by_route.get(key, []))


_missing = sorted(k for k in MUST_GATE if k in _auth_routes and not _is_gated(k))
ok("every route that should be gated IS gated", not _missing, _missing)
_extra = sorted(k for k in MUST_NOT_GATE if k in _auth_routes and _is_gated(k))
ok("...and none that should not be", not _extra,
   "%r -- gating one of these makes the app harder to secure, not safer" % (_extra,))

# The data surface, gated earlier today, must stay gated.
for _k in (("POST", "/api/burn"), ("POST", "/api/export"),
           ("GET", "/api/export/inventory")):
    ok("%s %s is still gated" % _k, _is_gated(_k))


# =============================================================================
print("\n=== 3. The gate runs AFTER the existing guard, never before ===")
# =============================================================================
_wrong = []
for _k in sorted(MUST_GATE):
    for _n in _by_route.get(_k, []):
        _s = _src(_n)
        if "_demand_elevation" not in _s:
            continue
        _e = _s.index("_demand_elevation")
        for _g in ("_require_owner(", "_require_session("):
            if _g in _s and _s.index(_g) > _e:
                _wrong.append((_k, _g))
ok("no endpoint asks for a password before checking you are signed in",
   not _wrong,
   "%r -- a signed-out caller should be told to sign in, not asked to "
   "re-enter a password for a session they do not have" % (_wrong,))


# =============================================================================
print("\n=== 4. The refusal is structured data, run for real ===")
# =============================================================================
_pieces = [_src(n) for n in _TREE.body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
           and n.name == "_demand_elevation"]
ok("_demand_elevation was found", len(_pieces) == 1)


class _HTTPException(Exception):
    def __init__(self, status_code, detail=None):
        self.status_code = status_code
        self.detail = detail


_ns = {"HTTPException": _HTTPException,
       "_require_elevated": lambda r: {"ok": False, "needs_reauth": True,
                                       "error": "needs your password"}}
exec(compile("\n\n".join(_pieces), "<main.py extract>", "exec"), _ns)

_raised = None
try:
    _ns["_demand_elevation"](object())
except _HTTPException as e:
    _raised = e
ok("it raises when the caller is not elevated", _raised is not None)
ok("...as 401, not 403", _raised and _raised.status_code == 401,
   "403 says 'never'; 401 says 'not yet' -- and this is a 'not yet'")
ok("...with needs_reauth as DATA, not prose",
   isinstance(_raised.detail, dict) and _raised.detail.get("needs_reauth") is True,
   "a browser matching on an error sentence breaks the day it is reworded")

_ns["_require_elevated"] = lambda r: None
_passed = True
try:
    _ns["_demand_elevation"](object())
except _HTTPException:
    _passed = False
ok("and it does NOT raise once elevated", _passed)


# =============================================================================
print("\n=== 5. One interceptor, deliberately narrow ===")
# =============================================================================
ok("the browser retries through a single wrapper", "_native" in REAUTH_JS,
   "eleven endpoints wired by hand is eleven chances to miss one, and the "
   "twelfth added next year arrives unwired")
ok("it only acts on 401", "res.status !== 401" in REAUTH_JS)
ok("...never on /api/reauth itself",
   'indexOf("/api/reauth")' in REAUTH_JS,
   "a refusal from the unlock endpoint prompting another unlock is a loop")
ok("...and only when the body can be replayed",
   'typeof body !== "string"' in REAUTH_JS,
   "re-sending a stream or FormData would send something other than what the "
   "caller wrote")
ok("it clones before reading, so the caller still gets its body",
   "res.clone()" in REAUTH_JS)
ok("it retries once, not in a loop",
   REAUTH_JS.count("return await _native(input, init);") == 1)
ok("a failure in the guard never breaks the request",
   "never let the guard break the request" in REAUTH_JS)


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
