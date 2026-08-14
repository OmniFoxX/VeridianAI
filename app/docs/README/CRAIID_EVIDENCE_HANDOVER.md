# CRAIID evidence ledger — what you need to do

Implementation complete, **82 tests passing in both trees**. This file is at the
STAGING root, so it is not packaged and does not affect the manifest.

---

## 1. Bump the version

This is a behaviour change to CRAIID, so a minor bump is right:

```
bump_version.bat        in EACH tree      2.13.18  ->  2.14.0
```

---

## 2. Re-run genmanifest — 7 hashed files changed per tree

```
python backend\build_integrity.py genmanifest
```

| File | Change |
|---|---|
| `backend/craiid/particulars.py` | **new** — the extractor |
| `backend/craiid/evidence_ledger.py` | **new** — the ledger |
| `backend/craiid/test_particulars.py` | **new** — 39 tests |
| `backend/craiid/test_evidence_ledger.py` | **new** — 43 tests |
| `backend/craiid/journalist.py` | particulars-preserving truncation, 280 → 900 |
| `backend/main.py` | capture, clear-chat hook, ZDR fix, 6 config knobs |
| `backend/sage_daemon.py` | ledger into the handoff, caps raised |
| `backend/sage_engine.py` | archive snapshot + restore |
| `backend/config_store.py` | 6 config knobs |

`docs/PLAN_craiid_evidence_ledger.md` also changed but is `.md` — not hashed.

---

## 3. Build and install

Portable: zip the tree. Store: `npm run build-store`, then install.

---

## 4. Verify on the machine (2 minutes)

Run the tests from the installed tree:

```
python backend\craiid\test_particulars.py         expect 39 passed, 0 failed
python backend\craiid\test_evidence_ledger.py     expect 43 passed, 0 failed
```

Then a live check — repeat the research run that exposed this, and after a
handoff fires look in the backend log for:

```
[CRAIID] evidence ledger: N preserved, M gap(s), T total source(s)
```

If that line is absent, the ledger was empty and the handoff behaved exactly as
it did before — nothing is broken, there was simply nothing to carry.

---

## 5. The thing to actually watch

**Does the bigger warm context re-trigger fatigue?**

The handoff now carries roughly 22 KB where it used to carry 3.4 KB. That is
still small against a 16–32 K context, but it has not been measured on a real
run on your hardware. If handoffs start chaining — one immediately triggering
the next — the budgets come down, not the design:

```
craiid_evidence_budget_chars      10000   ledger total
craiid_evidence_per_source_chars    700   per source
craiid_evidence_max_sources          12   sources kept
craiid_turn_chars                   900   per-turn prose cap
```

All six knobs are in `config.json` under the normal settings. Two full off
switches reproduce the old behaviour exactly:

```
craiid_evidence_enabled    = false      no ledger at all
craiid_particulars_enabled = false      no particulars in truncation
```

---

## 6. What changed behaviourally

- **Evidence survives handoffs.** Fetched pages and read files have their
  citation-bearing spans preserved **verbatim** across a fatigue boundary.
- **Conversational particulars survive truncation.** An itinerary keeps its
  addresses, phone numbers and times even when the prose around them is cut.
- **Gaps are declared.** A source that could not be extracted is carried as
  `preserved: false`, and the model is instructed it may cite the URL but not
  the contents. An explicit gap beats a silent one it will fill from memory.
- **The ledger belongs to the thread.** It survives every fatigue cycle in a
  conversation, is archived with it, restored when that archive is loaded,
  cleared on Clear Chat, and destroyed by ZDR burn.

---

## 7. Two findings worth knowing

**The ZDR burn had a hole.** A non-owner burn wipes the whole namespace
directory, so the ledger went with it. The **owner** burn uses an explicit file
list that did not include it — so the person most likely to have used research
tools was the one left with residue on disk. Fixed in the same change that
created the file.

**`sources` means two different things in this codebase.** `coordinator_signal`
emits a `sources` key that the overseer reads as a source-*health* map. The plan
said to reuse that slot; that was a misreading. The ledger travels as
`evidence` instead. They would not have collided today — different payloads,
different files — and would have collided badly the first time anyone merged
those paths.
