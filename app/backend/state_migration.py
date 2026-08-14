#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""state_migration.py -- move the owner's data into sage_data, once.

The owner's chat, archives, uploads and downloads used to live in the INSTALL
directory. They belong in sage_data with everything else that belongs to a
person. state_paths.py now points at the new home; this moves what is already
in the old one.

RULES, in the order they matter
-------------------------------
1. Never destroy the source until the destination is verified. Every file is
   copied, its size compared, and only then unlinked. A crash at any point
   leaves a readable copy in at least one place -- never neither.
2. Never overwrite. If a file already exists at the destination it is left
   alone and the source is kept, and the pair is reported. Two files with the
   same name are a question for a person, not something to resolve by
   picking one.
3. Never guess. If the old and new locations are the same directory (the
   read-only-install case, where STATE_DIR already resolved to sage_data),
   there is nothing to do and nothing is touched.
4. Say what happened. A migration that moves someone's conversations in
   silence is indistinguishable, from the outside, from one that loses them.

Re-running is safe: anything already moved is simply not there any more.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Dict, List, Tuple

MARKER = ".owner_data_moved.json"
SCHEMA_VERSION = 1


def _pairs() -> List[Tuple[str, Path, Path]]:
    from state_paths import (CHAT_MEMORY_FILE, ARCHIVES_DIR, UPLOADS_DIR,
                             DOWNLOADS_DIR, OLD_CHAT_MEMORY_FILE,
                             OLD_ARCHIVES_DIR, OLD_UPLOADS_DIR,
                             OLD_DOWNLOADS_DIR)
    return [
        ("chat", OLD_CHAT_MEMORY_FILE, CHAT_MEMORY_FILE),
        ("archives", OLD_ARCHIVES_DIR, ARCHIVES_DIR),
        ("uploads", OLD_UPLOADS_DIR, UPLOADS_DIR),
        ("downloads", OLD_DOWNLOADS_DIR, DOWNLOADS_DIR),
    ]


def _marker_path() -> Path:
    from state_paths import CHAT_MEMORY_FILE
    return CHAT_MEMORY_FILE.parent / MARKER


def already_done() -> bool:
    p = _marker_path()
    try:
        return p.exists() and bool(json.loads(p.read_text(encoding="utf-8")).get("done"))
    except Exception:
        # An unreadable marker means "unknown", which must read as "not done".
        # Re-running is safe; skipping when files are still in the old place
        # is not.
        return False


def _move_file(src: Path, dst: Path, out: Dict) -> None:
    if dst.exists():
        # Rule 2. Both copies survive and the person is told.
        out["conflicts"].append(str(dst))
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    size = src.stat().st_size
    tmp = dst.with_name(dst.name + ".migrating")
    try:
        shutil.copy2(str(src), str(tmp))
        if tmp.stat().st_size != size:
            raise IOError("copied %d of %d bytes" % (tmp.stat().st_size, size))
        os.replace(str(tmp), str(dst))
    except Exception as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        out["failed"] += 1
        out["errors"].append("%s: %s" % (src.name, type(e).__name__))
        return
    # Only now is the original expendable.
    try:
        src.unlink()
    except OSError:
        # The copy is in place and verified; a source that will not delete is
        # untidy, not dangerous. Do not report it as a failure to move.
        out["left_behind"].append(str(src))
    out["moved"] += 1
    out["bytes"] += size


def run(dry_run: bool = False) -> Dict:
    """Move what is in the old place. Returns a report; never raises."""
    out = {"moved": 0, "bytes": 0, "failed": 0, "conflicts": [],
           "left_behind": [], "errors": [], "sections": [], "ran": False,
           "ok": True, "same_place": False}
    try:
        pairs = _pairs()
    except Exception as e:
        out["ok"] = False
        out["errors"].append("paths unavailable: %s" % e)
        return out

    for name, old, new in pairs:
        try:
            if old.resolve() == new.resolve():
                out["same_place"] = True
                continue
        except Exception:
            pass
        if not old.exists():
            continue
        srcs = [old] if old.is_file() else [f for f in old.rglob("*") if f.is_file()]
        if not srcs:
            continue
        out["ran"] = True
        out["sections"].append(name)
        if dry_run:
            out["moved"] += len(srcs)
            continue
        for f in srcs:
            rel = Path(f.name) if old.is_file() else f.relative_to(old)
            _move_file(f, (new if old.is_file() else new / rel), out)
        # Tidy the emptied directory, but only if it really is empty.
        if old.is_dir():
            try:
                old.rmdir()
            except OSError:
                pass

    out["ok"] = out["failed"] == 0
    if out["ok"] and not dry_run:
        _write_marker(out)
    return out


def _write_marker(result: Dict) -> bool:
    p = _marker_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "version": SCHEMA_VERSION, "done": True, "at": int(time.time()),
            "moved": result.get("moved", 0),
            "sections": result.get("sections", []),
            "conflicts": result.get("conflicts", []),
        }, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def summary(result: Dict) -> str:
    if not result.get("ran"):
        return "nothing to move"
    bits = ["%d file(s) moved into your data folder" % result.get("moved", 0)]
    if result.get("sections"):
        bits.append("from: " + ", ".join(result["sections"]))
    if result.get("conflicts"):
        bits.append("%d left in place because a file of the same name was "
                    "already there" % len(result["conflicts"]))
    if result.get("failed"):
        bits.append("%d FAILED and were left where they were" % result["failed"])
    return "; ".join(bits)


def migrate_owner_data_once() -> Dict:
    """Startup entry point. Runs at most once, and never breaks a boot."""
    try:
        if already_done():
            return {"ran": False, "ok": True}
        r = run()
        if r.get("ran") or r.get("failed"):
            print("[STATE] owner data: " + summary(r), flush=True)
            for c in r.get("conflicts", []):
                print("[STATE]   kept both copies: " + c, flush=True)
        return r
    except Exception as e:
        print("[STATE] owner-data move did not run (%s: %s)"
              % (type(e).__name__, e), flush=True)
        return {"ran": False, "ok": False}
