#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ai_disclosure_default.py -- every generated reply says it was generated.

WHAT TODD ASKED FOR (2026-08-23)

    "the Prepend/Append plugin footer is off by default and I think it should
     ship on by default, it could help satisfy some regulations"

WHY IT IS NOT `"enabled": false` -> `true`

The example plugin carries TWO hooks. `append_footer` is the disclosure.
`prepend_system` injects "ALWAYS format code with proper syntax highlighting"
into every system prompt -- nothing to do with disclosure, and skill_gate.py
classifies prepend_system as GATED (human review) while append_footer is SAFE.
Flipping that one flag would have shipped a GATED hook enabled by default,
against this project's own policy, and quietly changed how the model is
instructed on every fresh install.

It would also have hidden a regulatory control behind the name "Prepend/Append"
/ "Prepends/Appends responses". Somebody tidying up their plugin list turns off
what looks like a formatting demo and silently stops disclosing. A control
people are expected to leave on has to say what it is.

So the disclosure ships as its own plugin, named for its job, carrying the SAFE
hook only. The example plugin is left exactly as it was: still off, still an
example.

COVERAGE IS THE FEATURE

plugin_manager.postprocess ran on ONE of the three paths that stream model
output to the user. Symposium (three models debating) and Build Battle both
sent their transcripts straight out. A disclosure that is absent from the most
obviously machine-generated screens in the app is not a disclosure. This is the
"right code, wrong coverage" shape, and it is the seventh time this project has
hit it.

    python test_ai_disclosure_default.py
"""
import ast
import io
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_PLUGINS = os.path.join(_ROOT, "plugins")

sys.path.insert(0, _HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


MAIN = io.open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()
PM = io.open(os.path.join(_HERE, "plugin_manager.py"), encoding="utf-8").read()


def _code_only(src):
    return "\n".join(ln.split("#", 1)[0] for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))


MAIN_CODE = _code_only(MAIN)


# =============================================================================
print("=== 1. The disclosure ships, on, as its own plugin ===")
# =============================================================================
_path = os.path.join(_PLUGINS, "ai_disclosure_plugin.json")
ok("ai_disclosure_plugin.json is in the plugins directory",
   os.path.exists(_path), _path)

_doc = {}
if os.path.exists(_path):
    _doc = json.load(io.open(_path, encoding="utf-8"))

ok("...and it ships ENABLED", _doc.get("enabled") is True,
   "enabled=%r -- the whole request was that this is on out of the box"
   % _doc.get("enabled"))
ok("...carrying the SAFE hook only",
   list((_doc.get("hooks") or {}).keys()) == ["append_footer"],
   "hooks=%r -- prepend_system is GATED in skill_gate.py and must not be "
   "enabled by default" % list((_doc.get("hooks") or {}).keys()))
ok("...under a name that says what turning it off would do",
   "disclosure" in (_doc.get("name", "") + _doc.get("id", "")).lower(),
   "name=%r id=%r" % (_doc.get("name"), _doc.get("id")))
ok("...and a description that warns before it is switched off",
   "requir" in _doc.get("description", "").lower(),
   _doc.get("description"))

_footer = (_doc.get("hooks") or {}).get("append_footer", "")
ok("the footer states the content is AI-generated",
   "ai-generated" in _footer.lower(),
   "%r -- 'Response provided by Toga' can be read as a person's name, which "
   "is the one thing a disclosure must not be ambiguous about" % _footer)


# =============================================================================
print("\n=== 2. The example plugin was left alone ===")
# =============================================================================
_ex = os.path.join(_PLUGINS, "example_plugin.json")
ok("example_plugin.json still exists", os.path.exists(_ex))
if os.path.exists(_ex):
    _exd = json.load(io.open(_ex, encoding="utf-8"))
    ok("...still disabled", _exd.get("enabled") is False, _exd.get("enabled"))
    ok("...still carrying its GATED hook, unshipped",
       "prepend_system" in (_exd.get("hooks") or {}),
       "the example is not the thing that changed; if this is gone somebody "
       "edited the demo instead of adding the feature")
    ok("the two are distinct plugins",
       _exd.get("id") != _doc.get("id"),
       "same id -> PluginManager keys by id and one silently replaces the "
       "other")


# =============================================================================
print("\n=== 3. Every path that streams model output appends it ===")
# =============================================================================
# Counted off the code, because this is the assertion that would have passed
# while the bug shipped: postprocess EXISTED and WAS called -- once.
_calls = len(re.findall(r"plugin_manager\.postprocess\(", MAIN_CODE))
ok("postprocess is called on more than the agentic path", _calls >= 3,
   "found %d call(s). Symposium and Build Battle stream model transcripts "
   "with the standard token/done events and were sending them undisclosed."
   % _calls)

# ASKED PER HANDLER, and derived rather than listed.
#
# The first version searched the 700 characters before each done payload and
# went red on all three. Two different reasons, both instructive:
#
#   * "model": "Symposium" occurs TWICE. The first is an early return --
#     "[Symposium: please type a proposition to debate.]" -- which is a canned
#     app string, not model output, and correctly carries no AI-generated
#     footer. str.find lands on it.
#   * in ws_chat the postprocess call and the done payload are ~100 lines
#     apart, with the chat-memory save and the reasoning ledger between them.
#     A fixed character window encodes a layout, not a rule.
#
# The rule is: any handler that sends a done payload carrying ACCUMULATED
# MODEL TEXT must have run the post-hooks on it. Collected from the AST, so a
# fourth streaming mode added next year fails here on the day it is written
# instead of shipping undisclosed.
_handlers = []
for _n in ast.walk(ast.parse(MAIN)):
    if not isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        continue
    _seg = ast.get_source_segment(MAIN, _n) or ""
    if '"type": "done"' not in _seg:
        continue
    # Accumulated model output, as opposed to a fixed string the app wrote
    # itself (the "please type a proposition" guard, the image-saved ack).
    # Those are not generated content and must NOT claim to be.
    if not re.search(r'"content":\s*(full|full_response)\b', _seg):
        continue
    _handlers.append((_n.name, "plugin_manager.postprocess(" in _code_only(_seg)))

ok("the streaming handlers were found", len(_handlers) >= 3,
   "found %r -- if this is short, the locator broke and every check under it "
   "passes without looking at a thing" % [h[0] for h in _handlers])
for _name, _has in _handlers:
    ok("...%s runs the post-hooks on its transcript" % _name, _has,
       "%s streams model output and sends it undisclosed" % _name)

ok("the footer reaches the client, not just the saved history",
   '"content": full_response' in MAIN_CODE or '"content": full,' in MAIN_CODE,
   "the done payload carries the postprocessed text; chat.js re-renders the "
   "bubble from it, so a footer added after streaming is still displayed")


# =============================================================================
print("\n=== 4. It appends once, no matter how many times it is asked ===")
# =============================================================================
ok("postprocess checks the tail before appending",
   "endswith" in PM,
   "the footer is saved into chat memory and returned to the model as its "
   "own previous turn; models imitate their own output shape, and an "
   "unconditional append then prints the disclosure twice")

sys.path.insert(0, _HERE)
try:
    from plugin_manager import PluginManager
    _pm = PluginManager.__new__(PluginManager)
    _pm._plugins = {"ai-disclosure": {"enabled": True,
                                      "hooks": {"append_footer": _footer}}}
    _once = _pm.postprocess("Here is your answer.")
    _twice = _pm.postprocess(_once)
    ok("running it twice appends one footer", _once == _twice,
       "second pass added %d more characters" % (len(_twice) - len(_once)))
    ok("...and a reply that already ends with it is left alone",
       _pm.postprocess("Answer.\n\n" + _footer.strip()).count(
           _footer.strip()) == 1,
       "a model imitating the footer it keeps seeing must not produce two")
    ok("a normal reply does get the footer", _footer.strip() in _once,
       _once[-120:])
    ok("a disabled plugin still appends nothing",
       PluginManager.postprocess.__get__(
           type("X", (), {"_plugins": {"p": {"enabled": False, "hooks": {
               "append_footer": "NOPE"}}}})())("hi") == "hi",
       "the toggle has to keep working, or 'on by default' becomes 'forced'")
except Exception as _e:
    ok("plugin_manager could be exercised", False, "%s: %s"
       % (type(_e).__name__, _e))


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
