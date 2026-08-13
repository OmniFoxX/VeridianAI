#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_evidence_ledger.py -- gate for the CRAIID evidence ledger.

    python backend/craiid/test_evidence_ledger.py

Test 4 is the one that decides whether this feature works. It reconstructs the
2026-08-10 incident: a research handoff that kept the URLs it had typed and
lost the pages it had read, then reconstructed authors and figures from
parametric memory -- "Mishra et al." for Hajizada et al., 37.3 ms for 23.2 ms,
333 mJ for 281 mJ.

It asserts BOTH directions. Presence alone would pass on a system that
faithfully preserved the truth and let a fabrication through beside it.

Runs standalone: sage_engine and atrest are stubbed so the ledger can be
exercised without a live backend.
"""

import json
import shutil
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

_TMP = Path(tempfile.mkdtemp(prefix="vai_ledger_test_"))

_se = types.ModuleType("sage_engine")
_se._memory_file = lambda ns=None: (_TMP / (f"users/{ns}" if ns else "") / "chat_memory.json")
_se._archive_folder = lambda ns=None: (_TMP / (f"users/{ns}" if ns else "") / "archives")
sys.modules["sage_engine"] = _se

_at = types.ModuleType("atrest")
_at.dump_json_encrypted = lambda o: b"ENC" + json.dumps(o).encode()
_at.load_json_auto = lambda b: json.loads(b[3:].decode()) if b[:3] == b"ENC" else json.loads(b)
sys.modules["atrest"] = _at

_pkg = types.ModuleType("craiid"); _pkg.__path__ = []
sys.modules["craiid"] = _pkg
import particulars                                        # noqa: E402
import evidence_ledger as L                               # noqa: E402
sys.modules["craiid.particulars"] = particulars
sys.modules["craiid.evidence_ledger"] = L
_pkg.particulars = particulars
_pkg.evidence_ledger = L

_passed = _failed = 0


def check(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}   {detail}")


PAGE = """Online Continual Learning on Intel Loihi 2 via a Co-designed Spiking
Neural Network. Elvin Hajizada, Danielle Rager and Mike Davies.
arXiv:2511.01553, November 2025. CLP-SNN delivers 70x faster inference
(0.33ms vs 23.2ms) and is 5,600x more energy efficient (0.05mJ vs 281mJ) than
the strongest alternative on an edge GPU."""

# ---------------------------------------------------------------------------
print("\n=== 1. Evidence vs action classification ===")
for key, want in (("browse:https://arxiv.org/abs/2511.01553", True),
                  ("web_search:neuromorphic stdp", True),
                  ("read:C:\\Users\\me\\itinerary.txt", True),
                  ("save:report.md", False),
                  ("code:python", False),
                  ("memory_search", False)):
    check(f"{key.split(':')[0]:12} evidence={want}", L.is_evidence(key) == want)

# ---------------------------------------------------------------------------
print("\n=== 2. Record, persist, encrypt ===")
L.clear()
check("recorded", L.record("browse:https://arxiv.org/abs/2511.01553", PAGE))
st = L.stats()
check("one source, preserved", st["sources"] == 1 and st["preserved"] == 1, str(st))
_lp = _TMP / ".evidence_ledger.dat"
check("stored beside chat_memory.json", _lp.exists())
check("at-rest encrypted", _lp.read_bytes()[:3] == b"ENC")

print("\n=== 3. Stores particulars, not the page ===")
big = ("Filler prose about neuromorphic tradeoffs. " * 500) + PAGE
L.clear(); L.record("browse:https://arxiv.org/abs/2511.01553", big)
ratio = _lp.stat().st_size / len(big)
check(f"ledger is {ratio*100:.1f}% of source (<15%)", ratio < 0.15)

# ---------------------------------------------------------------------------
print("\n=== 4. THE DECIDING TEST: the incident across the boundary ===")
L.clear()
L.record("browse:https://arxiv.org/abs/2511.01553", PAGE)
h = L.for_handoff(conversation_text="as shown at https://arxiv.org/abs/2511.01553")
flat = json.dumps(h)
for need in ("Hajizada", "23.2ms", "281mJ", "0.33ms", "0.05mJ", "arXiv:2511.01553"):
    check(f"preserved: {need}", need in flat)
for wrong in ("Mishra", "37.3", "333mJ", "113x", "6,600"):
    check(f"never fabricated: {wrong}", wrong not in flat)
check("citation rule present", "Cite ONLY from this ledger" in L.CITATION_RULE)

# ---------------------------------------------------------------------------
print("\n=== 5. Gaps are recorded, never silently dropped ===")
L.record("browse:https://example.com/empty", "Prose with nothing citable in it.")
h = L.for_handoff()
gap = [s for s in h["sources"] if not s["preserved"]]
check("unextractable source persists as a gap", len(gap) == 1, str(h))
check("gap carries a reason", bool(gap and gap[0].get("reason")))
check("gap counted", h["gaps"] == 1)

# ---------------------------------------------------------------------------
print("\n=== 6. Budget holds, over-budget becomes a gap not a vanishing ===")
L.clear()
for i in range(40):
    L.record(f"browse:https://example.com/p{i}",
             f"Paper {i}. A. Alpha, B. Beta and C. Gamma. arXiv:25{i:02d}.0100{i}. "
             f"Measured {i}.5ms against {i*2}.1ms baseline.")
h = L.for_handoff(budget_chars=3000, per_source_chars=400, max_sources=8)
check("source cap respected", len(h["sources"]) <= 8, str(len(h["sources"])))
check("marked truncated", h["truncated"])
check("payload within budget+overhead", len(json.dumps(h)) < 6000, str(len(json.dumps(h))))

# ---------------------------------------------------------------------------
print("\n=== 7. Thread continuity across consecutive fatigue cycles ===")
L.clear()
L.record("browse:https://arxiv.org/abs/2511.01553", PAGE)
cycle1 = json.dumps(L.for_handoff())
L.record("browse:https://example.com/second", "D. Delta, E. Echo and F. Foxtrot. arXiv:2512.00002. 12.5ms result.")
cycle2 = json.dumps(L.for_handoff())
check("cycle 1 evidence still present in cycle 2", "Hajizada" in cycle2 and "23.2ms" in cycle2)
check("cycle 2 added its own", "2512.00002" in cycle2 and "2512.00002" not in cycle1)

# ---------------------------------------------------------------------------
print("\n=== 8. Lifecycle: archive -> clear -> restore, and ZDR ===")
check("archive snapshot written", L.archive_to("archive_2026-08-10_120000.json"))
check("clear empties the live ledger", L.clear() and L.stats()["sources"] == 0)
check("restore brings it back", L.restore_from("archive_2026-08-10_120000.json")
      and L.stats()["sources"] == 2)
check("ledger file removed by clear (ZDR)", L.clear() and not _lp.exists())

print("\n=== 9. Namespace isolation ===")
L.clear(); L.clear("alice")
L.record("browse:https://arxiv.org/abs/2511.01553", PAGE, ns="alice")
check("alice has evidence", L.stats("alice")["sources"] == 1)
check("owner sees none of alice's", L.stats()["sources"] == 0)
check("alice's file is inside her namespace",
      (_TMP / "users" / "alice" / ".evidence_ledger.dat").exists())

print("\n=== 10. Never raises / no plaintext fallback ===")
for bad in (None, 123, "", ["x"]):
    try:
        L.record(bad, bad); L.for_handoff(); L.stats()
        check(f"survives {type(bad).__name__}", True)
    except Exception as e:
        check(f"survives {type(bad).__name__}", False, f"{type(e).__name__}: {e}")

L.clear()
L.record("browse:https://arxiv.org/abs/2511.01553", PAGE)
_before = _lp.read_bytes()
_at.dump_json_encrypted = lambda o: (_ for _ in ()).throw(RuntimeError("no key"))
L.record("browse:https://example.com/x", "G. Golf, H. Hotel and I. India. arXiv:2513.00003.")
check("encryption unavailable -> nothing written, no plaintext leak",
      _lp.read_bytes() == _before)

# ---------------------------------------------------------------------------
shutil.rmtree(_TMP, ignore_errors=True)
print(f"\n  {_passed} passed, {_failed} failed\n")
sys.exit(1 if _failed else 0)
