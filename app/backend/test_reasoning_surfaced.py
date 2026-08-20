#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_reasoning_surfaced.py -- the reasoning trace is stored, shown, and never replayed.

WHERE THIS PICKS UP

v2.15.2 already CAPTURED the trace: both backends write it into the per-turn
stats dict (Ollama's message.thinking, llama-server's reasoning_content), and
_no_answer_notice explains a turn that thought and never answered. Verified
against the live Ollama on 2026-08-20 -- a real trace came back.

And then nothing read it. main.py collected the trace every turn and discarded
it at the end of the handler. This file covers the three things that had to be
true for it to be worth capturing:

  1. STORED, encrypted, from its very first write.
  2. SHOWN, collapsed, and still there after a reload.
  3. NEVER REPLAYED to the model as if it were dialogue.

(3) is the one that bites. The client owns the conversation array and posts it
back every turn, and /api/history hands it whatever we persisted. So storing
the trace on the assistant message puts it exactly one round-trip away from the
prompt. If it got there: the model's own discarded thinking re-enters context
as dialogue, the prompt grows by every previous trace (often longer than the
answers), the cacheable prefix changes shape, and the llama-server path -- which
strips only `images` and forwards unknown keys verbatim -- could be rejected
outright by a strict endpoint.

STORAGE, deliberately: the trace rides sage_engine.save_chat_memory rather than
getting a store of its own. That path is already Fernet-encrypted and already
scoped to the profile namespace, so the trace is protected at rest from the
first write. A new store would have had to earn that separately -- which is
precisely the retrofit procedural.json needed after being missed.

    python test_reasoning_surfaced.py
"""
import ast
import io
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


MAIN = io.open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()
MM = io.open(os.path.join(_HERE, "model_manager.py"), encoding="utf-8").read()
_JS = os.path.join(_HERE, "..", "frontend", "js", "chat.js")
CHAT = io.open(_JS, encoding="utf-8").read() if os.path.exists(_JS) else ""
_T = ast.parse(MAIN)


# =============================================================================
print("=== 1. Capture still works (the half that already shipped) ===")
# =============================================================================
ok("Ollama's thinking channel is read", '_msg.get("thinking")' in MM)
ok("llama-server's reasoning_content is read",
   'delta.get("reasoning_content")' in MM)
ok("both write the trace into the per-turn dict",
   MM.count('_stats["reasoning"]') == 2, MM.count('_stats["reasoning"]'))
ok("a turn that thought and never answered still says so",
   "_no_answer_notice" in MM)


# =============================================================================
print("\n=== 2. NEVER replayed to the model ===")
# =============================================================================
_fns = {n.name for n in _T.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
ok("_sanitize_client_messages exists at module level",
   "_sanitize_client_messages" in _fns)
ok("the allowed keys are a WHITELIST, not a blacklist",
   "_ALLOWED_MESSAGE_KEYS" in MAIN and "k in _ALLOWED_MESSAGE_KEYS" in MAIN,
   "a field added to the stored shape later must be dropped BY DEFAULT -- a "
   "blacklist would forward it silently, which nobody would notice")
ok("reasoning is NOT in the allowed set",
   re.search(r"_ALLOWED_MESSAGE_KEYS\s*=\s*\([^)]*\)", MAIN)
   and "reasoning" not in re.search(
       r"_ALLOWED_MESSAGE_KEYS\s*=\s*\([^)]*\)", MAIN).group(0))

# The call must sit on the path the client's array actually takes.
_calls = [i + 1 for i, l in enumerate(MAIN.splitlines())
          if "_sanitize_client_messages(" in l and "def " not in l]
ok("it is applied where the client's messages enter", len(_calls) == 1, _calls)
ok("...wrapping data.get(\"messages\") directly",
   '_sanitize_client_messages(data.get("messages", []))' in MAIN,
   "sanitizing a COPY made later would leave the original in play")

# Behaviour, not just wiring.
sys.path.insert(0, _HERE)
_ns = {}
_src = MAIN[MAIN.index("_ALLOWED_MESSAGE_KEYS"):]
_src = _src[:_src.index("\nasync def _watched_generate")]
exec(compile(_src, "<sanitizer>", "exec"), _ns)
_san = _ns["_sanitize_client_messages"]

_dirty = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "answer",
     "reasoning": "SECRET THINKING THAT MUST NOT BE REPLAYED",
     "model": "qwen3.8", "ts": "2026-08-20T00:00:00Z"},
    {"role": "user", "content": "follow up", "images": ["b64data"]},
]
_clean = _san(_dirty)
ok("the reasoning key is stripped",
   all("reasoning" not in m for m in _clean),
   _clean)
ok("no trace text survives anywhere in the result",
   "SECRET THINKING" not in repr(_clean))
ok("role and content are preserved exactly",
   [(m["role"], m["content"]) for m in _clean]
   == [(m["role"], m["content"]) for m in _dirty])
ok("images survive (vision multi-turn depends on them)",
   _clean[3].get("images") == ["b64data"])
ok("incidental UI keys are dropped too",
   "model" not in _clean[2] and "ts" not in _clean[2],
   "they were never meant for the model either")
ok("message order is unchanged", len(_clean) == len(_dirty))
ok("the input list is not mutated",
   _dirty[2].get("reasoning") == "SECRET THINKING THAT MUST NOT BE REPLAYED",
   "the caller still archives and logs from the original array")
ok("a non-list input does not raise", _san(None) == [] and _san("x") == [])
ok("a non-dict element is passed through for the caller to reject",
   _san([1, "a"]) == [1, "a"])
ok("an empty array stays empty", _san([]) == [])


# =============================================================================
print("\n=== 3. Stored, and encrypted because of WHERE it is stored ===")
# =============================================================================
ok("the trace is attached to the assistant message",
   '_entry["reasoning"] = _reasoning' in MAIN)
ok("it is read from the per-turn dict, not a global",
   '(_turn_stats or {}).get(\n                                    "reasoning")' in MAIN
   or '(_turn_stats or {}).get("reasoning")' in MAIN
   or "_turn_stats or {}" in MAIN)
ok("it rides save_chat_memory (already encrypted, already ns-scoped)",
   "sage_engine.save_chat_memory(history, _ws_ns)" in MAIN)

_SE = os.path.join(_HERE, "sage_engine.py")
if os.path.exists(_SE):
    _se = io.open(_SE, encoding="utf-8").read()
    _fn = _se[_se.index("def save_chat_memory"):]
    _fn = _fn[:_fn.index("\ndef ", 5)] if "\ndef " in _fn[5:] else _fn
    ok("save_chat_memory encrypts",
       "dump_json_encrypted" in _fn or "encrypt_bytes" in _fn, _fn[:200])
    ok("...under the caller's namespace, not a default",
       "ns=ns" in _fn or "ns)" in _fn,
       "ns=None silently means the owner's key -- the cross-profile leak shape")

ok("an empty trace is not stored as an empty key",
   "if _reasoning:" in MAIN,
   "every non-thinking model would otherwise carry a dead field forever")


# =============================================================================
print("\n=== 4. Sent to the UI, on done, not as tokens ===")
# =============================================================================
ok("the done payload can carry it", '_done_payload["reasoning"] = _r' in MAIN)
ok("a character count rides along for the collapsed label",
   '_done_payload["reasoning_chars"] = len(_r)' in MAIN)
ok("it is NOT streamed as content tokens",
   '"type": "token"' in MAIN and 'reasoning' not in
   MAIN[MAIN.index('"type": "token"') - 200:MAIN.index('"type": "token"') + 200],
   "interleaving thinking with content is what made a reasoning model look "
   "like it answered with silence")
ok("failing to attach it cannot break the turn",
   "except Exception:\n                        pass" in MAIN)


# =============================================================================
print("\n=== 5. The panel: collapsed, safe, and survives a reload ===")
# =============================================================================
if not CHAT:
    ok("chat.js is reachable from the backend folder", False, _JS)
else:
    ok("chat.js is reachable from the backend folder", True)
    ok("there is a panel builder", "function attachReasoningPanel" in CHAT)
    ok("it uses a native <details> disclosure",
       'createElement("details")' in CHAT,
       "keyboard operable and announced by screen readers without extra JS")
    ok("collapsed by default (no `open` attribute set)",
       ".open = true" not in CHAT and 'setAttribute("open"' not in CHAT,
       "this is provenance, not conversation")

    _panel = CHAT[CHAT.index("function attachReasoningPanel"):]
    _panel = _panel[:_panel.index("\nfunction ", 10)]
    ok("the trace is inserted as textContent",
       "body.textContent = reasoning" in _panel)

    def _code_only(js):
        """JS with comments removed.

        The first version of this section searched the function text for
        "innerHTML" and failed -- on the comment reading "NOT innerHTML".
        Third time in this release that an assertion matched the prose ABOUT a
        thing instead of the thing: the `finally` check, the os.replace
        ordering check, and now this. The pattern is always the same, so the
        fix is the same: look at code, not at text that discusses code.
        """
        js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
        return re.sub(r"//[^\n]*", "", js)

    _panel_code = _code_only(_panel)
    ok("the trace is NEVER assigned into innerHTML",
       not re.search(r"\.innerHTML\s*=", _panel_code),
       "model output is not trusted markup -- this is an injection path")
    ok("...and nothing else in the panel writes markup",
       "insertAdjacentHTML" not in _panel_code
       and "outerHTML" not in _panel_code)
    # Prove the check can fail, so a future regression is actually caught.
    ok("that check would catch a real regression",
       bool(re.search(r"\.innerHTML\s*=",
                      _code_only("body.innerHTML = reasoning; // safe?"))),
       "a check that cannot go red proves nothing")
    ok("it will not double-attach",
       'querySelector(".reasoning-panel")' in _panel)

    ok("the live turn renders it", "attachReasoningPanel(target, meta.reasoning)"
       in CHAT)
    ok("a reloaded/archived message renders it too",
       "attachReasoningPanel(wrap, msg.reasoning)" in CHAT,
       "otherwise the panel appears once and vanishes on reload, which reads "
       "as data loss rather than a missing re-render")
    ok("the trace is kept on the message object for the archive",
       "last.reasoning = meta.reasoning" in CHAT)

    # The client's own whitelist -- the layer the server one backs up.
    ok("buildPayload still maps to role/content(+images) only",
       "const o = { role: m.role, content: m.content };" in CHAT,
       "if this ever grows, the server sanitizer is what holds the line")


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
