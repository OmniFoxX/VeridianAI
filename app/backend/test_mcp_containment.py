# -*- coding: utf-8 -*-
"""v2.14.2 -- MCP namespace containment.

The hole this closes: /mcp/v1/jsonrpc never asked whose token it was, so under
multi-user ANY valid token read and wrote the owner's archives, downloads and
procedural memory. Unlike the chat path (which was broken and therefore failed
closed), MCP worked perfectly and quietly crossed profiles.

These tests assert the plumbing rather than the storage: that every tool
touching per-profile data receives the caller's namespace, that stateless tools
do not, and that the browser profile is bound at the dispatch boundary.

    python test_mcp_containment.py
"""
import ast
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


# --- load call_tool + the ns tools against instrumented storage -------------
SRC = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "mcp_handlers.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)

seen = []          # (tool, target, ns)


class _Toga:
    def search_all_archives(self, q, ns=None):
        seen.append(("search_memory", "archives", ns)); return []

    def save_to_downloads(self, f, c, ns=None):
        seen.append(("save_file", "downloads", ns))
        return {"success": True, "filename": f, "path": "/x", "size": 1}

    def set_browser_ns(self, ns):
        seen.append(("<dispatch>", "browser_ns", ns))


class _PM:
    def __init__(self, ns): self.ns = ns
    def get_procedure(self, k, category=None):
        seen.append(("recall", "procedural", self.ns)); return None
    def add_procedure(self, **kw):
        seen.append(("remember", "procedural", self.ns)); return True


def _load():
    env = {"__name__": "mcp_handlers", "Dict": dict, "Any": object,
           "json": __import__("json"), "traceback": __import__("traceback")}
    want = {"_result_text", "_tool_search_memory", "_tool_save_file",
            "_tool_recall", "_tool_remember", "_tool_remember_fail", "call_tool"}
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name in want:
            exec(compile(ast.Module([n], []), "<t>", "exec"), env)
        if isinstance(n, ast.Assign) and any(
                getattr(t, "id", None) == "_NS_TOOLS" for t in n.targets):
            exec(compile(ast.Module([n], []), "<t>", "exec"), env)
    env["_sage"] = lambda: _Toga()
    env["_procedural_memory"] = lambda ns=None: _PM(ns)
    env["_DISPATCH"] = {
        "search_memory": env["_tool_search_memory"],
        "save_file":     env["_tool_save_file"],
        "recall":        env["_tool_recall"],
        "remember":      env["_tool_remember"],
        "search":        lambda a: env["_result_text"]("stateless"),
        "code":          lambda a: env["_result_text"]("stateless"),
    }
    return env


ENV = _load()
call_tool = ENV["call_tool"]
NS_TOOLS = ENV["_NS_TOOLS"]

ARGS = {"search_memory": {"query": "x"},
        "save_file": {"filename": "a.txt", "content": "c"},
        "recall": {"query": "k"},
        "remember": {"key": "k", "value": "v"},
        "search": {"query": "web"},
        "code": {"code": "1"}}


def run(tool, ns):
    seen.clear()
    call_tool(tool, ARGS[tool], ns=ns)
    return ([s for s in seen if s[0] != "<dispatch>"],
            [s for s in seen if s[0] == "<dispatch>"])


print("== every namespace reaches storage intact ==")
for tool in ("search_memory", "save_file", "recall", "remember"):
    for who in (None, "alice", "bob"):
        store, _ = run(tool, who)
        got = {s[2] for s in store}
        ok("%-14s ns=%-7r -> storage sees %r" % (tool, who, who),
           got == {who}, got)

print("\n== cross-profile isolation ==")
store, _ = run("search_memory", "alice")
ok("alice's read never carries bob's namespace",
   all(s[2] != "bob" for s in store))
ok("alice's read never carries the owner's (None)",
   all(s[2] is not None for s in store))

print("\n== stateless tools get NO namespace ==")
for tool in ("search", "code"):
    store, _ = run(tool, "alice")
    ok("%s touches no per-profile storage" % tool, store == [], store)

print("\n== the browser profile is bound at the dispatch boundary ==")
for tool in ("search_memory", "search"):
    _, br = run(tool, "alice")
    ok("%-14s binds browser_ns=alice" % tool,
       br and br[0][2] == "alice", br)
_, br = run("search", None)
ok("owner call binds browser_ns=None", br and br[0][2] is None, br)

print("\n== _NS_TOOLS is the audit list, and it is complete ==")
# It held exactly five for as long as the list meant "touches per-profile
# DATA". `code` joined it in 2026-08-30 for a different reason and the
# assertion was rewritten in the same commit rather than loosened: the tool
# stores nothing, but the switch that governs it (`code_exec_enabled`) is a
# PER_USER key, so without the namespace it would read the OWNER's answer to
# somebody else's question -- wrong in the unsafe direction, and invisible on a
# single-user install.
#
# Kept as an exact set on purpose. This is the list somebody is meant to look
# at when adding a tool, and a membership test that only checks the five would
# not notice a sixth arriving by accident.
ok("contains exactly the tools that need a namespace",
   set(NS_TOOLS) == {"search_memory", "save_file", "recall", "remember",
                     "remember_fail", "code"}, set(NS_TOOLS))
ok("...and `code` is there for its SWITCH, not for storage",
   "code" in NS_TOOLS,
   "see test_mcp_code_gate.py: the profile's own code_exec_enabled decides")
ok("is a frozenset (cannot be mutated at runtime)",
   isinstance(NS_TOOLS, frozenset), type(NS_TOOLS))

print("\n== access policy: mcp_allowed ==")
import access_policy as ap
ok("default is ON (parity by default)", ap.DEFAULTS.get("mcp_allowed") is True)
clean, err = ap.validate_patch({"mcp_allowed": False})
ok("accepts a boolean", err is None and clean.get("mcp_allowed") is False, (clean, err))
clean, err = ap.validate_patch({"mcp_allowed": "yes"})
ok("rejects a non-boolean", err is not None, (clean, err))

print("\n== the ns-aware storage signatures exist ==")
import inspect
import sage_engine as se
ok("search_all_archives(query, ns=)",
   "ns" in inspect.signature(se.search_all_archives).parameters)
ok("save_to_downloads(filename, content, ns=)",
   "ns" in inspect.signature(se.save_to_downloads).parameters)
ok("downloads_dir_for(ns) exists", hasattr(se, "downloads_dir_for"))
from procedural_memory import ProceduralMemory
ok("ProceduralMemory(owner_ns=)",
   "owner_ns" in inspect.signature(ProceduralMemory.__init__).parameters)

bad = [n for n, c in _results if not c]
print("\n%d/%d passed." % (len(_results) - len(bad), len(_results)))
if bad:
    print("FAILED:")
    for n in bad:
        print("  - " + n)
    sys.exit(1)
