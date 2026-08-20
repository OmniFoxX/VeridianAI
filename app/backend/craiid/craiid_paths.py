"""craiid_paths.py -- one answer to "where are the archives?", for all of CRAIID.

WHY THIS EXISTS (v2.15.2)
On 2026-08-13 user content moved out of the install directory and into
sage_data. state_paths.py records both locations:

    ARCHIVES_DIR      = <sage_data>/archives          # where they live now
    OLD_ARCHIVES_DIR  = <install>/archives            # where they used to

CRAIID did not get the memo. Six places across craiid/ found the archives by
walking up the directory tree looking for a folder literally named "archives",
and craiid_author.py hardcoded `_ROOT_DIR / "archives"`. After the move every
one of them resolved to an install-directory folder that no longer had anything
in it, so the Archivist built its compression key from zero archives and said
nothing was wrong -- an empty corpus is not an error, it is just an empty
corpus.

That is the same shape as the migration itself: craiid_author.py moved its
chat_memory, its reconstructs, its VLTS store and its logs into the data dir,
and left `_ARCHIVES_DIR` on the old line. Everything moved except one thing,
and the one thing was the input.

WHAT THIS DOES
Asks the app first, and only then falls back to searching. The walk-up is kept
because the audit scripts are also run standalone, from a terminal, outside the
app -- but it is now the LAST resort instead of the only strategy.

A half-finished migration is reported rather than silently papered over: if the
new location is empty and the old one still has archives, that is worth saying
out loud, because it means state_migration did not finish and somebody is about
to draw conclusions from a corpus that is not theirs.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

_THIS_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = next(
    (p for p in Path(__file__).resolve().parents if p.name == "backend"),
    _THIS_DIR.parent,
)

# Real archive files are archive_<timestamp>.json -- NOT "archives_NNNN.json",
# which is what craiid_author.py's module docstring and test_author_robustness
# both still say. sage_engine writes them (`archive_{ts}.json`) and gates reads
# on `name.startswith("archive_") and name.endswith(".json")`; craiid_author.py
# already carries a "FIX #69: real files are archive_<ts>.json" comment on its
# own glob. Getting this wrong is not cosmetic -- a glob that matches nothing
# makes a populated directory look empty, which is the exact failure this file
# exists to prevent.
#
# The leading dot on .evidence_*.dat and .archive_titles.dat keeps them out of
# this glob, which is why they are named that way.
_ARCHIVE_GLOB = "archive_*.json"


def _has_archives(p: Optional[Path]) -> bool:
    try:
        return bool(p) and p.is_dir() and any(p.glob(_ARCHIVE_GLOB))
    except Exception:
        return False


def _from_state_paths():
    """(new, old) as state_paths sees them, or (None, None) when it is not
    importable -- which is the standalone case, not an error."""
    try:
        if str(_BACKEND_DIR) not in sys.path:
            sys.path.insert(0, str(_BACKEND_DIR))
        from state_paths import ARCHIVES_DIR, OLD_ARCHIVES_DIR
        return Path(ARCHIVES_DIR), Path(OLD_ARCHIVES_DIR)
    except Exception:
        return None, None


def _walk_up(start: Optional[str] = None, hops: int = 5) -> Optional[Path]:
    """The original strategy, kept for standalone runs. Tries this file's own
    ancestors before the working directory, because a script run from anywhere
    should still find its own project."""
    roots = []
    if start:
        roots.append(Path(start))
    roots.append(_BACKEND_DIR.parent)          # the project root
    roots.append(Path(os.getcwd()))
    for root in roots:
        cur = root
        for _ in range(hops):
            cand = cur / "archives"
            if cand.is_dir():
                return cand
            if cur.parent == cur:
                break
            cur = cur.parent
    return None


def archives_dir(start_path: Optional[str] = None,
                 require_content: bool = True) -> Optional[Path]:
    """Where CRAIID should read chat archives from.

    require_content=True (the default) means "a folder that actually has
    archives in it". Pass False when you only need somewhere to write.
    """
    new, old = _from_state_paths()

    if _has_archives(new):
        return new
    if _has_archives(old):
        # The canonical location exists but is empty while the old one is not:
        # the move did not finish. Say so -- reading the old copy silently is
        # how you end up auditing a corpus you thought you had migrated.
        if new is not None:
            print(
                f"[craiid_paths] WARNING: archives found at the OLD location "
                f"{old}, while {new} is empty. state_migration has not "
                f"finished. Using the old location for now.",
                flush=True,
            )
        return old

    # Nothing populated yet. Prefer the canonical directory if it exists at all,
    # so a fresh install writes and reads in the right place.
    if not require_content:
        if new is not None:
            return new
        found = _walk_up(start_path)
        return found

    found = _walk_up(start_path)
    if _has_archives(found):
        return found

    # Nothing anywhere. Return the canonical location if we know it, so callers
    # report a useful path instead of "None".
    return new if new is not None else found


def archives_root(start_path: Optional[str] = None) -> Optional[Path]:
    """The PARENT of the archives directory.

    craiid_compression_validation_v4 wants the directory that CONTAINS
    "archives" and appends the name itself. Since the archives no longer sit
    under the project root, "the root" for that purpose is now sage_data.
    """
    d = archives_dir(start_path)
    return d.parent if d is not None else None
