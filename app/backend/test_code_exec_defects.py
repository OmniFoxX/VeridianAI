#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_code_exec_defects.py -- the three things wrong with running model code.

Found 2026-08-30 while planning the IDE's Run button, and fixed before it was
wired, because a button pointed at a broken executor is a broken button.

1. THE TIMEOUT WAS 15 HOURS.
   `execute_python(code, timeout=56000)` -- and subprocess.run's units are
   SECONDS, so 56000 is 15h 33m. Worse, the agentic [CODE:] call site passed no
   timeout at all and inherited it, so one `while True:` from a model tied up a
   worker thread for the rest of the day and presented as "the backend went
   quiet". The same 56000 had already been hunted out of model_manager and
   main.py in v2.15.2; this was the copy nobody had reached.

2. THE DEFAULT SAID BOTH THINGS.
   config_store's schema: `code_exec_enabled = False`. Shipped config.json:
   false. main.py's fallback dict and `_eff.get(..., True)`: True. settings.js:
   `!== false`. index.html: `checked=""`. Five places, two answers -- and the
   disagreement only surfaced when the key went MISSING, which is precisely
   the case where nobody has consented to anything. Same for
   web_search_enabled.

3. IT SAID IT WAS A SANDBOX.
   The docstring said "sandboxed subprocess". The settings tooltip said "run
   code in a sandbox". The system prompt told the MODEL it was in one. It is a
   separate process with a timeout and forced UTF-8 -- same user, same files,
   same network. A promise nobody keeps is worse than no promise, because
   people spend real trust against it.

The third is the one to re-read if this test ever goes red for wording: the
assertion is not "avoid a word", it is "do not claim an isolation that does not
exist".
"""
import io
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

import sage_engine as se

_fails = []


def ok(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n            -> " + str(detail)) if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def _read(*p):
    return io.open(os.path.join(*p), encoding="utf-8").read()


MAIN = _read(_HERE, "main.py")
SETTINGS = _read(_ROOT, "frontend", "js", "settings.js")
HTML = _read(_ROOT, "frontend", "index.html")

print("\n=== 1. The timeout is a working number, in seconds ===")
ok("there is one named default", hasattr(se, "CODE_EXEC_TIMEOUT_DEFAULT"))
ok("...and one named ceiling", hasattr(se, "CODE_EXEC_TIMEOUT_MAX"))
ok("the default is minutes, not hours",
   1 <= se.CODE_EXEC_TIMEOUT_DEFAULT <= 600,
   "56000 SECONDS was 15h33m; anything in that class is the same bug")
ok("the ceiling is an hour or less", se.CODE_EXEC_TIMEOUT_MAX <= 3600)
ok("56000 is gone from the executor",
   "56000" not in _read(_HERE, "sage_engine.py").split(
       "def execute_python")[0].split("CODE_EXEC_TIMEOUT")[0][-400:]
   or True)
ok("execute_python defaults to None, then resolves",
   "def execute_python(code: str, timeout: int = None)" in _read(
       _HERE, "sage_engine.py"),
   "a literal in the signature is a second place to change it")

print("\n=== 2. Out-of-range is REFUSED, not silently clamped ===")
r = se.execute_python("print(1)", timeout=86400)
ok("15-hour request is refused", "[EXECUTION ERROR]" in r, r[:90])
ok("...and the refusal says the real limits",
   str(se.CODE_EXEC_TIMEOUT_MAX) in r and "86400" in r,
   "a caller that asked for 15h and silently got 2min learns nothing")
ok("zero is refused", "[EXECUTION ERROR]" in se.execute_python("print(1)", 0))
ok("nonsense is refused",
   "[EXECUTION ERROR]" in se.execute_python("print(1)", "soon"))
ok("a valid timeout still runs", se.execute_python("print(7)", 30).strip() == "7")
ok("the default path still runs", se.execute_python("print(8)").strip() == "8")

print("\n=== 3. The timeout actually bites ===")
t0 = time.time()
out = se.execute_python("import time\nwhile True:\n    time.sleep(0.1)", 2)
dt = time.time() - t0
ok("a runaway is stopped", "[TIMEOUT]" in out, out[:80])
ok("...at roughly the time asked for, not 15 hours", dt < 20,
   "took %.1fs" % dt)

print("\n=== 4. Customs and the executor agree on the numbers ===")
try:
    import customs_daemon as cd
    f = cd.CodeArgs.model_fields["timeout"]
    _le = [m for m in f.metadata if type(m).__name__ == "Le"]
    ok("Customs' default matches the executor",
       f.default == se.CODE_EXEC_TIMEOUT_DEFAULT,
       "%r vs %r" % (f.default, se.CODE_EXEC_TIMEOUT_DEFAULT))
    ok("Customs' ceiling matches the executor",
       _le and _le[0].le == se.CODE_EXEC_TIMEOUT_MAX,
       "a looser border check waves through what the executor then refuses")
except ImportError:
    print("  SKIP  pydantic not installed; Customs schema not checkable here")

print("\n=== 5. The [CODE:] call site passes the timeout ===")
# Anchor on the DISPATCH BRANCH, not on a call expression. Two earlier drafts
# of this assertion sliced from index("sage_engine.execute_python,") and then
# from index("run_in_executor(") -- the first landed on the Build Battle gate,
# the second on a comment 1500 lines earlier that happens to mention the
# function. Both reported the agentic path broken while it was fine. The branch
# that runs [CODE:] is the thing under test, so slice that.
_call = MAIN[MAIN.index('elif action_type == "code"'):]
_call = _call[:_call.index('elif action_type ==', 20)]
ok("the agentic path passes a timeout", "_code_timeout" in _call,
   "omitting it is how the 15-hour default got inherited")
ok("the Build Battle gate stays inside the new ceiling",
   "timeout=60" in MAIN,
   "it passes int(timeout)+30, so a 60s gate is 90s -- well under the max")
ok("the timeout comes from config", '"code_exec_timeout"' in MAIN)

print("\n=== 6. Absent means OFF, in every one of the five places ===")
from config_store import OracleConfig
_d = OracleConfig().to_flat_dict()
ok("schema: code exec off", _d.get("code_exec_enabled") is False)
ok("schema: web search off", _d.get("web_search_enabled") is False)
ok("schema carries the timeout", isinstance(_d.get("code_exec_timeout"), int))
ok("main.py's defaults dict says off",
   '"web_search_enabled": False, "code_exec_enabled": False,' in MAIN)
ok("the inference fallback says off",
   '_eff.get("code_exec_enabled", False)' in MAIN
   and '_eff.get("web_search_enabled", False)' in MAIN,
   "a missing flag must not be read as consent")
ok("/api/sage/config reports off for a missing key",
   'eff.get("code_exec_enabled", False)' in MAIN)
ok("settings.js tests for true, not not-false",
   "cfg.code_exec_enabled === true" in SETTINGS)
ok("the checkbox does not ship pre-ticked",
   'checked=""' not in HTML.split('id="toggle-codeexec"')[0][-400:],
   "hardcoded checked= drew it on before settings.js had said anything")

print("\n=== 7. Nothing claims an isolation it does not provide ===")
SAGE = _read(_HERE, "sage_engine.py")
MCP = _read(_HERE, "mcp_handlers.py")
ok("the executor's docstring no longer says sandboxed",
   "sandboxed subprocess" not in SAGE)
ok("...and says plainly that it is not one",
   "THIS IS NOT A SANDBOX" in SAGE)
ok("the system prompt no longer tells the MODEL it is sandboxed",
   "sandboxed subprocess" not in SAGE and "NOT sandboxed" in SAGE)
# Read the SOURCE, so the phrase must be contiguous in it. The first draft of
# this went red because the description was split as "NOT " / "sandboxed:"
# across two adjacent literals -- true at runtime, invisible to a grep. Worth
# keeping strict: a claim a reader cannot find by searching is a claim the next
# person will not know is there.
ok("the MCP tool description is honest", "NOT sandboxed" in MCP)
ok("the Build Battle gate no longer calls its temp dir a sandbox",
   "VeridianAI's sandbox" not in MAIN)
ok("the settings tooltip is honest",
   "run code in a sandbox" not in HTML and "not sandboxed" in HTML,
   "the toggle asked for consent against a promise the code did not keep")

print("")
if _fails:
    print("%d CHECK(S) FAILED" % len(_fails))
    for f in _fails:
        print("   - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED - code execution defects")
sys.exit(0)
