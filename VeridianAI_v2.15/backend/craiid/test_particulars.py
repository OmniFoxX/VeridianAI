#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_particulars.py -- gate for backend/craiid/particulars.py

    python backend/craiid/test_particulars.py

Test 1 is the incident that caused this module to exist. A CRAIID handoff
recovered a research report and reconstructed its citations from parametric
memory: "Mishra et al." for Hajizada et al., 37.3 ms for 23.2 ms, 333 mJ for
281 mJ. Every error flattered the story, and none of them was detectable
without going back to the source.

That test asserts BOTH directions -- the real values present AND the invented
ones absent. Asserting only presence would pass on a module that dutifully
preserved the truth and then let a fabrication through beside it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import particulars as P  # noqa: E402

_passed = 0
_failed = 0


def check(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}   {detail}")


def has(ps, needle):
    n = needle.replace(" ", "").lower()
    return any(n in p["text"].replace(" ", "").lower() for p in ps)


# ---------------------------------------------------------------------------
print("\n=== 1. THE INCIDENT (arXiv:2511.01553) ===")
PAPER = """Online Continual Learning on Intel Loihi 2 via a Co-designed Spiking
Neural Network. Elvin Hajizada, Danielle Rager, Timothy Shea, Leobardo
Campos-Macias, Andreas Wild and Mike Davies. arXiv:2511.01553, November 2025.
CLP-SNN delivers transformative efficiency gains: 70x faster (0.33ms vs
23.2ms), and 5,600x more energy efficient (0.05mJ vs 281mJ) than the best
alternative OCL on edge GPU. See doi:10.1038/s41586-025-65197-x for related
associative memory results."""

ps = P.extract(PAPER)
for need in ("arXiv:2511.01553", "0.33ms", "23.2ms", "0.05mJ", "281mJ",
             "10.1038/s41586-025-65197-x"):
    check(f"preserved verbatim: {need}", has(ps, need))
check("author block includes Hajizada",
      any("Hajizada" in p["text"] for p in ps if p["kind"] == "author_block"))

# The other half: values that were INVENTED must not appear from nowhere.
for wrong in ("37.3", "333mJ", "Mishra", "113x", "6,600"):
    check(f"absent (was fabricated): {wrong}", not has(ps, wrong))

# ---------------------------------------------------------------------------
print("\n=== 2. THE ITINERARY (the conversational case) ===")
ITIN = """Day 3, Wednesday: check out of the Marriott at 11:00 AM. The shuttle
leaves from 1420 Bayshore Boulevard, Suite 300 at 11:45 am sharp. Call the
concierge on +1 415-555-0142 if the driver is late. Flight UA 2287 departs
2026-09-14 at 3:25 PM. Dinner is $185 total. Then a long stretch of narrative
about how lovely the harbour looks in the evening, which is pleasant and
entirely reconstructible, and continues for some time without saying anything
that could not be paraphrased without loss."""

ps2 = P.extract(ITIN)
for need in ("11:00 AM", "1420 Bayshore Boulevard, Suite 300", "11:45 am",
             "415-555-0142", "UA 2287", "2026-09-14", "3:25 PM", "$185"):
    check(f"preserved verbatim: {need}", has(ps2, need))

# ---------------------------------------------------------------------------
print("\n=== 3. preserve(): particulars survive past the cut ===")
kept = P.preserve(ITIN, 120)
check("output respects the prose budget (plus salvage marker)",
      kept.startswith("Day 3, Wednesday") and "[kept:" in kept)
for need in ("415-555-0142", "UA 2287", "2026-09-14", "$185"):
    check(f"survived a 120-char budget: {need}",
          need.replace(" ", "") in kept.replace(" ", ""))
check("short text passes through untouched",
      P.preserve("just a short line", 500) == "just a short line")

# ---------------------------------------------------------------------------
print("\n=== 4. OVER-capture: ordinary prose must yield ~nothing ===")
PROSE = """I've been thinking about how the project is going and honestly it
feels like we turned a corner last week. The team seems more settled, the
arguments about scope have mostly died down, and people are enjoying the work
again. I went to Paris, London, and Rome over the break which helped clear my
head. There's still plenty to do but it feels achievable rather than
overwhelming, which is a nice change from how it felt before."""
ps4 = P.extract(PROSE)
check("no particulars in reconstructible prose", len(ps4) == 0,
      f"got {[(p['kind'], p['text']) for p in ps4]}")
check("a plain city list is not an author block",
      not any(p["kind"] == "author_block" for p in ps4))

# ---------------------------------------------------------------------------
print("\n=== 5. Bounded, and the END of a long document is still seen ===")
import time
BIG = ("Filler prose about nothing in particular. " * 12000)
BIG += " Contact +1 415-555-0142 on 2026-09-14 costing $185. See arXiv:2511.01553."
t0 = time.perf_counter()
ps5 = P.extract(BIG)
elapsed = time.perf_counter() - t0
check(f"{len(BIG):,} chars scanned in {elapsed*1000:.0f} ms (< 2s)", elapsed < 2.0)
check("tail-of-document particulars found (head-only truncation would miss)",
      has(ps5, "415-555-0142") and has(ps5, "arXiv:2511.01553"))

# ---------------------------------------------------------------------------
print("\n=== 6. Never raises (it runs inside a recovery path) ===")
for bad in (None, 123, "", "\x00\x00", ["list"], {"d": 1}, 3.14, b"bytes"):
    try:
        P.extract(bad)
        P.preserve(bad, 50)
        P.summarize_kinds(bad if isinstance(bad, list) else [])
        check(f"survives {type(bad).__name__}", True)
    except Exception as e:
        check(f"survives {type(bad).__name__}", False, f"{type(e).__name__}: {e}")

# ---------------------------------------------------------------------------
print("\n=== 7. Verbatim guarantee: spans are substrings of the input ===")
for src in (PAPER, ITIN):
    for p in P.extract(src):
        if p["text"] not in src:
            check("span is a literal substring of the source", False, p["text"])
            break
    else:
        continue
    break
else:
    check("every span is a literal substring (no normalisation)", True)

# ---------------------------------------------------------------------------
print(f"\n  {_passed} passed, {_failed} failed\n")
sys.exit(1 if _failed else 0)
