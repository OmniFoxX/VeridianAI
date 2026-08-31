#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ide_run_tag.py -- [IDE_RUN]: Toga running the editor, visibly.

WHY THIS EXISTS

Expert mode's ladder has always promised "Toga may read, write, AND run". The
first two were real. The third was not, and it failed in the way that is
hardest to notice: Toga's only route to running was [CODE:], which

  * runs the code inside the TAG, so the model had to retype the buffer and
    could run something subtly different from what is on the person's screen;
  * reports into the CHAT, while `ideShowOutput` -- the only thing that writes
    the IDE display area -- was reachable solely from the Run button.

So the person watched an empty output box while the model, holding a perfectly
real tool result, said it had run. Todd hit this twice, after hitting the
same-shaped bug in [IDE_WRITE:]. A capability with no route to where somebody is
looking reads exactly like a lie, and is indistinguishable from one.

[IDE_RUN] carries NO payload on purpose: what runs is the buffer that rode in
with the turn, byte for byte.

The two things this file exists to hold in place:

  1. every gate is re-checked at dispatch and REFUSES OUT LOUD, telling the
     model not to claim success -- silence is what produced the false "done";
  2. a successful run emits `ide_output`, and the frontend routes that to the
     display area rather than only to the chat.

    python test_ide_run_tag.py
"""
import io
import os
import re
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


def _read(*parts):
    return io.open(os.path.join(*parts), encoding="utf-8").read()


import sage_engine as se                                   # noqa: E402

MAIN = _read(_HERE, "main.py")
CHATJS = _read(_ROOT, "frontend", "js", "chat.js")

# =============================================================================
print("=== 1. The tag parses, in every shape a model emits ===")
# =============================================================================
for text, why in (
    ("[IDE_RUN]", "the documented form"),
    ("[IDE_RUN:]", "an empty colon form"),
    ("[IDE_RUN: the editor]", "a chatty payload, which models add unbidden"),
    ("Sure, running it now. [IDE_RUN] Done.", "surrounded by prose"),
):
    acts = se.parse_agent_actions(text)
    ok("%s -> ide_run  (%s)" % (text[:26], why),
       [a for a in acts if a[0] == "ide_run"] != [], acts)

ok("a payload is accepted but carries nothing executable",
   se.parse_agent_actions("[IDE_RUN: rm -rf /]")[0][1] == "rm -rf /",
   "it is parsed, and the dispatcher ignores it -- what runs is the BUFFER")

ok("a fenced example is NOT dispatched",
   se.parse_agent_actions("use `[IDE_RUN]` when asked") == [],
   "pedagogical text must not fire a tool")

ok("IDE_RUN is a known tag name",
   "IDE_RUN" in se._KNOWN_TAG_NAMES,
   "otherwise orphan detection cannot reason about it")

# =============================================================================
print("\n=== 2. Customs sees it ===")
# =============================================================================
import customs_daemon as cd                                # noqa: E402
_r = cd.inspect_tag("ide_run", "", origin="agentic")
ok("an ide_run tag passes Customs", _r.allowed, _r.verdict)
ok("...and it is a REGISTERED tool, not the unknown-tool floor",
   "ide_run" in _read(_HERE, "customs_daemon.py").split(
       "_TAG_TO_ARGS")[1][:600],
   "an unregistered executor is an unpoliced path into the same subprocess")
ok("the audit preview leaks no code",
   "runs the current editor buffer" in _read(_HERE, "customs_daemon.py"),
   "there is no payload to preview, and the buffer must never enter the log")

# =============================================================================
print("\n=== 3. Every gate refuses OUT LOUD ===")
# =============================================================================
# The dispatch branch, sliced from main.py. Each refusal must (a) exist and
# (b) tell the model not to claim it ran -- silence is what produced the
# confident false "done" the first two times.
_br = MAIN.split('elif action_type == "ide_run":')[1].split(
    'elif action_type == "search_memory":')[0]
# Adjacent string literals joined and whitespace flattened, because the
# sentences these checks look for are split across source lines by ordinary
# formatting. Matching raw source for a message that spans two lines is the
# same brittleness that has now bitten this project four times in a day:
# a comment matched as code, a call broken by a line wrap, twice.
_flat = re.sub(r'"\s*\n\s*"', "", _br)
_flat = re.sub(r"\s+", " ", _flat)

ok("code execution off is refused", "code_ok" in _br)
ok("...naming the setting", "Settings -> Code Execution" in _flat,
   _flat[:200])
ok("running needs EXPERT, not merely advanced",
   '"expert"' in _br and '"advanced"' not in _br,
   "running is a strictly larger authority than writing")
ok("...and the copy/paste switch too", "ide_toga_clip" in _br)
ok("an empty editor is refused", "editor is empty" in _br)
ok("EVERY refusal forbids claiming success",
   _flat.count("Do not claim it ran") == 2,
   "one for each refusal a person can actually hit; the empty-editor case "
   "is self-evident from the output box")
ok("the gates are re-checked here, not inherited",
   "ui_prefs" in _br and "_uip3" in _br,
   "a turn that began in Expert and was dropped mid-stream must not still run")

# =============================================================================
print("\n=== 4. Output reaches the DISPLAY, not only the chat ===")
# =============================================================================
ok("a successful run emits ide_output", '"type": "ide_output"' in _br,
   "this is the entire reason the tag exists")
ok("...and ALSO a normal tool_result",
   '"tool": "ide_run"' in _br,
   "so the model sees what happened and cannot invent it")
ok("chat.js routes ide_output to the panel",
   'data.type === "ide_output"' in CHATJS and "ideShowOutput" in CHATJS)
ok("...defensively, in case ide.js has not loaded",
   re.search(r"if \(window\.ideShowOutput\)", CHATJS) is not None)
ok("ideShowOutput is exported for it to reach",
   "window.ideShowOutput = ideShowOutput" in _read(
       _ROOT, "frontend", "js", "ide.js"))

# =============================================================================
print("\n=== 5. The prompt tells Expert the tag exists ===")
# =============================================================================
# The bug in [IDE_WRITE:] was a capability the model was never told about.
# Verified by executing the real block, not by matching its source.
_blk = MAIN[MAIN.index("\n            if _ide_may_write:") + 1:]
_blk = _blk[:_blk.index('\n            if not messages')]
_blk = "\n".join(l[12:] if l.startswith("            ") else l
                 for l in _blk.split("\n"))


def _build(may_write, buf, mode):
    ns_ = {"_ide_may_write": may_write, "_ide_buf": buf, "sys_prompt": "",
           "_ws_ns": None,
           "_ide_mode_at_least": lambda ns, lvl: (
               ["beginner", "advanced", "expert"].index(mode)
               >= ["beginner", "advanced", "expert"].index(lvl))}
    exec(compile(_blk, "<main.py editor block>", "exec"), ns_)
    return ns_["sys_prompt"]


_exp = _build(True, "print(1)\n", "expert")
ok("EXPERT is told about [IDE_RUN]", "[IDE_RUN]" in _exp, _exp[:220])
ok("...and told not to retype it into [CODE:]",
   "Do NOT retype" in _exp,
   "a retyped copy can differ from what is on their screen")
ok("...and told the output goes to their display area",
   "display area" in _exp)

_adv = _build(True, "print(1)\n", "advanced")
ok("ADVANCED is NOT told about it", "[IDE_RUN]" not in _adv,
   "a model never told a tag exists cannot be argued into emitting it")
ok("...and is told the person presses Run", "You may NOT run it" in _adv)

_none = _build(False, "", "beginner")
ok("NO ACCESS is told about neither tag",
   "[IDE_RUN]" not in _none and "IDE_WRITE" not in _none)

print("\n=== 6. The REAL branch, executed, at every gate ===")
# Source matching says the gates are spelled right. This runs the actual
# dispatch code and asserts on what a person would SEE -- which is the check
# that would have caught the original bug, where every gate was spelled
# correctly and the output still never reached the display.
import asyncio                                             # noqa: E402
import types                                               # noqa: E402

_blk2 = MAIN.split('elif action_type == "ide_run":')[1].split(
    'elif action_type == "search_memory":')[0]
_blk2 = "\n".join(l[32:] if l.startswith(" " * 32) else l
                  for l in _blk2.split("\n"))
_SRC = "async def _run():\n" + "\n".join("    " + l for l in _blk2.split("\n"))


class _WS:
    def __init__(self):
        self.sent = []

    async def send_json(self, d):
        self.sent.append(d)


async def _case(code_ok, clip, mode, buf):
    ws, acc = _WS(), {}
    _m = types.ModuleType("ui_prefs")
    _m.get = lambda k, d, ns=None: clip
    sys.modules["ui_prefs"] = _m
    g = {"executed_any": False, "_ide_buf": buf, "code_ok": code_ok,
         "_ws_ns": None, "websocket": ws, "tool_results_acc": acc, "step": 1,
         "_ide_mode_at_least": lambda ns, lvl: (
             ["beginner", "advanced", "expert"].index(mode)
             >= ["beginner", "advanced", "expert"].index(lvl)),
         "sage_engine": se, "_code_timeout": 20, "asyncio": asyncio}
    exec(compile(_SRC, "<ide_run branch>", "exec"), g)
    await g["_run"]()
    return [d.get("type") for d in ws.sent], (list(acc.values()) or [""])[0]


def case(code_ok, clip, mode, buf):
    return asyncio.run(_case(code_ok, clip, mode, buf))

_types, _out = case(True, True, "expert", "print('HELLO FROM EDITOR')\n")
ok("a permitted run executes THE BUFFER", "HELLO FROM EDITOR" in _out, _out[:120])
ok("...and emits ide_output to the display", "ide_output" in _types, _types)
ok("...and a tool_result so the model sees it too",
   "tool_result" in _types, _types)
ok("...and announces itself first", _types[0] == "tool_call", _types)

for label, args in (
    ("code execution off", (False, True, "expert", "print(1)\n")),
    ("advanced, not expert", (True, True, "advanced", "print(1)\n")),
    ("expert but the switch is off", (True, False, "expert", "print(1)\n")),
    ("an empty editor", (True, True, "expert", "   \n")),
):
    _t, _o = case(*args)
    ok("%s is refused" % label, _o.startswith("[REFUSED]"), _o[:90])
    ok("...and NOTHING reaches the display", "ide_output" not in _t, _t)

_t, _o = case(True, True, "advanced", "print(1)\n")
ok("a refusal a person can act on names the mode AND the switch",
   "Expert" in _o and "Copy/Paste" in _o, _o)

print("")
if _fails:
    print("%d CHECK(S) FAILED" % len(_fails))
    for f in _fails:
        print("   - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED - IDE_RUN tag")
sys.exit(0)
