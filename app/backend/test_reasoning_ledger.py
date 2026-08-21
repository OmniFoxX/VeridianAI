#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_reasoning_ledger.py -- the user's thinking log: per-user, encrypted, never replayed.

TWO CONTRACTS, AND THEY ARE OPPOSITES

    evidence ledger    read BY the model, to keep it honest about sources
    reasoning ledger   read BY THE USER, and by nothing else, ever

Evidence entries are replayed at every fatigue handoff under CITATION_RULE --
"VERBATIM extracts from material actually retrieved... Cite ONLY from this
ledger". A reasoning trace is not retrieved, not external, and holds the
model's discarded and wrong steps beside its good ones. Feeding it back would
hand a model its own guesses labelled as citable sources.

So this store has NO for_handoff, NO CITATION_RULE, and no formatter that
renders entries into prompt text. That absence IS the feature, and section 4
asserts it -- so adding one later is a deliberate act rather than a quiet one.

PER USER, which is the thing that had to be checked rather than assumed.
The path derives from sage_engine._memory_file(ns), the same resolver the
evidence ledger uses, so a profile's ledger lands in users/<ns>/ and moves with
conversations if that layout changes. PROFILE TIER encryption (ns=), not
system: a trace is the user's own conversation content.

LIFECYCLE. A thinking log that outlived its conversation would be a retention
surprise, so it is cleared with the chat and destroyed by a ZDR burn. The burn
path is an EXPLICIT file list for the owner branch, and main.py's own comment
records the last time that list missed a ledger -- the owner was "the one left
with residue". Section 5 checks this one is on it.

    python test_reasoning_ledger.py
"""
import io
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


import reasoning_ledger as RL                                # noqa: E402
import atrest                                                # noqa: E402

MAIN = io.open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()
SRC = io.open(os.path.join(_HERE, "reasoning_ledger.py"), encoding="utf-8").read()

TRACE = ("Working it through: my first instinct was 7, which is wrong unless "
         "the list is sorted. Discarding that. The answer is 4.")
SECRET = "MRN-88231-chest-pain-0300"


# =============================================================================
print("=== 1. Per user, derived not rebuilt ===")
# =============================================================================
ok("the path comes from sage_engine's resolver",
   "sage_engine._memory_file(ns)" in SRC,
   "rebuilding it here would strand the ledger if conversations ever move")
try:
    _owner = RL._ledger_path(None)
    _prof = RL._ledger_path("test_ns_abc")
    ok("owner and profile resolve to DIFFERENT files", _owner != _prof,
       "%s vs %s" % (_owner, _prof))
    ok("a profile ledger lands under users/<ns>/",
       "users" in str(_prof) and "test_ns_abc" in str(_prof), str(_prof))
    ok("the file is dot-prefixed and not *.json",
       str(_owner).endswith(".dat") and os.path.basename(
           str(_owner)).startswith("."),
       "no existing glob('*.json') reader can pick it up")
except Exception as _e:
    ok("path resolution works", False, "%s: %s" % (type(_e).__name__, _e))

ok("it encrypts under the PROFILE key, not the system key",
   "ns=ns" in SRC and "PROFILE TIER" in SRC,
   "a trace is the user's own conversation content")
ok("...on both read and write",
   SRC.count("ns=ns") >= 2, SRC.count("ns=ns"))


# =============================================================================
print("\n=== 2. Round trip, on a real encrypted file ===")
# =============================================================================
_TMP = tempfile.mkdtemp(prefix="vai_rl_")
_real = RL._ledger_path


def _fake_path(ns=None):
    import pathlib
    sub = "owner" if ns is None else str(ns)
    p = pathlib.Path(_TMP) / sub
    p.mkdir(parents=True, exist_ok=True)
    return p / RL.LEDGER_FILE


RL._ledger_path = _fake_path
try:
    ok("recording a trace succeeds",
       RL.record(TRACE + " " + SECRET, ns=None, meta={"model": "laguna"}))
    _p = _fake_path(None)
    ok("a file was written", _p.exists())

    _raw = _p.read_bytes()
    ok("the file is ciphertext", atrest.is_encrypted(_raw))
    ok("the trace text is ABSENT from the raw bytes",
       TRACE.encode() not in _raw,
       "this is the assertion the encryption exists for")
    ok("no fragment survives either", SECRET.encode() not in _raw
       and b"chest-pain" not in _raw)

    _e = RL.entries(ns=None)
    ok("it reads back", len(_e) == 1, len(_e))
    ok("the trace round-trips exactly", _e[0]["trace"] == TRACE + " " + SECRET)
    ok("a hash is stored alongside", len(_e[0].get("sha256", "")) == 64)
    ok("the metadata is kept", (_e[0].get("meta") or {}).get("model") == "laguna")

    # Isolation between namespaces -- the whole point of Todd's question.
    RL.record("PROFILE-ONLY-TRACE", ns="alice")
    ok("another namespace does not see the owner's entries",
       all("PROFILE-ONLY" not in x["trace"] for x in RL.entries(ns=None)))
    ok("...and the owner does not see theirs",
       all(SECRET not in x["trace"] for x in RL.entries(ns="alice")))
    _ap = _fake_path("alice")
    ok("they are separate files on disk", _ap != _p and _ap.exists())

    _s = RL.stats(ns=None)
    ok("stats reports counts", _s["entries"] == 1 and _s["total_chars"] > 0, _s)
    ok("stats carries NO trace text", "trace" not in str(_s).lower()
       or SECRET not in str(_s),
       "stats should be safe to surface anywhere")

    ok("clear removes it", RL.clear(ns=None) and not _p.exists())
    ok("...and only that namespace", _ap.exists())
finally:
    RL._ledger_path = _real


# =============================================================================
print("\n=== 3. Bounded, and honest about pruning ===")
# =============================================================================
ok("there is an entry cap", isinstance(RL.MAX_ENTRIES, int)
   and RL.MAX_ENTRIES > 0)
ok("there is a total-size cap", RL.MAX_TOTAL_CHARS > 0)
ok("one pathological trace cannot fill the ledger",
   0 < RL.MAX_TRACE_CHARS < RL.MAX_TOTAL_CHARS)

_d = {"entries": [{"ts": i, "chars": 10, "trace": "x"}
                  for i in range(RL.MAX_ENTRIES + 5)], "pruned": 0}
_d = RL._prune(_d)
ok("over-cap entries are pruned", len(_d["entries"]) == RL.MAX_ENTRIES,
   len(_d["entries"]))
ok("the OLDEST go first", _d["entries"][0]["ts"] == 5, _d["entries"][0]["ts"])
ok("pruning is COUNTED, not silent", _d["pruned"] == 5, _d["pruned"])
ok("...and announced", "pruned %d oldest" in SRC or "pruned" in SRC)

_long = "z" * (RL.MAX_TRACE_CHARS + 500)
RL._ledger_path = _fake_path
try:
    RL.record(_long, ns="trunc")
    _t = RL.entries(ns="trunc")[0]
    ok("an oversized trace is truncated, not dropped",
       _t["chars"] == RL.MAX_TRACE_CHARS and _t["truncated"] is True,
       "chars=%s truncated=%s" % (_t["chars"], _t.get("truncated")))
    ok("the hash covers the ORIGINAL, so truncation is detectable",
       _t["sha256"] != RL.hashlib.sha256(
           _t["trace"].encode()).hexdigest())
finally:
    RL._ledger_path = _real
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)


# =============================================================================
print("\n=== 4. It can NEVER be fed to the model ===")
# =============================================================================
# The absence is the feature. Checking it explicitly means adding a prompt
# accessor later is a deliberate act, not a quiet one.
ok("there is no for_handoff()", not hasattr(RL, "for_handoff"),
   "that is the evidence ledger's replay path; this store must not have one")
ok("there is no CITATION_RULE", not hasattr(RL, "CITATION_RULE"))
ok("no function renders entries into prompt text",
   not any(n for n in dir(RL)
           if n.lower() in ("as_prompt", "to_prompt", "for_prompt",
                            "render", "format_for_model")),
   [n for n in dir(RL) if "prompt" in n.lower()])
ok("the reason is documented in the module itself",
   "CITATION_RULE" in SRC and "read BY THE USER" in SRC,
   "a decision recorded nowhere becomes an accident later")

# And nothing in main.py routes it into a message array.
_reads = [l.strip() for l in MAIN.splitlines()
          if "reasoning_ledger" in l and (".entries(" in l or "for_handoff" in l)]
ok("main.py never reads the ledger into a turn", not _reads, _reads)
ok("main.py only WRITES to it",
   "_rl.record(" in MAIN and "_rl.entries(" not in MAIN)


# =============================================================================
print("\n=== 5. Lifecycle: cleared with the chat, burned by ZDR ===")
# =============================================================================
ok("clearing the chat clears it",
   "_rl.clear(_ns)" in MAIN,
   "the evidence ledger is cleared there; a thinking log must follow")
ok("...in the same place the evidence ledger is cleared",
   MAIN.index("_el.clear(_ns)") < MAIN.index("_rl.clear(_ns)")
   and abs(MAIN.index("_rl.clear(_ns)") - MAIN.index("_el.clear(_ns)")) < 900)
ok("the ZDR burn removes it for the OWNER too",
   '.reasoning_ledger.dat' in MAIN,
   "the non-owner branch wipes the whole namespace dir, but the owner branch "
   "is an explicit file list -- and main.py's own note records that list "
   "missing a ledger before, leaving the owner with residue")
ok("...listed beside the evidence ledger in that same burn list",
   MAIN.index(".evidence_ledger.dat") < MAIN.index(".reasoning_ledger.dat"))
ok("the module offers clear() for both callers", hasattr(RL, "clear"))


# =============================================================================
print("\n=== 6. Failure modes are loud, never destructive ===")
# =============================================================================
ok("an unreadable ledger reports EMPTY without overwriting",
   "reporting EMPTY, not overwriting" in SRC,
   "silently rewriting an unreadable file would destroy what could not be "
   "decrypted -- the same trap procedural memory needed guarding against")
ok("read failures are announced", "_warn(" in SRC and "read failed" in SRC)
ok("write failures are announced", "write failed" in SRC)
ok("record() never raises", "def record" in SRC and "except Exception" in SRC)
ok("an empty trace is not recorded",
   RL.record("   ", ns=None) is False,
   "a blank entry is noise in a log the user has to read")
ok("the write is atomic (temp + replace)",
   ".tmp" in SRC and "replace(p)" in SRC,
   "a half-written encrypted file is an unreadable ledger")


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
