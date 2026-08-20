#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_reasoning_capture.py -- reasoning and token counts survive the stream.

WHAT WAS BEING THROWN AWAY (v2.15.2)

1. THE REASONING. llama-server runs --reasoning-format auto by default, which
   EXTRACTS the thinking out of message.content and returns it separately as
   `reasoning_content`. Ollama does the same thing under the name `thinking`.
   model_manager read only `content` in both loops, so on any reasoning model
   the trace was generated, parsed by the server, streamed to us, and dropped.
   The audit data Todd wanted to keep was being discarded at the last hop.

2. THE TOKEN COUNTS. Ollama's final chunk carries prompt_eval_count /
   eval_count. llama-server 8639 reports the same thing as `timings`
   (prompt_n / predicted_n) -- NOT as an OpenAI `usage` object requested via
   stream_options.include_usage, which that build does not implement at all
   (both strings are absent from the binary). Both shapes are read, because
   which one arrives depends on the build. These are the server's OWN
   tokenizer counts, and they are what restores CRAIID's context-fill signal
   now that llamacpp:kv_cache_usage_ratio is gone from llama.cpp entirely.

3. THE WHOLE REPLY, sometimes. A reasoning model can spend its entire budget
   inside the thinking block and emit zero content tokens. Both loops treated
   that as "nothing to yield" and returned in silence -- message sent, no
   answer, instantly the user's turn again. Four re-prompts for one news
   briefing on 2026-08-17. Tokens HAD arrived; they all went to the reasoning
   channel.

    python test_reasoning_capture.py
"""
import asyncio
import io
import json
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


MM = io.open(os.path.join(_HERE, "model_manager.py"), encoding="utf-8").read()

# Pull the two standalone helpers out of the module rather than importing the
# whole ModelManager (which wants config, httpx clients and a routing table).
_ns = {"List": list, "Dict": dict}
for _name in ("_turn_stats", "_no_answer_notice"):
    _src = MM[MM.index(f"def {_name}("):]
    _src = _src[:re.search(r"\n\ndef |\n\nclass ", _src).start()]
    exec(compile(_src, f"<{_name}>", "exec"), _ns)
turn_stats = _ns["_turn_stats"]
no_answer = _ns["_no_answer_notice"]


# =============================================================================
print("=== 1. The side-channel is caller-owned, not shared ===")
# =============================================================================
mine, yours = {}, {}
ok("a caller's own dict is handed back", turn_stats({"_turn_stats": mine}) is mine)
ok("two callers get two different dicts",
   turn_stats({"_turn_stats": mine}) is not turn_stats({"_turn_stats": yours}))
ok("absent means None, not a shared default", turn_stats({}) is None)
ok("a non-dict is refused rather than crashed on",
   turn_stats({"_turn_stats": "nope"}) is None)
# The whole point: no module- or instance-level slot that two chats could share.
ok("model_manager does NOT keep stats on the manager or the module",
   not re.search(r"^\s*(self|ModelManager)\.\w*turn_stats", MM, flags=re.M),
   "a shared slot is what broke the parallel sub-agents this same release")


# =============================================================================
print("\n=== 2. Ollama: thinking, counts, and no silent turns ===")
# =============================================================================
def ollama_stream(chunks):
    """Replay Ollama's newline-delimited JSON exactly as aiter_lines yields it."""
    return [json.dumps(c) for c in chunks]


def run_ollama(chunks, stats):
    """The Ollama loop's logic, lifted verbatim from _gen_ollama."""
    out, reasoning, content_seen = [], [], False
    effective_ctx, tier_label, base_url = 32768, "Oracle", "http://x"
    for line in ollama_stream(chunks):
        chunk = json.loads(line)
        _msg = chunk.get("message") or {}
        _think = _msg.get("thinking")
        if _think:
            reasoning.append(_think)
        content = _msg.get("content", "")
        if content:
            content_seen = True
            out.append(content)
        if chunk.get("done"):
            if stats is not None:
                stats["prompt_tokens"] = chunk.get("prompt_eval_count")
                stats["completion_tokens"] = chunk.get("eval_count")
                stats["n_ctx"] = effective_ctx
                stats["backend"] = "ollama"
                stats["tier"] = tier_label
                stats["base_url"] = base_url
                if reasoning:
                    stats["reasoning"] = "".join(reasoning)
            if not content_seen and reasoning:
                out.append(no_answer(tier_label, reasoning))
            break
    return out


st = {}
body = run_ollama([
    {"message": {"thinking": "Let me check "}},
    {"message": {"thinking": "the date."}},
    {"message": {"content": "It is "}},
    {"message": {"content": "Wednesday."}},
    {"done": True, "prompt_eval_count": 812, "eval_count": 44},
], st)
ok("the answer streams normally", "".join(body) == "It is Wednesday.", body)
ok("the reasoning is captured", st.get("reasoning") == "Let me check the date.",
   st.get("reasoning"))
ok("prompt tokens are captured", st.get("prompt_tokens") == 812, st)
ok("completion tokens are captured", st.get("completion_tokens") == 44, st)
ok("the backend is identified", st.get("backend") == "ollama")
ok("...and the tier, so n_ctx can be resolved later",
   st.get("tier") == "Oracle" and st.get("base_url") == "http://x")

# Thought its whole budget away: the failure that produced four blank turns.
st2 = {}
body2 = run_ollama([
    {"message": {"thinking": "I should consider every angle, first "}},
    {"message": {"thinking": "the sources, then the dates, then..."}},
    {"done": True, "prompt_eval_count": 900, "eval_count": 4096},
], st2)
ok("a thought-only turn is NOT silent", len(body2) == 1 and body2[0], body2)
ok("...it says what happened", "no answer" in body2[0], body2)
ok("...and what to change", "max_tokens" in body2[0], body2)
ok("...and the reasoning is still kept", bool(st2.get("reasoning")))
ok("...with the counts that prove it", st2.get("completion_tokens") == 4096)


# =============================================================================
print("\n=== 3. llama-server: usage arrives AFTER finish_reason ===")
# =============================================================================
def run_llama(chunks, stats):
    """The SSE loop's logic, lifted verbatim from _gen_llama_server."""
    out, reasoning, yielded, finished = [], [], False, False
    tier_label, base_url = "Toga", "http://y"
    for chunk in chunks:
        if stats is not None:
            _usage = chunk.get("usage")
            if isinstance(_usage, dict):
                stats["prompt_tokens"] = _usage.get("prompt_tokens")
                stats["completion_tokens"] = _usage.get("completion_tokens")
            _tm = chunk.get("timings")
            if isinstance(_tm, dict):
                if _tm.get("prompt_n") is not None:
                    stats["prompt_tokens"] = _tm.get("prompt_n")
                if _tm.get("predicted_n") is not None:
                    stats["completion_tokens"] = _tm.get("predicted_n")
            if _usage or chunk.get("timings"):
                stats["backend"] = "llama-server"
                stats["tier"] = tier_label
                stats["base_url"] = base_url
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}
        _rc = delta.get("reasoning_content")
        if _rc:
            reasoning.append(_rc)
        content = delta.get("content") or delta.get("text") \
            or choice.get("text") or ""
        if content:
            yielded = True
            out.append(content)
        if choice.get("finish_reason") is not None:
            finished = True
            continue
    if stats is not None and reasoning:
        stats["reasoning"] = "".join(reasoning)
    if not yielded and reasoning:
        out.append(no_answer(tier_label, reasoning))
    return out, finished


st3 = {}
body3, fin = run_llama([
    {"choices": [{"delta": {"reasoning_content": "The user asked "}}]},
    {"choices": [{"delta": {"reasoning_content": "for the date."}}]},
    {"choices": [{"delta": {"content": "Wednesday."}}]},
    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    {"choices": [], "usage": {"prompt_tokens": 1204, "completion_tokens": 31}},
], st3)
ok("the answer streams normally", "".join(body3) == "Wednesday.", body3)
ok("reasoning_content is captured",
   st3.get("reasoning") == "The user asked for the date.", st3.get("reasoning"))
ok("the usage chunk is read even though its choices list is EMPTY",
   st3.get("prompt_tokens") == 1204 and st3.get("completion_tokens") == 31, st3)
ok("...which requires not breaking on finish_reason", fin is True)

# Build 8639 reports counts as `timings`, not `usage`. Same outcome required.
st_t = {}
run_llama([
    {"choices": [{"delta": {"content": "hi"}}]},
    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    {"choices": [], "timings": {"prompt_n": 640, "predicted_n": 12}},
], st_t)
ok("timings.prompt_n is accepted where usage is absent",
   st_t.get("prompt_tokens") == 640, st_t)
ok("timings.predicted_n likewise", st_t.get("completion_tokens") == 12, st_t)
ok("...and the backend is still identified",
   st_t.get("backend") == "llama-server", st_t)

# The exact shape of Todd's blank turns on the llama-server tiers.
st4 = {}
body4, _ = run_llama([
    {"choices": [{"delta": {"reasoning_content": "Let me think about "}}]},
    {"choices": [{"delta": {"reasoning_content": "what I should think about."}}]},
    {"choices": [{"delta": {}, "finish_reason": "length"}]},
    {"choices": [], "usage": {"prompt_tokens": 800, "completion_tokens": 4096}},
], st4)
ok("a thought-only turn is NOT silent here either", len(body4) == 1 and body4[0])
ok("...and the counts still land", st4.get("completion_tokens") == 4096)


# =============================================================================
print("\n=== 4. Wired into the real file, not just this test ===")
# =============================================================================
# llama-server 8639 does NOT implement stream_options.include_usage -- a string
# search of the binary came back ABSENT for both `include_usage` and
# `stream_options`. Setting it would have been accepted, ignored, and this
# feature would have silently done nothing on the llama-server tiers while
# looking correct in the diff. `timings` is what that build actually provides.
ok("no unsupported stream_options flag is sent",
   '"stream_options"' not in re.sub(r"^\s*#.*$", "", MM, flags=re.M))
ok("...and timings is read instead", 'chunk.get("timings")' in MM)
ok("...alongside usage, for builds that do send it", 'chunk.get("usage")' in MM)
ok("llama-server reads reasoning_content", 'delta.get("reasoning_content")' in MM)
ok("ollama reads message.thinking", '_msg.get("thinking")' in MM)
ok("ollama keeps prompt_eval_count", 'chunk.get(\n                                    "prompt_eval_count")' in MM
   or '"prompt_eval_count"' in MM)
ok("finish_reason no longer breaks before usage",
   "Do NOT break: the usage chunk is emitted AFTER" in MM)
_after_finish = MM[MM.index("_finished = True"):][:200]
ok("...it continues instead", "continue" in _after_finish, _after_finish[:80])
ok("both paths can emit the no-answer notice",
   MM.count("_no_answer_notice(") >= 3, MM.count("_no_answer_notice("))


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
