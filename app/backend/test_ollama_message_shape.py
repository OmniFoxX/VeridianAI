#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ollama_message_shape.py -- Ollama gets a message array it accepts.

THE BUG THIS LOCKS OUT (v2.15.2)
main.py injects volatile context -- the current date/time block, procedural
memory, and the CRAIID warm handoff -- as `system` messages immediately before
the final user turn, so the cacheable system+history prefix stays byte-stable
and only the tail is reprocessed each turn. Good reason; it stays.

llama.cpp accepts system messages at any position. Ollama does not, and rejects
the whole request before generation starts:

    msg="chat prompt error" error="system message must be at the beginning"
    POST /api/chat -> 500

So every Ollama-served model 500'd the instant a date block was injected --
which is every turn. It looked like a reasoning-model problem because that is
what Todd happened to be testing (qwen3.8, laguna-xs-2.1), but the request
never reached the model at all. No model, thinking or otherwise, would have
worked on that tier.

Confirmed against the live Ollama, qwen3.8:27b-q4_K_M, 2026-08-19:
    [system, user]                 -> 200
    [system, user, system, user]   -> 500
    [system, user, user,   user]   -> 200

    python test_ollama_message_shape.py
"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_HERE = os.path.dirname(os.path.abspath(__file__))

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


MM = io.open(os.path.join(_HERE, "model_manager.py"), encoding="utf-8").read()
MAIN = io.open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()

# Import without dragging in the whole model manager: pull the function out of
# the module source and exec it standalone. It is deliberately self-contained
# (module level, no self, stdlib only) so this is possible.
_src = MM[MM.index("def _ollama_safe_messages"):]
_src = _src[:_src.index("\n    async def _gen_ollama")]
_ns = {"List": list, "Dict": dict}
exec(compile(_src, "<_ollama_safe_messages>", "exec"), _ns)
safe = _ns["_ollama_safe_messages"]


# =============================================================================
print("=== 1. The rule Ollama actually enforces ===")
# =============================================================================
def legal(msgs):
    """Ollama's constraint: no system message at any index but 0."""
    return not any(m.get("role") == "system" for m in msgs[1:])


shipped = [
    {"role": "system", "content": "SYSTEM PROMPT"},
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "hi"},
    {"role": "system", "content": "=== CURRENT DATE & TIME ===\nnow\n=== END ==="},
    {"role": "system", "content": "=== PROCEDURAL MEMORY ===\nx\n=== END ==="},
    {"role": "user", "content": "what day is it"},
]
ok("the array main.py builds is one Ollama REFUSES", not legal(shipped))
ok("...and the normaliser makes it legal", legal(safe(shipped)))


# =============================================================================
print("\n=== 2. Nothing is lost, moved, or reordered ===")
# =============================================================================
out = safe(shipped)
ok("no message is dropped", len(out) == len(shipped), (len(out), len(shipped)))
ok("no message is reordered",
   [m["content"] for m in out] == [m["content"] for m in shipped])
ok("every content string survives byte-for-byte",
   all(a["content"] == b["content"] for a, b in zip(out, shipped)))
ok("the tail blocks stay AT the tail -- the whole point of the KV-cache design",
   out[3]["content"].startswith("=== CURRENT DATE") and
   out[4]["content"].startswith("=== PROCEDURAL"))
ok("the leading system prompt keeps its role",
   out[0]["role"] == "system" and out[0]["content"] == "SYSTEM PROMPT")
ok("the trailing system blocks become user turns",
   out[3]["role"] == "user" and out[4]["role"] == "user")
ok("user and assistant turns are untouched",
   out[1]["role"] == "user" and out[2]["role"] == "assistant")
ok("the input list is not mutated in place",
   shipped[3]["role"] == "system",
   "callers reuse `messages` for logging and archiving")


# =============================================================================
print("\n=== 3. Shapes that must pass through unchanged ===")
# =============================================================================
plain = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
ok("a normal system-first array is returned as-is", safe(plain) == plain)
ok("an empty array does not raise", safe([]) == [])
no_sys = [{"role": "user", "content": "u"}]
ok("a system-less array is untouched", safe(no_sys) == no_sys)
lead_only = [{"role": "user", "content": "u"}, {"role": "system", "content": "s"}]
ok("a system message at index 1 IS relabelled (index 0 is the only exemption)",
   safe(lead_only)[1]["role"] == "user")


# =============================================================================
print("\n=== 4. It is applied where it is needed, and only there ===")
# =============================================================================
ok("the Ollama payload uses it",
   '"messages": _ollama_safe_messages(messages),' in MM)
_openai = MM[MM.index("# --- llama-server streaming"):]
ok("the llama-server path does NOT -- it was never broken",
   "_ollama_safe_messages" not in _openai)
ok("the relabelling is announced in the log, not done silently",
   "relabelled" in MM and "[OLLAMA]" in MM)

# If main.py ever stops tail-injecting, this whole function becomes dead weight
# and should go. Pin the assumption so that shows up as a failure, not a
# mystery.
ok("main.py still tail-injects system blocks (the reason this exists)",
   "_late_ctx.append({\"role\": \"system\"" in MAIN and
   "messages[_tail_at:_tail_at] = _late_ctx" in MAIN)


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
