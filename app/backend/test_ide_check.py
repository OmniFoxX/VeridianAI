#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ide_check.py -- checking code without running it, and without ceremony.

WHY THIS EXISTS

Checking is the one thing in the IDE panel that executes NOTHING. `ast.parse`
builds a tree and discards it; pyflakes walks that tree. Nothing is imported,
nothing is evaluated, and a buffer full of `os.system("rm -rf /")` is exactly
as inert here as one full of comments.

That is not a footnote, it is the design. Because it runs nothing, checking
needs none of Run's machinery -- no Expert mode, no confinement decision, no
`code_exec_enabled` -- and therefore works for everyone, in every mode, on an
install with code execution switched off entirely. Which is precisely when
somebody most wants to know whether what they just wrote is even valid.

The failure this file guards against is drift in the other direction: somebody
later "tidying up" by giving the check endpoint the same gates as the run
endpoint, on the reasonable-sounding grounds that both deal with code. That
would quietly take a working feature away from every Beginner-mode user and
every install with execution off, and nothing would look broken.

pyflakes is OPTIONAL, so the assertions below split into "always true" and
"true when pyflakes is installed" -- and the checker NAMES which one ran, so a
thin answer is never mistaken for a clean bill of health.

    python test_ide_check.py
"""
import asyncio
import io
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

_fails = []


def ok(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n            -> " + str(detail)) if detail and not cond else ""))
    if not cond:
        _fails.append(name)


import sage_engine as se                                   # noqa: E402

try:
    import pyflakes                                        # noqa: F401
    _HAVE_PF = True
except Exception:
    _HAVE_PF = False

print("pyflakes available here: %s" % _HAVE_PF)

# =============================================================================
print("\n=== 1. Syntax, which works with or without pyflakes ===")
# =============================================================================
r = se.check_python_code("x = 1\nprint(x)\n")
ok("valid code passes", r["ok"] and r["syntax_ok"], r)

r = se.check_python_code("def f(a, b)\n    return a\n")
ok("a syntax error is caught", not r["syntax_ok"])
ok("...with the line number", r["issues"][0]["line"] == 1, r["issues"])
ok("...and the column, so the caret can land on it",
   r["issues"][0]["col"] > 0, r["issues"])
ok("...and the parser's own message",
   "expected" in r["issues"][0]["msg"].lower(), r["issues"])

ok("an empty buffer is refused, not called clean",
   not se.check_python_code("   ")["ok"],
   "reporting 'no problems' about nothing is a wrong answer")

ok("the checker NAMES itself",
   "ast" in se.check_python_code("x=1")["checker"],
   "a thin answer must not read like a clean bill of health")

# A syntax error short-circuits: pyflakes cannot say anything useful about code
# that does not parse, and twenty follow-on warnings bury the missing colon.
r = se.check_python_code("import os\ndef f(\n")
ok("a syntax error short-circuits to ONE issue", len(r["issues"]) == 1, r)

# =============================================================================
print("\n=== 2. It runs NOTHING ===")
# =============================================================================
_canary = os.path.join(_HERE, "_check_canary_%d.txt" % os.getpid())
_evil = ("import os\n"
         "open(r'%s', 'w').write('EXECUTED')\n" % _canary.replace("\\", "\\\\"))
se.check_python_code(_evil)
ok("checking code that writes a file does NOT write it",
   not os.path.exists(_canary),
   "if this ever fails, checking has become running and every gate that "
   "was skipped on the grounds that it executes nothing is now wrong")
try:
    os.unlink(_canary)
except OSError:
    pass

_slow = "import time\nwhile True:\n    time.sleep(1)\n"
import time as _t
_t0 = _t.time()
se.check_python_code(_slow)
ok("checking an infinite loop returns immediately", _t.time() - _t0 < 5,
   "%.1fs" % (_t.time() - _t0))

ok("checking never raises, whatever it is given",
   all(isinstance(se.check_python_code(c), dict)
       for c in ("", "\x00", "(" * 200, "def", "\xff\xfe", "é" * 100)),
   "a checker that throws is worse than one that shrugs")

# =============================================================================
print("\n=== 3. Real linting, when pyflakes is present ===")
# =============================================================================
if not _HAVE_PF:
    ok("pyflakes checks (SKIPPED: not installed in this interpreter -- it is "
       "pinned in requirements.txt and bundled with the app)", True,
       "the syntax half above is the part that must work everywhere")
    ok("...and the checker says the answer is thin",
       "syntax only" in se.check_python_code("x=1")["checker"],
       "silence here would look identical to a clean result")
else:
    for label, code, needle in (
        ("unused import", "import os\nprint(1)\n", "imported but unused"),
        ("undefined name", "print(nope)\n", "undefined name"),
        ("unused local", "def f():\n    y = 5\n    return 1\n",
         "never used"),
        ("redefinition", "import os\nimport os\nprint(os)\n", "redefinition"),
    ):
        rr = se.check_python_code(code)
        hit = any(needle in i["msg"] for i in rr["issues"])
        ok("%s is reported" % label, hit, rr["issues"])
    ok("clean code reports nothing",
       se.check_python_code("import os\nprint(os.sep)\n")["issues"] == [])
    ok("the checker names pyflakes",
       "pyflakes" in se.check_python_code("x=1")["checker"])

# =============================================================================
print("\n=== 4. The endpoint is UNGATED, on purpose ===")
# =============================================================================
try:
    import main                                            # noqa: E402
except Exception as e:                                     # pragma: no cover
    print("  (skipping endpoint checks: %s)" % e)
    main = None

if main is not None:
    class _Req:
        client = type("c", (), {"host": "127.0.0.1"})()
        headers = {}
        cookies = {}

    res = asyncio.run(main.api_ide_check({"code": "def f(\n"}, _Req()))
    ok("the endpoint answers", res.get("syntax_ok") is False, res)
    ok("...with pre-formatted text for the display area", "text" in res)

    _src = io.open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()
    _ep = _src.split("async def api_ide_check")[1].split("\n@app.")[0]

    def _code_of(fn_src):
        # The executable lines only: no docstring, no comments.
        #
        # THE FIFTH TIME IN THIS PROJECT that a source assertion matched the
        # PROSE EXPLAINING a thing rather than the thing. Here the docstring
        # says "no Expert mode, no confinement decision, no
        # code_exec_enabled" -- so a plain substring check reported the
        # sentence promising the gate is ABSENT as evidence it is present.
        #
        # The others: a comment quoting the old condition it replaced; a
        # comment saying subprocess had been removed; a call broken across
        # two lines by formatting; a message split across two string
        # literals.
        #
        # If a check reads source, it has to read the CODE. Strip first,
        # assert second. It took five goes to learn that.
        _qd, _qs = chr(34) * 3, chr(39) * 3
        out, in_doc = [], False
        for line in fn_src.split("\n"):
            t = line.strip()
            q = _qd if t.startswith(_qd) else (_qs if t.startswith(_qs) else None)
            if q is not None:
                if not (len(t) > 3 and t.endswith(q)):
                    in_doc = not in_doc
                continue
            if in_doc or t.startswith("#"):
                continue
            out.append(line)
        return "\n".join(out)

    _ep_code = _code_of(_ep)
    ok("it does NOT check code_exec_enabled",
       "code_exec_enabled" not in _ep_code,
       "checking is not running; gating it would take the feature away from "
       "everyone with execution off, and nothing would look broken")
    ok("it does NOT check the mode ladder", "_ide_mode" not in _ep_code)
    ok("it does NOT go through Customs",
       "customs" not in _ep_code.lower(),
       "Customs guards dispatch into executors; this reaches none")
    ok("...and the stripper actually strips",
       "code_exec_enabled" in _ep and "code_exec_enabled" not in _ep_code,
       "if this fails the two assertions above are VACUOUS -- they would "
       "pass on any input, which is exactly how the first four went wrong")
    ok("the docstring still says WHY it is ungated",
       "executes" in _ep.lower() and "inert" in _ep.lower(),
       "the next person to tidy this up needs the reason, not just the code")

# =============================================================================
print("\n=== 5. Wired to the panel and to Toga ===")
# =============================================================================
HTML = io.open(os.path.join(_ROOT, "frontend", "index.html"),
               encoding="utf-8").read()
IDEJS = io.open(os.path.join(_ROOT, "frontend", "js", "ide.js"),
                encoding="utf-8").read()
CSS = io.open(os.path.join(_ROOT, "frontend", "css", "styles.css"),
              encoding="utf-8").read()

ok("there is a Check button", 'id="ide-check"' in HTML)
ok("...that is never disabled", "ide-check" not in CSS.split(".ide-run:disabled")[0][-400:]
   or ".ide-check:disabled" not in CSS,
   "it cannot do anything to your machine, so there is nothing to disable it for")
ok("...with an accessible name saying it does not run",
   "without running" in HTML)
ok("...calling ideCheck()", "ideCheck()" in HTML)
ok("ide.js defines and exports it",
   "function ideCheck" in IDEJS and "window.ideCheck = ideCheck" in IDEJS)
ok("it posts to /api/ide/check", "/api/ide/check" in IDEJS)
ok("it moves the caret to the first problem",
   "focusEditorLine" in IDEJS,
   "a line number you then have to hunt for is half an answer")

ok("Toga has an [IDE_CHECK] tag",
   "IDE_CHECK" in se._KNOWN_TAG_NAMES
   and se.parse_agent_actions("[IDE_CHECK]") == [("ide_check", "")])
if main is not None:
    _br = _src.split('elif action_type == "ide_check":')[1].split(
        'elif action_type == "ide_run":')[0]
    ok("...gated only on SEEING the buffer, not on Expert",
       '"expert"' not in _br,
       "a model that can read your code should be able to tell you it will "
       "not parse")
    ok("...and its result reaches the display area",
       '"type": "ide_output"' in _br)

print("\n=== 6. What of an exception may reach the browser ===")
# CodeQL alert 201, py/stack-trace-exposure, on this endpoint's `return res`.
# It was RIGHT about the dataflow and partly right about the risk. Six places
# put exception-derived text in the result; two of them ARE the feature and
# four were gratuitous:
#
#   KEPT   SyntaxError.msg / .lineno / .offset -- the parser's diagnostic about
#          source THE PERSON SUBMITTED. Bounded, and the whole point.
#   KEPT   pyflakes findings ("'os' imported but unused") -- likewise.
#   GONE   "%s: %s" % (type(e).__name__, e) for ValueError/MemoryError/
#          RecursionError -- arbitrary interpreter text.
#   GONE   the pyflakes class name in the checker label.
#   GONE   pyflakes' unexpectedError message (checker internals).
#   GONE   the collector's syntaxError message.
#
# What follows holds that line: the first two stay, the other four do not
# come back.
_res_src = io.open(os.path.join(_HERE, "sage_engine.py"),
                   encoding="utf-8").read()
_chk = _res_src.split("def check_python_code")[1].split(
    "\ndef format_check_result")[0]
_col = _res_src.split("class _FlakeCollector")[1].split(
    "\ndef check_python_code")[0]


def _strip(src):
    # Same stripper as section 4, same reason: assertions about code must not
    # read the prose explaining the code.
    _qd, _qs = chr(34) * 3, chr(39) * 3
    out, in_doc = [], False
    for line in src.split("\n"):
        t = line.strip()
        q = _qd if t.startswith(_qd) else (_qs if t.startswith(_qs) else None)
        if q is not None:
            if not (len(t) > 3 and t.endswith(q)):
                in_doc = not in_doc
            continue
        if in_doc or t.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


_chk_code, _col_code = _strip(_chk), _strip(_col)
ok("no exception CLASS NAME reaches the result",
   "type(e).__name__" not in _chk_code, _chk_code)
ok("no exception is str()'d into a message",
   "% e)" not in _chk_code and "str(e)" not in _chk_code, _chk_code)
ok("pyflakes' own error text does not travel",
   "str(msg)" not in _col_code, _col_code)
ok("...and SyntaxError.msg still does, because that is the feature",
   "e.msg" in _chk_code,
   "the parser telling somebody why THEIR code will not parse is the "
   "entire product; suppressing it would close the alert and remove the "
   "reason the endpoint exists")

# Behavioural: the inputs that used to leak interpreter text now do not.
for label, code, banned in (
    ("null byte", "x = 1\x00", ("ValueError", "null bytes")),
    ("deep nesting", "(" * 300 + ")" * 300, ("RecursionError",)),
):
    m = se.check_python_code(code)["issues"][0]["msg"]
    ok("%s reports without naming the exception" % label,
       not any(b in m for b in banned if b.endswith("Error")), m)
    ok("...and still says something useful", len(m) > 15, m)

# The response as a whole: only these keys, so nothing can ride along unseen.
_r = asyncio.run(main.api_ide_check({"code": "def f(\n"}, _Req())) \
    if main is not None else None
if _r is not None:
    ok("the response has a fixed, small shape",
       set(_r.keys()) == {"ok", "syntax_ok", "checker", "issues", "text"},
       sorted(_r.keys()))
    ok("...and each issue does too",
       all(set(i.keys()) == {"line", "col", "kind", "msg"}
           for i in _r["issues"]), _r["issues"])
    ok("no server path appears in the response",
       _HERE.lower() not in str(_r).lower()
       and "sage_engine" not in str(_r),
       "ast.parse is given filename='editor' precisely so nothing of ours "
       "can end up in a SyntaxError")

print("")
if _fails:
    print("%d CHECK(S) FAILED" % len(_fails))
    for f in _fails:
        print("   - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED - IDE check")
sys.exit(0)
