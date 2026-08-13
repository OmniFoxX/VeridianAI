"""
test_action_intent.py -- SUB-TASK 2 gate: requested/opportunistic tagging
=========================================================================

Run: python test_action_intent.py
Separate from test_intent_scope.py (sub-task 1) by design.

Centerpiece: a full regression model of the 2026-07-17 fibonacci
incident -- standing observe-only instruction present, verify failure
triggers a retry, the retry's path-fix must tag `requested` and the
unrelated print->return rewrite must tag `opportunistic` (spec-
regression rule: 'print the first 11 numbers' literally asked for
printing). Also verifies the intent field lands in the Customs
hash-chain event.
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import intent_scope as isc  # noqa: E402

PASS = 0
FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f"FAIL  {label}  {extra}")


USER = "write a python script that prints the first 11 fibonacci numbers, save it and verify it"

PRINT_VERSION = (
    "def fib(n):\n"
    "    seq = [0, 1]\n"
    "    for _ in range(n - 2):\n"
    "        seq.append(seq[-1] + seq[-2])\n"
    "    for x in seq[:n]:\n"
    "        print(x)\n\n"
    "fib(11)\n")

# The opportunistic rewrite: print-based output replaced by return-based
# list -- never requested, and it deletes the literally-requested print.
RETURN_VERSION = (
    "def fib(n):\n"
    "    seq = [0, 1]\n"
    "    for _ in range(n - 2):\n"
    "        seq.append(seq[-1] + seq[-2])\n"
    "    return seq[:n]\n\n"
    "result = fib(11)\n")

# Path-fix retry: same code, only the save target/path corrected.
PRINT_VERSION_PATHFIX = PRINT_VERSION

SCOPED = ["code_review"]   # standing observe-only instruction present

print("== fibonacci incident regression ==")
# Step 1: initial save -- linked to request ("save", "script", "python")
lab, why = isc.classify_action("save_file", "fibonacci_first_11.py",
                               USER, {}, {}, SCOPED, PRINT_VERSION)
check("initial save is requested", lab == "requested", f"{lab}: {why}")

# Step 2: verify fails (wrong path) -- recorded in the turn ledger
turn_saves = {"fibonacci_first_11.py": [
    {"content": PRINT_VERSION, "ok": True}]}
results = {"verify_file:fibonacci_first_11.py":
           "[ERROR] file not found at fibonacci_first_11.py"}

# Step 3a: the GOOD retry -- path fix only, content essentially same
lab, why = isc.classify_action("save_file", "fibonacci_first_11.py",
                               USER, turn_saves, results, SCOPED,
                               PRINT_VERSION_PATHFIX)
check("path-fix retry is requested (recovery)", lab == "requested",
      f"{lab}: {why}")
check("recovery reason mentions retry", "retry" in why, why)

# Step 3b: the ACTUAL incident -- retry that also rewrites print->return
lab, why = isc.classify_action("save_file", "fibonacci_first_11.py",
                               USER, turn_saves, results, SCOPED,
                               RETURN_VERSION)
check("print->return rewrite is opportunistic", lab == "opportunistic",
      f"{lab}: {why}")
check("reason cites the literal spec term",
      "'print'" in why, why)
check("standing instruction cited as candidate cause",
      "code_review" in why, why)

# Step 3c: verify passed + resave anyway = redo beyond request
results_ok = {"verify_file:fibonacci_first_11.py":
              "[VERIFY OK] file exists, AST parses clean"}
lab, why = isc.classify_action("save_file", "fibonacci_first_11.py",
                               USER, turn_saves, results_ok, SCOPED,
                               RETURN_VERSION)
check("resave after clean verify is opportunistic",
      lab == "opportunistic", f"{lab}: {why}")

print("== spec regression beats small-diff similarity ==")
# print->return on this file is a small diff (high similarity) -- the
# spec-term rule must catch it even though drift alone would not.
import difflib
ratio = difflib.SequenceMatcher(None, PRINT_VERSION,
                                RETURN_VERSION).ratio()
check(f"fixture is a small diff (ratio {ratio:.2f} >= floor)",
      ratio >= isc.DRIFT_RATIO_FLOOR,
      "fixture needs adjusting")

print("== non-save classifications ==")
lab, _ = isc.classify_action("verify_file", "downloads/fib.py", USER,
                             turn_saves, {}, SCOPED)
check("verify after save is requested", lab == "requested")
lab, _ = isc.classify_action("search", "fibonacci algorithm", USER,
                             {}, {}, SCOPED)
check("read-only search is requested (decomposition)",
      lab == "requested")
lab, why = isc.classify_action("generate_image", "a nice spiral",
                               USER, {}, {}, SCOPED)
check("unrequested image gen is opportunistic", lab == "opportunistic",
      f"{lab}: {why}")
lab, _ = isc.classify_action("save_file", "notes.txt",
                             "jot this down in a file for me",
                             {}, {}, [])
check("save with request linkage, no instructions cited",
      lab == "requested")

print("== v2.13.4 proliferation rule (BugSquashNote cascade) ==")
USER_NOTE = "save a bug squash note as BugSquashNote.txt and verify it"
cascade_saves = {"BugSquashNote.txt": [
    {"content": "note v1", "ok": True},
    {"content": "note v2", "ok": True}]}
cascade_results = {"verify_file:BugSquashNote.txt":
                   "[VERIFY FAILED] File not found: "
                   "E:\\VeridianAI_v2.12\\BugSquashNote.txt"}
lab, why = isc.classify_action("save_file", "squash_cheer.txt",
                               USER_NOTE, cascade_saves,
                               cascade_results, SCOPED, "yay bugs")
check("new unnamed file mid-cascade is opportunistic",
      lab == "opportunistic", f"{lab}: {why}")
check("reason names the cascade + failed file",
      "mid-retry-cascade" in why and "BugSquashNote.txt" in why, why)
lab, why = isc.classify_action("save_file", "BugSquashNote.txt",
                               USER_NOTE, {}, {}, SCOPED, "note v1")
check("user-named file stays requested",
      lab == "requested", f"{lab}: {why}")
lab, why = isc.classify_action("save_file", "second_file.txt",
                               "save two files for me please",
                               {"first.txt": [{"content": "a",
                                              "ok": True}]},
                               {}, [], "b")
check("second file WITHOUT any failure is not cascade-flagged",
      "mid-retry-cascade" not in why, f"{lab}: {why}")

print("== classifier never raises ==")
lab, why = isc.classify_action(None, None, None, None, None, None, None)
check("all-None input yields quiet label", lab in ("requested",
      "opportunistic"), f"{lab}: {why}")

print("== intent field rides the Customs hash-chain event ==")
try:
    import customs_daemon as cd
    tmp = Path(tempfile.mkdtemp(prefix="intent_chain_"))
    cd.set_runtime(lambda k, d=None: True if k == "customs_enabled"
                   else d, tmp)
    cd._ledger.clear()
    r = cd.inspect_tag("save_file",
                       ("fib.py", RETURN_VERSION), "agentic",
                       intent="opportunistic")
    check("gated call still allowed", r.allowed, r.verdict)
    chain = tmp / "handoff_audit.log"
    blob = chain.read_text(encoding="utf-8") if chain.exists() else ""
    check("chain event carries intent field",
          '\\"intent\\": \\"opportunistic\\"' in blob, blob[:200])
    r2 = cd.inspect("browse", {"url": "https://example.com"}, "mcp")
    blob = chain.read_text(encoding="utf-8")
    check("intent omitted when not supplied (MCP baseline unchanged)",
          blob.count('\\"intent\\"') == 1)
    shutil.rmtree(tmp, ignore_errors=True)
except Exception as e:
    check("customs intent integration", False,
          f"{type(e).__name__}: {e}")

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
