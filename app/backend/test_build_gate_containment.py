# -*- coding: utf-8 -*-
"""v2.15 -- Build Battle gate-test containment.

The hole this closes: a Build Battle gate test is named by a line "GATE: <x>"
in the chat message, and `_bb_resolve_gate_path` used to try that value AS-IS
before anything else -- so an ABSOLUTE path resolved. The named file was then
read and written into a temp dir and EXECUTED as a Python subprocess, with
stdout and stderr returned to the caller.

/ws/chat requires only *a* session, not the owner's. So any authenticated
profile could read and execute an arbitrary file as the backend process, which
can read every other profile's keys -- around per-profile encryption rather
than through it. It also composed with api_save_to_downloads, whose filename
scrub permits ".py": write the file, then name it.

The gate-test FEATURE is the point of Build Battle's execute-gate (submissions
have to pass a real test rather than win on prose), so these tests assert both
halves: that naming a project test file still works, and that naming anything
else does not.

Functions are lifted from main.py by AST rather than imported, so this runs
without standing up the whole app.

    python test_build_gate_containment.py
"""
import ast
import io
import os
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
MAIN = os.path.join(HERE, "main.py")

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


# --- lift the three functions under test out of main.py ---------------------
SRC = io.open(MAIN, encoding="utf-8").read()
TREE = ast.parse(SRC)
WANT = {"_within", "_bb_gate_roots", "_bb_resolve_gate_path"}
WANT_CONST = {"_BB_GATE_SUBDIRS"}
defs = [n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name in WANT]
consts = [n for n in TREE.body if isinstance(n, ast.Assign)
          and any(isinstance(t, ast.Name) and t.id in WANT_CONST for t in n.targets)]
missing = (WANT - {n.name for n in defs}) | (
    WANT_CONST - {t.id for n in consts for t in n.targets if isinstance(t, ast.Name)})
if missing:
    print("FATAL: could not find %s in main.py" % sorted(missing))
    sys.exit(1)

NS = {"Path": Path, "os": os, "__file__": MAIN}
exec(compile(ast.Module(body=consts + defs, type_ignores=[]), MAIN, "exec"), NS)
resolve = NS["_bb_resolve_gate_path"]


print("=== 1. The feature still works ===")
real = "test_relay.py"
got = resolve(real)
ok("a project test file resolves", got is not None, got)
ok("it resolves INSIDE the backend directory",
   got is not None and Path(got).parent.resolve() == Path(HERE).resolve(), got)
ok("surrounding quotes are tolerated", resolve('"%s"' % real) == got)
ok("whitespace is tolerated", resolve("  %s  " % real) == got)


print("\n=== 2. Absolute paths -- THE capability being removed ===")
for probe in (
    os.path.join(HERE, "test_relay.py"),        # absolute, real, in-tree
    r"C:\Windows\System32\drivers\etc\hosts",
    "/etc/passwd",
    r"C:\Users\anyone\AppData\Roaming\sage_data\test_x.py",
    r"\\server\share\test_evil.py",
):
    ok("absolute path refused: %s" % probe[:44], resolve(probe) is None, resolve(probe))


print("\n=== 3. Traversal and separators ===")
for probe in ("../test_relay.py", "..\\test_relay.py", "gates/../test_relay.py",
              "sub/test_relay.py", "sub\\test_relay.py", "../../test_relay.py",
              "..", ".", "test_relay.py/../../test_relay.py"):
    ok("refused: %r" % probe, resolve(probe) is None, resolve(probe))


print("\n=== 4. Only test_*.py is runnable ===")
for probe in ("main.py", "config.json", "keywrap.py", "atrest.py",
              "relay_client.py", "test_relay.txt", "notatest.py", "test_relay",
              "TEST_RELAY.PY"):
    ok("refused: %r" % probe, resolve(probe) is None, resolve(probe))


print("\n=== 5. Degenerate input ===")
for probe in (None, "", "   ", "\x00", "test_\x00.py", "test_relay.py\x00.txt"):
    ok("refused: %r" % probe, resolve(probe) is None, resolve(probe))


print("\n=== 6. A file that does not exist is not invented ===")
ok("unknown test name -> None", resolve("test_does_not_exist_here.py") is None)


print("\n=== 7. The gate test is inspected by Customs too ===")
# It enters the same subprocess as the candidate; until v2.15 only the candidate
# was inspected. Source-level assertion: _bb_run_gate must inspect BOTH.
gate_fn = [n for n in TREE.body
           if isinstance(n, ast.AsyncFunctionDef) and n.name == "_bb_run_gate"]
ok("_bb_run_gate found", bool(gate_fn))
if gate_fn:
    body = ast.dump(gate_fn[0])
    ok("customs_daemon.inspect is called twice (candidate + gate test)",
       body.count("'inspect'") >= 2, body.count("'inspect'"))
    ok("the gate test content is what the second call inspects",
       "build_battle_gate" in body)


print("\n=== 8. Regression guard on the original defect ===")
fn_src = ast.get_source_segment(SRC, [n for n in defs if n.name == "_bb_resolve_gate_path"][0])
ok("the resolver no longer builds a candidate list starting with the raw value",
   "cands = [p]" not in (fn_src or ""))
ok("the resolver checks isabs", "isabs" in (fn_src or ""))
ok("the resolver confirms containment with _within", "_within" in (fn_src or ""))


_p = sum(1 for _, c in _results if c)
_f = len(_results) - _p
print("\n%d/%d passed." % (_p, len(_results)))
sys.exit(1 if _f else 0)
