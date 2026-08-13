# Plan — CRAIID evidence ledger + particulars preservation

Status: **planned, not implemented.** Written 2026-08-10, revised same day
after review.

---

## The observed failure

A six-thread research run used `browser_tool` successfully — real pages fetched
and read. Context fatigue triggered, CRAIID handed off, and the recovered report
was complete and well-structured. Its citations were wrong.

Checked against the real paper (arXiv:2511.01553):

| | The report said | The paper says |
|---|---|---|
| First author | "S. B. Mishra et al." | **Hajizada et al.** |
| Latency | 0.33 ms vs **37.3 ms → 113×** | 0.33 ms vs **23.2 ms → 70×** |
| Energy | 0.05 mJ vs **333 mJ → 6,600×** | 0.05 mJ vs **281 mJ → 5,600×** |

The SNN-side figures are right; every GPU baseline and ratio is inflated.
Elsewhere: two entries share one URL while describing different papers, DOI
fragments appear as volume numbers, an IEEE document ID predates its hardware.

**The URLs are right. Everything requiring the page body is invented.**

---

## Root cause, and the second cause found on review

### Primary — tool results never reach the conversation record

```
main.py:5650   tool_results_acc = {}            # fresh per request, loop-local
main.py:5770   step_messages = list(messages)   # a COPY
main.py:5775   tool_text += f"--- {k} ---\n{v}"  # injected into the COPY only
```

Discarded when the request ends. Never written back, never persisted. CRAIID's
Journalist summarises `chat_history + chat_tail` (`sage_daemon.py:1218`) — the
user's prompts and the assistant's prose. The fetched pages were never in there,
which is why URLs survived (typed by the assistant) and authors did not.

### Secondary — conversational turns are truncated below their information density

```
journalist._render (279)   280 chars per turn
summarize_stream           max_turns = 14
sage_daemon.py:1221        theme_summary[:2000]
sage_daemon.py:1206        history_preview 220 chars each
```

280 characters is fine for "user asked about X, assistant answered Y". It is
**not** fine for a week-long itinerary with addresses, phone numbers and times,
or any turn where the value is in the particulars rather than the gist. A date
squeaks through; a schedule does not.

So both channels lose detail, for the same underlying reason: **the handoff
preserves meaning and discards specifics**, and specifics are precisely what
cannot be reconstructed.

---

## Design — one mechanism, two channels

The unifying idea: **particulars are verbatim or they are absent.**

A *particular* is a span that cannot be paraphrased without becoming wrong —
a phone number, an address, a date, a time, a price, a DOI, an author name, a
measurement with units, a file path, a version string. Everything else is prose
and may be summarised freely.

### 1. The extractor — `backend/craiid/particulars.py` (new)

One function, used by both channels: given text, return the verbatim spans that
must not be lost, plus the sentence each sits in for context.

| Pattern class | Examples |
|---|---|
| identifiers | DOI, arXiv ID, ISBN, order/ticket numbers |
| contact | phone numbers, emails, street addresses, postcodes |
| temporal | dates, times, durations, flight/train numbers |
| quantities | numbers with units (ms, mJ, W, %, ×, $, km, kg) |
| attribution | author blocks, `et al.`, quoted titles |
| technical | file paths, URLs, version strings, hashes |

Extraction **never rewrites**. It selects spans and keeps them byte-for-byte.
A summarised number is a number that can drift, and drift is the failure.

### 2. Channel A — the evidence ledger (tool output)

`backend/craiid/evidence_ledger.py` (new). Records externally-sourced tool
results at the moment they are produced, before they are discarded.

```python
record(ns, key, content, meta)      # key is tool_results_acc's key
```

Called from the ~10 sites in the agentic loop where `tool_results_acc[key] = …`.
A single helper keeps each call site to one line.

**Recorded:** browse, web_search, file reads.
**Not recorded:** code execution output, save confirmations, memory searches.
Those are actions, not sources, and would spend the budget on nothing. *File
reads are in* because a document the user asked to be read is exactly the
itinerary case — its value is entirely in its particulars.

### 3. Channel B — particulars-preserving truncation (conversation)

`_render` keeps its cap, but stops being blind:

1. Run the extractor over the turn.
2. Truncate the *prose* to the budget.
3. Append any particulars that fell outside the kept window, verbatim.

The narrative shortens; the phone number survives. This is strictly better than
raising the number, because it targets what matters instead of buying more of
everything.

Caps rise modestly as well — enough to carry a dense turn, not enough to
re-trigger the fatigue the handoff exists to survive:

| Where | Now | Proposed |
|---|---|---|
| `_render` per turn | 280 | **900** + particulars appended |
| `max_turns` | 14 | 14 (unchanged) |
| `theme_summary` | 2000 | **6000** |
| `history_preview` per entry | 220 | **500** + particulars |

### 4. Scope — the whole thread, never per-session

**This is the point of the feature.** CRAIID exists to survive fatigue *inside*
a long conversation; a ledger that reset at each fatigue cycle would go blind at
exactly the boundary it was built to bridge.

The ledger is therefore keyed to the **namespace + live conversation**, mirroring
`chat_memory.json`, and:

- **persists across every fatigue cycle** within the thread
- **is archived with the conversation**, so reloading an old research thread
  restores its sources
- **is cleared when the chat is cleared** — a new thread starts empty
- **is destroyed by ZDR burn**, without exception. It is user data holding
  fetched content and personal particulars; the burn control means all of it.

Stored at `sage_data/craiid/evidence/<ns>.json`, at-rest encrypted, following
the VLTS convention.

### 5. Carry — under its own key, `evidence`

**Correction (during implementation):** the plan originally said to fill the
existing `sources` slot. That was a misreading. `coordinator_signal.py:48` does
emit `"sources": {}`, but the overseer consumes it as a source-HEALTH map --
`{name: {status, entries_included}}` for its log line -- in a different payload
(`craiid_task.json`) travelling a different path.

They would not collide today. They would collide the first time anyone merged
the two paths, and the symptom would be an `AttributeError` deep in the
overseer's logging on a field nobody remembered was overloaded.

So `sage_daemon._build_digest` gains `digest["evidence"]`, plus
`digest["evidence_rule"]`, outside the `theme_summary` cap. The ledger has its
own budget.

### 6. Constrain — the rule that actually stops fabrication

Preservation alone is not enough. A model with a gap fills it. The warm context
must say:

> **The sources below are verbatim extracts from material actually retrieved in
> this conversation. Cite ONLY from this ledger. If an entry is marked
> `preserved: false`, you may cite its URL but MUST NOT state its authors,
> figures or findings — re-fetch it, or say the detail is unavailable.**

### 7. Fail loudly

A source fetched but not extractable is recorded, not dropped:

```json
{"url": "...", "preserved": false, "reason": "extraction empty"}
```

An explicit gap the model is told to respect beats a silent absence it will fill
from memory. Same principle as the tier logs, the embed-source tag, and the
`[extras] python` line: **degrade loudly, never quietly.**

---

## Budget

The handoff exists *because* context is full.

| Knob | Default | Meaning |
|---|---|---|
| `craiid_evidence_enabled` | `true` | master switch |
| `craiid_evidence_budget_chars` | `10000` | ledger total |
| `craiid_evidence_per_source_chars` | `700` | per source |
| `craiid_evidence_max_sources` | `12` | keep the most-referenced |
| `craiid_particulars_enabled` | `true` | channel B switch |
| `craiid_turn_chars` | `900` | per-turn prose cap |

Roughly: conversation ~12 KB + ledger ~10 KB + theme ≈ **22 KB**, against
~3.4 KB today. Must be measured against a real run before going default-on —
if it re-triggers fatigue, the budgets come down, not the design.

Over budget, sources rank by how often the assistant referenced their URL in its
own prose: the ones it is about to cite are the ones worth keeping.

---

## Verification

1. **Extractor unit tests** — arXiv abstract text, and an itinerary with
   addresses/phones/times. Assert every particular emerges **byte-identical**.
2. **Channel B test** — a 3000-char itinerary turn through `_render`; assert the
   prose shortens and no phone number, address or time is lost.
3. **Boundary test** — ledger through the handoff assembly; numbers verbatim on
   the far side.
4. **The regression that matters** — reconstruct this incident. Assert the
   recovered context contains `Hajizada`, `23.2`, `281` and does **not** contain
   `Mishra`, `37.3`, `333`.
5. **Thread continuity** — two consecutive fatigue cycles in one thread; assert
   cycle 2 still holds cycle 1's sources.
6. **Lifecycle** — archive/reload restores the ledger; clear-chat empties it;
   **ZDR burn destroys it**.
7. **Budget** — 50 sources in, cap holds, dropped ones appear as
   `preserved: false` rather than vanishing.
8. **Off switches** — both flags false reproduces today's behaviour exactly.

---

## Explicitly out of scope

- `max_turns` stays at 14.
- The overseer schema is unchanged; `sources` already exists.
- No change to `browser_tool`. It worked. Its sanitisation boundary (`⟦⟧`)
  survives, since extracted spans stay verbatim.
- Whether Toga *should* re-fetch a missing detail is a behaviour question for a
  later pass.

---

## Risk

| Risk | Mitigation |
|---|---|
| Combined budget re-triggers fatigue | measured on a real run before default-on; budgets are knobs |
| Extractor misses a particular | `preserved: false` + the model is told not to invent |
| Extractor over-captures, wasting budget | per-source cap; dedupe; ranked eviction |
| Personal data now persisted in a second place | same at-rest encryption as chat memory, same namespace isolation, **and ZDR burns it** |
| Encrypted store fails to load in the daemon | ledger absent → `sources: {}` → today's behaviour |

**Every failure mode degrades to current behaviour.** Nothing here changes what
happens when the ledger is empty or disabled.
