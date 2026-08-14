#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_atrest_call_sites.py -- every at-rest call is classified, and stays so.

Per-user encryption split the codebase in two. Some data belongs to a PROFILE
(chat, archives, uploads, evidence) and must be encrypted under that profile's
key -- reached by passing ``ns=`` to atrest. Some data is SYSTEM TIER (MFA
records read during login before anyone is authenticated, the audit chain,
CRAIID pipeline state written by background work with no signed-in user) and
must stay on the system key.

Both are correct. What is NOT correct is a call site nobody decided about --
because the default is ns=None, which silently means "the owner's key". That is
precisely the shape of the cross-profile archive leak found in v2.14.2:
``keyword_search`` read the owner's folder while listing another profile's
files, and every test passed.

So this file makes the classification a rule instead of a memory:

    every atrest call either passes ns=  OR  carries a SYSTEM TIER comment.

A new call site added later without either is a FAILURE here, and the fix is to
decide which tier it belongs to -- not to add the marker to quiet the test.
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Calls that read or write user-visible content. Key management itself
# (register_profile_key etc.) is not in scope: it TAKES the ns explicitly.
NS_AWARE = {"dump_json_encrypted", "load_json_auto", "encrypt_bytes",
            "decrypt_bytes", "read_file_auto"}
MODULE_NAMES = {"atrest", "_atrest"}
MARKER = "SYSTEM TIER"

SKIP_FILES = {"atrest.py"}          # the implementation itself


def _sources():
    for root in (HERE, os.path.join(HERE, "craiid")):
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if not name.endswith(".py"):
                continue
            if name.startswith("test_") or name in SKIP_FILES:
                continue
            yield os.path.join(root, name)


def _enclosing_ranges(tree):
    """(start, end) line span of every function, so a marker anywhere in the
    function that owns the call counts -- the comment usually sits above the
    try/except, not glued to the call."""
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans.append((node.lineno, getattr(node, "end_lineno", node.lineno)))
    return spans


_passed = _failed = 0


def check(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  PASS  " + label)
    else:
        _failed += 1
        print("  FAIL  " + label + "   " + str(detail))


per_profile = []
system_tier = []
unclassified = []

for path in _sources():
    src = io.open(path, encoding="utf-8", errors="replace").read()
    lines = src.splitlines()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        unclassified.append((os.path.basename(path), 0, "SYNTAX: %s" % e))
        continue
    spans = _enclosing_ranges(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute) or fn.attr not in NS_AWARE:
            continue
        if not (isinstance(fn.value, ast.Name) and fn.value.id in MODULE_NAMES):
            continue

        where = (os.path.basename(path), node.lineno, fn.attr)
        if any(k.arg == "ns" for k in node.keywords):
            per_profile.append(where)
            continue

        # Look for the marker inside the enclosing function; fall back to the
        # 15 lines above for calls at module level.
        lo, hi = node.lineno - 15, node.lineno
        for s, e in spans:
            if s <= node.lineno <= e:
                lo, hi = s, e
                break
        window = "\n".join(lines[max(0, lo - 1):hi])
        if MARKER in window:
            system_tier.append(where)
        else:
            unclassified.append(where)

print("\n=== 1. Every at-rest call site is classified ===")
for w in unclassified:
    print("        UNCLASSIFIED  %s:%s  %s" % w)
check("no unclassified atrest call sites", not unclassified,
      "%d found" % len(unclassified))

print("\n=== 2. Both tiers are actually populated ===")
# A rule that passes because one side is empty is not a rule. If either list
# empties out, something was converted wholesale and this file should be read
# again rather than trusted.
check("per-profile call sites exist", len(per_profile) >= 10,
      "%d" % len(per_profile))
check("system-tier call sites exist", len(system_tier) >= 5,
      "%d" % len(system_tier))

print("\n=== 3. The known system-tier modules are still system-tier ===")
# Named individually because each is a decision with a reason, and a silent
# flip to ns= would break login (mfa) or the audit chain (handoff_guard).
sys_files = set(f for f, _l, _a in system_tier)
for must in ("mfa.py", "handoff_guard.py", "craiid_author.py"):
    check("%-20s stays on the system key" % must, must in sys_files,
          str(sorted(sys_files)))

print("\n=== 4. The profile-content modules pass ns ===")
prof_files = set(f for f, _l, _a in per_profile)
for must in ("sage_engine.py", "sage_rag.py", "evidence_ledger.py",
             "data_export.py", "main.py", "comfyui_client.py"):
    check("%-20s passes ns" % must, must in prof_files, str(sorted(prof_files)))

print("\n  per-profile: %d   system-tier: %d   unclassified: %d"
      % (len(per_profile), len(system_tier), len(unclassified)))
print("\n  %d passed, %d failed\n" % (_passed, _failed))
sys.exit(1 if _failed else 0)
