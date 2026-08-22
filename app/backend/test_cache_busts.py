#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_cache_busts.py -- every ?v= is the file's own hash, everywhere.

WHAT WAS WRONG

A cache-bust answers exactly one question -- has THIS FILE changed? -- and it
was being answered by hand. By v2.16.0 index.html carried busts spanning 2.9.10
to 2.16.1, and one of them was `chat.js?v=2.15.2a`: a value typed
mid-development purely to shove a browser past a stale copy.

The failure mode is nasty because it is silent. A bust that did not get bumped
means the browser serves yesterday's module, which looks exactly like the
feature you just shipped not working -- and the code, the tests and the
manifest all say it is fine.

WHY THIS TEST CHECKS THE OUTCOME, NOT THE TOOL

_bust_cache.py could pass its own --check and still be wrong, because both
sides would share the same mistaken idea of the answer. So this recomputes the
hashes here, from the files, and compares. If the tool and this test ever
disagree, one of them is broken and that is worth knowing.

THE TWO PROPERTIES

  1. Every ?v= equals the sha256 prefix of the file it points at.
  2. A file referenced from MORE THAN ONE PLACE carries the SAME bust in all
     of them. settings.js builds two of these URLs in JavaScript, so
     index.html and settings.js both reference hljs-github-dark-dimmed.css.
     Two different values would mean one file fetched under two URLs -- a
     double download, and a theme swap pulling the copy that was NOT
     refreshed.

AND THE FIXED-POINT BUG THIS FOUND

The first version of the tool made one pass. settings.js both CARRIES busts
and IS busted from index.html, so rewriting it changed its own bytes, changed
its hash, and left index.html's reference to it stale in the very pass that
fixed everything else -- "26 rewritten" followed immediately by "1 stale". It
now iterates to a fixed point, bounded, with a cycle between two files
reported rather than spun on.

    python test_cache_busts.py
"""
import hashlib
import io
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_FRONTEND = os.path.join(_ROOT, "frontend")

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


TOOL_PATH = os.path.join(_ROOT, "_bust_cache.py")
TOOL = io.open(TOOL_PATH, encoding="utf-8").read() if os.path.exists(TOOL_PATH) else ""

_REF = re.compile(
    r"/static/((?:js|css)/[A-Za-z0-9._-]+)\?v=([^\"'\s>]*)")


def _files():
    out = []
    idx = os.path.join(_FRONTEND, "index.html")
    if os.path.exists(idx):
        out.append(idx)
    jsd = os.path.join(_FRONTEND, "js")
    if os.path.isdir(jsd):
        out += [os.path.join(jsd, f) for f in sorted(os.listdir(jsd))
                if f.endswith(".js")]
    return out


# =============================================================================
print("=== 1. The tool exists and is wired the way it claims ===")
# =============================================================================
ok("_bust_cache.py is present", bool(TOOL))
ok("it scans frontend scripts, not just index.html",
   'glob("*.js")' in TOOL,
   "settings.js writes two of these URLs itself; index.html alone would let "
   "the two drift apart")
ok("it iterates to a fixed point",
   "_MAX_PASSES" in TOOL,
   "rewriting a file changes its own hash, so one pass leaves the reference "
   "TO that file stale")
ok("...and a reference cycle is reported, not spun on",
   "STILL STALE after" in TOOL)
ok("a bust pointing at a missing file is reported, never invented",
   "[BROKEN]" in TOOL,
   "inventing a hash for a file that is not there papers over a 404")
ok("references with no bust at all are surfaced", "[unmanaged]" in TOOL)


# =============================================================================
print("\n=== 2. Every bust equals its file's hash (recomputed here) ===")
# =============================================================================
_seen = {}          # rel -> {bust: [where]}
_bad = []
_missing = []
for _f in _files():
    _name = os.path.basename(_f)
    _text = io.open(_f, encoding="utf-8").read()
    for _rel, _bust in _REF.findall(_text):
        _seen.setdefault(_rel, {}).setdefault(_bust, []).append(_name)
        _target = os.path.join(_FRONTEND, _rel.replace("/", os.sep))
        if not os.path.exists(_target):
            _missing.append((_rel, _name))
            continue
        _want = hashlib.sha256(
            io.open(_target, "rb").read()).hexdigest()[:len(_bust)]
        if _bust != _want:
            _bad.append((_rel, _name, _bust, _want))

ok("at least one cache-bust was found to check", bool(_seen), _seen)
ok("no bust points at a file that does not exist", not _missing, _missing)
ok("every bust matches its file's content hash", not _bad,
   "stale: %r -- run: python _bust_cache.py" % (_bad,))

# THE COUPLING. This is the one a per-file check would miss entirely.
_split = {rel: v for rel, v in _seen.items() if len(v) > 1}
ok("a file referenced from several places has ONE bust in all of them",
   not _split,
   "%r -- the same file fetched under two URLs is a double download, and a "
   "theme swap would pull whichever copy was not refreshed" % (_split,))

_multi = {rel: sorted({w for ws in v.values() for w in ws})
          for rel, v in _seen.items()}
_multi = {k: v for k, v in _multi.items() if len(v) > 1}
ok("...and that case really is present, so the check above has teeth",
   bool(_multi), "no file is referenced from more than one place right now; "
                 "the assertion is still correct but proves nothing today")


# =============================================================================
print("\n=== 3. Busts are hashes, not versions ===")
# =============================================================================
# The whole point. A version-shaped bust means somebody hand-edited it back,
# or a file was added without running the tool.
_versionish = {rel: b for rel, v in _seen.items() for b in v
               if re.fullmatch(r"\d+\.\d+(\.\d+)?[a-z]?", b)}
ok("no bust is a hand-typed version number", not _versionish,
   "%r -- a version answers 'what release is this', which is not the question "
   "a cache-bust asks" % (_versionish,))
_short = {rel: b for rel, v in _seen.items() for b in v if len(b) < 8}
ok("every bust is long enough to be a real hash", not _short, _short)


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
