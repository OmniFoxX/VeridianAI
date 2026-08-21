#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_reasoning_budget.py -- thinking is bounded, per tier, and says so.

THE FAILURE THIS BOUNDS

A reasoning model emits its thinking on a channel separate from its reply. With
no ceiling it can spend an entire generation budget there and produce ZERO
answer tokens: the turn ends, the user gets nothing back, and nothing explains
why. Four re-prompts in a row for one news briefing, 2026-08-17.

v2.15.2 already made that legible -- _no_answer_notice says what happened
instead of ghosting. This is the other half Todd asked for: cap the thinking so
it mostly does not happen at all.

VERIFIED, NOT ASSUMED

These flags were read off the shipped binary (llama-server.exe --help) before a
line of this was written, because an unrecognised flag does not degrade -- the
tier fails to start and there is no llama backend at all:

    --reasoning-budget N          -1 unrestricted (llama.cpp default),
                                  0 end thinking immediately, N>0 token budget
    --reasoning-budget-message M  injected before the end-of-thinking tag when
                                  the budget runs out

Section 4 goes further and has the REAL binary parse the command we build,
because "the flag is in the help text" and "the binary accepts this argv" are
different claims.

TIERED BY SERVER TIER because the tiers have different jobs: sage is the user's
conversation (someone is waiting, but a hard question deserves room); daemon is
background CRAIID work (nobody is watching, and unbounded thinking there burns
GPU the conversation wants); embed has no chat surface at all.

    python test_reasoning_budget.py
"""
import io
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


import config as C                                          # noqa: E402

# build_llama_server_command refuses to build without a model path, which is
# correct and inconvenient here. Point the tier model vars at a real-looking
# path so we can inspect argv without spawning anything.
_FAKE = os.path.join(_HERE, "does-not-exist.gguf")
C.MODEL_SAGE = _FAKE
C.MODEL_DAEMON = _FAKE
C.MODEL_EMBED = _FAKE


def _argv(tier, **kw):
    return C.build_llama_server_command(tier, **kw)


def _flag(argv, name):
    """Value following `name`, or None if the flag is absent."""
    return argv[argv.index(name) + 1] if name in argv else None


# =============================================================================
print("=== 1. The defaults are tiered, and finite ===")
# =============================================================================
ok("a sage budget is defined", isinstance(C.REASONING_BUDGET_SAGE, int))
ok("a daemon budget is defined", isinstance(C.REASONING_BUDGET_DAEMON, int))
ok("the sage default is 8192", C.REASONING_BUDGET_SAGE == 8192,
   C.REASONING_BUDGET_SAGE)
ok("the sage default is NOT unlimited", C.REASONING_BUDGET_SAGE > 0,
   "-1 is what let a model think forever and answer with nothing")
ok("the daemon gets LESS room than the conversation",
   0 < C.REASONING_BUDGET_DAEMON < C.REASONING_BUDGET_SAGE,
   "nobody is waiting on a daemon turn; that GPU time belongs to the chat")
ok("there is a budget-exhausted message",
   isinstance(C.REASONING_BUDGET_MESSAGE, str)
   and len(C.REASONING_BUDGET_MESSAGE) > 20)
ok("...and it tells the model to ANSWER, not just to stop",
   "answer" in C.REASONING_BUDGET_MESSAGE.lower(),
   "cutting a model off mid-thought without asking for an answer relocates "
   "the no-reply bug instead of fixing it")


# =============================================================================
print("\n=== 2. The flags land on the command line ===")
# =============================================================================
_sage = _argv("sage")
_daemon = _argv("daemon")
_embed = _argv("embed")

ok("sage carries --reasoning-budget", "--reasoning-budget" in _sage)
ok("...at the tier default",
   _flag(_sage, "--reasoning-budget") == str(C.REASONING_BUDGET_SAGE),
   _flag(_sage, "--reasoning-budget"))
ok("daemon carries its own, smaller budget",
   _flag(_daemon, "--reasoning-budget") == str(C.REASONING_BUDGET_DAEMON))
ok("a finite budget brings the wrap-up message",
   "--reasoning-budget-message" in _sage)
ok("...with the configured text",
   _flag(_sage, "--reasoning-budget-message") == C.REASONING_BUDGET_MESSAGE)

ok("the EMBED tier gets neither flag",
   "--reasoning-budget" not in _embed
   and "--reasoning-budget-message" not in _embed,
   "no chat surface -- the flag would be inert noise")
ok("the embed tier is otherwise untouched",
   "--embedding" in _embed and "--pooling" in _embed)


# =============================================================================
print("\n=== 3. Overrides, including the unlimited escape hatch ===")
# =============================================================================
_o = _argv("sage", reasoning_budget=1234)
ok("an explicit budget overrides the tier default",
   _flag(_o, "--reasoning-budget") == "1234")

_u = _argv("sage", reasoning_budget=-1)
ok("-1 is passed through as unrestricted",
   _flag(_u, "--reasoning-budget") == "-1",
   "unlimited must stay AVAILABLE -- it is a supported choice")
ok("unlimited does NOT carry a wrap-up message",
   "--reasoning-budget-message" not in _u,
   "there is no budget to exhaust, so the message would never fire")

_z = _argv("sage", reasoning_budget=0)
ok("0 (end thinking immediately) is passed through",
   _flag(_z, "--reasoning-budget") == "0")
ok("0 also carries no wrap-up message",
   "--reasoning-budget-message" not in _z)

_bad = _argv("sage", reasoning_budget="not-a-number")
ok("a non-numeric budget falls back to unrestricted rather than crashing",
   _flag(_bad, "--reasoning-budget") == "-1",
   "a bad config value must not stop the tier from starting")
_neg = _argv("sage", reasoning_budget=-99)
ok("a below--1 value is normalised to -1",
   _flag(_neg, "--reasoning-budget") == "-1",
   "llama-server does not define anything below -1")

# The warning is the point of the unlimited path: it must be impossible for an
# unlimited setting to sit in a config file silently.
import io as _io                                            # noqa: E402
import contextlib                                           # noqa: E402
_buf = _io.StringIO()
with contextlib.redirect_stdout(_buf):
    _argv("sage", reasoning_budget=-1)
_out = _buf.getvalue()
ok("spawning with UNLIMITED thinking prints a warning",
   "UNLIMITED" in _out and "WARNING" in _out, _out.strip()[:120])
ok("...and the warning names the knob that fixes it",
   "SAGE_REASONING_BUDGET" in _out, _out.strip()[:160])

_buf2 = _io.StringIO()
with contextlib.redirect_stdout(_buf2):
    _argv("sage")
ok("a bounded spawn is quiet (no warning fatigue)",
   "WARNING" not in _buf2.getvalue(), _buf2.getvalue().strip()[:120])


# =============================================================================
print("\n=== 4. The REAL binary accepts what we build ===")
# =============================================================================
# Sections 1-3 prove we construct the argv we intended. They cannot prove
# llama-server understands it -- and an unrecognised flag is not a degraded
# feature here, it is a tier that will not start at all. So ask the binary.
_exe = str(C.LLAMA_SERVER_EXE)
if not os.path.exists(_exe):
    ok("llama-server.exe is present to check against", False, _exe)
else:
    ok("llama-server.exe is present to check against", True)
    try:
        _help = subprocess.run([_exe, "--help"], capture_output=True,
                               timeout=60).stdout.decode("utf-8", "replace")
    except Exception as e:
        _help = ""
        ok("could read --help", False, "%s: %s" % (type(e).__name__, e))
    if _help:
        ok("the binary documents --reasoning-budget",
           "--reasoning-budget" in _help)
        ok("the binary documents --reasoning-budget-message",
           "--reasoning-budget-message" in _help)
        ok("its documented default really is -1 (why we set our own)",
           "default: -1" in _help.replace("  ", " ")
           or "-1 for unrestricted" in _help)

        # Every flag we emit must be one this binary knows. Catches a flag
        # renamed or dropped by a future llama.cpp bump -- which would
        # otherwise surface as "the Toga tier stopped starting".
        _emitted = [a for a in _sage + _embed if a.startswith("--")]
        _unknown = sorted({a for a in _emitted if a not in _help})
        ok("every flag we emit is known to this binary", not _unknown,
           "unknown to llama-server: %r" % (_unknown,))


# =============================================================================
print("\n=== 5. BOTH spawn paths carry it (the coverage bug) ===")
# =============================================================================
# The first version of this feature shipped correct flags that no running tier
# ever received. There are TWO llama-server spawners:
#
#   tier_launcher.py  -- what start.bat and store_launch.py run AT BOOT. These
#                        are the servers the user actually talks to.
#   build_llama_server_command -- reached only when tier_lifecycle respawns a
#                        tier after a ctx change.
#
# The budget went into the second one alone, so a booted install never saw it.
# Same "right code, wrong coverage" shape as the reasoning hook that sat on the
# streaming path while every agentic turn went around it, and as the at-rest
# guard that could not see a module making no atrest calls.
#
# Checking "the flag is in build_llama_server_command" would not have caught
# it -- the flag WAS there. What was wrong was the set of callers.
_TL = os.path.join(_HERE, "tier_launcher.py")
ok("tier_launcher.py is present", os.path.exists(_TL))
if os.path.exists(_TL):
    _tl = io.open(_TL, encoding="utf-8").read()
    ok("the boot launcher has a reasoning-args helper",
       "def _reasoning_args" in _tl)
    ok("...which delegates to config, rather than re-deriving the flags",
       "from config import reasoning_args" in _tl,
       "a second copy of the logic is how these two drifted in the first place")
    ok("...and is never fatal, like _eos_args beside it",
       "reasoning budget skipped for" in _tl,
       "a tier that cannot compute a budget must still start")
    ok("the BOOT Toga spawn carries it", '_reasoning_args("sage")' in _tl)
    ok("the BOOT Daemon spawn carries it", '_reasoning_args("daemon")' in _tl)

    # Both spawns must be llama-server ones; the embed tier is excluded by
    # reasoning_args itself, so it needs no call site.
    ok("the embed spawn does NOT ask for a budget",
       _tl.count("_reasoning_args(") == 3,   # def + sage + daemon
       "found %d occurrences" % _tl.count("_reasoning_args("))

# The parity assertion: the same tier must produce the same flags whichever
# path spawns it. This is what stops the next divergence.
for _tier in ("sage", "daemon"):
    _from_builder = _argv(_tier)
    _slice = [a for a in _from_builder
              if a.startswith("--reasoning") or _from_builder[
                  _from_builder.index(a) - 1].startswith("--reasoning")]
    _direct = C.reasoning_args(_tier)
    ok("%s: build_llama_server_command uses reasoning_args verbatim" % _tier,
       all(x in _from_builder for x in _direct) and _direct,
       "builder=%r direct=%r" % (_from_builder, _direct))
    # And the order/pairing survives: flag then value, contiguous.
    _i = _from_builder.index("--reasoning-budget")
    ok("%s: the flag and its value stay adjacent" % _tier,
       _from_builder[_i + 1] == _direct[1], _from_builder[_i:_i + 2])

ok("the embed tier gets an empty fragment, not a flag",
   C.reasoning_args("embed") == [])
ok("tier matching is case-insensitive",
   C.reasoning_args("DAEMON") == C.reasoning_args("daemon"))
ok("an explicit override still wins through the shared helper",
   C.reasoning_args("sage", 777)[1] == "777")


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
