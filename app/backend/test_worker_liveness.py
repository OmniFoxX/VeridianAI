#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_worker_liveness.py -- a dead background thread cannot be survivable in silence.

WHAT HAPPENED (2026-08-20)

sage_daemon's periodic worker ran one consolidate job at 06:56:28 and then
stopped. The daemon carried on for 12 hours 40 minutes: process up, socket
bound, `status` answering every field correctly. And in that time CRAIID's
digest, chain verify, ops snapshot, fatigue check and context-fill jobs did not
run once. The log did not record a crash -- it simply stopped having new lines.

It was noticed only because someone read log timestamps and thought "that gap
looks wrong". Nothing in the system said so.

THREE REASONS IT STAYED INVISIBLE, each fixed here

1. The staleness canary (_craiid_canary_check) lives INSIDE the worker loop.
   A dead worker takes its own watchdog down with it. A watchdog that shares
   its subject's fate is not a watchdog.

2. Everything `status` reported was LAST-ACTION data -- last_digest_msg,
   last_consolidate_ts. A worker dead for 12 hours still has a perfectly good
   last_digest_msg. That shape of information structurally cannot report its
   own absence.

3. The loop caught `Exception`, which does not include BaseException. A
   MemoryError or SystemExit escaping ends the thread, and Python's default
   handler writes it to STDERR -- which for a windowless child process goes
   nowhere. "The log records a crash" and "the log stops" look identical from
   the outside, and we got the second.

The root cause of that specific death was never established: no traceback
survived and the process was replaced on restart. So this does not claim to fix
the cause. It makes a recurrence LOUD and self-healing instead of silent.

    python test_worker_liveness.py
"""
import ast
import io
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


SRC = io.open(os.path.join(_HERE, "sage_daemon.py"), encoding="utf-8").read()
_T = ast.parse(SRC)


def _body(name):
    """Source of one top-level function."""
    for n in _T.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return "\n".join(SRC.splitlines()[n.lineno - 1:n.end_lineno])
    return ""


# =============================================================================
print("=== 1. The heartbeat exists and is written unconditionally ===")
# =============================================================================
_w = _body("_periodic_worker")
ok("_periodic_worker is present", bool(_w))
ok("a heartbeat key is tracked", '"worker_heartbeat_ts"' in SRC)
ok("the worker writes it", '_tick_state["worker_heartbeat_ts"] = now' in _w)

# It must be written BEFORE the jobs, or a job that throws every pass would
# leave the heartbeat stale and the supervisor would fight a healthy loop.
_hb = _w.index('_tick_state["worker_heartbeat_ts"] = now')
_try = _w.index("\n        try:")
ok("the heartbeat is written BEFORE the job block", _hb < _try,
   "written after the jobs, a job that always throws would look like a dead "
   "thread and the supervisor would restart a loop that is actually running")
ok("it is inside the while loop, not just at startup",
   _w.index("while not _shutdown_event.is_set()") < _hb)


# =============================================================================
print("\n=== 2. BaseException cannot end the thread quietly ===")
# =============================================================================
ok("the tick body catches Exception", "except Exception as e:" in _w)
ok("...and BaseException separately", "except BaseException as e:" in _w)
ok("a BaseException is RECORDED before it propagates",
   '_tick_state["worker_last_death"]' in _w and "logger.critical" in _w)
ok("...and re-raised, not swallowed",
   _w.rstrip().count("raise") >= 1,
   "MemoryError/SystemExit are not conditions to keep looping through")
ok("the reporting path cannot mask the cause",
   "never let the reporting path mask the cause" in _w)


# =============================================================================
print("\n=== 3. Something OUTSIDE the thread watches it ===")
# =============================================================================
_rs = _body("run_server")
ok("run_server is present", bool(_rs))
ok("the worker is spawned through a helper", "_spawn_periodic_worker()" in _rs)
ok("the accept loop checks liveness", "worker.is_alive()" in _rs)
ok("...and restarts it", "worker = _spawn_periodic_worker()" in _rs)
ok("a restart is logged at CRITICAL, not swallowed",
   "logger.critical" in _rs and "restarting it" in _rs)
ok("restarts are counted so a flapping worker is visible",
   '_tick_state["worker_restarts"] += 1' in _rs,
   "one restart is a blip; a rising count is a bug")
ok("the supervision check cannot itself kill the accept loop",
   "Worker supervision check failed" in _rs)

# The canary must NOT be what we rely on -- it shares the worker's fate.
ok("the canary still exists (it catches a DIFFERENT failure)",
   "_craiid_canary_check" in _w,
   "stale timestamps while the loop still runs -- the #69 mis-indent shape")
ok("but liveness does not depend on it",
   "_craiid_canary_check" not in _rs,
   "a watchdog inside the thread it watches cannot report that thread's death")


# =============================================================================
print("\n=== 4. status reports liveness, not just last-action ===")
# =============================================================================
_hs = _body("handle_status")
ok("handle_status is present", bool(_hs))
for _k in ("worker_heartbeat_ts", "worker_heartbeat_age_sec",
           "worker_healthy", "worker_restarts", "worker_last_death"):
    ok("status exposes %s" % _k, '"%s"' % _k in _hs)
ok("healthy is None before the first pass, not False",
   "None if not tick_snap.get" in _hs,
   "a daemon inside its 15s startup grace is not faulty")
ok("the staleness bound is documented as 2+ missed passes",
   "two missed passes" in _hs)


# =============================================================================
print("\n=== 5. It actually works (threads, not text) ===")
# =============================================================================
# Sections 1-4 read source. The bug being fixed was a live thread dying, so
# this section builds the real shape and kills a worker on purpose.
_stop = threading.Event()
_state = {"heartbeat": None, "passes": 0}
_lock = threading.Lock()


def _fake_worker(die_after=None):
    n = 0
    while not _stop.is_set():
        n += 1
        with _lock:
            _state["heartbeat"] = time.time()
            _state["passes"] += 1
        if die_after is not None and n >= die_after:
            raise MemoryError("simulated non-Exception death")
        _stop.wait(0.05)


def _spawn(die_after=None):
    t = threading.Thread(target=_fake_worker, args=(die_after,), daemon=True)
    t.start()
    return t


# A worker that dies from a BaseException really does end the thread.
_t = _spawn(die_after=2)
time.sleep(0.4)
ok("a BaseException genuinely ends a worker thread", not _t.is_alive(),
   "if this did not hold, the whole failure mode would be impossible")
_beats_before = _state["passes"]
time.sleep(0.3)
ok("...and the heartbeat then goes stale on its own",
   _state["passes"] == _beats_before,
   "nothing is advancing it -- which is exactly what 12.5 hours of silence "
   "looked like from outside")

# The supervisor pattern revives it.
_revived = None
for _ in range(20):
    if not _t.is_alive():
        _revived = _spawn(die_after=None)   # the restart
        break
    time.sleep(0.05)
ok("a supervisor outside the thread can detect the death",
   _revived is not None)
time.sleep(0.3)
ok("...and the revived worker resumes beating",
   _state["passes"] > _beats_before,
   "%d -> %d" % (_beats_before, _state["passes"]))
ok("the revived thread is alive", _revived is not None and _revived.is_alive())
_stop.set()
time.sleep(0.2)

# And the liveness verdict itself: stale heartbeat -> unhealthy.
def _healthy(hb, now, bound=90):
    return None if not hb else (now - hb) < bound


ok("a fresh heartbeat reads healthy", _healthy(1000.0, 1010.0) is True)
ok("a 12-hour-old heartbeat reads UNHEALTHY",
   _healthy(1000.0, 1000.0 + 12.5 * 3600) is False,
   "the exact case that reported healthy all day")
ok("no heartbeat yet reads as unknown, not failed",
   _healthy(None, 1000.0) is None)


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
