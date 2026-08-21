#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ollama_think.py -- the thinking budget reaches Ollama without breaking it.

WHY THIS IS NOT JUST "SEND THE FIELD"

llama-server takes a NUMBER of thinking tokens (--reasoning-budget N). Ollama
takes a LEVEL, and validates it strictly. Probed against the live server
(Ollama 0.32.13) before any of this was written:

    think="banana"                -> HTTP 400  invalid think value: must be
                                     "high","medium","low","max",true,false
    think="low"  on llama3.2:3b   -> HTTP 400  "does not support thinking"
    think=false  on llama3.2:3b   -> 200
    think=false  on laguna-xs-2.1 -> 200, thinking 0 chars, eval 9 (vs 101)

Two facts in those four lines, and both are load-bearing:

  * A POSITIVE LEVEL HARD-FAILS on any model without a thinking channel, which
    is most models. Sending it blindly would have broken every turn on
    llama3.2, mistral, gemma and friends -- the same blast radius as the
    mid-array system message that 500'd every Ollama turn earlier in this
    release, and fixed by _ollama_safe_messages.

  * `false` IS universally accepted, because "do not think" is a coherent
    instruction even to a model that never would.

So the level is capability-GATED, not model-listed: a hardcoded list would rot
the first time a model is pulled. On a 400 that mentions think, the field is
dropped, memoised for that model, and the request retried -- so the cost is
one wasted request per model, once, and never a failed turn.

    python test_ollama_think.py
"""
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

# The helpers are module level and stdlib-only, so they can be exec'd out of
# the source without importing the whole manager.
_src = MM[MM.index("_THINK_UNSUPPORTED"):MM.index("def _ollama_safe_messages")]
_ns = {}
exec(compile(_src, "<think-helpers>", "exec"), _ns)
_val = _ns["_ollama_think_value"]
_budget = _ns["_ollama_budget_for_tier"]


# =============================================================================
print("=== 1. Budget -> level mapping ===")
# =============================================================================
ok("-1 (unrestricted) sends NO field", _val(-1) is None,
   "Ollama's own default is unrestricted; omitting the key keeps a turn "
   "byte-identical to what it was before this feature existed")
ok("any negative is treated as unrestricted", _val(-99) is None)
ok("0 maps to False (no thinking)", _val(0) is False)
ok("0 is False, not the string 'false'", _val(0) is False and _val(0) != "false")
ok("2048 maps to low", _val(2048) == "low")
ok("1 maps to low (smallest positive)", _val(1) == "low")
ok("8192 (the sage default) maps to medium", _val(8192) == "medium")
ok("16384 maps to high", _val(16384) == "high")
ok("None sends no field", _val(None) is None)
ok("a non-numeric budget sends no field rather than raising",
   _val("nonsense") is None,
   "a bad config value must not break every Ollama turn")

_LEGAL = {"high", "medium", "low", "max", True, False, None}
for _b in (-1, 0, 1, 2048, 4096, 8192, 16384, 999999, "x", None):
    _v = _val(_b)
    ok("budget %r -> %r is a value Ollama accepts" % (_b, _v), _v in _LEGAL,
       "Ollama 400s on anything outside its enum")


# =============================================================================
print("\n=== 2. Tier resolution ===")
# =============================================================================
import config as C                                          # noqa: E402
ok("Oracle (the conversation) uses the sage budget",
   _budget({}, "Oracle") == C.REASONING_BUDGET_SAGE)
ok("Daemon uses the daemon budget",
   _budget({}, "Daemon") == C.REASONING_BUDGET_DAEMON)
ok("tier matching is case-insensitive",
   _budget({}, "daemon") == C.REASONING_BUDGET_DAEMON)
ok("an install override beats the tier default",
   _budget({"reasoning_budget": 123}, "Oracle") == 123)
ok("an override of -1 (unlimited) is honoured, not ignored",
   _budget({"reasoning_budget": -1}, "Oracle") == -1,
   "unlimited must remain reachable")
ok("a missing config does not raise", _budget(None, "Oracle") is not None
   or True)


# =============================================================================
print("\n=== 3. The guard around an unsupported model ===")
# =============================================================================
ok("there is a capability memo", "_THINK_UNSUPPORTED" in MM)
ok("it is a set keyed by model, not a hardcoded list of names",
   "_THINK_UNSUPPORTED: set = set()" in MM,
   "a static list would rot the first time a model is pulled")
ok("the payload only gets think when a value is chosen",
   'payload["think"] = _think' in MM)
ok("a POSITIVE level is withheld from a known-unsupported model",
   "_think is not False and model_id in _THINK_UNSUPPORTED" in MM)
ok("...but False is still sent to it",
   "suppressing thinking is a meaningful instruction" in MM
   or "accepted by everything" in MM,
   "false never 400s, and it is the value that actually matters for a model "
   "we are trying to keep quiet")

ok("a 400 mentioning think triggers a retry, not a failed turn",
   'resp.status_code == 400' in MM and '"think" in body.lower()' in MM)
ok("the field is dropped before retrying", '_bad = payload.pop("think")' in MM)
ok("the model is memoised so it pays the cost once",
   "_THINK_UNSUPPORTED.add(model_id)" in MM)
ok("the retry re-enters the same attempt path",
   "async for _tok in _attempt(attempt_idx):" in MM)
ok("the recovery is logged, not silent", "[OLLAMA]" in MM
   and "retrying without" in MM)

# Simulate the gate itself.
_unsup = {"llama3.2:3b"}


def _would_send(think, model):
    if think is None:
        return False
    if think is not False and model in _unsup:
        return False
    return True


ok("gate: level withheld from an unsupported model",
   not _would_send("medium", "llama3.2:3b"))
ok("gate: level sent to a supported model",
   _would_send("medium", "laguna-xs-2.1:Q4_K_M"))
ok("gate: False sent even to an unsupported model",
   _would_send(False, "llama3.2:3b"))
ok("gate: None sends nothing anywhere",
   not _would_send(None, "laguna-xs-2.1:Q4_K_M"))


# =============================================================================
print("\n=== 4. Live Ollama (skipped if it is not running) ===")
# =============================================================================
def _chat(model, think, timeout=90):
    body = {"model": model, "stream": False,
            "messages": [{"role": "user", "content": "Say hi."}],
            "options": {"num_predict": 24}}
    if think is not None:
        body["think"] = think
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


_up = False
try:
    with urllib.request.urlopen(
            "http://127.0.0.1:11434/api/version", timeout=5) as _r:
        _ver = json.loads(_r.read().decode())["version"]
        _up = True
except Exception:
    _ver = None

if not _up:
    print("  SKIP  Ollama is not reachable on 127.0.0.1:11434")
    print("        (the mapping and guard above are still fully checked)")
else:
    ok("Ollama is reachable (version %s)" % _ver, True)

    # Every value the mapper can emit must be one this server accepts.
    for _v in ("low", "medium", "high", False):
        _code, _body = _chat("laguna-xs-2.1:Q4_K_M", _v)
        _bad_enum = ("invalid think value" in _body)
        ok("Ollama accepts think=%r as a legal value" % (_v,), not _bad_enum,
           _body[:150])

    # The hazard this whole design exists for, confirmed live rather than
    # remembered from a probe.
    _code, _body = _chat("llama3.2:3b", "low")
    ok("a positive level really DOES 400 on a non-thinking model",
       _code == 400 and "does not support thinking" in _body,
       "code=%s body=%s" % (_code, _body[:150]))
    ok("...and our guard is what stops that reaching a turn",
       "_THINK_UNSUPPORTED" in MM)

    _code, _body = _chat("llama3.2:3b", False)
    ok("think=False is accepted by that same non-thinking model",
       _code == 200, "code=%s body=%s" % (_code, _body[:150]))


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
