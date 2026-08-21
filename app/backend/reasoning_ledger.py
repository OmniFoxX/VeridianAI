#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reasoning_ledger.py -- the model's thinking, kept for the USER. Never replayed.

WHY A SEPARATE LEDGER

The obvious home was CRAIID's evidence ledger, and that would have been a
mistake. Evidence entries are replayed to the model at every fatigue handoff
under CITATION_RULE:

    "SOURCES: the entries below are VERBATIM extracts from material actually
     retrieved earlier in this conversation. Cite ONLY from this ledger."

That mechanism exists to STOP fabrication -- its docstring names the incident,
a report whose URLs were right and whose authors and figures were invented.

A reasoning trace is not retrieved, not external, and unverified by
construction: it holds the model's discarded and wrong intermediate steps
alongside the good ones. Putting it there would hand a model its own earlier
guesses labelled as citable sources -- the very failure that ledger prevents,
wearing its authority.

So the trace gets its own store with the opposite contract:

    evidence ledger    read BY the model, to keep it honest about sources
    reasoning ledger   read BY THE USER, and by nothing else, ever

WHAT THAT MEANS IN CODE

There is deliberately NO for_handoff(), no CITATION_RULE, no formatter that
renders entries into prompt text. That absence is the feature. If a future
change needs the model to see its own past reasoning, that is a design
decision to argue on its own merits -- not something to reach for because a
convenient accessor happened to exist. test_reasoning_ledger asserts the
absence, so adding one is a deliberate act rather than a quiet one.

NOT IN backend/craiid/ ON PURPOSE. Every other ledger there feeds the handoff.
Filing this one beside them would imply the same, which is the one thing it
must not do.

SCOPE AND SAFETY

Per-namespace, derived from sage_engine._memory_file(ns) exactly as the
evidence ledger does -- so it lands in users/<ns>/ for a profile and moves with
conversations if that layout ever changes, rather than being rebuilt here and
stranded.

PROFILE TIER (ns=), not system: a reasoning trace is the user's own
conversation content, encrypted under their key. Dot-prefixed and not *.json,
following the .archive_titles.dat convention, so no existing glob("*.json")
reader can ever pick it up.

Bounded on purpose. A long thinking session can produce more trace than reply,
and an unbounded log of it would quietly eat the disk. Oldest entries are
pruned first and the pruning is RECORDED, because a log that silently drops
what you came looking for is worse than one that admits it.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Dict, List, Optional

LEDGER_FILE = ".reasoning_ledger.dat"
SCHEMA_VERSION = 1

# Growth bounds. Generous enough to be useful, finite enough to be safe.
MAX_ENTRIES = 500
MAX_TOTAL_CHARS = 2_000_000        # ~2 MB of text before pruning
MAX_TRACE_CHARS = 200_000          # one pathological trace cannot fill it


def _warn(msg: str) -> None:
    """Ledger trouble must never break a request -- and never be silent."""
    print(f"[reasoning_ledger] {msg}", flush=True)


def _ledger_path(ns=None) -> Optional[Path]:
    """Beside chat_memory.json -- per-namespace and per-thread for free.

    Derived from sage_engine's resolver rather than rebuilt, so a future change
    to where conversations live moves this with them instead of stranding it.
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


def _empty() -> Dict:
    return {"version": SCHEMA_VERSION, "updated": 0,
            "entries": [], "pruned": 0}


def _load(ns=None) -> Dict:
    p = _ledger_path(ns)
    if not p or not p.exists():
        return _empty()
    try:
        import atrest
    except ImportError:
        return _empty()
    try:
        # PROFILE TIER: a reasoning trace is this user's own conversation
        # content and belongs under their key, not the system key.
        data = atrest.load_json_auto(p.read_bytes(), ns=ns)
    except Exception as exc:
        # Say it out loud. An unreadable ledger means the user's thinking log
        # is gone; degrading quietly to "no entries" would look identical to
        # never having recorded any.
        _warn("read failed (%s: %s) -- reporting EMPTY, not overwriting"
              % (type(exc).__name__, exc))
        return _empty()
    if isinstance(data, dict) and "entries" in data:
        return data
    return _empty()


def _save(data: Dict, ns=None) -> bool:
    p = _ledger_path(ns)
    if not p:
        return False
    try:
        import atrest
    except ImportError:
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data["updated"] = int(time.time())
        # PROFILE TIER (ns=): see _load.
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(atrest.dump_json_encrypted(data, ns=ns))
        tmp.replace(p)
        return True
    except Exception as exc:
        _warn("write failed (%s: %s)" % (type(exc).__name__, exc))
        return False


def _prune(data: Dict) -> Dict:
    """Keep the ledger bounded. Oldest first, and count what went."""
    ents = data.get("entries") or []
    dropped = 0
    while len(ents) > MAX_ENTRIES:
        ents.pop(0)
        dropped += 1
    total = sum(int(e.get("chars", 0)) for e in ents)
    while ents and total > MAX_TOTAL_CHARS:
        total -= int(ents[0].get("chars", 0))
        ents.pop(0)
        dropped += 1
    if dropped:
        # Recorded, not silent: a log that quietly drops what you came looking
        # for is worse than one that tells you it did.
        data["pruned"] = int(data.get("pruned", 0)) + dropped
        _warn("pruned %d oldest entr%s to stay within bounds"
              % (dropped, "y" if dropped == 1 else "ies"))
    data["entries"] = ents
    return data


def record(trace: str, ns=None, meta: Optional[Dict] = None) -> bool:
    """Append one reasoning trace. True if stored.

    Never raises. Called from the turn path, which must not be able to fail
    because a log write did.
    """
    try:
        text = str(trace or "")
        if not text.strip():
            return False
        truncated = False
        if len(text) > MAX_TRACE_CHARS:
            text = text[:MAX_TRACE_CHARS]
            truncated = True

        data = _load(ns)
        entry = {
            "ts": int(time.time()),
            "sha256": hashlib.sha256(
                str(trace or "").encode("utf-8")).hexdigest(),
            "chars": len(text),
            "truncated": truncated,
            "trace": text,
        }
        if meta:
            entry["meta"] = {k: str(v)[:200] for k, v in dict(meta).items()}
        data.setdefault("entries", []).append(entry)
        _prune(data)
        return _save(data, ns)
    except Exception as exc:
        _warn("record failed (%s: %s)" % (type(exc).__name__, exc))
        return False


def entries(ns=None, limit: int = 50, newest_first: bool = True) -> List[Dict]:
    """The stored traces, FOR THE USER.

    The only reader. Nothing in a prompt-building path may call this -- see the
    module docstring for why that is a rule and not a preference.
    """
    try:
        ents = list((_load(ns) or {}).get("entries") or [])
        if newest_first:
            ents = list(reversed(ents))
        return ents[:max(0, int(limit))]
    except Exception as exc:
        _warn("read failed (%s: %s)" % (type(exc).__name__, exc))
        return []


def stats(ns=None) -> Dict:
    """Counts only -- safe to surface anywhere, carries no trace text."""
    try:
        data = _load(ns) or {}
        ents = data.get("entries") or []
        return {
            "entries": len(ents),
            "total_chars": sum(int(e.get("chars", 0)) for e in ents),
            "pruned": int(data.get("pruned", 0)),
            "oldest_ts": ents[0].get("ts") if ents else None,
            "newest_ts": ents[-1].get("ts") if ents else None,
            "path": str(_ledger_path(ns) or ""),
        }
    except Exception as exc:
        _warn("stats failed (%s: %s)" % (type(exc).__name__, exc))
        return {"entries": 0, "total_chars": 0, "pruned": 0}


def clear(ns=None) -> bool:
    """Delete this namespace's ledger. Part of chat-clear and ZDR burn.

    The ledger is user data and its lifecycle belongs to the conversation it
    describes: cleared with the chat, destroyed by a burn. A thinking log that
    outlived the conversation it came from would be a retention surprise.
    """
    try:
        p = _ledger_path(ns)
        if p and p.exists():
            p.unlink()
        return True
    except Exception as exc:
        _warn("clear failed (%s: %s)" % (type(exc).__name__, exc))
        return False
