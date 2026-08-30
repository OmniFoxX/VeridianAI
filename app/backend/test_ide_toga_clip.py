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
ok("...and the stored mode", '_ide_mode_at_least(_ws_ns, "advanced")' in _ws)
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

print("\n=== 5. The prompt block only exists when the buffer does ===")
ok("the editor block is conditional on the buffer", "if _ide_buf:" in MAIN)
ok("Expert is told it may run, others are told they may not",
   'You may NOT run it' in MAIN and '_ide_mode_at_least(_ws_ns, "expert")' in MAIN,
   "a model never told about a capability cannot be argued into using it")
ok("the block tells the model to send the WHOLE file",
   "replaces the whole buffer" in MAIN,
   "a fragment would silently truncate the person's work")
ok("...and not to echo it back", "do not " in MAIN.lower() and "display" in MAIN.lower())

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
