#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_task_prioritiser.py -- the parallel dispatcher delivers what it runs.

THE BUG THIS LOCKS OUT (v2.15.2)
Both callers of the dispatcher collected results by swapping their own function
into a shared slot:

    def _patched_cb(tr): ...                       # ONE argument
    sage_engine.oracle_d._result_callback = _patched_cb

task_prioritiser calls that slot with TWO:

    self._result_callback(result, task if not success else None)

So every result raised TypeError inside the worker thread -- where

    except Exception:
        pass

ate it. Nothing was ever collected. The caller's Event never fired. It waited
out its whole timeout and reported "(timed out)" for every subtask, while the
searches underneath had succeeded every single time.

It hid for months because of three things at once, and this file checks all
three:

  1. the exception was swallowed in silence          -> section 1
  2. the work SUCCEEDED, so nothing else looked wrong -> section 3
  3. the visible symptom was a timeout, which sent    -> section 4
     everyone to tune timeouts, and no timeout value
     could ever have fixed it

    python test_task_prioritiser.py
"""
import io
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_HERE = os.path.dirname(os.path.abspath(__file__))

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


def read(name):
    return io.open(os.path.join(_HERE, name), encoding="utf-8").read()


import task_prioritiser as tp   # noqa: E402

TP = read("task_prioritiser.py")
MAIN = read("main.py")
SAGE = read("sage_engine.py")


# =============================================================================
print("=== 1. A callback that raises is never swallowed in silence ===")
# =============================================================================
_call = "self._result_callback(result, task if not success else None)"
ok("the dispatcher still calls back with TWO arguments", _call in TP)

# The explanation of the bug lives between the call and the report, and it is
# long on purpose -- so slice generously rather than assuming it is short.
_after = TP.split(_call, 1)[1][:3000]
ok("a failing callback no longer hits a bare `except Exception: pass`",
   not re.search(r"except Exception:\s*\n\s*pass", _after),
   _after[:200])
ok("...it is reported instead", "RESULT CALLBACK FAILED" in _after)
ok("...naming the task, so it can be traced", "task {task.task_id}" in _after)
ok("...and the exception type, so it is diagnosable",
   "type(cb_err).__name__" in _after)
# A `raise` STATEMENT, not the word "raised" in the prose explaining the bug.
ok("...and it does NOT re-raise (that would kill the worker thread)",
   not re.search(r"^\s*raise\b", _after.split("RESULT CALLBACK FAILED")[0],
                 flags=re.M))
ok("the two-argument contract is written down at the top of the file",
   "callback(result: TaskResult, failed_task: PrioritizedTask | None)" in TP)


# =============================================================================
print("\n=== 2. Nobody reaches into the shared callback slot any more ===")
# =============================================================================
# The slot is a single attribute on a process-wide dispatcher. Two chats using
# it at once overwrite each other, and whichever finishes first restores a
# callback the other still needs. Assigning to it is the trap; not assigning is
# the fix.
def assignments(src):
    return re.findall(r"^[^#\n]*_result_callback\s*=", src, flags=re.M)


ok("main.py does not assign to _result_callback", not assignments(MAIN),
   assignments(MAIN))
ok("sage_engine.py does not assign to _result_callback", not assignments(SAGE),
   assignments(SAGE))
ok("main.py's PRIORITISE reports per-subtask instead", "_make_runner" in MAIN)
ok("sage_engine's pre_process_query reports per-job instead",
   "def reporting(" in SAGE)
# Scope this to the PRIORITISE block: main.py calls submit_raw_task in
# _taskp_run_or_direct thousands of lines earlier, so a whole-file index
# comparison answers a different question than the one being asked.
_PB = MAIN[MAIN.index('elif action_type == "prioritise":'):]
_PB = _PB[:_PB.index('tool_results_acc[')]
ok("the count to wait for is fixed BEFORE dispatch (main.py)",
   _PB.index("_expected = len(subtasks_raw)") < _PB.index("submit_raw_task"))
ok("...and the PRIORITISE block submits self-reporting runners",
   "_make_runner(key, fn)" in _PB)
ok("the count to wait for is fixed BEFORE dispatch (sage_engine.py)",
   SAGE.index("expected = len(jobs)") < SAGE.index("reporting(key, fn),"))


# =============================================================================
print("\n=== 3. The dispatcher was never broken -- delivery was ===")
# =============================================================================
p = tp.OAgentP()
d = tp.OAgentD(p, num_subagents=3)
try:
    for i in range(3):
        d.submit_raw_task({"type": "news", "key": f"raw{i}",
                           "fn": lambda i=i: f"work {i} ran"})
    t0 = time.time()
    while len(d.get_results()) < 3 and time.time() - t0 < 10:
        time.sleep(0.02)
    res = d.get_results()
    ok("three submitted tasks all execute", len(res) == 3, len(res))
    ok("...and all report success", all(r.success for r in res))
    ok("...quickly", time.time() - t0 < 5, time.time() - t0)
finally:
    d.stop()


# =============================================================================
print("\n=== 4. The shipped bug, reproduced, then the fix ===")
# =============================================================================
def batch(use_self_reporting, wait_s=3.0):
    """Run three instant subtasks the way a caller does. Returns (got, want)."""
    p = tp.OAgentP()
    d = tp.OAgentD(p, num_subagents=3)
    try:
        collected, lock, done = {}, threading.Lock(), threading.Event()
        keys = [f"sub{i}" for i in range(3)]
        expected = len(keys)

        if use_self_reporting:
            def make(k):
                def _run():
                    with lock:
                        collected[k] = f"result for {k}"
                        if len(collected) >= expected:
                            done.set()
                    return {"key": k, "value": collected[k]}
                return _run
            for k in keys:
                d.submit_raw_task({"type": "news", "key": k, "fn": make(k)})
        else:
            # EXACTLY the shipped pattern: a one-argument callback in the slot.
            def patched(tr):                       # noqa: ARG001
                out = tr.output
                if isinstance(out, dict) and "key" in out:
                    with lock:
                        collected[out["key"]] = out["value"]
                        if len(collected) >= expected:
                            done.set()
            d._result_callback = patched
            for k in keys:
                d.submit_raw_task({"type": "news", "key": k,
                                   "fn": lambda k=k: f"result for {k}"})

        t0 = time.time()
        done.wait(wait_s)
        return len(collected), expected, time.time() - t0
    finally:
        d.stop()


got_bad, want, el_bad = batch(False)
ok("the OLD pattern collects nothing and burns its whole budget",
   got_bad == 0 and el_bad >= 2.9, f"got {got_bad}/{want} in {el_bad:.2f}s")
ok("...which is why raising TASK_TIMEOUT could never have helped it",
   tp.TASK_TIMEOUT > el_bad,
   f"TASK_TIMEOUT={tp.TASK_TIMEOUT} was never what fired")

got_ok, want_ok, el_ok = batch(True)
ok("the NEW pattern collects everything", got_ok == want_ok,
   f"got {got_ok}/{want_ok}")
ok("...almost immediately", el_ok < 2.0, f"{el_ok:.2f}s")


# =============================================================================
print("\n=== 5. A slow job cannot be cut short by a fast one ===")
# =============================================================================
# The old completion test compared against a list still being appended to, so
# one fast result could satisfy it while the rest were still being submitted.
p = tp.OAgentP()
d = tp.OAgentD(p, num_subagents=3)
try:
    collected, lock, done = {}, threading.Lock(), threading.Event()
    keys = ["fast", "slow", "fast2"]
    expected = len(keys)

    def make(k, delay):
        def _run():
            time.sleep(delay)
            with lock:
                collected[k] = k
                if len(collected) >= expected:
                    done.set()
            return {"key": k, "value": k}
        return _run

    for k, delay in (("fast", 0.0), ("slow", 1.0), ("fast2", 0.0)):
        d.submit_raw_task({"type": "t", "key": k, "fn": make(k, delay)})

    done.wait(6.0)
    ok("the batch waits for the slow one", len(collected) == 3, collected)
    ok("...and the slow one's result is present", "slow" in collected)
finally:
    d.stop()


# =============================================================================
print("\n=== 6. A failing subtask reports, it does not just go quiet ===")
# =============================================================================
p = tp.OAgentP()
d = tp.OAgentD(p, num_subagents=2)
try:
    collected, lock, done = {}, threading.Lock(), threading.Event()
    expected = 2

    def make(k, boom):
        def _run():
            try:
                if boom:
                    raise RuntimeError("rate limited")
                value = "fine"
            except Exception as e:
                value = f"({k} failed: {e})"
            with lock:
                collected[k] = value
                if len(collected) >= expected:
                    done.set()
            return {"key": k, "value": value}
        return _run

    d.submit_raw_task({"type": "t", "key": "good", "fn": make("good", False)})
    d.submit_raw_task({"type": "t", "key": "bad", "fn": make("bad", True)})
    done.wait(5.0)
    ok("both jobs report", len(collected) == 2, collected)
    ok("the failure says what went wrong, not '(timed out)'",
       "rate limited" in collected.get("bad", ""), collected.get("bad"))
    ok("...and the healthy one is unaffected", collected.get("good") == "fine")
finally:
    d.stop()


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
