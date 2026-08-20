#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_timeout_policy.py -- the safety nets are actually armed.

WHAT HAPPENED (2026-08-20)

Ollama wedged. Process alive, port accepting connections, 0.8 seconds of CPU
across 40 minutes, no model loaded, answering nothing -- not even /api/tags.
A turn hung for 21+ minutes showing "thinking", with no error and no timeout.

Every mechanism built to catch that was set to 56000 seconds -- 15.5 HOURS:

    ollama_read_timeout_sec  56010     httpx connect  56000
    stall_token_timeout_sec  56000     httpx write    56000
    stall_tool_timeout_sec   56000     httpx pool     56000
    _get_trained_ctx client  56000     _taskp_run_or_direct  56000

And the comments beside them said otherwise:

    "stall_token_timeout_sec": 56000,   # 5 min between tokens = stall
    "stall_tool_timeout_sec":  56000,   # 3 min for a tool result = stall

    "... Default raised to 1800s (30 min) which comfortably covers ..."
        -- above a line reading self.config.get(..., 56000.0)

    "a second-layer wait so a wedged dispatcher can't hang the agentic
     loop forever"
        -- above timeout_seconds: float = 56000.0

The numbers were cranked during the Arc B580 era, when the hardware genuinely
was that slow. The card is gone; the numbers stayed; the comments never
matched them in the first place. So the system READ as protected at every
level while being, in practice, unbounded at every level.

TWO THINGS THIS FILE ENFORCES

1. Every bound stays inside a sane range. A future "just bump it for now"
   fails here instead of silently disarming a watchdog for years.

2. A comment claiming "N min" next to a value must agree with that value.
   That mismatch is what made the disarming invisible -- anyone reading this
   code, including whoever wrote it, would have concluded it was protected.

    python test_timeout_policy.py
"""
import ast
import io
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


MAIN = io.open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()
MM = io.open(os.path.join(_HERE, "model_manager.py"), encoding="utf-8").read()

# =============================================================================
print("=== 1. No request-path timeout is absurdly long ===")
# =============================================================================
# Found STRUCTURALLY, via the AST, not by grepping for 56000.
#
# The first version of this section did grep for it -- `\b5600\d\b` -- and it
# MISSED "ollama_read_timeout_sec": 56010, one of the two values that caused
# the incident. It also flagged the digits in prose inside docstrings. A magic
# number is the symptom; the property worth enforcing is "no timeout on a
# request path is longer than an hour", whatever the digits happen to be.
_LIMIT = 3600.0

# Legitimate long waits, named individually so an exemption cannot spread.
_ALLOWED_LONG = {
    # A pip install of torch + whisper: genuinely gigabytes, not on any
    # request path, and it reports progress through _voice_install.
    ("main.py", "timeout", 3600.0),
}


def _num(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return float(node.value)
    return None


def _long_timeouts(src, fname):
    tree = ast.parse(src)
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            _ctor = ((isinstance(n.func, ast.Attribute)
                      and n.func.attr == "Timeout")
                     or (isinstance(n.func, ast.Name) and n.func.id == "Timeout"))
            for kw in n.keywords:
                if kw.arg is None:
                    continue
                _rel = ("timeout" in kw.arg.lower()
                        or (_ctor and kw.arg in ("connect", "read",
                                                 "write", "pool")))
                v = _num(kw.value)
                if _rel and v is not None and v >= _LIMIT:
                    hits.append((n.lineno, kw.arg, v))
        if isinstance(n, ast.Dict):
            for k, val in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str) \
                        and "timeout" in k.value.lower():
                    v = _num(val)
                    if v is not None and v >= _LIMIT:
                        hits.append((k.lineno, k.value, v))
        if isinstance(n, ast.Assign):
            for tgt in n.targets:
                if isinstance(tgt, ast.Name) and "TIMEOUT" in tgt.id.upper():
                    v = _num(n.value)
                    if v is not None and v >= _LIMIT:
                        hits.append((n.lineno, tgt.id, v))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _a = n.args
            _pos = list(zip(_a.args[-len(_a.defaults):] if _a.defaults else [],
                            _a.defaults))
            _kw = [(a, d) for a, d in zip(_a.kwonlyargs, _a.kw_defaults)
                   if d is not None]
            for arg, dflt in _pos + _kw:
                if "timeout" in arg.arg.lower():
                    v = _num(dflt)
                    if v is not None and v >= _LIMIT:
                        hits.append((n.lineno, arg.arg, v))
    return [h for h in sorted(set(hits))
            if (fname, h[1], h[2]) not in _ALLOWED_LONG]


for _src, _name in ((MAIN, "main.py"), (MM, "model_manager.py")):
    _bad = _long_timeouts(_src, _name)
    ok("no request-path timeout >= 1h in %s" % _name, not _bad,
       "%r -- if one of these is genuinely long-running and off the request "
       "path, add it to _ALLOWED_LONG with a reason" % (_bad,))

# The detector must actually detect. Every shape the incident took, checked
# against the checker itself -- including the 56010 the regex version missed.
_PRE_FIX = '''
D = {"ollama_read_timeout_sec": 56010, "stall_token_timeout_sec": 56000}
t = httpx.Timeout(connect=56000.0, read=_r, write=56000.0, pool=56000.0)
c = httpx.AsyncClient(timeout=56000.0)
async def f(*, timeout_seconds: float = 56000.0): pass
'''
_caught = {h[1] for h in _long_timeouts(_PRE_FIX, "<synthetic>")}
for _shape in ("ollama_read_timeout_sec", "stall_token_timeout_sec",
               "connect", "write", "pool", "timeout", "timeout_seconds"):
    ok("the checker catches %s" % _shape, _shape in _caught,
       "a check that cannot fail on the original bug proves nothing")


# =============================================================================
print("\n=== 2. Each bound is inside a defensible range ===")
# =============================================================================
_t = ast.parse(MAIN)
_defaults = {}
for _n in ast.walk(_t):
    if isinstance(_n, ast.Dict):
        for _k, _v in zip(_n.keys, _n.values):
            if isinstance(_k, ast.Constant) and isinstance(_k.value, str) \
                    and isinstance(_v, ast.Constant) \
                    and isinstance(_v.value, (int, float)):
                _defaults.setdefault(_k.value, _v.value)

# (key, low, high, why)
_RANGES = [
    ("stall_first_token_timeout_sec", 120, 1800,
     "must cover a 120b cold load, must not outlast the user's patience"),
    ("stall_token_timeout_sec", 30, 900,
     "real inter-token gaps are sub-second"),
    ("stall_tool_timeout_sec", 30, 900,
     "a tool that has said nothing this long is wedged"),
    ("ollama_read_timeout_sec", 300, 3600,
     "per-chunk on a stream; bounds the cold-load silence"),
]
for _key, _lo, _hi, _why in _RANGES:
    _val = _defaults.get(_key)
    ok("%s is present" % _key, _val is not None)
    if _val is not None:
        ok("%s = %s is within [%s, %s]" % (_key, _val, _lo, _hi),
           _lo <= _val <= _hi, _why)

ok("the cold-load budget is LOOSER than the between-token budget",
   _defaults.get("stall_first_token_timeout_sec", 0)
   > _defaults.get("stall_token_timeout_sec", 0),
   "if it were not, splitting them would have bought nothing")
ok("the read timeout sits ABOVE the user-facing cold-load budget",
   _defaults.get("ollama_read_timeout_sec", 0)
   > _defaults.get("stall_first_token_timeout_sec", 0),
   "the watchdog should fire first -- it is the one that can explain itself")


# =============================================================================
print("\n=== 3. Comments must agree with the values beside them ===")
# =============================================================================
# The specific failure: `"stall_token_timeout_sec": 56000,  # 5 min ...`.
# Anyone reading that line concluded the system was protected. Parse the
# minutes out of the trailing comment and hold the number to it.
_MIN_RE = re.compile(r"^\s*\"(\w+_sec)\"\s*:\s*([0-9.]+)\s*,\s*#.*?(\d+)\s*min")
_checked = 0
for _line in MAIN.splitlines():
    _m = _MIN_RE.match(_line)
    if not _m:
        continue
    _checked += 1
    _key, _val, _mins = _m.group(1), float(_m.group(2)), int(_m.group(3))
    ok("%s: value %.0fs matches its comment's %d min" % (_key, _val, _mins),
       abs(_val - _mins * 60) < 1.0,
       "the comment says %d min (%ds) -- this is exactly the mismatch that "
       "hid a 15.5-hour timeout behind the words '5 min'" % (_mins, _mins * 60))
ok("there were annotated timeout defaults to check", _checked >= 2, _checked)


# =============================================================================
print("\n=== 4. connect is short, because loopback is fast or broken ===")
# =============================================================================
ok("model_manager defines a named connect timeout", "_CONNECT_TIMEOUT" in MM)
_conn = re.search(r"_CONNECT_TIMEOUT\s*=\s*([0-9.]+)", MM)
ok("connect timeout is defined as a number", _conn is not None)
if _conn:
    ok("connect timeout <= 30s (a TCP handshake to 127.0.0.1)",
       float(_conn.group(1)) <= 30.0, _conn.group(1),)
ok("no generator still hardcodes its own connect value",
   "connect=56000" not in MM)
ok("both stream paths use the shared constants",
   MM.count("connect=_CONNECT_TIMEOUT") == 2,
   "_gen_ollama and _gen_llama_server; %d found"
   % MM.count("connect=_CONNECT_TIMEOUT"))
ok("metadata lookups have their own short bound", "_META_TIMEOUT" in MM)
_meta = re.search(r"_META_TIMEOUT\s*=\s*([0-9.]+)", MM)
ok("metadata timeout <= 60s", _meta and float(_meta.group(1)) <= 60.0,
   "_get_trained_ctx runs BEFORE the stream opens -- at 56000 it hung the "
   "turn before any generation timeout could apply")


# =============================================================================
print("\n=== 5. The watchdog really distinguishes the two waits ===")
# =============================================================================
_wd = [n for n in ast.walk(_t)
       if isinstance(n, ast.ClassDef) and n.name == "_StallWatchdog"]
ok("_StallWatchdog is present", len(_wd) == 1)
if _wd:
    _init = [n for n in _wd[0].body
             if isinstance(n, ast.FunctionDef) and n.name == "__init__"][0]
    _args = [a.arg for a in _init.args.args]
    ok("__init__ accepts first_token_timeout_sec",
       "first_token_timeout_sec" in _args, _args)
    _dflt = _init.args.defaults
    ok("it is optional, so older constructions still work",
       len(_dflt) >= 1)
    ok("the class stores it", "self.first_token_timeout" in MAIN)

_watch = MAIN[MAIN.index("    async def watch("):]
_watch = _watch[:_watch.index("\nclass ") if "\nclass " in _watch
                else len(_watch)]
ok("watch() picks the limit by whether a token has arrived",
   "self.token_count == 0" in _watch and "self.first_token_timeout" in _watch,
   "one limit for both waits is what forced the 15.5-hour value")
ok("the two stalls give DIFFERENT explanations",
   "never produced its" in _watch and "hung mid-generation" in _watch,
   "'never started' and 'stopped partway' send the user to different places")
ok("the runaway ceiling is still checked first",
   _watch.index("runaway_limit") < _watch.index("tok_gap"),
   "a runaway refreshes last_token_ts, so time checks can never catch it")

ok("the construction site passes the new budget",
   "first_token_timeout_sec=_stall_first" in MAIN)
ok("an install that pinned the OLD single knob is not silently tightened",
   "max(_stall_tok, 600.0)" in MAIN)


# =============================================================================
print("\n=== 6. Runtime: the wiring holds together ===")
# =============================================================================
try:
    _cwd = os.getcwd()
    os.chdir(_HERE)
    import main as _main                                     # noqa: E402
    os.chdir(_cwd)
    ok("main.py imports for real", True)

    _w = _main._StallWatchdog(300.0, 300.0, runaway_token_limit=-1,
                              first_token_timeout_sec=600.0)
    ok("first-token budget is applied", _w.first_token_timeout == 600.0)
    ok("between-token budget is separate", _w.token_timeout == 300.0)
    ok("no token has arrived yet", _w.token_count == 0)
    _w.record_token()
    ok("recording a token flips the phase", _w.token_count == 1)

    _w2 = _main._StallWatchdog(300.0, 300.0)
    ok("omitting the new arg falls back to the old single budget",
       _w2.first_token_timeout == 300.0,
       "back-compat: existing constructions must not change behaviour")

    import model_manager as _mm                              # noqa: E402
    ok("model_manager imports for real", True)
    ok("ModelManager._gen_ollama is still a real method",
       "_gen_ollama" in _mm.ModelManager.__dict__,
       "a zero-indented helper ended this class body once already")
    ok("_CONNECT_TIMEOUT is a live module constant",
       isinstance(getattr(_mm, "_CONNECT_TIMEOUT", None), float))
    ok("_META_TIMEOUT is a live module constant",
       isinstance(getattr(_mm, "_META_TIMEOUT", None), float))
except Exception as _e:
    ok("runtime wiring holds", False, "%s: %s" % (type(_e).__name__, _e))


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
