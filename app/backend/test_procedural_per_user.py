#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_procedural_per_user.py -- one profile's procedures stay that profile's.

WHAT WAS WRONG

ProceduralMemory has accepted `owner_ns` since v2.14.2, and the docstring in
its own __init__ says:

    "The JSON store is one per namespace: procedures are user DATA, and one
     profile's dead-end cache has no business in another's."

The capability was built and correct. It was never wired. main.py constructed
ONE instance with no ns and every profile shared it -- so every profile's
`user_request` (up to 500 chars of their verbatim message) and
`final_answer_preview` sat in a file every other profile's session read.

FOURTH INSTANCE OF THE SAME SHAPE IN THIS RELEASE

  * the reasoning hook covered the streaming path; every agentic turn went
    around it
  * the at-rest call-site guard audited how atrest calls were written and could
    not see a module that made none
  * the thinking budget reached tier_lifecycle's respawn path, not the boot
    spawner every install actually uses
  * this: a per-namespace store constructed once, globally

Right code, wrong coverage, every time. And every time, a test asserting the
code EXISTED passed. Section 4 is written the other way round -- it asserts the
CALLERS pass a namespace, because that is the half that was missing.

MIGRATION IS A NO-OP BY CONSTRUCTION. The owner keeps sage_data/
procedural_memory and owner_ns=None resolves to the system key, so the 71
entries already on disk still open. Profiles get their own store on first use.

    python test_procedural_per_user.py
"""
import ast
import io
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


MAIN = io.open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()
PM = io.open(os.path.join(_HERE, "procedural_memory.py"), encoding="utf-8").read()
DAEMON = io.open(os.path.join(_HERE, "sage_daemon.py"), encoding="utf-8").read()

from procedural_memory import ProceduralMemory                # noqa: E402
import atrest                                                 # noqa: E402


# =============================================================================
print("=== 1. The store encrypts under the PROFILE's key ===")
# =============================================================================
ok("_load passes the owner's namespace",
   "load_json_auto(f.read(), ns=self.owner_ns)" in PM)
ok("_save passes it too", "dump_json_encrypted(self._knowledge_base,\n"
   "                                                ns=self.owner_ns)" in PM
   or "ns=self.owner_ns)" in PM)
ok("verify_integrity uses the SAME ns as _load",
   PM.count("ns=self.owner_ns") >= 3, PM.count("ns=self.owner_ns"))
ok("no at-rest call is left on the system key by default",
   "load_json_auto(f.read())" not in PM
   and "dump_json_encrypted(self._knowledge_base)" not in PM)
ok("the earlier SYSTEM TIER label is corrected, not silently replaced",
   "CORRECTION" in PM and "SYSTEM TIER" in PM,
   "the wrong classification should stay visible as history -- it explains "
   "how the store came to be shared")


# =============================================================================
print("\n=== 2. Real files, real isolation ===")
# =============================================================================
_TMP = tempfile.mkdtemp(prefix="vai_pm_")
try:
    _owner_dir = os.path.join(_TMP, "owner")
    _alice_dir = os.path.join(_TMP, "users", "alice")

    _own = ProceduralMemory(storage_dir=_owner_dir, owner_ns=None)
    _own.add_procedure("task:o:owner_secret_request",
                       {"user_request": "OWNER-PRIVATE-TEXT"})

    _al = ProceduralMemory(storage_dir=_alice_dir, owner_ns=None)
    _al.add_procedure("task:a:alice_request",
                      {"user_request": "ALICE-PRIVATE-TEXT"})

    ok("each store writes its own file",
       os.path.exists(os.path.join(_owner_dir, "procedural.json"))
       and os.path.exists(os.path.join(_alice_dir, "procedural.json")))

    _raw_o = io.open(os.path.join(_owner_dir, "procedural.json"), "rb").read()
    ok("the owner's file is ciphertext", atrest.is_encrypted(_raw_o))
    ok("...with no verbatim text in the bytes",
       b"OWNER-PRIVATE-TEXT" not in _raw_o)

    ok("the owner's store does not contain alice's procedure",
       "task:a:alice_request" not in _own.list_procedures("successful"))
    ok("alice's store does not contain the owner's",
       "task:o:owner_secret_request" not in _al.list_procedures("successful"))

    _raw_a = io.open(os.path.join(_alice_dir, "procedural.json"), "rb").read()
    ok("neither file carries the other's text",
       b"ALICE-PRIVATE-TEXT" not in _raw_o
       and b"OWNER-PRIVATE-TEXT" not in _raw_a)

    # Reopening must not merge them.
    ok("reopening keeps them separate",
       ProceduralMemory(storage_dir=_owner_dir).get_procedure(
           "task:a:alice_request") is None)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)


# =============================================================================
print("\n=== 3. main.py resolves a per-profile path ===")
# =============================================================================
_t = ast.parse(MAIN)
_mods = {n.name for n in _t.body
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
ok("_procedural_dir exists", "_procedural_dir" in _mods)
ok("procedural_for exists", "procedural_for" in _mods)
ok("the path derives from sage_engine's resolver",
   "_se.user_data_dir(ns)" in MAIN,
   "rebuilding it here would strand the store if conversations move")
ok("the owner falls back to the existing install-wide path",
   "return Path(PROCEDURAL_DIR)" in MAIN,
   "this is what makes the migration a no-op")
ok("instances are cached per namespace", "_PROCEDURAL_BY_NS" in MAIN)
ok("...and the cache is locked",
   "_PROCEDURAL_LOCK" in MAIN and "with _PROCEDURAL_LOCK:" in MAIN,
   "two turns on different profiles can arrive at once, and two instances "
   "over one file would each hold a stale half of it")
ok("the namespace is passed to the constructor", "owner_ns=ns," in MAIN)
ok("the module-level singleton is now explicitly the OWNER's",
   "procedural = procedural_for(None)" in MAIN)


# =============================================================================
print("\n=== 4. THE CALLERS pass a namespace (the half that was missing) ===")
# =============================================================================
# Asserting that procedural_for EXISTS would have passed before this fix too --
# ProceduralMemory already took owner_ns. What was wrong was the call sites.
_all_funcs = [n for n in ast.walk(_t)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _enclosing_early(line):
    """Innermost function containing `line`, or None if module level."""
    best = None
    for n in _all_funcs:
        if n.lineno <= line <= (n.end_lineno or n.lineno):
            if best is None or n.lineno > best.lineno:
                best = n
    return best

# Found on the AST, by ENCLOSING SCOPE. A line-based version flagged the boot
# banner's wrapped continuation line -- it did not carry the "[PROCEDURAL]"
# marker the filter keyed on, because the f-string spilled onto a second line.
# Fifth time this release that an assertion keyed on text layout instead of
# structure. Wrapping is not a behaviour change; the parser knows the scope.
#
# The real rule: at MODULE level, `procedural` is the owner's store and using
# it is correct (that is the boot banner). Inside a function that serves a
# request, it is a bug -- that code runs for whichever profile is talking.
_bare = []
for _n in ast.walk(_t):
    if isinstance(_n, ast.Attribute) and isinstance(_n.value, ast.Name) \
            and _n.value.id == "procedural":
        _fn = _enclosing_early(_n.lineno)
        if _fn is not None:
            _bare.append((_n.lineno, _fn.name, _n.attr))
ok("no request-scoped caller uses the bare owner store", not _bare,
   "%r -- each of these runs for whichever profile is talking, so it must "
   "pass that turn's ns" % (_bare,))
ok("the boot banner still reads the OWNER's store at module level",
   any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
       and n.value.id == "procedural" and _enclosing_early(n.lineno) is None
       for n in ast.walk(_t)),
   "the startup count is legitimately the owner's and should stay")

_ns_calls = MAIN.count("procedural_for(_ws_ns)")
ok("the per-turn call sites pass the turn's namespace", _ns_calls >= 7,
   "found %d" % _ns_calls)

# And the namespace has to be in scope wherever it is used.
_funcs = [n for n in ast.walk(_t)
          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _enclosing(line):
    best = None
    for n in _funcs:
        if n.lineno <= line <= (n.end_lineno or n.lineno):
            if best is None or n.lineno > best.lineno:
                best = n
    return best


_lines = MAIN.splitlines()
_call_lines = [i + 1 for i, l in enumerate(_lines)
               if "procedural_for(_ws_ns)" in l]
_ws = [n for n in _funcs if n.name == "ws_chat"]
ok("ws_chat was found", bool(_ws))
if _ws and _call_lines:
    _lo, _hi = _ws[0].lineno, _ws[0].end_lineno
    ok("every _ws_ns call site is inside ws_chat (or nested in it)",
       all(_lo <= c <= _hi for c in _call_lines),
       [c for c in _call_lines if not (_lo <= c <= _hi)])
    _assign = min(i + 1 for i, l in enumerate(_lines) if "_ws_ns =" in l)
    ok("...and after _ws_ns is assigned", _assign < min(_call_lines),
       "assigned %d, first use %d" % (_assign, min(_call_lines)))


# =============================================================================
print("\n=== 5. The daemon's scope is stated, not assumed ===")
# =============================================================================
ok("the daemon names its store as the OWNER's",
   "the OWNER's procedural store" in DAEMON)
ok("it explains why a profile's store is unreachable",
   "registered only" in DAEMON or "unlocked" in DAEMON,
   "a background process cannot hold a profile key -- that is the encryption "
   "working, not a gap")
ok("...and records it as an accepted trade",
   "Accepted trade" in DAEMON,
   "an unstated limitation becomes a bug report later")
ok("the key alignment is explained rather than left to luck",
   "resolves to the system key" in DAEMON)


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
