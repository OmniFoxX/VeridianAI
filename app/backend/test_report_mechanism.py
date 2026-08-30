#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_report_mechanism.py -- reporting AI content is reachable, consented,
and carries the least data that can still fix the problem.

WHY THIS EXISTS

The Microsoft Store rejected VeridianAI v2.16.1 for exactly one reason: the app
documented an email address for reporting problems and provided no MECHANISM
inside the app. A way written in a manual is not a way.

So the thing under test is not "does a function work". It is "can a person who
has just been given a harmful answer actually report it, without being asked to
trust anything they cannot see". Three properties, and losing any one of them
puts the submission back in the rejected pile:

  REACHABLE   A button in the app, present before anybody signs in, not buried
              in a settings panel and not behind a password prompt.

  CONSENTED   The warning comes FIRST, the actual text is shown verbatim and
              editable, and every optional part is unticked. Consent obtained
              after the fact is not consent.

  MINIMAL     Ticking nothing sends the flagged reply and the version string.
              Not the prompt, not the conversation, not the machine's name.

WHAT IT ALSO CHECKS, AND WHY

That the mechanism works with NO MAIL CLIENT INSTALLED. A reviewer on a clean
VM who clicks Report and gets a mailto: that opens nothing has found the same
defect again, dressed up. The file path and the address must be on screen as
text, whatever else happens.

    python test_report_mechanism.py
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


def _read(*p):
    return io.open(os.path.join(*p), encoding="utf-8").read()


MAIN = _read(_HERE, "main.py")
HTML = _read(_ROOT, "frontend", "index.html")
JS = _read(_ROOT, "frontend", "js", "report.js")
CSS = _read(_ROOT, "frontend", "css", "styles.css")


def _code_only(src, c="#"):
    return "\n".join("" if ln.lstrip().startswith(c) else ln.split(c, 1)[0]
                     for ln in src.splitlines())


def _js_code_only(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(ln.split("//", 1)[0] for ln in src.splitlines())


# =============================================================================
print("=== 1. Ticking nothing sends almost nothing ===")
# =============================================================================
import report_issue as R                                      # noqa: E402

_T = Path(tempfile.mkdtemp(prefix="vai_report_"))
try:
    # Every optional field is SUPPLIED and none is ticked. This is the shape of
    # the real call: the browser sends what it has, the server decides what is
    # written. If include{} is ignored, this leaks.
    r = R.build(
        flagged="THE FLAGGED REPLY",
        description="it invented a citation",
        model="qwen2.5-coder", backend="Ollama",
        include={},
        prompt="MY PRIVATE PROMPT",
        reasoning="MY PRIVATE REASONING",
        context_turns=[{"role": "user", "content": "MY PRIVATE TURN"}],
        downloads_dir=_T)
    ok("the report is written", r.get("ok") is True,
       r.get("error") or "build() did not return ok=True")
    body = Path(r["path"]).read_text(encoding="utf-8")

    ok("the flagged reply is in it", "THE FLAGGED REPLY" in body)
    ok("the description is in it", "invented a citation" in body)
    # CANARIES, not secrets -- and the name matters. These are marker strings
    # the test plants in the CALL so it can prove they are absent from the
    # FILE. Nothing here is confidential; the whole assertion is that this text
    # does not survive.
    #
    # This loop variable used to be called `secret`, and that name alone was
    # enough for CodeQL py/clear-text-logging-sensitive-data (alert #193) to
    # read `ok()`'s print as leaking a credential. The finding was wrong, but
    # the name was wrong first: a value whose entire purpose is to be checked
    # for absence is a canary. Renaming it says what it is, and the false
    # positive goes with it. Do not rename it back.
    for canary in ("MY PRIVATE PROMPT", "MY PRIVATE REASONING",
                   "MY PRIVATE TURN"):
        ok("...but NOT %s" % canary.lower(), canary not in body,
           "an unticked box must mean the text never reaches the file, even "
           "though the caller passed it")

    # THE PROMISE THE DIALOG MAKES, CHECKED AGAINST THE FILE IT PRODUCES.
    # The dialog tells people their username, machine name and paths are never
    # included. That is a claim about this function, so it is tested here.
    _identifiers = []
    try:
        import platform
        if platform.node():
            _identifiers.append(("machine name", platform.node()))
    except Exception:
        pass
    try:
        _u = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        if _u:
            _identifiers.append(("username", _u))
    except Exception:
        pass
    for label, value in _identifiers:
        ok("the file does not contain the %s" % label,
           value.lower() not in body.lower(),
           "the consent screen promises this is never included")
    ok("...and identifiers were actually available to leak",
       bool(_identifiers),
       "nothing to check against -- the assertion above proved nothing")

    ok("the support address is written into the file", R.SUPPORT_EMAIL in body,
       "the copy on disk should still say where it was meant to go")
    ok("it lists what it included", "Sections included" in body)
    ok("...and what it never includes", "not in this file" in body.lower())

    # =========================================================================
    print("\n=== 2. Ticking a box includes that, and only that ===")
    # =========================================================================
    r2 = R.build(flagged="F", description="d", include={"prompt": True},
                 prompt="MY PRIVATE PROMPT", reasoning="MY PRIVATE REASONING",
                 context_turns=[{"role": "user", "content": "MY PRIVATE TURN"}],
                 downloads_dir=_T)
    b2 = Path(r2["path"]).read_text(encoding="utf-8")
    ok("the ticked part is included", "MY PRIVATE PROMPT" in b2)
    ok("...and the unticked ones still are not",
       "MY PRIVATE REASONING" not in b2 and "MY PRIVATE TURN" not in b2)

    # A truthy-but-not-true value must NOT count. A malformed payload should
    # err towards writing less.
    r3 = R.build(flagged="F", description="d",
                 include={"prompt": "yes", "reasoning": 1},
                 prompt="MY PRIVATE PROMPT", reasoning="MY PRIVATE REASONING",
                 downloads_dir=_T)
    b3 = Path(r3["path"]).read_text(encoding="utf-8")
    ok("only an explicit true counts",
       "MY PRIVATE PROMPT" not in b3 and "MY PRIVATE REASONING" not in b3,
       "a string or a 1 is truthy in Python; if those opened a section, a "
       "sloppy client could widen the report without the person choosing to")

    # An unknown key cannot invent a section.
    r4 = R.build(flagged="F", description="d",
                 include={"everything": True, "chat_memory": True},
                 downloads_dir=_T)
    ok("an unknown include key does nothing",
       r4.get("ok") is True, r4)

    ok("the report is readable text, not an opaque bundle",
       str(r.get("filename", "")).endswith(".md"),
       "the person is about to email this to a stranger and is entitled to "
       "open it first")
finally:
    shutil.rmtree(_T, ignore_errors=True)


# =============================================================================
print("\n=== 3. It is REACHABLE ===")
# =============================================================================
ok("there is a Report button in the header",
   'id="report-btn"' in HTML)
ok("...in the static markup, not injected after sign-in",
   "report-btn" in HTML and "report-btn" not in _js_code_only(
       _read(_ROOT, "frontend", "js", "auth.js")),
   "a reporting mechanism that appears only once you have an account is not "
   "reachable to a reviewer setting the app up for the first time")

_hdr = HTML.split('<div class="header-controls">')[1][:2000] \
    if '<div class="header-controls">' in HTML else ""
ok("the header controls block was found", bool(_hdr))
ok("...and the button sits before the model selector",
   _hdr.find('id="report-btn"') < _hdr.find('id="model-select"'),
   "the auth cluster inserts at firstChild, so this is what puts Report "
   "between the account buttons and the model dropdown")
ok("it says what it is", ">" in _hdr and "Report" in _hdr)
ok("...and has an accessible name",
   'aria-label="Report a problem with AI-generated content"' in HTML)
ok("report.js is loaded", "js/report.js" in HTML)
ok("...after chat.js, whose `messages` it reads",
   HTML.find("js/chat.js") < HTML.find("js/report.js"),
   "loaded first, the pre-filled reply would always be empty")
ok("the button has a visible focus indicator",
   ".report-btn:focus-visible" in CSS)


# =============================================================================
print("\n=== 4. It is CONSENTED ===")
# =============================================================================
_JC = _js_code_only(JS)
ok("nothing is uploaded by the app",
   "XMLHttpRequest" not in _JC and "navigator.sendBeacon" not in _JC,
   "the only fetch here writes a local file through the app's own API")
_fetches = re.findall(r'fetch\(\s*"([^"]+)"', _JC)
ok("...and the only endpoint it calls is the local report writer",
   _fetches == ["/api/report"], _fetches)

_dlg = _JC.split("openReportDialog")[1] if "openReportDialog" in _JC else ""
ok("the dialog was found", bool(_dlg))
ok("the warning comes BEFORE the controls",
   _dlg.find("does not upload anything") < _dlg.find("report-flagged"),
   "consent obtained after the fact is not consent")
ok("the flagged text is shown verbatim and editable",
   'flagged.value = ex.flagged' in _dlg and "textarea" in _dlg,
   "not a summary and not a promise -- the words that will be written")
ok("every optional box starts UNTICKED",
   "cb.checked = false" in _dlg and "cb.checked = true" not in _dlg,
   "the prompt, the earlier turns and the reasoning trace are the person's "
   "own words; each is a decision they make, not one they discover")
ok("...and the dialog says what is never included",
   "Never included" in _dlg)
ok("unticked content is not even put on the wire",
   "include.prompt ? ex.prompt" in _dlg,
   "the server checks again, but there is no reason to transmit text nobody "
   "asked to include")


# =============================================================================
print("\n=== 5. It works with NO mail client ===")
# =============================================================================
# The failure this guards against is a reviewer on a clean VM clicking Report,
# getting a mailto: that opens nothing, and filing the same finding twice.
_res = _JC.split("function showResult")[1] if "function showResult" in _JC else ""
ok("the result panel was found", bool(_res))
ok("the address is shown as text", 'field("Send it to"' in _res)
ok("the file path is shown as text", 'field("The file"' in _res)
ok("...both copyable", _res.count("copyText(") >= 1 and "Copy" in _res)
ok("the email draft is offered as an extra, not the only route",
   _res.find('field("Send it to"') < _res.find("mailto:"),
   "if the draft is the mechanism, a machine without a mail client has none")
ok("nothing is sent automatically",
   "Nothing has been sent" in _res)


# =============================================================================
print("\n=== 6. The endpoint behaves like the rest of the app ===")
# =============================================================================
_ep = MAIN.split("async def api_report_build")[1][:2200] \
    if "async def api_report_build" in MAIN else ""
ok("the endpoint exists", bool(_ep))
ok("...localhost only", "_is_local_client(request)" in _code_only(_ep))
ok("...and audited", '"content.report"' in _ep)

# THE AUDIT CALL ONLY, not "the next 400 characters".
#
# The first version took a fixed window after _audit_api_action and went red on
# correct code: the window ran past the audit into the report_issue.build call,
# where payload.get("flagged") appears for the entirely right reason. A window
# measured in characters does not know where a statement ends.
_audit = _ep.split("_audit_api_action(")[1].split("result =")[0] \
    if "_audit_api_action(" in _ep else ""
ok("the audit call was isolated", bool(_audit))
ok("the audit records the SHAPE, not the content",
   "flagged" not in _audit and "description" not in _audit,
   "logging what somebody reported would store a second copy of the text "
   "here, which is the one place it must not go -- they chose to send it to "
   "a person, not to file it. Audit args: %r" % _audit.strip()[:200])
ok("...and it does record which optional parts were included",
   "included" in _audit,
   "the shape is the useful part: it says what was shared without saying what "
   "was said")

# A considered exception, so it is asserted rather than left to drift back.
ok("it is NOT behind the step-up unlock",
   "_require_elevated" not in _code_only(_ep),
   "export is gated because it extracts everything; a report is one reply. A "
   "password wall in front of 'this AI said something harmful' is friction "
   "on a safety mechanism the Store requires to be reachable")

# Checked against the ROUTE BLOCK, comment included. The rationale lives above
# the decorator, which is where a reader meets the route -- so the first
# version, slicing from `async def`, could not see the very thing it was
# asking for and reported the reasoning missing while it sat six lines up.
_route = MAIN.split("# --- Report AI-generated content that went wrong")[1][:4000] \
    if "# --- Report AI-generated content that went wrong" in MAIN else ""
ok("the route block was found", bool(_route))
ok("...and the reason for the missing gate is written where the decision is",
   "step-up" in _route.lower(),
   "an unexplained missing gate reads as an oversight and gets 'fixed' by "
   "the next person tidying up")


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
