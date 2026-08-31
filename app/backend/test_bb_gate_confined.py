#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_bb_gate_confined.py -- Build Battle's gate still gates, and now confines.

WHY THIS EXISTS

The gate runs code a MODEL just wrote. Todd's reason for confining it, in his
words: "I don't want them somehow competing themselves into the open web and
beyond." Candidate code needs no network, no new processes and no reach into
VeridianAI's data folder to find out whether it passes a test suite.

But confinement here was NOT free, and that is the thing this file guards.
The gate driver used to launch the test with `subprocess.run` -- and the
confined runner blocks exactly that. Confining it naively would have turned
every gate into a failure, which fails SAFE in the security sense and fails
uselessly in the product sense: Build Battle would simply stop working, and
the first symptom would be every candidate losing.

So the driver now runs the test in-process with runpy, inside the confined
child. Two things therefore have to be true at once, and both are asserted:

  1. the gate still DISCRIMINATES -- a correct candidate passes and a wrong one
     fails, through every failure mode a model actually produces;
  2. the gate CONFINES -- network and process creation are refused.

Assertion 1 is the one that matters most. A gate that cannot fail a bad
candidate is worse than no gate, and "everything is confined now" would hide it
perfectly.

    python test_bb_gate_confined.py
"""
import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_fails = []


def ok(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n            -> " + str(detail)) if detail and not cond else ""))
    if not cond:
        _fails.append(name)


try:
    import main
    import sage_engine as se
except Exception as e:                                    # pragma: no cover
    print("SKIP: backend not importable here (%s)" % e)
    sys.exit(0)

TEST = ("import sys\n"
        "from mymod import add\n"
        "assert add(2, 2) == 4, 'add is wrong'\n"
        "print('1 passed, 0 failed')\n"
        "sys.exit(0)\n")


def gate(candidate, test=TEST, timeout=60):
    return asyncio.run(
        main._bb_run_gate(candidate, test, "mymod", "test_mymod.py",
                          timeout=timeout))


# =============================================================================
print("=== 1. It still discriminates ===")
# =============================================================================
# NOTE FOR THE NEXT PERSON: the first draft of this used `return a*b` as the
# wrong answer, and it PASSED -- because 2*2 is also 4. The gate was fine; the
# test of the gate was not. Every wrong candidate below actually produces a
# wrong result for the inputs the suite uses.
_p, _raw = gate("def add(a, b):\n    return a + b\n")
ok("a correct candidate passes", _p is True, main._bb_gate_summary(_raw))
ok("...and the summary says so",
   "passed" in main._bb_gate_summary(_raw).lower())

for label, cand in (
    ("a wrong result", "def add(a, b):\n    return a - b\n"),
    ("a raised exception", "def add(a, b):\n    raise RuntimeError('boom')\n"),
    ("a syntax error", "def add(a, b)\n    return a + b\n"),
    ("a non-zero exit", "def add(a, b):\n    return a + b\n"
                        "import sys\nsys.exit(1)\n"),
    ("an empty module", ""),
):
    _p, _raw = gate(cand)
    ok("%s fails the gate" % label, _p is False,
       main._bb_gate_summary(_raw))

# =============================================================================
print("\n=== 2. It confines ===")
# =============================================================================
_IMPORTS_OK = ("import sys\nimport mymod\nprint('imported')\nsys.exit(0)\n")

_p, _raw = gate("import socket\ns = socket.socket()\n", test=_IMPORTS_OK)
ok("a candidate cannot open a socket", _p is False, _raw[:200])
ok("...and is told why rather than failing blankly",
   "confined runner" in _raw or "Network access" in _raw, _raw[:200])

_p, _raw = gate("import urllib.request as u\n"
                "u.urlopen('http://example.com')\n", test=_IMPORTS_OK)
ok("...nor reach the web through urllib", _p is False, _raw[:200])

_p, _raw = gate("import subprocess\nsubprocess.run(['echo', 'x'])\n",
                test=_IMPORTS_OK)
ok("a candidate cannot start another program", _p is False, _raw[:200])

_deny = (se._confine_deny_roots() or [None])[0]
if _deny:
    _p, _raw = gate("import os\nprint(os.listdir(r'%s'))\n" % _deny,
                    test=_IMPORTS_OK)
    ok("a candidate cannot list the data folder", _p is False, _raw[:200])

_p, _raw = gate("while True:\n    pass\n", test=_IMPORTS_OK, timeout=3)
ok("a runaway candidate is still bounded in time", _p is False,
   main._bb_gate_summary(_raw))

# =============================================================================
print("\n=== 3. The wiring says what it does ===")
# =============================================================================
_src = open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()
_fn = _src.split("async def _bb_run_gate")[1].split("\nasync def ")[0]
ok("the gate calls the CONFINED executor",
   "execute_python_confined" in _fn)
ok("...and not the unconfined one",
   "sage_engine.execute_python," not in _fn,
   "one call site, one executor")
# Comment lines stripped first. The plain substring matched the comment that
# EXPLAINS why subprocess is gone -- the third time in one day that a matcher
# in this project reported the note describing a fix as the bug itself. If a
# check reads source, it has to read the code and not the prose around it.
_code_only = "\n".join(
    l for l in _fn.split("\n") if not l.lstrip().startswith("#"))
ok("the driver does not shell out",
   "subprocess.run(" not in _code_only,
   "the confined runner blocks it; a driver that needs it cannot be confined")
ok("the driver uses runpy instead", "runpy.run_path" in _fn)
ok("the GATE_RC contract is unchanged",
   "GATE_RC=" in _fn and "GATE_RC=0" in _fn,
   "_bb_gate_summary and the caller both key off it")
ok("Customs still inspects the candidate AND the gate test",
   _fn.count("customs_daemon.inspect") == 2,
   "the test file was a second entrance that once had no door")
ok("it no longer calls itself a sandbox",
   "NOT a sandbox" not in _fn or "confined" in _fn.lower())

print("")
if _fails:
    print("%d CHECK(S) FAILED" % len(_fails))
    for f in _fails:
        print("   - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED - Build Battle gate (confined)")
sys.exit(0)
