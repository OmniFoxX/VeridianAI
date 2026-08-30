#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ide_run.py -- the IDE Run button, end to end through the real endpoint.

WHY THIS EXISTS

Phase 3b-ii wires a button in a side panel to a subprocess. Everything that
makes that acceptable rather than reckless lives in three gates and a choice of
executor, and all four are invisible from the outside: a Run that works looks
exactly the same whether or not it asked Customs, and whether or not the child
could open a socket.

So this drives the REAL endpoint coroutines -- main.api_ide_run and
main.api_ide_stop -- with the surrounding request plumbing stubbed, and asserts
on behaviour rather than on the presence of the code that should produce it.
The distinction matters: an earlier version of the mode gate could have been
deleted entirely and every source-matching test in this project would still
have been green.

Two things are asserted by OBSERVATION, not by reading main.py:

  * a Beginner run genuinely cannot open a socket, and an Expert run genuinely
    can -- so "tied to the mode ladder" is a property of the running system
  * Stop genuinely ends the child, measured on the clock against a timeout it
    would otherwise have had to wait out

    python test_ide_run.py
"""
import asyncio
import os
import sys
import time

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


# --- the surrounding plumbing, stubbed ---------------------------------------
# Only the request-shaped things. The endpoint's own logic -- the gate order,
# the executor choice, the run registry -- is the thing under test and is not
# touched.
class _Req:
    """Enough of a Request for the helpers below, which are all stubbed."""
    client = type("c", (), {"host": "127.0.0.1"})()
    headers = {}
    cookies = {}


_CFG = {"code_exec_enabled": True, "code_exec_timeout": 30}
_MODE = {"v": "beginner"}
_AUDIT = []

main._effective_config = lambda ns=None: dict(_CFG)
main._session_ns = lambda request: None
main._safe_ns = lambda ns: ns
main._ide_mode = lambda ns: _MODE["v"]
main._audit_api_action = lambda request, action, detail=None: _AUDIT.append(
    (action, detail))


def run(code, mode="beginner", payload_extra=None):
    _MODE["v"] = mode
    body = {"code": code}
    if payload_extra:
        body.update(payload_extra)
    return asyncio.run(main.api_ide_run(body, _Req()))


# =============================================================================
print("=== 1. The consent toggle is not optional ===")
# =============================================================================
_CFG["code_exec_enabled"] = False
try:
    run("print(1)")
    ok("code execution off REFUSES the run", False, "it ran anyway")
except HTTPException as e:
    ok("code execution off REFUSES the run", e.status_code == 403,
       "status was %s" % e.status_code)
    ok("...and names the toggle that would fix it",
       "Settings" in str(e.detail) and "Code Execution" in str(e.detail),
       "a refusal that does not say what to do is a dead end")
_CFG["code_exec_enabled"] = True

try:
    run("   \n  ")
    ok("an empty editor is a 400, not an empty run", False)
except HTTPException as e:
    ok("an empty editor is a 400, not an empty run", e.status_code == 400)


# =============================================================================
print("\n=== 2. Ordinary running works ===")
# =============================================================================
r = run("print(6 * 7)")
ok("it runs and returns the output", "42" in r["output"], r["output"][:120])
ok("...with status ok", r["status"] == "ok", r["status"])
ok("...and reports which mode it ran as", r["mode"] == "beginner")
ok("a traceback comes back rather than an empty box",
   "ZeroDivisionError" in run("print(1/0)")["output"])
ok("the run is audited", any(a == "ide.run" for a, _ in _AUDIT))
ok("...recording whether it was confined",
   any(a == "ide.run" and d.get("confined") is True for a, d in _AUDIT),
   "an audit line that cannot answer 'confined or not' answers the wrong "
   "question about this endpoint")


# =============================================================================
print("\n=== 3. Confinement is TIED TO THE LADDER -- observed, not read ===")
# =============================================================================
_SOCK = "import socket\ntry:\n    socket.socket()\n    print('SOCKET OPENED')\n" \
        "except Exception as e:\n    print('refused:', type(e).__name__)\n"

r_beg = run(_SOCK, mode="beginner")
ok("Beginner cannot open a socket",
   "SOCKET OPENED" not in r_beg["output"], r_beg["output"][:160])
ok("...and the response says it was confined", r_beg["confined"] is True)

r_adv = run(_SOCK, mode="advanced")
ok("Advanced cannot either", "SOCKET OPENED" not in r_adv["output"],
   r_adv["output"][:160])
ok("...and says so", r_adv["confined"] is True)

r_exp = run(_SOCK, mode="expert")
ok("Expert CAN -- that is what Expert buys",
   "SOCKET OPENED" in r_exp["output"], r_exp["output"][:160])
ok("...and the response admits it is unconfined", r_exp["confined"] is False,
   "if this ever reports True while the socket opens, the panel is telling "
   "someone they are protected when they are not")

# The whole reason the mode is read server-side.
_PAYLOAD_LIE = run(_SOCK, mode="beginner", payload_extra={"mode": "expert"})
ok("a payload claiming Expert does NOT get Expert",
   "SOCKET OPENED" not in _PAYLOAD_LIE["output"]
   and _PAYLOAD_LIE["confined"] is True,
   "a client that says 'I am in Expert' is a client talking about itself")


# =============================================================================
print("\n=== 4. Customs sees it ===")
# =============================================================================
_seen = []
_real_inspect_tag = main.customs_daemon.inspect_tag


def _spy(action_type, content, origin, intent=None):
    _seen.append((action_type, origin))
    return _real_inspect_tag(action_type, content, origin, intent=intent)


main.customs_daemon.inspect_tag = _spy
try:
    run("print('customs')")
    ok("the run goes through Customs", len(_seen) == 1, _seen)
    ok("...as tool 'code'", _seen and _seen[0][0] == "code", _seen)
    ok('...with origin "ide"', _seen and _seen[0][1] == "ide", _seen)

    # A refusal must be visible AND must not have run anything.
    class _Bounced:
        allowed = False
        message = "code: refused at the border for testing"
        content = ""

    main.customs_daemon.inspect_tag = lambda *a, **k: _Bounced()
    # A name that has never existed, so "was it created?" is a clean question
    # and no delete has to succeed first. Same reasoning as _MARK below.
    _marker = os.path.join(se._confine_workdir(None),
                           "_customs_ran_%d_%d.txt" % (os.getpid(),
                                                       int(time.time() * 1000)))
    rb = run("open(r'%s', 'w').write('ran')" % _marker.replace("\\", "\\\\"))
    ok("a Customs refusal comes back as output, not an exception",
       rb["status"] == "refused", rb)
    ok("...carrying the reason", "border" in rb["output"], rb["output"])
    ok("...and NOTHING was executed", not os.path.exists(_marker),
       "a bounce that still runs the code is not a bounce")
finally:
    main.customs_daemon.inspect_tag = _real_inspect_tag


# =============================================================================
print("\n=== 5. Stop actually stops ===")
# =============================================================================
_CFG["code_exec_timeout"] = 120
# The program announces itself by touching a file, and the test waits for THAT
# rather than for the Popen handle to appear. The handle exists a few
# milliseconds before the interpreter has run a single line, so waiting on it
# meant stopping a child that had not printed yet -- and "output printed before
# the stop survives" failed for a reason that had nothing to do with the
# product. A sleep would have papered over it on this machine and reappeared on
# a slower one.
# UNIQUE PER INVOCATION, so nothing has to be deleted for this to be correct.
# The first version used a fixed name and unlinked it up front, which turned
# "can this environment delete files?" into a precondition of a test about
# stopping a subprocess -- and duly failed in one tree where the data folder
# does not permit unlink. A stale marker from an earlier run would also have
# satisfied the wait loop instantly and quietly restored the very race the
# marker was added to remove. A fresh name cannot go stale.
_MARK = os.path.join(se._confine_workdir(None),
                     "_ide_run_started_%d_%d.tmp" % (os.getpid(),
                                                     int(time.time() * 1000)))
_FOREVER = (
    "import time\n"
    "print('started', flush=True)\n"
    "open(r'%s', 'w').write('1')\n"
    "while True:\n"
    "    time.sleep(0.05)\n" % _MARK.replace("\\", "\\\\"))


async def _run_then_stop():
    t0 = time.time()
    task = asyncio.ensure_future(main.api_ide_run({"code": _FOREVER}, _Req()))
    for _ in range(600):
        await asyncio.sleep(0.05)
        if os.path.exists(_MARK):
            break
    stopped = await main.api_ide_stop(_Req())
    out = await task
    try:
        os.unlink(_MARK)
    except OSError:
        pass
    return time.time() - t0, stopped, out


_MODE["v"] = "beginner"
elapsed, stopped, out = asyncio.run(_run_then_stop())
ok("stop reports that it stopped something", stopped.get("stopped") is True,
   stopped)
ok("the run ends promptly, not at its timeout",
   elapsed < 30, "%.1fs against a 120s timeout" % elapsed)
ok("...with status 'stopped'", out["status"] == "stopped", out["status"])
ok("...saying so in words the person can read",
   "[STOPPED]" in out["output"], out["output"][:160])
ok("...and NOT as a bare exit code",
   "EXIT CODE" not in out["output"],
   "a killed child exits -9; that is the truth about the process and a lie "
   "about what happened -- the person pressed Stop")
ok("output printed before the stop survives",
   "started" in out["output"], out["output"][:160])
ok("the registry is empty afterwards", not main._IDE_RUNS, main._IDE_RUNS)

ok("stopping nothing is not an error",
   asyncio.run(main.api_ide_stop(_Req())).get("stopped") is False,
   "the button races the run finishing on its own; neither side should "
   "have to win")

_CFG["code_exec_timeout"] = 30


# =============================================================================
print("\n=== 6. One run at a time ===")
# =============================================================================
async def _double():
    task = asyncio.ensure_future(main.api_ide_run(
        {"code": "import time\ntime.sleep(4)\nprint('done')"}, _Req()))
    for _ in range(200):
        await asyncio.sleep(0.02)
        with main._IDE_RUNS_LOCK:
            if None in main._IDE_RUNS:
                break
    try:
        await main.api_ide_run({"code": "print('second')"}, _Req())
        second = "ran"
    except HTTPException as e:
        second = e.status_code
    await main.api_ide_stop(_Req())
    await task
    return second


_second = asyncio.run(_double())
ok("a second concurrent run is refused with 409", _second == 409, _second)


# =============================================================================
print("\n=== 7. The timeout is the configured one ===")
# =============================================================================
_CFG["code_exec_timeout"] = 2
t0 = time.time()
rt = run("import time\nwhile True:\n    time.sleep(0.05)\n")
ok("a runaway is stopped by the config timeout", rt["status"] == "timeout",
   rt["output"][:120])
ok("...at roughly the configured value, not the executor default",
   (time.time() - t0) < 25, "%.1fs" % (time.time() - t0))
_CFG["code_exec_timeout"] = 30


# =============================================================================
print("\n=== 7b. Two tracebacks, and only one of them is the person's ===")
# =============================================================================
# CodeQL alert 200, py/stack-trace-exposure, and it was a true positive.
#
# The child's stderr is a traceback of the code the PERSON wrote and pressed
# Run on. Showing it is the entire point of an IDE. VeridianAI's own executor
# blowing up is a different thing wearing the same clothes, and it used to be
# interpolated straight into the response. Both directions are asserted here,
# because fixing this by suppressing tracebacks generally would have "closed"
# the alert and broken the feature.
r_user = run("raise ValueError('the user wrote this')")
ok("the PERSON's traceback still reaches them",
   "ValueError" in r_user["output"]
   and "the user wrote this" in r_user["output"],
   r_user["output"][:160])
ok("...with the traceback body, not just the type",
   "Traceback" in r_user["output"], r_user["output"][:160])

_real_exec = se.execute_python_confined


def _explode(*a, **k):
    raise RuntimeError("SECRET-INTERNAL-DETAIL /srv/veridian/private/key.pem")


se.execute_python_confined = _explode
try:
    r_int = run("print('never gets here')")
finally:
    se.execute_python_confined = _real_exec

ok("an INTERNAL failure does not leak its message",
   "SECRET-INTERNAL-DETAIL" not in r_int["output"], r_int["output"][:200])
ok("...nor the path inside it",
   "key.pem" not in r_int["output"], r_int["output"][:200])
ok("...nor the exception type",
   "RuntimeError" not in r_int["output"], r_int["output"][:200])
ok("...but the person is told something went wrong",
   "[EXECUTION ERROR]" in r_int["output"], r_int["output"][:200])
ok("...with a correlation ref they can quote",
   "ref" in r_int["output"].lower(),
   "the full error is in the server log under that ref -- see _safe_detail")
ok("...and it is reported as an error, not a successful run",
   r_int["status"] == "error", r_int["status"])


# =============================================================================
print("\n=== 8. What the source must keep saying ===")
# =============================================================================
_src = open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()
_ep = _src.split("async def api_ide_run")[1].split("async def api_ide_stop")[0]
ok("the endpoint reads the mode from the namespace, not the payload",
   "_ide_mode(_ns)" in _ep and 'payload.get("mode")' not in _ep)
ok("the consent check comes BEFORE the executor is chosen",
   _ep.index("code_exec_enabled") < _ep.index("execute_python"))
ok("Customs comes before the executor too",
   _ep.index("inspect_tag") < _ep.index("execute_python"))
ok("Expert is the ONLY unconfined branch",
   '_confined = _mode != "expert"' in _ep)
ok("the confined call passes the namespace",
   "execute_python_confined,\n                code, _timeout, _ns" in _ep,
   "each person's scratch directory is their own")

# Best effort, and best effort ONLY: the markers carry unique names so
# nothing depends on these succeeding. This just avoids leaving litter in
# a directory the person can open and look at.
for _leftover in [_marker, _MARK]:
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
print("ALL CHECKS PASSED - IDE run/stop")
sys.exit(0)
