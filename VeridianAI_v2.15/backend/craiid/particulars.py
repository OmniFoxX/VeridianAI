#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
particulars.py -- the spans that must survive verbatim or not at all.

WHY THIS EXISTS
---------------
CRAIID's handoff preserves MEANING and discards SPECIFICS. For conversation
that is exactly right: "the user asked about neuromorphic hardware and I
summarised six threads" is a faithful compression. For anything whose value IS
the specifics, it is destructive in a way that does not announce itself.

Two observed failures, one cause:

  * A research handoff kept the URLs (the assistant had typed them into its own
    prose, so they lived in the conversation) and lost the page bodies. It then
    reconstructed authors and benchmark figures from parametric memory and got
    them confidently wrong -- "Mishra et al." for Hajizada et al., 37.3 ms for
    23.2 ms, 333 mJ for 281 mJ. Every error flattered the story.

  * A week-long itinerary summarised to 280 characters keeps the trip and loses
    the addresses, phone numbers and times. The gist survives; the thing the
    user actually needed does not.

A PARTICULAR is a span that cannot be paraphrased without becoming wrong. A
phone number, an address, a date, a price, a DOI, an author name, a measurement
with units. Everything else is prose and may be summarised freely.

THE RULE
--------
    Particulars are verbatim, or they are absent and SAID to be absent.

There is no third option. A "roughly 37 ms" that was 23.2 ms is worse than a
gap, because a gap can be re-fetched and a plausible wrong number cannot be
noticed.

DESIGN CONSTRAINTS
------------------
1. NEVER REWRITES. This module selects spans; it does not normalise, reformat
   or correct them. A reformatted date is a date that can drift.
2. NEVER RAISES. It runs inside a handoff that exists because something is
   already under stress. A crash here would take out the recovery path.
3. BOUNDED. Input may be a 500 KB fetched page. Every scan is capped, and the
   caps are explicit rather than emergent.
4. UNDER-CAPTURE BEATS OVER-CAPTURE. A missed particular is reported as a gap
   the model is told not to fill. A false positive silently spends the budget
   that a real one needed. Patterns that cannot be made specific (bare 5-digit
   numbers as "ZIP codes", for instance) are deliberately omitted.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

# Scanning caps. A fetched page can be enormous; these bound the work without
# needing the caller to know or care.
MAX_INPUT_CHARS = 400_000
MAX_SPANS = 400
MAX_SENTENCE_CHARS = 320

# --------------------------------------------------------------------------
# Patterns.
#
# Ordered by specificity: the first pattern to claim a character position wins,
# so "10.1038/s41586-025-1234" is a DOI rather than three loose numbers. Each
# entry is (kind, compiled regex).
#
# Every pattern here had to justify itself against the under-capture rule. Some
# tempting ones are deliberately ABSENT -- see the note at the bottom.
# --------------------------------------------------------------------------
_PATTERNS: List = [
    # ---- identifiers: the citation IS the identifier -------------------
    ("doi", re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b")),
    ("arxiv", re.compile(r"\barXiv[:\s]\s*\d{4}\.\d{4,5}(?:v\d+)?\b", re.I)),
    ("isbn", re.compile(r"\bISBN(?:-1[03])?[:\s]\s*[\d\-Xx]{10,17}\b")),
    ("url", re.compile(r"https?://[^\s<>\"')\]]+")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),

    # ---- technical strings: wrong by one character is wrong ------------
    ("winpath", re.compile(r"\b[A-Za-z]:\\[^\s\"'<>|]+")),
    ("hash", re.compile(r"\b[a-f0-9]{32,64}\b", re.I)),
    ("version", re.compile(r"\bv?\d+\.\d+(?:\.\d+){0,2}(?:-[A-Za-z0-9.]+)?\b")),

    # ---- contact: the itinerary case -----------------------------------
    # Requires punctuation or grouping so a bare 10-digit number does not
    # masquerade as a phone number.
    ("phone", re.compile(
        r"(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)|\b\d{3})[\s.\-]\d{3}[\s.\-]\d{3,4}\b")),
    ("address", re.compile(
        r"\b\d{1,5}\s+(?:[A-Z][A-Za-z.'-]+\s+){1,4}"
        r"(?:Street|St|Road|Rd|Avenue|Ave|Boulevard|Blvd|Lane|Ln|Drive|Dr|"
        r"Way|Court|Ct|Place|Pl|Terrace|Ter|Highway|Hwy|Parkway|Pkwy)"
        r"\b\.?"
        # optional unit designator -- "..., Suite 300" is part of the address,
        # and an address missing its suite is an address that does not work
        r"(?:,?\s*(?:Suite|Ste|Apt|Apartment|Unit|Floor|Fl|Rm|Room|#)\s*[\w-]+)?")),

    # ---- temporal -------------------------------------------------------
    ("date_iso", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("date_long", re.compile(
        r"\b(?:\d{1,2}\s+)?(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|"
        r"Sept|Oct|Nov|Dec)\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b", re.I)),
    ("date_num", re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")),
    ("time", re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?\b")),
    ("flight", re.compile(r"\b[A-Z]{2}\s?\d{2,4}\b")),

    # ---- quantities: the numbers that were inflated ---------------------
    ("money", re.compile(r"[$£€¥]\s?\d[\d,]*(?:\.\d{2})?\b")),
    ("quantity", re.compile(
        r"\b\d+(?:[.,]\d+)?\s*"
        r"(?:ms|µs|us|ns|ps|s\b|mJ\b|J\b|kWh|Wh|mW|kW|MW|W\b|"
        r"%|×|GB|MB|KB|TB|GHz|MHz|kHz|Hz|"
        r"km|cm|mm|m\b|mi\b|ft\b|kg|lb|mg|°C|°F|"
        r"tokens?/s|params?|B\b)")),
    ("multiplier", re.compile(r"\b\d+(?:[.,]\d+)?\s*[x×]\b")),

    # ---- attribution: the field that was wrong --------------------------
    # "et al." is precise and high-value: it IS the citation form.
    ("attribution", re.compile(r"\b[A-Z][A-Za-z'\u2019-]+\s+et\s+al\.?")),
    # An author BLOCK -- three or more comma-separated multi-word names. This
    # is the span that was wrong in the incident: the paper lists its authors
    # in full and never says "Hajizada et al.", so an et-al pattern alone would
    # have preserved nothing and the model would still have invented a name.
    # Requiring >=2 capitalised tokens per name keeps ordinary lists out
    # ("Paris, London, and Rome" has one token each and does not match).
    # Two shapes, both requiring THREE OR MORE multi-word names:
    #   "A Alpha, B Beta, C Gamma"            (3+ separated by commas)
    #   "A Alpha, B Beta and C Gamma"         (2 commas' worth + "and")
    #
    # Three is the threshold that separates an author list from an ordinary
    # one. "New York, Los Angeles" is two multi-word proper nouns and must not
    # match; "Elvin Hajizada, Danielle Rager and Mike Davies" is three and must.
    # An earlier version demanded three COMMAS and silently missed every paper
    # with fewer than four authors -- which is most of them, and which is the
    # exact span whose absence produced the fabricated citation.
    ("author_block", re.compile(
        r"\b(?:"
        r"(?:[A-Z][A-Za-z.'\u2019-]+\s+){1,3}[A-Z][A-Za-z'\u2019-]+"
        r"(?:,\s*(?:[A-Z][A-Za-z.'\u2019-]+\s+){1,3}[A-Z][A-Za-z'\u2019-]+){2,}"
        r"(?:,?\s+and\s+(?:[A-Z][A-Za-z.'\u2019-]+\s+){1,3}[A-Z][A-Za-z'\u2019-]+)?"
        r"|"
        r"(?:[A-Z][A-Za-z.'\u2019-]+\s+){1,3}[A-Z][A-Za-z'\u2019-]+"
        r"(?:,\s*(?:[A-Z][A-Za-z.'\u2019-]+\s+){1,3}[A-Z][A-Za-z'\u2019-]+)"
        r",?\s+and\s+(?:[A-Z][A-Za-z.'\u2019-]+\s+){1,3}[A-Z][A-Za-z'\u2019-]+"
        r")")),
    ("quoted_title", re.compile(r"[\"“][^\"”\n]{12,180}[\"”]")),
]

# NOT INCLUDED, on purpose:
#   * bare ZIP/postcode  -- indistinguishable from any other 5-digit number
#   * bare integers      -- would match everything and starve the budget
#   * person names       -- no reliable pattern; "attribution" catches the
#                           citation case, which is the one that failed
# Under-capture is recoverable (the gap is reported); over-capture is not.

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“])")


class Particular(dict):
    """A verbatim span plus where it came from.

    A dict subclass so it serialises straight into the handoff JSON with no
    conversion step -- one less place for the text to be touched.
    """

    @property
    def text(self) -> str:
        return self["text"]

    @property
    def kind(self) -> str:
        return self["kind"]


def _sentence_for(text: str, start: int, end: int) -> str:
    """The sentence containing a span, capped.

    Context matters: "281 mJ" alone does not say what was measured. The
    sentence is what makes a preserved figure usable rather than merely
    present.
    """
    lo = text.rfind(". ", 0, start)
    lo = 0 if lo < 0 else lo + 2
    hi = text.find(". ", end)
    hi = len(text) if hi < 0 else hi + 1
    s = " ".join(text[lo:hi].split())
    if len(s) > MAX_SENTENCE_CHARS:
        # Keep the span itself centred rather than truncating from the left,
        # or a figure near the end of a long sentence would be cut off.
        rel = start - lo
        half = MAX_SENTENCE_CHARS // 2
        a = max(0, rel - half)
        s = ("..." if a else "") + s[a:a + MAX_SENTENCE_CHARS] + "..."
    return s


def extract(text: str,
            kinds: Optional[Iterable[str]] = None,
            max_spans: int = MAX_SPANS,
            with_sentence: bool = True) -> List[Particular]:
    """Verbatim particulars in `text`, in document order, deduplicated.

    Returns [] on any failure. Never raises: this runs inside a recovery path
    and must not be able to break it.
    """
    out: List[Particular] = []
    try:
        if not text or not isinstance(text, str):
            return out
        if len(text) > MAX_INPUT_CHARS:
            # Head AND tail, not head alone.
            #
            # Truncating from the front is the intuitive bound and the wrong
            # one for exactly the documents this exists to serve: a paper puts
            # its abstract at the top (reconstructible) and its results tables
            # and bibliography at the BOTTOM (not). Head-only truncation would
            # discard precisely the citation-bearing end of every long source
            # and report the gap as "extraction empty".
            head = int(MAX_INPUT_CHARS * 0.6)
            tail = MAX_INPUT_CHARS - head
            text = text[:head] + "\n...\n" + text[-tail:]
        wanted = set(kinds) if kinds else None

        claimed: List[range] = []          # positions already taken
        found: List[Dict] = []
        for kind, rx in _PATTERNS:
            if wanted and kind not in wanted:
                continue
            for m in rx.finditer(text):
                a, b = m.start(), m.end()
                # First (most specific) pattern to claim a position wins, so a
                # DOI is not also reported as three "version" strings.
                if any(a < r.stop and b > r.start for r in claimed):
                    continue
                claimed.append(range(a, b))
                found.append({"kind": kind, "text": m.group(0), "start": a, "end": b})
                if len(found) >= max_spans * 3:
                    break

        found.sort(key=lambda d: d["start"])
        seen = set()
        for d in found:
            key = (d["kind"], d["text"])
            if key in seen:
                continue
            seen.add(key)
            p = Particular(kind=d["kind"], text=d["text"])
            if with_sentence:
                p["sentence"] = _sentence_for(text, d["start"], d["end"])
            out.append(p)
            if len(out) >= max_spans:
                break
    except Exception:
        return out
    return out


def preserve(text: str, budget: int, max_extra: int = 12) -> str:
    """Truncate prose to `budget`, then re-append particulars that fell outside.

    This is the conversational channel. The narrative shortens; the phone
    number survives.

    Raising the budget alone would not do this -- it would keep whichever
    characters happened to come first, which for an itinerary is the preamble
    and for a paper is the abstract. Both are the parts that could be
    reconstructed. What cannot be reconstructed sits further in.
    """
    try:
        if not text or not isinstance(text, str):
            return text or ""
        flat = " ".join(text.split())
        if len(flat) <= budget:
            return flat

        head = flat[:budget]
        tail_particulars = []
        seen_in_head = {p["text"] for p in extract(head, with_sentence=False)}
        for p in extract(flat, with_sentence=False):
            if p["text"] in seen_in_head:
                continue
            tail_particulars.append(p["text"])
            if len(tail_particulars) >= max_extra:
                break

        if not tail_particulars:
            return head + "..."
        # Marked so a reader (human or model) knows these are salvaged spans
        # and not continuous prose -- they are out of context by construction.
        return head + "... [kept: " + " | ".join(tail_particulars) + "]"
    except Exception:
        return (text or "")[:budget]


def summarize_kinds(particulars: List[Particular]) -> Dict[str, int]:
    """Counts by kind. For logging what a handoff actually carried."""
    out: Dict[str, int] = {}
    for p in particulars or []:
        try:
            out[p["kind"]] = out.get(p["kind"], 0) + 1
        except Exception:
            continue
    return out


if __name__ == "__main__":   # quick manual check: python particulars.py < file
    import sys, json
    data = sys.stdin.read()
    ps = extract(data)
    print(json.dumps({"counts": summarize_kinds(ps),
                      "particulars": ps[:40]}, indent=2, ensure_ascii=False))
