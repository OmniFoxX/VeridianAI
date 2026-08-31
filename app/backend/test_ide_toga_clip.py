#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ide_toga_clip.py -- Toga reading and rewriting the IDE editor.

THE TWO QUESTIONS, WHICH ARE NOT THE SAME QUESTION

  READING   the buffer riding in with a turn is the person's choice about
            their own text. If they switched it on, it goes; if not, it is not
            summarised or redacted, it is never put on the wire at all.

  WRITING   the model replacing what is in their editor is authority over
            their workspace. That is re-checked at the moment of the write,
            not inherited from the fact that a buffer arrived earlier in the
            same turn -- because a person can drop from Advanced to Beginner
            while a reply is still streaming.

Both gates are ANDs of the same two things: the stored mode, and the stored
switch. Neither is taken from the payload. The client sends the buffer; it
does not get to say what it is allowed to do.

WHY IDE_WRITE IS BRACKET-BALANCED

Its body is source code, and source is full of `[`. The non-greedy regex that
[CODE:] used to have stopped at the first `]`, which truncated `data["k"][0]`
mid-expression. For [CODE:] that produced a SyntaxError; for [IDE_WRITE:] it
would hand the person a silently corrupted buffer.
"""
import ast
import io
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

import sage_engine as se
import customs_daemon as cd

_fails = []


def ok(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n            -> " + str(detail)) if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def _read(*p):
    return io.open(os.path.join(*p), encoding="utf-8").read()


MAIN = _read(_HERE, "main.py")
IDEJS = _read(_ROOT, "frontend", "js", "ide.js")
CHATJS = _read(_ROOT, "frontend", "js", "chat.js")
HTML = _read(_ROOT, "frontend", "index.html")

print("\n=== 1. The tag parses as source, not as prose ===")
acts = se.parse_agent_actions('ok\n[IDE_WRITE: rows = data["k"][0]\n'
                              'print(rows[:3])]\ndone')
_w = [c for t, c in acts if t == "ide_write"]
ok("an IDE_WRITE tag is recognised", len(_w) == 1, acts)
ok("...and survives brackets in the code",
   _w and _w[0] == 'rows = data["k"][0]\nprint(rows[:3])',
   _w[0] if _w else None)
ok("IDE_WRITE is in the bracket-balanced group",
   '("IDE_WRITE", "ide_write")' in _read(_HERE, "sage_engine.py"),
   "a non-greedy scan would truncate the buffer at the first ]")
ok("a bare tag is detected as an orphan",
   "IDE_WRITE" in se.detect_orphan_tool_tags("bare [IDE_WRITE: x]"),
   "a tag that was meant to fire and did not must be visible")
ok("...but not when it is a pedagogical example",
   "IDE_WRITE" not in se.detect_orphan_tool_tags("use `[IDE_WRITE: ...]`"),
   "explaining the tag is not emitting it")

print("\n=== 2. Customs knows the tool ===")
v = cd.registry.get("ide_write")
ok("a validator is registered", v is not None and type(v).__name__ == "IdeWriteValidator")
ok("the tag maps to args", cd._TAG_TO_ARGS["ide_write"]("x=1") == {"code": "x=1"})
ok("...and back again", cd._ARGS_TO_TAG["ide_write"]({"code": "x=1"}) == "x=1")
ok("the audit preview is a LENGTH, never the code",
   v.safe_preview({"code": "SECRET" * 10}) == "60 chars",
   "the person's working code must not reach an audit record")
ok("a fenced body is repaired, like [CODE:]",
   v.attempt_repair({"code": "```python\nx=1\n```"}, None) == {"code": "x=1"})
ok("there is no length ceiling on the code field",
   "max_length" not in _read(_HERE, "customs_daemon.py").split(
       "class IdeWriteArgs")[1].split("class ")[0],
   "a truncating validator would corrupt code rather than refuse it")

print("\n=== 3. READING is gated on mode AND switch, server-side ===")
_ws = MAIN[MAIN.index("_ide_buf = \"\""):]
_ws = _ws[:_ws.index("# ---")] if "# ---" in _ws[:4000] else _ws[:2000]
ok("the read checks the stored switch", 'ui_prefs' in _ws and 'ide_toga_clip' in _ws)
# Whitespace-tolerant on purpose. The single-line spelling went red the moment
# the call was wrapped across two lines by ordinary formatting -- a correct
# change reported as a defect, which is how a test teaches people to stop
# reading it. The claim is "the mode is checked here", not "it is checked on
# one line".
ok("...and the stored mode",
   re.search(r'_ide_mode_at_least\(\s*_ws_ns\s*,\s*"advanced"\s*\)', _ws)
   is not None,
   _ws[:200])
ok("...combined with AND, not OR", "_clip_on and _ide_mode_at_least" in _ws,
   "either one alone must not be enough")
ok("the buffer is capped", "_IDE_BUFFER_MAX" in _ws,
   "prompt context; an enormous paste would evict the conversation")
ok("the cap is a real number", re.search(r"_IDE_BUFFER_MAX = \d+", MAIN) is not None)
ok("only a string is accepted", "isinstance(_raw_buf, str)" in _ws,
   "a payload is web content; a dict here would reach the prompt builder")

print("\n=== 4. WRITING is re-checked at the moment of the write ===")
_wr = MAIN[MAIN.index('elif action_type == "ide_write"'):]
_wr = _wr[:_wr.index('elif action_type == "search_memory"')]
ok("the write re-reads the switch", "ide_toga_clip" in _wr,
   "inheriting permission from the read would survive a mid-stream downgrade")
ok("...and the mode", '_ide_mode_at_least(' in _wr)
ok("...combined with AND", "_clip_ok and _ide_mode_at_least" in _wr)
ok("a refusal is reported to the model, not silently dropped",
   "[REFUSED]" in _wr,
   "a tool that silently no-ops teaches the model to keep trying")
ok("a refusal sends NOTHING to the browser",
   _wr.index("[REFUSED]") < _wr.index('"type": "ide_write"'),
   "the refusal branch must return before the send")
ok("the write is announced as a tool call", '"tool": "ide_write"' in _wr)
ok("the model is told the person can undo",
   "undo" in _wr.lower(),
   "so it does not treat the write as final and irreversible")

print("\n=== 5. The prompt block is gated on PERMISSION, not on content ===")
# THIS SECTION ONCE ASSERTED THE BUG.
#
# It read: ok("the editor block is conditional on the buffer",
#             "if _ide_buf:" in MAIN)
#
# Which was true, and was the defect. Gating the DESCRIPTION of [IDE_WRITE:] on
# the editor having text in it meant that asking for code to be put into an
# EMPTY editor -- the most natural first thing anyone does with this panel --
# was the single case that could never work. Toga was never told the tag
# existed, so she did the nearest thing she knew and reported success. Todd hit
# it twice before anyone thought to look at the prompt builder.
#
# The instinct behind the old assertion was right: the block SHOULD be
# conditional, because a model never told a tag exists cannot be argued into
# emitting it. It was pinned to the wrong condition. A test can hold a bug in
# place as firmly as it holds a feature.
ok("the block is gated on permission", "if _ide_may_write:" in MAIN)
# Matched as a STATEMENT (line start + exact indent), not as a substring: the
# comment above the block quotes the old condition on purpose, and a loose
# `"if _ide_buf:" in MAIN` reports that comment as the bug it warns about.
ok("...and permission is NOT the buffer being non-empty",
   "\n            if _ide_buf:" not in MAIN,
   "content and consent are different questions")
ok("permission is clip AND mode",
   "_ide_may_write = _clip_on and _ide_mode_at_least(" in MAIN)
ok("Expert is told it may run, others are told they may not",
   'You may NOT run it' in MAIN and '_ide_mode_at_least(_ws_ns, "expert")' in MAIN,
   "a model never told about a capability cannot be argued into using it")
ok("the block tells the model to send the WHOLE file",
   "replaces the whole buffer" in MAIN,
   "a fragment would silently truncate the person's work")
ok("...and not to echo it back", "do not " in MAIN.lower() and "display" in MAIN.lower())

print("\n=== 5b. The REAL block, executed, in all four states ===")
# Source matching says the condition is spelled right. This runs the actual
# text-building code with stubs, which is what says the person gets a usable
# answer -- and it is the check that would have caught the original bug without
# anyone having to suspect the prompt builder.
# Anchored at a LINE START with the exact indent. Without the leading newline
# the 12-space needle also matches the 16-space `if _ide_may_write:` in the
# buffer-intake gate several hundred lines earlier, and the slice runs off into
# unrelated code.
_blk = MAIN[MAIN.index("\n            if _ide_may_write:") + 1:]
_blk = _blk[:_blk.index('\n            if not messages')]
_blk = "\n".join(line[12:] if line.startswith("            ") else line
                 for line in _blk.split("\n"))


def _build(may_write, buf, mode):
    ns_ = {"_ide_may_write": may_write, "_ide_buf": buf, "sys_prompt": "",
           "_ws_ns": None,
           "_ide_mode_at_least": lambda ns, lvl: (
               ["beginner", "advanced", "expert"].index(mode)
               >= ["beginner", "advanced", "expert"].index(lvl))}
    exec(compile(_blk, "<main.py editor block>", "exec"), ns_)
    return ns_["sys_prompt"]


_adv_empty = _build(True, "", "advanced")
ok("PERMITTED + EMPTY editor still names the tag",
   "[IDE_WRITE:" in _adv_empty, _adv_empty[:200])
ok("...and says writing into an empty editor is expected",
   "EMPTY" in _adv_empty,
   "otherwise a model reasonably concludes there is nothing to replace")
ok("...and says printing it in chat is not the same thing",
   "not doing what they asked" in _adv_empty,
   "that is the exact failure the person saw")

_adv_full = _build(True, "x = 1\n", "advanced")
ok("PERMITTED + contents: the buffer is included",
   "--- EDITOR BEGIN ---" in _adv_full and "x = 1" in _adv_full)
ok("...and Advanced is told it may not run it",
   "You may NOT run it" in _adv_full)

_exp_full = _build(True, "x = 1\n", "expert")
ok("EXPERT is told it may run", "may also run it" in _exp_full)

_denied = _build(False, "", "beginner")
ok("NOT PERMITTED does not name the tag",
   "IDE_WRITE" not in _denied,
   "the safety property: never told, cannot be argued into it")
ok("...but is told it has no access", "NO access" in _denied)
ok("...and is told which two switches change that",
   "Advanced" in _denied and "Copy/Paste" in _denied,
   "a refusal nobody can act on is how the person ends up asking twice")
ok("...and is forbidden from claiming it wrote anything",
   "NEVER say you have written" in _denied,
   "the confident 'done' over an unchanged editor is the whole complaint")

print("\n=== 6. The client sends nothing it was not asked to ===")
ok("chat.js asks ide.js, it does not read the textarea itself",
   "window.ideBufferForSend" in CHATJS and "ide-editor" not in CHATJS,
   "one place decides whether the buffer may travel")
ok("null becomes undefined, so the key is omitted",
   "|| undefined" in CHATJS,
   "an empty string on the wire says the same thing less clearly")
ok("ideBufferForSend refuses in Beginner",
   '_mode === "beginner" || !_togaClip' in IDEJS)
ok("chat.js routes ide_write to the panel",
   'data.type === "ide_write"' in CHATJS and "ideApplyWrite" in CHATJS)
ok("...defensively, in case ide.js has not loaded",
   "if (window.ideApplyWrite)" in CHATJS)

print("\n=== 7. A write is undoable, and visible ===")
ok("the previous contents are kept", "_undoBuffer = ta.value" in IDEJS)
ok("an undo control appears", 'id="ide-undo-write"' in HTML)
ok("...hidden until there is something to undo", "hidden" in HTML.split(
   'id="ide-undo-write"')[1].split(">")[0])
ok("the panel is brought to the front so the change is not silent",
   "tab.click()" in IDEJS)
ok("the write sets .value, never innerHTML",
   "ta.value = text" in IDEJS,
   "model output must not become markup")

print("\n=== 8. The switch is its own preference, and Beginner overrides it ===")
ok("toga_clip is an allowlisted pref", '"toga_clip": False' in MAIN)
ok("Beginner disables the control", 'var allowed = _mode !== "beginner"' in IDEJS)
ok("...and forces the displayed state off",
   "var on = allowed && _togaClip" in IDEJS,
   '"on but not permitted" is not a state anyone should reason about')

import ui_prefs as _up
ok("ide_toga_clip is not a machine key",
   "ide_toga_clip" not in _up.MACHINE_KEYS)

print("\n=== 9. main.py still parses ===")
ok("main.py is syntactically valid", ast.parse(MAIN) is not None)

print("")
if _fails:
    print("%d CHECK(S) FAILED" % len(_fails))
    for f in _fails:
        print("   - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED - Toga copy/paste")
sys.exit(0)
