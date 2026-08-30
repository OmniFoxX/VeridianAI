#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ide_multiuser.py -- the IDE panel with more than one person on the box.

WHY THIS EXISTS

"Most installs are single-user" is the sentence this project has decided not to
finish. Commercial licences are the paid path, so a non-owner is not the edge
case -- and every gate in the IDE panel was written owner-first, which is
exactly the direction that produces a surface nobody tested from the other
side.

So this is the panel driven as ALICE and BOB, neither of whom owns the machine,
and the questions are the ones that only have answers when there are two people:

  * can a non-owner give a model the Run button?          (must be: no)
  * is one person's mode another person's mode?           (must be: no)
  * does one person's code land in another person's dirs? (must be: no)
  * can one person's Stop kill another person's run?      (must be: no)

The last one is not hypothetical. The run registry is keyed by namespace, and
"keyed by namespace" is a claim about a dict that is trivial to get wrong and
invisible until two people press buttons at once.

    python test_ide_multiuser.py
"""
import asyncio
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_fails = []


def ok(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n            -> " + str(detail)) if detail and not cond else ""))
    if not cond:
        _fails.append(name)


try:
    from fastapi import HTTPException
except Exception as e:                                    # pragma: no cover
    print("SKIP: fastapi not available here (%s)" % e)
    sys.exit(0)

import main                                               # noqa: E402
import sage_engine as se                                  # noqa: E402
import ui_prefs                                           # noqa: E402
import ns_guard                                           # noqa: E402


class _Req:
    """One request, standing in for one signed-in person."""
    def __init__(self, who):
        self.who = who
    client = type("c", (), {"host": "127.0.0.1"})()
    headers = {}
    cookies = {}


_OWNER, _ALICE, _BOB = _Req(None), _Req("alice"), _Req("bob")

_CFG = {"code_exec_enabled": True, "code_exec_timeout": 30}
_AUDIT = []

main._effective_config = lambda ns=None: dict(_CFG)
main._session_ns = lambda request: request.who
main._safe_ns = lambda ns: ns
main._is_owner = lambda request: request.who is None
main._audit_api_action = lambda request, action, detail=None: _AUDIT.append(
    (getattr(request, "who", "?"), action, detail))
# Single-user reauth is a no-op by design; the owner gate is the thing under
# test here, and it runs FIRST.
main._demand_elevation = lambda request: None
main._elevation_applies = lambda request: False


# =============================================================================
print("=== 1. A non-owner cannot hand a model the Run button ===")
# =============================================================================
for req, who in ((_ALICE, "alice"), (_BOB, "bob")):
    try:
        asyncio.run(main.api_set_ide_prefs({"mode": "expert"}, req))
        ok("%s is refused Expert" % who, False, "it was granted")
    except HTTPException as e:
        ok("%s is refused Expert" % who, e.status_code == 404,
           "status was %s" % e.status_code)

# Written as `ok(..., True, ...)` first time round, which is a label wearing a
# test's clothes -- the same "or True" shape this project has now found three
# times. The real assertion is about the SOURCE: the gate must be _owner_gate,
# whose refusal is the uniform 404 cloak, and not a hand-rolled 403 that tells
# a stranger the surface exists.
_prefs_src = open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()
_prefs_src = _prefs_src.split("async def api_set_ide_prefs")[1].split(
    "\n@app.")[0]
ok("the refusal is the 404 CLOAK, not a hand-rolled 403",
   "_owner_gate(request)" in _prefs_src and "403" not in _prefs_src,
   "matches _require_owner and the WAN guard: no hint the surface exists")

# The owner still can, or the gate is just broken.
_r = asyncio.run(main.api_set_ide_prefs({"mode": "expert"}, _OWNER))
ok("the owner still can", _r.get("mode") == "expert", _r)
asyncio.run(main.api_set_ide_prefs({"mode": "beginner"}, _OWNER))

ok("a non-owner is TOLD Expert is not offerable",
   asyncio.run(main.api_get_ide_prefs(_ALICE)).get("can_expert") is False,
   "so the panel says 'owner only' instead of presenting a choice that 404s")
ok("...and the owner is told it is",
   asyncio.run(main.api_get_ide_prefs(_OWNER)).get("can_expert") is True)


# =============================================================================
print("\n=== 2. One person's mode is not another's ===")
# =============================================================================
asyncio.run(main.api_set_ide_prefs({"mode": "advanced"}, _ALICE))
asyncio.run(main.api_set_ide_prefs({"mode": "beginner"}, _BOB))
ok("alice is Advanced", main._ide_mode("alice") == "advanced")
ok("bob is still Beginner", main._ide_mode("bob") == "beginner",
   "ide_mode must not be a MACHINE key")
ok("...and so is the owner", main._ide_mode(None) == "beginner")

ok("ide_mode is not in MACHINE_KEYS",
   "ide_mode" not in getattr(ui_prefs, "MACHINE_KEYS", ()),
   "a machine key would make one person's ladder everybody's ladder")

# The expanded/toga_clip prefs too -- same store, same trap.
asyncio.run(main.api_set_ide_prefs({"expanded": True}, _ALICE))
ok("alice's expanded pref is hers alone",
   asyncio.run(main.api_get_ide_prefs(_ALICE))["expanded"] is True
   and asyncio.run(main.api_get_ide_prefs(_BOB))["expanded"] is False)


# =============================================================================
print("\n=== 2b. Expert does not outlive the session ===")
# =============================================================================
# THIS SECTION EXISTS BECAUSE THE CHECK FAILED THE FIRST TIME IT WAS RUN.
#
# The plan's Phase 4 line was "escalate, de-escalate, sign out, sign back in --
# confirm Expert does not survive". It did survive: the ladder lived in
# ui_prefs, which outlives everything, so escalating on Monday and signing out
# handed the next person to sign in a model that could write into an executable
# buffer and press Run, unconfined, with nothing to confirm. That is exactly
# what the password gate was for, arriving through the preference store.
#
# Expert is now held in memory for the process and dropped on sign-out.
asyncio.run(main.api_set_ide_prefs({"mode": "expert"}, _OWNER))
ok("the owner is in Expert", main._ide_mode(None) == "expert")
ok("...and Toga may run", main._ide_mode_at_least(None, "expert") is True)
ok("but Expert never reached DISK",
   ui_prefs.get("ide_mode", "beginner", ns=None) != "expert",
   "what is stored is at most Advanced; anything persisted comes back")

main._ide_set_expert_live(None, False)                     # signing out
ok("signing out drops it to Advanced", main._ide_mode(None) == "advanced")
ok("...and Toga may NOT run any more",
   main._ide_mode_at_least(None, "expert") is False)
ok("...and a fresh read agrees",
   asyncio.run(main.api_get_ide_prefs(_OWNER))["mode"] == "advanced",
   "the panel must not paint a notch the server will refuse")

# An install upgraded from before this fix already has "expert" on disk.
ui_prefs.set("ide_mode", "expert", ns=None)
ok("a stored 'expert' from an older build is clamped, not honoured",
   main._ide_mode(None) == "advanced",
   "reading it back would restore the authority this exists to drop")
asyncio.run(main.api_set_ide_prefs({"mode": "beginner"}, _OWNER))

# And the same for a named profile, since that is where sign-out is real.
asyncio.run(main.api_set_ide_prefs({"mode": "advanced"}, _ALICE))
main._ide_set_expert_live("alice", True)
ok("alice in Expert does not make bob Expert",
   main._ide_mode("alice") == "expert" and main._ide_mode("bob") != "expert")
main._ide_set_expert_live("alice", False)
ok("...and alice's sign-out leaves her at Advanced",
   main._ide_mode("alice") == "advanced")

_logout_src = open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()
_logout_src = _logout_src.split("async def api_auth_logout")[1].split(
    "\n@app.")[0]
ok("logout actually drops it -- the wiring, not just the helper",
   "_ide_set_expert_live" in _logout_src,
   "a helper nothing calls on sign-out is a fix that never runs")
ok("...before the session is destroyed",
   _logout_src.index("_ide_set_expert_live")
   < _logout_src.index("destroy_session"),
   "the namespace comes from the session; afterwards there is nothing to ask")


# =============================================================================
print("\n=== 3. Separate scratch directories, separately proven ===")
# =============================================================================
_wa = str(se._confine_workdir("alice"))
_wb = str(se._confine_workdir("bob"))
_wo = str(se._confine_workdir(None))
ok("alice and bob get different run directories", _wa != _wb, (_wa, _wb))
ok("...and neither is the owner's", _wo not in (_wa, _wb), (_wo, _wa))
ok("each sits under that profile's own data root",
   os.path.join("users", "alice") in _wa
   and os.path.join("users", "bob") in _wb, (_wa, _wb))

# Not "the paths differ" -- the FILES do. A shared directory with two names
# would satisfy the string comparison above and fail this.
_MARK = "ns-probe-%d" % os.getpid()
se.execute_python_confined(
    "open('who.txt','w').write(%r)" % (_MARK + "-alice"), timeout=30, ns="alice")
se.execute_python_confined(
    "open('who.txt','w').write(%r)" % (_MARK + "-bob"), timeout=30, ns="bob")
_ra = se.execute_python_confined("print(open('who.txt').read())",
                                 timeout=30, ns="alice")
_rb = se.execute_python_confined("print(open('who.txt').read())",
                                 timeout=30, ns="bob")
ok("alice's run reads back alice's file", (_MARK + "-alice") in _ra, _ra[:120])
ok("bob's run reads back bob's file", (_MARK + "-bob") in _rb, _rb[:120])
ok("...so the two runs did not share a working directory",
   (_MARK + "-bob") not in _ra and (_MARK + "-alice") not in _rb)


# =============================================================================
print("\n=== 4. A confined run cannot reach the data folder at all ===")
# =============================================================================
# The deny root is DATA_DIR, so this covers reading ANOTHER PROFILE's files
# without needing a per-user rule -- alice's run cannot see sage_data, and
# bob's directory is inside sage_data.
_deny = (se._confine_deny_roots() or [None])[0]
ok("there is a deny root to test against", bool(_deny), _deny)
if _deny:
    _peek = se.execute_python_confined(
        "import os\nprint(os.listdir(r'%s'))" % os.path.join(_deny, "users"),
        timeout=30, ns="alice")
    ok("alice cannot even LIST the users directory",
       "users" not in _peek or "confined runner" in _peek, _peek[:160])
    _steal = se.execute_python_confined(
        "print(open(r'%s').read())" % os.path.join(_wb, "who.txt"),
        timeout=30, ns="alice")
    ok("alice cannot read bob's file by absolute path",
       (_MARK + "-bob") not in _steal, _steal[:160])
    ok("...and is told why, rather than getting a bare error",
       "confined runner" in _steal, _steal[:160])


# =============================================================================
print("\n=== 5. One person's Stop does not touch another's run ===")
# =============================================================================
# Unique per invocation -- see the note in test_ide_run.py. A fixed name means
# a stale file satisfies the wait loop instantly and hands back the race the
# marker exists to remove, and deleting it first makes "can this environment
# unlink?" a precondition of a test about stopping subprocesses.
_STAMP = "%d_%d" % (os.getpid(), int(__import__("time").time() * 1000))
_FOREVER_A = os.path.join(_wa, "_started_a_%s.tmp" % _STAMP)
_FOREVER_B = os.path.join(_wb, "_started_b_%s.tmp" % _STAMP)


def _forever(mark):
    return ("import time\n"
            "open(r'%s','w').write('1')\n"
            "while True:\n"
            "    time.sleep(0.05)\n" % mark.replace("\\", "\\\\"))


async def _cross_stop():
    ta = asyncio.ensure_future(
        main.api_ide_run({"code": _forever(_FOREVER_A)}, _ALICE))
    tb = asyncio.ensure_future(
        main.api_ide_run({"code": _forever(_FOREVER_B)}, _BOB))
    for _ in range(600):
        await asyncio.sleep(0.05)
        if os.path.exists(_FOREVER_A) and os.path.exists(_FOREVER_B):
            break
        if tb.done() or ta.done():
            break
    # A registry keyed by anything but the namespace makes bob's run collide
    # with alice's and 409. That is the FAILURE this section exists to catch,
    # so it is reported as one -- an uncaught HTTPException here would abort
    # the file and take the three sections after it down with a stack trace,
    # which is a worse way to learn the same thing. (Verified by keying the
    # registry on a constant and watching this line fire.)
    for t, who in ((ta, "alice"), (tb, "bob")):
        if t.done() and t.exception() is not None:
            for other in (ta, tb):
                other.cancel()
            with main._IDE_RUNS_LOCK:
                main._IDE_RUNS.clear()
            return ("collided:%s:%s" % (who, t.exception()), None, None, None)
    both = len(main._IDE_RUNS)
    # Alice stops. Bob must not notice.
    await main.api_ide_stop(_ALICE)
    ra = await ta
    still = "bob" in main._IDE_RUNS
    await main.api_ide_stop(_BOB)
    rb = await tb
    return both, ra, still, rb


_both, _ra2, _bob_still, _rb2 = asyncio.run(_cross_stop())
if isinstance(_both, str):
    ok("two people can have a run at the same time", False, _both)
    ok("alice's stop stopped alice", False, "not reached")
    ok("...and bob's run was untouched by it", False, "not reached")
    ok("bob's own stop then stops bob", False, "not reached")
    ok("the registry drains completely", False, "not reached")
else:
    ok("two people can have a run at the same time", _both == 2, _both)
    ok("alice's stop stopped alice", _ra2["status"] == "stopped", _ra2["status"])
    ok("...and bob's run was untouched by it", _bob_still is True,
       "a registry keyed by anything but the namespace would have killed both")
    ok("bob's own stop then stops bob", _rb2["status"] == "stopped",
       _rb2["status"])
    ok("the registry drains completely", not main._IDE_RUNS, main._IDE_RUNS)


# =============================================================================
print("\n=== 6. A non-owner can still USE the thing ===")
# =============================================================================
# A gate that refuses everything is not a gate, it is an outage.
_r = asyncio.run(main.api_ide_run({"code": "print(2 + 2)"}, _ALICE))
ok("alice can run code", "4" in _r["output"], _r["output"][:120])
ok("...confined, because she is not in Expert and cannot be",
   _r["confined"] is True and _r["mode"] == "advanced", _r)
ok("...and it is audited AS HER",
   any(w == "alice" and a == "ide.run" for w, a, _d in _AUDIT),
   "an audit line that cannot say who ran it answers the wrong question")


# =============================================================================
print("\n=== 7. Downloads land in the right person's folder ===")
# =============================================================================
_da = se.downloads_dir_for("alice")
_db = se.downloads_dir_for("bob")
ok("alice and bob have different downloads folders", str(_da) != str(_db))
_sa = se.save_to_downloads("ns_probe.txt", "alice-was-here", ns="alice")
ok("a save as alice succeeds", _sa.get("success") is True, _sa)
ok("...and lands in alice's folder",
   (_da / "ns_probe.txt").exists() and not (_db / "ns_probe.txt").exists(),
   "one shared downloads dir is the export-containment bug in a new coat")
try:
    (_da / "ns_probe.txt").unlink()
except OSError:
    pass


# =============================================================================
print("\n=== 8. A crafted namespace never becomes a path ===")
# =============================================================================
for bad in ("../../etc", "C:\\Windows", "\\\\server\\share", "a/b",
            "alice\nbob", "x" * 65):
    try:
        se.user_data_dir(bad)
        ok("rejected: %r" % bad, False, "it built a path")
    except Exception as e:
        ok("rejected: %r" % bad,
           isinstance(e, getattr(ns_guard, "InvalidNamespace", Exception)),
           "%s: %s" % (type(e).__name__, e))
ok("a legitimate namespace still works",
   se.user_data_dir("alice") is not None)


# Best effort, and best effort ONLY: the markers carry unique names so
# nothing depends on these succeeding. This just avoids leaving litter in
# a directory the person can open and look at.
for _leftover in [_FOREVER_A, _FOREVER_B]:
    try:
        os.unlink(_leftover)
    except OSError:
        pass

print("")
if _fails:
    print("%d CHECK(S) FAILED" % len(_fails))
    for f in _fails:
        print("   - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED - IDE multi-user")
sys.exit(0)
