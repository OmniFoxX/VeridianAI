#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_answer_guarantee.py -- a turn that thinks must still produce an answer.

THE FAILURE, AND THE THREE THINGS THAT WERE NOT ENOUGH

A reasoning model emits thinking on a channel separate from its reply. Spend
the whole generation budget there and the turn ends with zero answer tokens:
message sent, no reply, instantly the user's turn again. Four re-prompts in a
row for one news briefing, 2026-08-17.

  1. Capture (v2.15.2) made the trace visible instead of dropped.
  2. _no_answer_notice made the failure LEGIBLE -- the user is told what
     happened rather than ghosted.
  3. --reasoning-budget / think levels made it RARER.

None of those produce an answer, which is the thing that was actually asked
for. This is the fourth piece: when a turn is about to end with reasoning and
no reply, ask once more with thinking suppressed and an explicit instruction to
answer.

MODELLED ON AIQNudge, MINUS THE SIGNATURE

AIQNudge already solves "inject a directive mid-run and show the user it
landed", and reusing a proven shape beats inventing a second one. Its HMAC is
deliberately NOT copied: that exists because a nudge arrives from outside the
process as a file any local program could drop. This directive is composed in
model_manager from a module constant. There is no trust boundary, and signing
our own string would imply a guarantee it does not provide.

WHAT IS AND IS NOT PROVEN

Ollama: think=False is the lever, and it is verified -- laguna-xs went from 383
chars of thinking to 0, eval 101 to 9, and every model accepts the value.

llama-server: the directive is the load-bearing part. reasoning_budget=0 is
llama.cpp's documented per-request control and this build accepts it (200), but
no thinking model was loaded on the Toga tier when it was probed, so its effect
is unproven. It is sent because it is harmless and documented, not claimed as
the mechanism. The tests below say so rather than implying otherwise.

    python test_answer_guarantee.py
"""
import ast
import io
import json
import os
import sys
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


MM = io.open(os.path.join(_HERE, "model_manager.py"), encoding="utf-8").read()


def _method_src(name):
    t = ast.parse(MM)
    for n in ast.walk(t):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and n.name == name:
            return "\n".join(MM.splitlines()[n.lineno - 1:n.end_lineno])
    return ""


_OLL = _method_src("_gen_ollama")
_LLM = _method_src("_gen_llama_server")


# =============================================================================
print("=== 1. The directive ===")
# =============================================================================
ok("a directive constant exists", "_ANSWER_NOW_DIRECTIVE" in MM)
_d = MM[MM.index("_ANSWER_NOW_DIRECTIVE = ("):]
_d = _d[:_d.index("\n)") + 2]
ok("it tells the model to stop reasoning",
   "not reason" in _d.lower() or "do not reason" in _d.lower())
ok("...and to answer NOW", "answer" in _d.lower())
ok("...and gives it an honest way out when it is unsure",
   "unsure" in _d.lower(),
   "without this, a model with nothing to say is pushed to invent something")
ok("it is a constant, not built from user text",
   "format(" not in _d and "%" not in _d and "f\"" not in _d,
   "a directive assembled from turn content would be an injection surface")

# The HMAC decision, recorded so it is a decision and not an oversight.
ok("the AIQNudge comparison is documented", "AIQNudge" in MM)
ok("...including WHY the HMAC is not copied",
   "no trust boundary" in MM.lower(),
   "reusing a security control without its threat model is cargo cult; "
   "skipping one silently is worse")


# =============================================================================
print("\n=== 2. Ollama path ===")
# =============================================================================
ok("_gen_ollama was found", bool(_OLL))
ok("the guarantee fires on reasoning-with-no-content",
   "if not _content_seen and _reasoning_parts:" in _OLL)
ok("it is latched to fire ONCE", '_answered_retry["done"]' in _OLL)
ok("...and the latch is set BEFORE the retry",
   _OLL.index('_answered_retry["done"] = True')
   < _OLL.index("async for _tok in _attempt(attempt_idx):\n"
                "                                        yield _tok")
   if "async for _tok in _attempt(attempt_idx):" in _OLL else False,
   "setting it after would allow unbounded recursion")
ok("the retry suppresses thinking", 'payload["think"] = False' in _OLL)
ok("the directive is appended as a USER turn, not a system one",
   '"role": "user",\n                                            "content": '
   '_ANSWER_NOW_DIRECTIVE,' in _OLL
   or ('"role": "user"' in _OLL and "_ANSWER_NOW_DIRECTIVE" in _OLL),
   "Ollama rejects any system message that is not first -- that is the whole "
   "reason _ollama_safe_messages exists, and a system directive here would "
   "trade a no-answer turn for a 500")
ok("system is NOT used for the directive in the Ollama path",
   '"role": "system",\n                                            "content": '
   '_ANSWER_NOW_DIRECTIVE' not in _OLL)
# Checked on the AST. The first version matched an exact-whitespace string and
# failed on a line the formatter had wrapped -- the same "assert on text, not
# on structure" mistake this release has now made four times. Wrapping is not
# a behaviour change; the parser does not care where the newline went.
def _copies_messages(fn_name):
    """True if the function assigns payload["messages"] from a list(...) copy
    and never mutates the caller's list in place."""
    tree = ast.parse(MM)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == fn_name), None)
    if fn is None:
        return False, "function not found"
    # In-place mutation of the shared array would corrupt the caller's view.
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "append" \
                and isinstance(node.func.value, ast.Subscript):
            return False, "found an in-place payload[...].append"
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Subscript) and isinstance(
                        getattr(tgt.slice, "value", None), str) \
                        and tgt.slice.value == "messages":
                    for sub in ast.walk(node.value):
                        if isinstance(sub, ast.Call) and isinstance(
                                sub.func, ast.Name) and sub.func.id == "list":
                            return True, "assigned from list(...)"
                    return False, "assigned, but not from a list() copy"
    return False, "no payload['messages'] assignment found"


_copy_ok, _copy_why = _copies_messages("_gen_ollama")
ok("the messages list is COPIED, not mutated in place", _copy_ok, _copy_why)
ok("the retry is logged", "[ANSWER GUARANTEE]" in _OLL)
ok("the notice still fires if the retry also produces nothing",
   _OLL.index("_no_answer_notice") > _OLL.index('_answered_retry["done"]'),
   "the user must never end up with silence")


# =============================================================================
print("\n=== 3. llama-server path ===")
# =============================================================================
ok("_gen_llama_server was found", bool(_LLM))
ok("it has its own latch", '_answered_retry = {"done": False}' in _LLM)
ok("the guarantee fires there too", "[ANSWER GUARANTEE]" in _LLM)
ok("it sends the documented per-request control",
   'payload["reasoning_budget"] = 0' in _LLM)
ok("it appends the same directive", "_ANSWER_NOW_DIRECTIVE" in _LLM)
ok("the notice remains the final fallback", "_no_answer_notice" in _LLM)
ok("the unproven-effect caveat is recorded honestly",
   "could not be proven" in _LLM or "unproven" in _LLM,
   "reasoning_budget=0 is accepted by this build but was never observed "
   "acting on a thinking model -- the comment must not imply otherwise")

_copy_ok2, _copy_why2 = _copies_messages("_gen_llama_server")
ok("the llama path copies the messages list too", _copy_ok2, _copy_why2)

ok("both paths latch independently",
   MM.count('_answered_retry = {"done": False}') == 2)
ok("both paths log the retry", MM.count("[ANSWER GUARANTEE]") == 2)


# =============================================================================
print("\n=== 4. Live: the retry really does produce an answer ===")
# =============================================================================
# The claim is behavioural, so text checks cannot settle it. Reproduce the
# exact shape against the live server: a thinking model given a budget so small
# it cannot finish thinking, then the same request with thinking off plus the
# directive.
def _chat(model, think, extra_msgs=(), predict=200, timeout=180):
    msgs = [{"role": "user",
             "content": "Think carefully about why the sky is blue, then "
                        "answer in one sentence."}]
    msgs += list(extra_msgs)
    body = {"model": model, "messages": msgs, "stream": True,
            "options": {"num_predict": predict}}
    if think is not None:
        body["think"] = think
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    th, co = [], []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            s = raw.decode("utf-8", "replace").strip()
            if not s:
                continue
            m = (json.loads(s).get("message") or {})
            if m.get("thinking"):
                th.append(m["thinking"])
            if m.get("content"):
                co.append(m["content"])
    return "".join(th), "".join(co)


_up = False
try:
    with urllib.request.urlopen(
            "http://127.0.0.1:11434/api/version", timeout=5):
        _up = True
except Exception:
    pass

_MODEL = "laguna-xs-2.1:Q4_K_M"
if not _up:
    print("  SKIP  Ollama not reachable; the wiring above is still checked")
else:
    try:
        # Starve it: enough tokens to think, not enough to finish and answer.
        _th, _co = _chat(_MODEL, True, predict=60)
        _starved = (len(_co.strip()) == 0 and len(_th) > 0)
        ok("a starved thinking turn really can end with NO answer", _starved,
           "thinking=%d content=%d -- if this did not reproduce, the model "
           "answered anyway and the live check below is inconclusive"
           % (len(_th), len(_co)))

        # Now exactly what the guarantee does: thinking off + directive.
        _d2 = MM[MM.index('_ANSWER_NOW_DIRECTIVE = ('):]
        _d2 = _d2[:_d2.index("\n)")]
        _directive = " ".join(
            p.strip().strip('"') for p in _d2.split("\n")[1:] if p.strip())
        _th2, _co2 = _chat(_MODEL, False,
                           extra_msgs=[{"role": "user",
                                        "content": _directive}],
                           predict=200)
        ok("the retry shape produces an ANSWER", len(_co2.strip()) > 0,
           "thinking=%d content=%d" % (len(_th2), len(_co2)))
        ok("...with thinking actually suppressed", len(_th2) == 0,
           "think=False left %d chars of thinking" % len(_th2))
        if _co2.strip():
            print("        answer: %r" % _co2.strip()[:90])
    except Exception as _e:
        ok("live guarantee check ran", False,
           "%s: %s" % (type(_e).__name__, _e))


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
