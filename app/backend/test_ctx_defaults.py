#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ctx_defaults.py -- four launch paths, one answer for tier context size.

WHAT WAS WRONG (2026-08-21)

"How big is Toga's context?" had four answers, and three of them disagreed:

    start.bat            set SAGE_CTX_SIZE=256000
    store_launch.py      os.environ.get("SAGE_CTX_SIZE", "16384")
    tier_launcher.py     os.environ.get("SAGE_CTX_SIZE", "16384")
    config.py            SAGE_CTX_DEFAULT = 32768

So the Store package and the portable build sized the SAME tier differently on
the same machine, and which one you got depended on how you launched.

Normally none of them is used: _tier_config_reader.py reads config.json and
overrides the environment before the tiers spawn (it returns 32768 today).
These are fallbacks, and they fire exactly when that helper CANNOT run --
Python not found, config.json unreadable. A fallback runs when something has
already gone wrong, so it must be the SAFEST value, not the largest.

WHY 256000 WAS THE DANGEROUS ONE

From config.SAGE_CTX_MAX's own comment: the clamp exists because an unclamped
ctx of that magnitude "made the CPU Toga tier try a ~30 GB KV-cache allocation
at every boot (intermittent boot failure + system-wide thrash)". That incident
is why 65536 is there. start.bat's fallback was the same order of magnitude and
sits on the ONE path where the clamp is never consulted -- so it would have
reproduced a known, already-fixed boot failure, in the situation least likely
to be diagnosed correctly.

It also sat directly under a comment block explaining, in detail, why 16384 was
the right number. Third instance this release of a comment describing a value
the code does not hold.

WHAT THE SIZE MEANS (Todd, 2026-08-21): this is one SESSION's working context,
not the whole thread -- so it wants to be a reasonable ceiling for large
documents rather than "as high as possible". The thread-level, future-proofed
number is config.json's inference.n_ctx, which is clamped PER TIER on the way
down. 32768 is OpenHands 7B's full trained window; past that costs RAM and buys
nothing, because the model was never trained to use it.

    python test_ctx_defaults.py
"""
import io
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
_ROOT = os.path.dirname(_HERE)          # backend/ -> project root

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


import config as C                                           # noqa: E402


def _read(*parts):
    p = os.path.join(*parts)
    return io.open(p, encoding="utf-8", errors="replace").read() if \
        os.path.exists(p) else ""


BAT = _read(_ROOT, "start.bat")
STORE = _read(_ROOT, "store_launch.py")
LAUNCH = _read(_HERE, "tier_launcher.py")


# =============================================================================
print("=== 1. config is the reference, and it is sane ===")
# =============================================================================
ok("SAGE_CTX_DEFAULT is defined", isinstance(C.SAGE_CTX_DEFAULT, int))
ok("SAGE_CTX_MAX is defined", isinstance(C.SAGE_CTX_MAX, int))
ok("the default does not exceed the hardware-sanity clamp",
   C.SAGE_CTX_DEFAULT <= C.SAGE_CTX_MAX,
   "%s > %s" % (C.SAGE_CTX_DEFAULT, C.SAGE_CTX_MAX))
ok("the clamp is a real clamp, not 1M-Max again",
   C.SAGE_CTX_MAX <= 131072, C.SAGE_CTX_MAX)
ok("the daemon default stays small", C.DAEMON_CTX_DEFAULT <= 8192,
   "daemon work is mechanical; RAM spent there helps nobody")
ok("daemon gets less than the conversation tier",
   C.DAEMON_CTX_DEFAULT < C.SAGE_CTX_DEFAULT)


# =============================================================================
print("\n=== 2. Every launch path agrees with it ===")
# =============================================================================
ok("start.bat is readable", bool(BAT), os.path.join(_ROOT, "start.bat"))
if BAT:
    _m = re.search(r"^set SAGE_CTX_SIZE=(\d+)", BAT, re.M)
    ok("start.bat sets a Toga ctx fallback", _m is not None)
    if _m:
        ok("start.bat matches config (%s)" % C.SAGE_CTX_DEFAULT,
           int(_m.group(1)) == C.SAGE_CTX_DEFAULT, _m.group(1))
        ok("...and is within the clamp",
           int(_m.group(1)) <= C.SAGE_CTX_MAX,
           "this is the ONE path the clamp never sees, so the literal has to "
           "be right by itself")
    _d = re.search(r"^set DAEMON_CTX_SIZE=(\d+)", BAT, re.M)
    ok("start.bat daemon fallback matches config",
       _d and int(_d.group(1)) == C.DAEMON_CTX_DEFAULT,
       _d.group(1) if _d else None)

ok("store_launch.py is readable", bool(STORE))
if STORE:
    _m = re.search(r'"SAGE_CTX_SIZE":\s*os\.environ\.get\('
                   r'"SAGE_CTX_SIZE",\s*"(\d+)"\)', STORE)
    ok("store_launch has a Toga ctx fallback", _m is not None)
    if _m:
        ok("store_launch matches config (%s)" % C.SAGE_CTX_DEFAULT,
           int(_m.group(1)) == C.SAGE_CTX_DEFAULT, _m.group(1),)

ok("tier_launcher no longer holds its own literal default",
   'os.environ.get("SAGE_CTX_SIZE", "16384")' not in LAUNCH)
ok("...it derives from config instead", "_cfg_ctx(" in LAUNCH)
ok("...and _cfg_ctx is never fatal",
   "ctx default from config unavailable" in LAUNCH)

try:
    import tier_launcher as TL
    ok("tier_launcher's sage default equals config's",
       TL._cfg_ctx("sage", 1) == C.SAGE_CTX_DEFAULT, TL._cfg_ctx("sage", 1))
    ok("tier_launcher's daemon default equals config's",
       TL._cfg_ctx("daemon", 1) == C.DAEMON_CTX_DEFAULT,
       TL._cfg_ctx("daemon", 1))
    ok("an unknown tier still returns its fallback rather than raising",
       TL._cfg_ctx("nonsense", 4242) in (4242, C.SAGE_CTX_DEFAULT))
except Exception as _e:
    ok("tier_launcher importable for the ctx check", False,
       "%s: %s" % (type(_e).__name__, _e))


# =============================================================================
print("\n=== 3. The known-bad value is gone ===")
# =============================================================================
# 256000 / 262144 is the magnitude that caused the ~30 GB KV allocation and the
# intermittent boot failures SAGE_CTX_MAX was added to stop.
# Scoped to lines that are ABOUT ctx. The first version matched any 6+ digit
# number anywhere in the file and flagged OLLAMA_GPU_OVERHEAD=536870912 -- a
# 512 MB memory setting that has nothing to do with context. A check that
# reports unrelated numbers as ctx bugs is a check people learn to skim past.
for _name, _src in (("start.bat", BAT), ("store_launch.py", STORE),
                    ("tier_launcher.py", LAUNCH)):
    if not _src:
        continue
    _bad = []
    for _line in _src.splitlines():
        _code = _line.split("::", 1)[0].split("#", 1)[0]
        if not re.search(r"CTX|ctx_size|ctx\b", _code):
            continue
        for _n in re.findall(r"\b(\d{4,})\b", _code):
            if int(_n) > C.SAGE_CTX_MAX:
                _bad.append((_line.strip()[:60], _n))
    ok("%s has no ctx literal above the clamp" % _name, not _bad,
       "found %r -- see SAGE_CTX_MAX's comment for what that costs" % (_bad,))

ok("the reason 256000 was dangerous is written down, not just removed",
   "30 GB" in BAT or "30 GB" in C.__doc__ if C.__doc__ else "30 GB" in BAT,
   "a value deleted without its reason comes back")


# =============================================================================
print("\n=== 4. The comment matches the code ===")
# =============================================================================
# The specific failure: a comment block reasoning carefully about 16384 sat
# directly above `set SAGE_CTX_SIZE=256000`.
if BAT:
    _blk = BAT[max(0, BAT.find("Per-tier context sizes")):]
    _blk = _blk[:_blk.find("set EMBED_CTX_SIZE")] if "set EMBED_CTX_SIZE" in _blk else _blk
    # The real property: a number the comment states as CURRENT must match the
    # code. Historical numbers are fine -- they are the record of what changed
    # and why -- but only inside the part explicitly marked as history.
    #
    # The first version of this check just banned unknown numbers anywhere in
    # the block, and flagged 16384 and 8192: the very figures being quoted as
    # the superseded values. It punished the file for explaining itself.
    _marker = "v2.15.2 -- WHY THESE CHANGED"
    ok("the block marks its historical section", _marker in _blk)
    _current, _history = (_blk.split(_marker, 1) + [""])[:2] if _marker in _blk \
        else (_blk, "")
    _nums = {int(n) for n in re.findall(r"\b(\d{4,7})\b", _current)}
    _stale = {n for n in _nums
              if n not in (C.SAGE_CTX_DEFAULT, C.DAEMON_CTX_DEFAULT,
                           C.EMBED_CTX_DEFAULT, C.SAGE_CTX_MAX)}
    ok("the CURRENT part of the comment states no number the code contradicts",
       not _stale,
       "%r -- move it below the history marker, or correct it" % (sorted(_stale),))
    ok("the superseded values ARE preserved as history",
       "16384" in _history and "256000" in _history,
       "a value deleted without its reason comes back")
    ok("the block says these are fallbacks, not the live values",
       "FALLBACK" in _blk.upper())
    ok("...and names what normally overrides them",
       "_tier_config_reader" in _blk)


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
