#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_mcp_code_gate.py -- the Code Execution switch governs the MCP tool too.

WHY THIS EXISTS

`code_exec_enabled` is the switch in Settings -> Code Execution. The chat path
has always read it. `mcp_handlers._tool_code` never did, so an API client
holding a valid token (Continue.dev, Claude Desktop, curl) could run Python on
a machine where the owner had switched code execution OFF.

That is the same shape as the defect fixed in the executor in Phase 3a: a
control that says one thing and does another. The difference between "the
toggle is off" and "the toggle is off for some callers" is the whole value of
the toggle.

Two things are asserted, and the second is the one that would rot quietly:

  * OFF means off, ABSENT means off, ON means on;
  * a PROFILE's own answer is used, not the owner's. `code_exec_enabled` is a
    PER_USER key, so reading only the shared config would answer the owner's
    question on somebody else's behalf -- wrong in the unsafe direction, and
    invisible on a single-user install.

    python test_mcp_code_gate.py
"""
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
    import mcp_handlers as m
except Exception as e:                                    # pragma: no cover
    print("SKIP: backend not importable here (%s)" % e)
    sys.exit(0)


def call(ns=None):
    """Returns (it_ran, it_was_refused)."""
    txt = str(m.call_tool("code", {"code": "print('RAN', 6*7)"}, ns=ns))
    return ("RAN 42" in txt, "refused" in txt.lower())


_saved = main.config.get("code_exec_enabled")

print("=== 1. The switch is obeyed ===")
main.config["code_exec_enabled"] = False
ran, refused = call()
ok("OFF refuses", not ran and refused)
ok("...and says which switch", "Code Execution" in str(
    m.call_tool("code", {"code": "print(1)"})),
   "a refusal that does not name the fix wastes somebody's afternoon")

main.config["code_exec_enabled"] = True
ran, refused = call()
ok("ON runs", ran and not refused)

main.config.pop("code_exec_enabled", None)
ran, refused = call()
ok("ABSENT means OFF", not ran and refused,
   "a missing key is exactly the case where nobody has consented")

print("\n=== 2. It is the PROFILE's answer, not the owner's ===")
main.config["code_exec_enabled"] = True          # shared config says ON
_real_overlay = main._load_user_overlay
main._load_user_overlay = (
    lambda ns: {"code_exec_enabled": False} if ns == "alice" else {})
try:
    ran_a, refused_a = call(ns="alice")
    ok("alice's own OFF beats the shared ON", not ran_a and refused_a,
       "reading only the shared config answers the owner's question for "
       "somebody else, and does it in the unsafe direction")
    ran_b, _ = call(ns="bob")
    ok("bob, with no overlay, still gets the shared ON", ran_b,
       "a gate that refuses everyone is an outage, not a gate")
finally:
    main._load_user_overlay = _real_overlay

print("\n=== 3. The wiring, not just the helper ===")
_src = open(os.path.join(_HERE, "mcp_handlers.py"), encoding="utf-8").read()
ok("the gate runs BEFORE the executor is reached",
   _src.index("_code_exec_allowed(ns)") < _src.index("execute_python(code"),
   "a check after the call is not a check")
ok("`code` receives the namespace",
   '"code",' in _src.split("_NS_TOOLS = frozenset({")[1].split("})")[0],
   "without it the tool cannot know whose switch to read")
ok("absent-means-off is spelled out at every read",
   _src.count('"code_exec_enabled", False') >= 2,
   "the default belongs at the read, not in somebody's memory")

print("\n=== 4. Its sibling, for the record ===")
# NOT a failure, and deliberately not fixed here: web_search reaches the
# network from the same dispatcher and does not consult web_search_enabled.
# It is the same shape as this bug, it was found while fixing this one, and it
# is left as a decision for its owner rather than a change smuggled in beside
# an approved one. If someone gates it later, turn this into a real assertion.
_ws_gated = "web_search_enabled" in _src
print("  note   web_search consults web_search_enabled: %s" % _ws_gated)
print("         (same shape as this bug; not fixed here on purpose)")

if _saved is None:
    main.config.pop("code_exec_enabled", None)
else:
    main.config["code_exec_enabled"] = _saved

print("")
if _fails:
    print("%d CHECK(S) FAILED" % len(_fails))
    for f in _fails:
        print("   - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED - MCP code-execution gate")
sys.exit(0)
