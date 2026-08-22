#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_no_content_leak.py -- exception text and chat content stay out of sight.

TWO CodeQL FINDINGS, ONE PRINCIPLE

  #190/#191/#192  py/stack-trace-exposure    main.py  (mine, same day)
  #189            py/clear-text-logging      memory_logger_surprise.py

Different rules, same mistake: something that knows too much got handed to a
place that shows it to someone.

WHAT WAS WRONG, PART ONE

verify_reasoning_provenance and verify_reasoning_ledger_entry ended their
except branches with

    out["message"] = f"{type(_e).__name__}: {_e}"

and three endpoints returned that dict straight to the browser. So a chain-walk
failure -- a file permission error, a missing path -- put the path in an API
response. CodeQL flagged the three returns; the cause was one shared line,
plus a third site it did NOT flag (_chain_has_reasoning's "chain walk failed:
%s"), which would have been left behind by anyone fixing only what was
reported.

main.py already had _safe_detail for exactly this: log the real exception with
a correlation ref, return the ref. It was used by every other endpoint in the
file. These three were written without it.

WHAT WAS WRONG, PART TWO

memory_logger_surprise printed stripped[:80] of content it was refusing to
chain. The guard proves the string STARTS with an error prefix; it does not
prove the rest is free of conversation. An inference backend that echoes part
of a prompt inside its error text would put that prompt on stdout in the
clear -- in an app whose whole posture is that conversation stays encrypted at
rest. The terminal is the one place none of that protection reaches.

Nothing diagnostic was lost by removing it: the matched PREFIX is what says
which tier failed, and that is why the line exists.

    python test_no_content_leak.py
"""
import ast
import io
import os
import re
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
MLS = io.open(os.path.join(_HERE, "memory_logger_surprise.py"),
              encoding="utf-8").read()
_TREE = ast.parse(MAIN)

SECRET = r"C:\clinic\patients\jane_doe_notes.json"


# =============================================================================
print("=== 1. No returned message carries raw exception text ===")
# =============================================================================
# The pattern itself, anywhere in main.py. Written broadly on purpose: the bug
# was one idiom repeated, and the fix is only real if the idiom is gone.
_raw = re.findall(r'\[.message.\]\s*=\s*f?"[^"\n]*\{_?e[!:}]', MAIN)
ok("no 'message' is assigned raw exception text", not _raw, _raw)
_walk = re.findall(r'"[^"\n]*failed[^"\n]*%s"\s*%\s*\(?_?\w*err', MAIN)
ok("...including the chain-walk failure CodeQL did NOT flag", not _walk,
   "%r -- fixing only the reported lines would have left this one" % (_walk,))

_verifiers = [n for n in _TREE.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name in ("verify_reasoning_provenance",
                             "verify_reasoning_ledger_entry",
                             "_chain_has_reasoning")]
ok("all three verifier functions were found", len(_verifiers) == 3)
for _v in _verifiers:
    _s = ast.get_source_segment(MAIN, _v) or ""
    if "except" not in _s:
        continue
    ok("%s routes failures through _safe_detail" % _v.name,
       "_safe_detail(" in _s,
       "the helper exists in this very file and every other endpoint uses it")


# =============================================================================
print("\n=== 2. Run it: force a failure and look at what comes back ===")
# =============================================================================
# The verifier group lifted from the SHIPPED source, so this cannot pass
# against a copy that has drifted.
_want = {"_reasoning_hash", "_chain_has_reasoning",
         "verify_reasoning_provenance", "verify_reasoning_ledger_entry",
         "_safe_detail"}
_pieces, _got = [], set()
for _n in _TREE.body:
    if isinstance(_n, ast.Assign) and any(
            isinstance(x, ast.Name) and x.id == "_CHAIN_SEARCH_WINDOW"
            for x in _n.targets):
        _pieces.append(ast.get_source_segment(MAIN, _n))
    if isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
            and _n.name in _want:
        _pieces.append(ast.get_source_segment(MAIN, _n))
        _got.add(_n.name)
ok("every piece was lifted out of main.py", _got == _want,
   sorted(_want - _got))

_ns = {}
exec(compile("\n\n".join(_pieces), "<main.py extract>", "exec"), _ns)


class _Boom(object):
    """A chain that fails with something worth hiding."""

    def get_recent(self, n=10, **kw):
        raise RuntimeError("%s is locked" % SECRET)

    def count_entries(self):
        return 1


_ns["memory_logger"] = _Boom()

_v = _ns["verify_reasoning_provenance"](
    {"reasoning": "anything", "reasoning_chain_hash": "abc"})
ok("a chain-walk failure still produces a verdict", bool(_v.get("message")))
ok("...that does NOT contain the exception text",
   SECRET not in _v["message"] and "RuntimeError" not in _v["message"],
   _v["message"])
ok("...and DOES carry a correlation ref for the server log",
   "ref " in _v["message"], _v["message"])
ok("...while still saying it could not be checked, not that it failed",
   _v["hash_matches"] is None,
   "a check that could not run must not read as a check that failed")

_v2 = _ns["verify_reasoning_ledger_entry"](
    {"trace": "anything", "sha256": "0" * 64, "meta": {"chain_hash": "a"}})
ok("the ledger verifier is clean too", SECRET not in _v2.get("message", ""),
   _v2.get("message"))


# =============================================================================
print("\n=== 3. The memory logger prints the PREFIX, never the content ===")
# =============================================================================
def _code_only(src):
    """Source with comments stripped.

    The first version of the next assertion was '"stripped[:80]" not in MLS'
    and it went red on the COMMENT that says "This used to log stripped[:80]" --
    a check tripping over the note explaining why the thing it forbids is gone.
    That exact shape has now bitten this project five times (the at-rest
    call-site guard, the os.replace ordering check, the innerHTML check in
    test_export_gate, and test_reasoning_surfaced, which grew its own helper
    for it). Strip the comments and ask the code.
    """
    out = []
    for line in src.splitlines():
        s = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        out.append(s)
    return "\n".join(out)


MLS_CODE = _code_only(MLS)
ok("the content slice is gone from the CODE", "stripped[:80]" not in MLS_CODE)
ok("...and it is still explained in a comment", "stripped[:80]" in MLS,
   "removing the slice without saying why invites its return")
ok("...replaced by the matched prefix", "matched {_hit!r}" in MLS_CODE)

_tmp = tempfile.mkdtemp(prefix="vai_leak_")
try:
    from memory_logger_surprise import MemoryLogger              # noqa: E402
    import contextlib

    _ml = MemoryLogger(storage_dir=os.path.join(_tmp, "chain"))
    _payload = ("[Toga Ollama error] failed while handling: " + SECRET)
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        _r = _ml.log(content=_payload, temperature=0.5, role="assistant")
    _printed = _buf.getvalue()
    ok("error-shaped content is still refused from the chain", _r is None)
    ok("...and the refusal is still announced", "skipping error-shaped" in _printed,
       _printed)
    ok("...WITHOUT printing the content",
       SECRET not in _printed,
       "printed: %r" % (_printed[:200],))
    ok("...but naming which tier failed, so it is still diagnostic",
       "Toga Ollama error" in _printed, _printed)
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

# And the same rule for the rest of the file: nothing prints a slice of content.
_slices = re.findall(r"print\([^)]*\b(?:content_str|stripped)\[[^\]]*\]", MLS_CODE)
ok("no other print in this module slices content", not _slices, _slices)


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
