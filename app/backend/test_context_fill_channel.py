#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_context_fill_channel.py -- the daemon can actually READ the ratio.

WHAT THIS FILE IS FOR (v2.15.2)

llama.cpp build 8639 removed llamacpp:kv_cache_usage_ratio. sage_daemon's
_job_llama_progress scraped that metric, so after the upgrade the scrape could
never succeed: _tick_state["llama_progress"] sat at 0.0 forever and the fatigue
check's `llama_progress >= _LLAMA_CLIFF_THRESHOLD` branch became unreachable
code against a constant. The detector had quietly been running on one signal
instead of two, and nothing said so.

The replacement computes the ratio in main.py from the server's own token
counts and publishes it at /api/context-fill for the daemon to poll.

THE SECOND BUG, WHICH THIS FILE EXISTS FOR

That replacement very nearly reproduced the original failure one layer over.
_session_gate requires a login session for the app surface whenever multiuser
is enabled. sage_daemon runs in its OWN PROCESS, holds no cookie, and cannot
get one -- it is a daemon, not a user. So with multiuser on, every poll would
have taken a hard 401, _job_llama_progress would have returned "context-fill
endpoint returned 401" forever, and llama_progress would have stayed pinned at
0.0. Identical symptom, identical silence, new cause.

Caught on the live install, which answered 401 on the endpoint. It was not
hypothetical -- it was the running configuration.

Section 2 is therefore the load-bearing part of this file. If someone tidies
the exempt list later, that must fail loudly here rather than silently
re-zeroing the fatigue detector.

    python test_context_fill_channel.py
"""
import ast
import io
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


MAIN = io.open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()
DAEMON = io.open(os.path.join(_HERE, "sage_daemon.py"), encoding="utf-8").read()
MM = io.open(os.path.join(_HERE, "model_manager.py"), encoding="utf-8").read()


# =============================================================================
print("=== 1. The publisher exists and is wired ===")
# =============================================================================
ok("main.py declares the shared reading", "_CTX_FILL = {" in MAIN)
ok("...guarded by a lock (a daemon polls it while a turn writes it)",
   "_CTX_FILL_LOCK = threading.Lock()" in MAIN)
ok("the endpoint is declared", '@app.get("/api/context-fill")' in MAIN)
ok("_record_context_fill exists", "async def _record_context_fill" in MAIN)
ok("_resolve_n_ctx exists", "async def _resolve_n_ctx" in MAIN)
ok("n_ctx lookups are cached, not re-fetched every turn", "_NCTX_CACHE" in MAIN)

_t = ast.parse(MAIN)
_fns = {n.name for n in _t.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
ok("_record_context_fill is MODULE level, not nested in another def",
   "_record_context_fill" in _fns,
   "a zero-indented helper in the wrong place already broke this release once")
ok("_resolve_n_ctx is module level too", "_resolve_n_ctx" in _fns)

ok("the turn hook is called from _watched_generate",
   "await _record_context_fill(options.get(\"_turn_stats\"))" in MAIN)
# Asserted STRUCTURALLY, via the AST, not by looking for the word "finally"
# near the call. The first draft of this check did exactly that and failed --
# on the comment that explains why the call is not in a finally. Searching for
# a keyword in prose that is about the keyword proves nothing; ask the parser
# which block the node actually sits in.
_watched = [n for n in ast.walk(_t)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_watched_generate"]
ok("_watched_generate is present to inspect", len(_watched) == 1, len(_watched))
if _watched:
    _finally_lines = set()
    for _node in ast.walk(_watched[0]):
        if isinstance(_node, ast.Try):
            for _fin in _node.finalbody:
                for _sub in ast.walk(_fin):
                    if hasattr(_sub, "lineno"):
                        _finally_lines.add(_sub.lineno)
    _hook_lines = [
        _c.lineno for _c in ast.walk(_watched[0])
        if isinstance(_c, ast.Call)
        and isinstance(_c.func, ast.Name)
        and _c.func.id == "_record_context_fill"
    ]
    ok("the hook is called inside _watched_generate", len(_hook_lines) == 1,
       _hook_lines)
    ok("the hook is deliberately NOT in a finally block",
       bool(_hook_lines) and not (set(_hook_lines) & _finally_lines),
       "a generator's finally runs at finalization; if the consumer abandons "
       "the stream Python defers that to collection, and the reading would "
       "arrive late or never")
ok("telemetry failure cannot break a turn",
   "telemetry must never break a turn" in MAIN)


# =============================================================================
print("\n=== 2. The daemon can REACH it (the 401 trap) ===")
# =============================================================================
_gate = MAIN[MAIN.index("async def _session_gate"):]
_gate = _gate[:_gate.index("return await call_next(request)",
                           _gate.index("Always-open"))]
ok("/api/context-fill is in the _session_gate always-open list",
   '_p == "/api/context-fill"' in _gate,
   "without this, multiuser installs 401 every poll and llama_progress "
   "stays 0.0 -- the exact bug this endpoint was written to fix")

_handler = MAIN[MAIN.index('@app.get("/api/context-fill")'):]
_handler = _handler[:_handler.index("@app.get", 40)]
ok("...and the handler still self-gates on locality",
   "_is_local_client(request)" in _handler,
   "opening the gate is only safe because the handler cloaks off-box callers")
ok("off-box callers get the 404 cloak, not a 403 that confirms existence",
   "_cloak_not_found()" in _handler)
ok("the endpoint is read-only (no POST/PUT sibling)",
   '@app.post("/api/context-fill")' not in MAIN
   and '@app.put("/api/context-fill")' not in MAIN)


# =============================================================================
print("\n=== 3. The daemon polls the endpoint, not the deleted metric ===")
# =============================================================================
_job = DAEMON[DAEMON.index("def _job_llama_progress"):]
_job = _job[:_job.index("\ndef ", 10)]
ok("_job_llama_progress polls /api/context-fill",
   "/api/context-fill" in _job)
ok("it no longer scrapes the removed llama.cpp gauge",
   "kv_cache_usage_ratio" not in _job.split('"""')[-1],
   "may still be NAMED in the docstring as history; must not be FETCHED")
ok("it still writes the key the fatigue check reads",
   '_tick_state["llama_progress"]' in _job,
   "publishing a ratio nobody stores would be a no-op fix")
ok("it still stamps freshness (the cadence scheduler reads llama_last_ts)",
   '_tick_state["llama_last_ts"]' in _job)
ok("the cliff threshold is still applied", "_LLAMA_CLIFF_THRESHOLD" in _job)
ok("the warn threshold is still applied", "_LLAMA_WARN_THRESHOLD" in _job)
ok("it still emits an MLM training row", "_log_mlm_training_row" in _job)

# The except clauses name _httpx.ConnectError. An except clause is EVALUATED
# when an exception fires -- so if the import sat inside the same try and
# failed, _httpx would be unbound and the handler itself would raise NameError,
# turning a tidy return into a traceback inside the daemon loop.
_import_at = _job.index("import httpx as _httpx")
_try_at = _job.index("try:", _job.index("_FILL_URL") - 400) \
    if "_FILL_URL" in _job else len(_job)
ok("httpx is imported BEFORE the try whose except clauses name it",
   _import_at < _job.index("resp = _httpx.get"),
   "an unbound _httpx would make the error handler itself raise NameError")
ok("an unimportable httpx returns a message instead of raising",
   "httpx not importable" in _job)

ok("a null ratio is NOT coerced to zero",
   "if kv_ratio is None:" in _job,
   "'no reading yet' and 'empty cache' both render as 0.000 and are opposites")
ok("the 'no reading yet' notice is latched, not repeated every tick",
   "_KV_ABSENT_LOGGED" in _job and "Logged once" in _job)
ok("a non-200 is reported rather than swallowed",
   "context-fill endpoint returned" in _job)


# =============================================================================
print("\n=== 4. The counts are per-turn, not process-wide ===")
# =============================================================================
ok("main.py builds a FRESH stats dict per turn", "_turn_stats = {}" in MAIN)
ok("...and passes it down through options",
   'options["_turn_stats"] = _turn_stats' in MAIN)
ok("the stats dict is NOT an attribute on the shared manager",
   "model_manager._turn_stats" not in MAIN
   and "self._turn_stats" not in MM,
   "one slot on a process-wide object; two concurrent chats would collide")
ok("a client cannot pre-seed it (server-assigned, like _priority)",
   "Server-assigned like _priority" in MAIN)

ok("model_manager records the llama-server counts", '_stats["base_url"]' in MM)
ok("...from timings, since this build sends no stream usage block",
   '_tm.get("prompt_n")' in MM and '_tm.get("predicted_n")' in MM)
ok("...and the Ollama counts too",
   '_stats["backend"] = "ollama"' in MM)


# =============================================================================
print("\n=== 5. The arithmetic ===")
# =============================================================================
_rec = MAIN[MAIN.index("async def _record_context_fill"):]
_rec = _rec[:_rec.index("\nasync def ", 10)]
ok("the ratio is (prompt + completion) / n_ctx",
   "int(_pt or 0) + int(_ct or 0)" in _rec and "_used / _n_ctx" in _rec)
ok("a missing n_ctx yields None, not a divide-by-zero",
   "if _n_ctx else None" in _rec)
ok("a backend that reports nothing leaves the LAST reading alone",
   "if _pt is None and _ct is None:" in _rec and "return" in _rec,
   "overwriting with None would look like 'no data' after real data existed")


# =============================================================================
print("\n=== 6. Runtime: it imports, and the route is really registered ===")
# =============================================================================
# Sections 1-5 read text. Text passed while ModelManager._gen_ollama was
# silently swallowed into another function earlier in this release, so this
# section asks the interpreter instead.
try:
    _cwd = os.getcwd()
    os.chdir(_HERE)
    import main as _main                                     # noqa: E402
    os.chdir(_cwd)
    ok("main.py imports for real", True)
    for _n in ("_CTX_FILL", "_CTX_FILL_LOCK", "_resolve_n_ctx",
               "_record_context_fill", "api_context_fill", "_session_gate"):
        ok("main.%s exists at runtime" % _n, hasattr(_main, _n))
    _routes = [getattr(r, "path", "") for r in _main.app.routes]
    ok("/api/context-fill is registered on the app",
       "/api/context-fill" in _routes)
    ok("the initial reading is null, not zero",
       _main._CTX_FILL.get("ratio") is None,
       "zero would read as 'empty context' before any turn has run")
except Exception as _e:
    ok("main.py imports for real", False, "%s: %s" % (type(_e).__name__, _e))

try:
    import sage_daemon as _sd                                # noqa: E402
    ok("sage_daemon.py imports for real", True)
    ok("_job_llama_progress is callable", callable(_sd._job_llama_progress))
    ok("the absent-reading latch starts disarmed", _sd._KV_ABSENT_LOGGED is False)
except Exception as _e:
    ok("sage_daemon.py imports for real", False,
       "%s: %s" % (type(_e).__name__, _e))


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
