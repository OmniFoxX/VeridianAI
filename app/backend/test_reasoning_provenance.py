#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_reasoning_provenance.py -- the trace is witnessed, and NOT called evidence.

TWO CLAIMS, AND THE SECOND IS THE IMPORTANT ONE.

1. A reasoning trace is committed to the memory chain as a HASH, so it can be
   proven unaltered later -- the same shape procedural memory already uses
   (procedural_memory._witness_to_chain), where the authoritative copy lives in
   its own encrypted store and the chain holds only a witness.

2. It is deliberately NOT registered with the CRAIID evidence ledger, and that
   is a decision rather than an omission.

WHY NOT THE LEDGER

The ledger is replayed to the model at every fatigue handoff under this rule:

    CITATION_RULE = ("SOURCES: the entries below are VERBATIM extracts from
    material actually retrieved earlier in this conversation. Cite ONLY from
    this ledger. ...")

It exists to stop fabrication. Its own docstring names the incident: a research
report whose URLs were right and whose authors and figures were invented --
"Mishra et al." for Hajizada et al., 37.3 ms for 23.2 ms.

A reasoning trace is not retrieved, not external, and unverified by
construction: it contains the model's discarded and wrong intermediate steps
alongside its good ones. Registering it would mean that at the next handoff the
model is handed its own earlier guesses, labelled as verbatim retrieved
material it may cite. That is not provenance. It is the exact failure the
ledger was built to prevent, wearing the ledger's authority.

EVIDENCE_KINDS agrees: retrieval only, and even tool ACTIONS are excluded
because they record "what it did, not what it learned". A trace is one step
further out again.

    python test_reasoning_provenance.py
"""
import io
import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "craiid"))

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


MAIN = io.open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()

TRACE = ("Let me work through this. First I thought the answer was 7, but "
         "that was wrong -- 7 only holds if the input is sorted. Discard it. "
         "The real answer is 4.")


# =============================================================================
print("=== 1. One hash definition, two callers ===")
# =============================================================================
ok("a canonical hash helper exists", "def _reasoning_hash" in MAIN)
ok("the witness uses it", "_h = _reasoning_hash(trace)" in MAIN)
ok("the verifier uses the SAME one",
   "_expect = _reasoning_hash(entry[\"reasoning\"])" in MAIN,
   "a witness and verifier that hash differently would flag healthy data, and "
   "a provenance check that cries wolf gets switched off")


# =============================================================================
print("\n=== 2. The chain gets a hash, never the text ===")
# =============================================================================
_w = MAIN[MAIN.index("def _witness_reasoning"):]
_w = _w[:_w.index("\ndef ", 5)]
ok("the committed content is the hash", 'content=f"reasoning:{_h}"' in _w)
ok("the trace itself is NOT the content",
   "content=trace" not in _w and "content=f\"reasoning:{trace}" not in _w,
   "the chain is append-only and never re-encrypted -- conversation text must "
   "not accumulate in it")
ok("the entry is tagged with its own role", 'role="reasoning"' in _w)
ok("the metadata carries the hash and size",
   '"reasoning_sha256"' in _w and '"reasoning_chars"' in _w)
ok("...and attribution, like procedural memory", '"owner_ns"' in _w)
ok("a failed witness never breaks the turn",
   "except Exception" in _w and "return None" in _w)


# =============================================================================
print("\n=== 3. NOT the evidence ledger ===")
# =============================================================================
try:
    import evidence_ledger as EL
    _have_el = True
except Exception:
    try:
        from craiid import evidence_ledger as EL   # type: ignore
        _have_el = True
    except Exception:
        _have_el = False

ok("the evidence ledger is importable", _have_el)
if _have_el:
    ok("'reasoning' is NOT an evidence kind",
       not EL.is_evidence("reasoning:abc123"),
       "EVIDENCE_KINDS is retrieval-only, and even tool actions are excluded")
    ok("recording a trace as evidence is refused by the ledger itself",
       EL.record("reasoning:abc123", TRACE) is False,
       "belt and braces: even a mistaken call site could not pollute it")
    ok("retrieval kinds are still accepted", EL.is_evidence("browse:http://x"))
    ok("the citation rule is what makes this dangerous",
       "Cite ONLY from this ledger" in EL.CITATION_RULE,
       "ledger entries are replayed to the model as citable retrieved material")

ok("main.py does not register reasoning with the ledger",
   "evidence_ledger.record" not in MAIN
   or "reasoning" not in MAIN[
       max(0, MAIN.find("evidence_ledger.record") - 300):
       MAIN.find("evidence_ledger.record") + 300]
   if "evidence_ledger.record" in MAIN else True)
ok("the reason is written down where the decision lives",
   "CITATION_RULE" in MAIN and "not retrieved" in MAIN,
   "a security decision recorded nowhere becomes an accident later")


# =============================================================================
print("\n=== 4. Verification tells apart 'unverifiable' and 'tampered' ===")
# =============================================================================
# v2.15.2: the slice starts at the WINDOW CONSTANT, not at the verifier.
#
# The chain walk moved out of verify_reasoning_provenance into
# _chain_has_reasoning so that the ledger reader could share one definition of
# it -- and it grew a third answer while it was there: "not found in the window
# I searched" is now distinct from "not in the chain". The walk is defined
# ABOVE the verifier, so a slice that began at the verifier stopped covering
# the very failure handling this section checks.
#
# Widened rather than deleted: these assertions are about the verifier GROUP,
# and the group is what has to keep the property.
_v = MAIN[MAIN.index("_CHAIN_SEARCH_WINDOW ="):]
_v = _v[:_v.index("\n_ALLOWED_MESSAGE_KEYS")]
ok("a message with no trace is reported plainly", "no reasoning trace" in _v)
ok("a trace with no witness is UNVERIFIABLE, not tampered",
   "Unverifiable, not " in _v and "tampered" in _v,
   "traces predating the feature would otherwise all read as tampering, and "
   "a check that cries wolf gets ignored")
ok("hash_matches starts as None, not False",
   '"hash_matches": None' in _v,
   "False would assert a negative finding before anything was checked")
# v2.16.1: asserted as a PROPERTY, not a sentence.
#
# This was '"chain walk failed" in _v' and went red when that exact wording
# changed -- and the wording changed for a good reason: CodeQL
# py/stack-trace-exposure found that the message was interpolating the raw
# exception, which reached the browser through /api/reasoning-ledger. The
# behaviour under test never changed; only the string did.
#
# What actually matters is unchanged and is what is checked now: the walk still
# HANDLES the failure (does not let it escape), still SAYS something about it
# (does not swallow it), and now routes the detail through _safe_detail so the
# reason reaches the server log instead of the client.
ok("a chain-walk failure is caught, not allowed to escape",
   "except Exception" in _v and "_walk_err" in _v)
ok("...and is reported rather than swallowed",
   "return None, (" in _v,
   "returning None with no message would make a broken chain look identical "
   "to an unwitnessed trace")
ok("...with the reason logged, not returned",
   "_safe_detail(_walk_err" in _v,
   "the exception text carries file paths; the caller gets a correlation ref "
   "and the server log gets the rest")


# =============================================================================
print("\n=== 5. Runtime: witness, verify, and detect a real tamper ===")
# =============================================================================
_TMP = tempfile.mkdtemp(prefix="vai_prov_")
try:
    from memory_logger_surprise import MemoryLogger
    _ml = MemoryLogger(storage_dir=_TMP, baseline_temp=0.5)

    import hashlib
    _h = hashlib.sha256(TRACE.encode("utf-8")).hexdigest()
    _chain_hash = _ml.log(content=f"reasoning:{_h}", temperature=0.0,
                          token_prob=None,
                          metadata={"reasoning_sha256": _h,
                                    "reasoning_chars": len(TRACE),
                                    "owner_ns": "(owner)"},
                          role="reasoning")
    ok("the witness write returns a chain hash", bool(_chain_hash), _chain_hash)

    _recent = _ml.get_recent(n=50)
    _entry = next((e for e in _recent
                   if str(e.get("content", "")) == f"reasoning:{_h}"), None)
    ok("the witness is findable in the chain", _entry is not None)
    if _entry:
        ok("it is tagged role=reasoning", _entry.get("role") == "reasoning",
           _entry.get("role"))

    # The load-bearing privacy assertion: the trace text is NOT in the chain.
    _all = json.dumps(_recent, default=str)
    ok("the trace text does NOT appear anywhere in the chain",
       "discarded and wrong" not in _all and TRACE not in _all
       and "The real answer is 4" not in _all,
       "the chain holds a witness, not the content")

    # A matching trace verifies.
    _found = any(str(e.get("content", "")) == f"reasoning:{_h}"
                 for e in _ml.get_recent(n=200))
    ok("an unaltered trace verifies against the chain", _found)

    # A TAMPERED trace must not.
    _tampered = TRACE.replace("The real answer is 4", "The real answer is 7")
    _h2 = hashlib.sha256(_tampered.encode("utf-8")).hexdigest()
    ok("the tampered text really does hash differently", _h2 != _h)
    _found2 = any(str(e.get("content", "")) == f"reasoning:{_h2}"
                  for e in _ml.get_recent(n=200))
    ok("a TAMPERED trace fails verification", not _found2,
       "if this passed, the witness would prove nothing at all")

    # And the chain itself still verifies -- witnessing must not corrupt it.
    try:
        _valid, _msg, _n = _ml.verify_chain()
        ok("the chain still verifies after the witness write", _valid,
           "%s (%s entries)" % (_msg, _n))
    except Exception as _e:
        ok("the chain still verifies after the witness write", False,
           "%s: %s" % (type(_e).__name__, _e))
except Exception as _e:
    ok("runtime provenance check ran", False,
       "%s: %s" % (type(_e).__name__, _e))
finally:
    shutil.rmtree(_TMP, ignore_errors=True)


# =============================================================================
print("\n=== 6. The stored field never reaches the model ===")
# =============================================================================
_ns = {}
_src = MAIN[MAIN.index("_ALLOWED_MESSAGE_KEYS"):]
_src = _src[:_src.index("\nasync def _watched_generate")]
exec(compile(_src, "<sanitizer>", "exec"), _ns)
_san = _ns["_sanitize_client_messages"]
_msg = [{"role": "assistant", "content": "4",
         "reasoning": TRACE, "reasoning_chain_hash": "deadbeef"}]
_clean = _san(_msg)
ok("reasoning_chain_hash is stripped on the way in",
   "reasoning_chain_hash" not in _clean[0],
   "a provenance field is bookkeeping, not dialogue")
ok("the trace is stripped too", "reasoning" not in _clean[0])
ok("the reply itself survives", _clean[0]["content"] == "4")


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
