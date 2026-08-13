# -*- coding: utf-8 -*-
"""v2.15 -- exception text must not leave the process.

CodeQL py/stack-trace-exposure #153/#155/#156/#157/#158. Four surfaces were
returning raw exception text to callers:

  /api/hardware          hw_utils sets info["error"] = str(e) on a failed probe
  POST .../burn          the outer handler appended str(e), and burn walks
                         per-profile directories, so it could name other profiles
  /api/build/integrity   f"{type(e).__name__}: {e}" -- mostly filesystem-shaped
  /mcp/v1/jsonrpc        f"Internal error: {type(e).__name__}: {e}" on a
                         TOKEN-authenticated surface: the widest of the four

The pattern applied: full text to the server log, a correlation ref to the
caller. /api/hardware is the exception -- its probe text is what identified the
missing MSVC runtime, so the OWNER keeps it and other profiles do not.

Assertions are source-level where the code path needs a live app, and
functional where it does not.

    python test_stack_trace_containment.py
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


def src(f):
    return io.open(os.path.join(HERE, f), encoding="utf-8").read()


def fn_source(tree, source, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return ast.get_source_segment(source, n) or ""
    return ""


MAIN = src("main.py"); MAIN_T = ast.parse(MAIN)
BI = src("build_integrity.py"); BI_T = ast.parse(BI)
MCP = src("mcp_handlers.py"); MCP_T = ast.parse(MCP)


print("=== #153  /api/hardware -- probe errors are owner-only ===")
hw = fn_source(MAIN_T, MAIN, "api_hardware")
ok("api_hardware takes the request", "request" in hw.split("\n")[0] if hw else False, hw[:80])
ok("it asks whether the caller is the owner", "_is_owner(request)" in hw)
ok("non-owners get a scrubbed report", "_scrub_probe_errors" in hw)

# functional: the scrubber itself
NS = {}
exec(compile(ast.Module(
    body=[n for n in MAIN_T.body
          if isinstance(n, ast.FunctionDef) and n.name == "_scrub_probe_errors"],
    type_ignores=[]), "main.py", "exec"), NS)
scrub = NS["_scrub_probe_errors"]

probe = {"nvidia": {"available": False, "error": "WinError 126 C:\\Users\\x\\ggml.dll"},
         "amd": {"available": True, "name": "RX 7900"},
         "tiers": [{"error": "traceback ..."}, {"ok": True}],
         "count": 2}
out = scrub(probe)
ok("the nested probe error string is replaced",
   out["nvidia"]["error"] == "(probe error hidden; see server log)", out["nvidia"])
ok("the original path text is gone entirely",
   "WinError" not in repr(out) and "C:\\Users" not in repr(out), out)
ok("errors inside lists are scrubbed too",
   out["tiers"][0]["error"] == "(probe error hidden; see server log)", out["tiers"])
ok("non-error fields survive untouched",
   out["amd"] == {"available": True, "name": "RX 7900"} and out["count"] == 2, out)
ok("shape is preserved (the UI still renders)",
   set(out) == set(probe) and isinstance(out["tiers"], list), out)
ok("the scrubber does not mutate its input",
   probe["nvidia"]["error"].startswith("WinError"), probe["nvidia"])


print("\n=== #155  burn -- the outer handler ===")
ok("burn's catch-all uses _safe_detail, not str(e)",
   "errors.append(_safe_detail(e" in MAIN)
ok("no bare errors.append(str(e)) remains", "errors.append(str(e))" not in MAIN)


print("\n=== #156  build_integrity.verify ===")
vf = fn_source(BI_T, BI, "verify")
ok("no raw exception interpolation remains",
   'f"{type(e).__name__}: {e}"' not in vf and "{e}\"" not in vf.replace('{type(e).__name__}', ''), vf[-200:])
ok("the failure is logged server-side", "getLogger" in vf)
ok("the caller gets a correlation ref", "ref %s" in vf or "ref {" in vf)
ok("the exception TYPE is still reported (useful, not sensitive)",
   "type(e).__name__" in vf)


print("\n=== #157/#158  MCP JSON-RPC ===")
jr = fn_source(MCP_T, MCP, "handle_jsonrpc")
ok("the -32603 reply no longer interpolates the exception",
   "Internal error: {type(e).__name__}: {e}" not in jr, jr[-260:])
ok("the failure is logged server-side with the method name",
   "getLogger" in jr and "method" in jr)
ok("the caller gets a correlation ref", "ref %s" in jr)


print("\n=== the pattern held across all four ===")
ok("no surface still interpolates a bare exception into a client reply",
   'f"Internal error: {type(e).__name__}: {e}"' not in MCP
   and 'res["error"] = f"{type(e).__name__}: {e}"' not in BI)

_p = sum(1 for _, c in _results if c)
print("\n%d/%d passed." % (_p, len(_results)))
sys.exit(1 if _p != len(_results) else 0)
