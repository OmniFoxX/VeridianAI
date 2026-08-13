#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evidence_ledger.py -- what was actually retrieved, kept across the handoff.

THE PROBLEM
-----------
Tool results never reach the conversation record. In main.py's agentic loop:

    tool_results_acc = {}            # fresh per request, loop-local
    step_messages = list(messages)   # a COPY
    tool_text += f"--- {k} ---\\n{v}" # injected into the COPY only

Discarded when the request ends. So when CRAIID summarises the conversation at
a fatigue handoff, the fetched pages were never in its input. The assistant
keeps the URLs it typed into its own prose and loses everything it read.

Observed consequence: a research report whose URLs were correct and whose
authors and benchmark figures were invented -- "Mishra et al." for Hajizada
et al., 37.3 ms for 23.2 ms, 333 mJ for 281 mJ. Every error flattered the
story, and none was detectable without returning to the source.

WHAT THIS DOES
--------------
Records externally-sourced tool output at the moment it is produced, extracts
the spans that cannot be paraphrased (see particulars.py), and keeps them
across every fatigue cycle in the thread.

STORES PARTICULARS, NOT PAGES
-----------------------------
Only extracted spans are persisted -- each with the SENTENCE around it, because
"281 mJ" alone does not say what was measured -- but never the raw document.
Measured: a 47 KB page reduces to a 1.2 KB ledger entry, ~2.5% of the source.
Three reasons,
in order of importance:

  1. Privacy. A fetched page may contain anything. Keeping the citation-bearing
     spans is enough to prevent fabrication; keeping the whole document is a
     larger personal-data footprint than the feature needs.
  2. Size. Ledgers stay in kilobytes rather than megabytes, so a long research
     thread does not quietly consume the disk.
  3. Honesty. If extraction found nothing, that is recorded as a GAP the model
     is told not to fill -- storing the page would let a later stage silently
     "recover" content the extractor deliberately did not vouch for.

SCOPE: THE THREAD, NEVER THE SESSION
------------------------------------
Stored beside chat_memory.json, so it is per-namespace and per-conversation by
construction. CRAIID exists to survive fatigue INSIDE a long conversation; a
ledger that reset each cycle would go blind at exactly the boundary it was
built to bridge.

Lifecycle mirrors the conversation it belongs to: archived with it, cleared
when the chat is cleared, and destroyed by ZDR burn. It is user data.

Follows the .archive_titles.dat convention -- dot-prefixed, not *.json (so no
existing glob("*.json") reader can ever see it), at-rest encrypted.
"""

from __future__ import annotations

import hashlib
import json
import time
import os
from pathlib import Path
from typing import Dict, List, Optional

LEDGER_FILE = ".evidence_ledger.dat"
SCHEMA_VERSION = 1

# Which tool results are EVIDENCE. Actions are not evidence: a save
# confirmation or a code-execution result says what the assistant did, not what
# it learned, and recording them would spend the budget on nothing.
EVIDENCE_KINDS = ("browse", "web_search", "web_search_browser", "search",
                  "search_general", "read", "read_file", "file")

DEFAULTS = {
    "budget_chars": 10000,
    "per_source_chars": 700,
    "max_sources": 12,
}


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------
def _ledger_path(ns=None) -> Optional[Path]:
    """Beside chat_memory.json -- per-namespace and per-thread for free.

    Derived from sage_engine's own resolver rather than rebuilt here, so a
    future change to where conversations live moves the ledger with them
    instead of stranding it.
    """
    try:
        import sage_engine
        return Path(sage_engine._memory_file(ns)).parent / LEDGER_FILE
    except Exception:
        try:
            from config import DATA_DIR
            return Path(DATA_DIR) / LEDGER_FILE
        except Exception:
            return None


def _load(ns=None) -> Dict:
    empty = {"version": SCHEMA_VERSION, "updated": 0, "sources": {}}
    p = _ledger_path(ns)
    if not p or not p.exists():
        return empty
    try:
        import atrest
        data = atrest.load_json_auto(p.read_bytes())
        if isinstance(data, dict) and "sources" in data:
            return data
    except Exception:
        pass
    return empty


def _save(data: Dict, ns=None) -> bool:
    p = _ledger_path(ns)
    if not p:
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data["updated"] = int(time.time())
        try:
            import atrest
            blob = atrest.dump_json_encrypted(data)
        except Exception:
            # Encryption unavailable -> do NOT silently write plaintext user
            # data to disk. Losing the ledger degrades a handoff; writing
            # fetched content unencrypted breaks a promise the app makes.
            return False
        p.write_bytes(blob)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------
def _kind_of(key: str) -> str:
    return str(key or "").split(":", 1)[0].strip().lower()


def _url_of(key: str, content: str) -> str:
    rest = str(key or "").split(":", 1)[1].strip() if ":" in str(key or "") else ""
    if rest.startswith("http"):
        return rest
    try:
        import particulars as _p
    except Exception:
        try:
            from craiid import particulars as _p   # type: ignore
        except Exception:
            return rest
    urls = [x["text"] for x in _p.extract(content or "", kinds=("url",),
                                          with_sentence=False)]
    return urls[0] if urls else rest


def is_evidence(key: str) -> bool:
    return _kind_of(key) in EVIDENCE_KINDS


def record(key: str, content: str, ns=None, meta: Optional[Dict] = None) -> bool:
    """Record one tool result. Returns True if it was stored as evidence.

    Never raises. Called from the agentic loop, which must not be able to fail
    because a ledger write did.
    """
    try:
        if not is_evidence(key) or not content:
            return False
        try:
            import particulars as _p
        except Exception:
            from craiid import particulars as _p   # type: ignore

        text = str(content)
        found = _p.extract(text)

        data = _load(ns)
        src = data["sources"].get(key) or {
            "key": key,
            "kind": _kind_of(key),
            "url": _url_of(key, text),
            "first_seen": int(time.time()),
            "hits": 0,
            "particulars": [],
        }
        src["hits"] = int(src.get("hits", 0)) + 1
        src["last_seen"] = int(time.time())
        src["chars_seen"] = int(src.get("chars_seen", 0)) + len(text)
        if meta:
            src["meta"] = {k: str(v)[:200] for k, v in dict(meta).items()}

        # Merge, de-duplicated, order preserved.
        seen = {(p.get("kind"), p.get("text")) for p in src["particulars"]}
        for p in found:
            k = (p.get("kind"), p.get("text"))
            if k not in seen:
                seen.add(k)
                src["particulars"].append(dict(p))

        # A source fetched but unextractable is recorded as a GAP, not dropped.
        # An explicit absence the model is instructed to respect beats a silent
        # one it will fill from parametric memory.
        src["preserved"] = bool(src["particulars"])
        if not src["preserved"]:
            src["reason"] = "extraction found no citable particulars"

        data["sources"][key] = src
        return _save(data, ns)
    except Exception:
        return False


# --------------------------------------------------------------------------
# handoff
# --------------------------------------------------------------------------
def _rank(sources: List[Dict], conversation_text: str) -> List[Dict]:
    """Most-referenced first.

    A source the assistant has been citing in its own prose is a source it is
    about to cite again -- that is a better predictor of what the next instance
    needs than recency or size.
    """
    convo = (conversation_text or "").lower()

    def score(s):
        url = str(s.get("url") or "").lower()
        mentions = convo.count(url) if url and len(url) > 12 else 0
        return (mentions * 10) + int(s.get("hits", 0)) + (1 if s.get("preserved") else 0)

    return sorted(sources, key=score, reverse=True)


def for_handoff(ns=None, conversation_text: str = "",
                budget_chars: int = None, per_source_chars: int = None,
                max_sources: int = None) -> Dict:
    """The `sources` object for the warm-instance handoff.

    The schema slot has existed since #69 (coordinator_signal.py:48) and has
    always shipped empty. This fills it.
    """
    out = {"schema": "craiid_evidence_v1", "sources": [],
           "total": 0, "preserved": 0, "gaps": 0, "truncated": False}
    try:
        budget = int(budget_chars or DEFAULTS["budget_chars"])
        per_src = int(per_source_chars or DEFAULTS["per_source_chars"])
        cap = int(max_sources or DEFAULTS["max_sources"])

        data = _load(ns)
        srcs = list(data.get("sources", {}).values())
        out["total"] = len(srcs)
        if not srcs:
            return out

        ranked = _rank(srcs, conversation_text)
        if len(ranked) > cap:
            out["truncated"] = True
        spent = 0
        for s in ranked[:cap]:
            entry = {"url": s.get("url", ""), "kind": s.get("kind", ""),
                     "preserved": bool(s.get("preserved"))}
            if not entry["preserved"]:
                entry["reason"] = s.get("reason", "not preserved")
                out["gaps"] += 1
                out["sources"].append(entry)
                continue

            kept, used = [], 0
            for p in s.get("particulars", []):
                # The sentence gives a figure its meaning: "281 mJ" alone does
                # not say what was measured.
                line = {"kind": p.get("kind"), "text": p.get("text")}
                if p.get("sentence"):
                    line["sentence"] = p["sentence"]
                cost = len(json.dumps(line, ensure_ascii=False))
                if used + cost > per_src or spent + cost > budget:
                    break
                kept.append(line)
                used += cost
                spent += cost
            entry["particulars"] = kept
            # Over budget with nothing kept is still a GAP, not a silent drop.
            entry["preserved"] = bool(kept)
            if kept:
                out["preserved"] += 1
            else:
                entry["reason"] = "over budget"
                out["gaps"] += 1
            out["sources"].append(entry)
            if spent >= budget:
                out["truncated"] = True
                break
    except Exception:
        pass
    return out


CITATION_RULE = (
    "SOURCES: the entries below are VERBATIM extracts from material actually "
    "retrieved earlier in this conversation. Cite ONLY from this ledger. For "
    "any entry marked preserved=false you may cite its URL, but you MUST NOT "
    "state its authors, figures or findings -- re-fetch it, or say the detail "
    "is unavailable. Do not reconstruct a citation from memory."
)


# --------------------------------------------------------------------------
# lifecycle -- the ledger belongs to the conversation
# --------------------------------------------------------------------------
def clear(ns=None) -> bool:
    """Delete the ledger. Used by clear-chat and by ZDR burn.

    ZDR is a promise, not a preference: this holds fetched content and personal
    particulars, so 'burn everything' has to include it.
    """
    try:
        p = _ledger_path(ns)
        if p and p.exists():
            p.unlink()
        return True
    except Exception:
        return False


def archive_to(archive_name: str, ns=None) -> bool:
    """Snapshot the ledger alongside an archived conversation, so reloading an
    old research thread restores the sources it was built on."""
    try:
        import sage_engine
        folder = Path(sage_engine._archive_folder(ns))
        folder.mkdir(parents=True, exist_ok=True)
        src = _ledger_path(ns)
        if not src or not src.exists():
            return False
        (folder / _snapshot_name(archive_name)).write_bytes(src.read_bytes())
        return True
    except Exception:
        return False


def _snapshot_name(archive_name) -> str:
    """Filename for a conversation's evidence snapshot, DERIVED not composed.

    The previous version sanitised the archive name and then built a path from
    it. That was safe -- `Path(x).name` strips directories, and a containment
    check confirmed the result -- but it kept the user's string in the path, so
    every use of that path stayed tainted. CodeQL flagged the two uses, and
    when the sanitiser was made explicit it flagged the sanitiser too: three
    alerts where there had been two. Arguing with the tool was the wrong move.

    Hashing settles it. The filename is 32 hex characters derived from the
    name, so nothing the user typed reaches the filesystem at all. That is not
    only tool-appeasement -- it removes a class of problems the sanitiser never
    addressed:

      - archive titles are user-supplied text and may contain emoji, accents,
        or characters the filesystem cannot encode
      - a long title could exceed MAX_PATH once prefixed and suffixed
      - Windows reserves CON, PRN, AUX, NUL and COM1-9 as filenames
      - two titles differing only in case collide on Windows, not on Linux

    None of those were traversal, and all of them were bugs waiting.
    """
    digest = hashlib.sha256(str(archive_name).encode("utf-8")).hexdigest()
    return ".evidence_%s.dat" % digest[:32]


def _legacy_snapshot_name(archive_name) -> Optional[str]:
    """The pre-hash filename, for reading snapshots written before this change.

    Read-only and best-effort: if the old name is unusable we simply have no
    legacy snapshot, which is the same outcome as not having one at all.
    """
    stem = Path(str(archive_name)).name
    if not stem or stem in (".", "..") or "/" in stem or "\\" in stem:
        return None
    return ".evidence_%s.dat" % stem


def _find_snapshot(folder: "Path", archive_name) -> Optional["Path"]:
    """Locate an existing snapshot: current name first, then the legacy one.

    The legacy lookup ENUMERATES the folder and compares names, rather than
    joining the archive name onto a path. Same result, but every candidate
    path originates from the directory listing -- the user's string is only
    ever compared against one, never used to construct one. Composing it
    (even after sanitising) is what kept the taint alive through three
    rounds of this.
    """
    cur = folder / _snapshot_name(archive_name)
    if cur.exists():
        return cur
    want = _legacy_snapshot_name(archive_name)
    if not want:
        return None
    try:
        for cand in folder.glob(".evidence_*.dat"):
            if cand.name == want:
                return cand
    except OSError:
        pass
    return None


def restore_from(archive_name: str, ns=None) -> bool:
    """Restore a snapshot when its conversation is loaded."""
    try:
        import sage_engine
        folder = Path(sage_engine._archive_folder(ns))
        snap = _find_snapshot(folder, archive_name)
        dest = _ledger_path(ns)
        if not snap or not dest:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(snap.read_bytes())
        return True
    except Exception:
        return False


def stats(ns=None) -> Dict:
    """For logging what a thread has accumulated."""
    try:
        data = _load(ns)
        srcs = list(data.get("sources", {}).values())
        return {
            "sources": len(srcs),
            "preserved": sum(1 for s in srcs if s.get("preserved")),
            "gaps": sum(1 for s in srcs if not s.get("preserved")),
            "particulars": sum(len(s.get("particulars", [])) for s in srcs),
            "updated": data.get("updated", 0),
        }
    except Exception:
        return {"sources": 0, "preserved": 0, "gaps": 0, "particulars": 0}
