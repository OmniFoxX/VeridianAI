#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_state_migration.py -- moving the owner's data must not be able to lose it.

The owner's chat, archives, uploads and downloads moved out of the install
directory into sage_data. That is a one-time move of files that may be the only
copy someone has, so the interesting assertions are the ones about what it
REFUSES to do: it never overwrites, it never deletes a source it has not first
copied and verified, and it never touches anything when old and new resolve to
the same place.

The whole test runs against fabricated paths injected into state_paths. An
earlier ad-hoc version ran against the REAL install directory and relocated 35
files out of the checkout -- all of them test debris, but the lesson stands:
a migration test that uses the live paths is a migration, not a test.

    python test_state_migration.py
"""
import io
import os
import shutil
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path                                     # noqa: E402
import state_paths as sp                                     # noqa: E402
import state_migration as sm                                 # noqa: E402

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


def fresh():
    """A fake old/new pair, wired into state_paths for the duration."""
    root = Path(tempfile.mkdtemp(prefix="vai_statemig_"))
    old, new = root / "install", root / "sage_data"
    (old / "archives" / "nested").mkdir(parents=True)
    (old / "uploads").mkdir(parents=True)
    (old / "downloads").mkdir(parents=True)
    sp.OLD_CHAT_MEMORY_FILE = old / "chat_memory.json"
    sp.OLD_ARCHIVES_DIR = old / "archives"
    sp.OLD_UPLOADS_DIR = old / "uploads"
    sp.OLD_DOWNLOADS_DIR = old / "downloads"
    sp.CHAT_MEMORY_FILE = new / "chat_memory.json"
    sp.ARCHIVES_DIR = new / "archives"
    sp.UPLOADS_DIR = new / "uploads"
    sp.DOWNLOADS_DIR = new / "downloads"
    return root, old, new


CHAT = b"the owner's only conversation"
DEEP = b"an archive three folders down"

print("=== 1. It moves what is there, including nested files ===")
root, old, new = fresh()
(old / "chat_memory.json").write_bytes(CHAT)
(old / "archives" / "a.json").write_bytes(b"archive one")
(old / "archives" / "nested" / "deep.json").write_bytes(DEEP)
r = sm.run()
ok("reports success", r["ok"] is True, r.get("errors"))
ok("the conversation arrived intact",
   (new / "chat_memory.json").read_bytes() == CHAT)
ok("nested structure is preserved",
   (new / "archives" / "nested" / "deep.json").read_bytes() == DEEP)
ok("the old copies are gone", not (old / "chat_memory.json").exists())
ok("it counted what it moved", r["moved"] == 3, r)
ok("and named the sections", set(r["sections"]) == {"chat", "archives"}, r["sections"])

print("\n=== 2. It never overwrites ===")
root2, old2, new2 = fresh()
(old2 / "uploads").mkdir(exist_ok=True)
(old2 / "uploads" / "scan.txt").write_bytes(b"the old one")
(new2 / "uploads").mkdir(parents=True)
(new2 / "uploads" / "scan.txt").write_bytes(b"the one already here")
r2 = sm.run()
ok("the destination file is untouched",
   (new2 / "uploads" / "scan.txt").read_bytes() == b"the one already here")
ok("AND the source is kept, not discarded",
   (old2 / "uploads" / "scan.txt").read_bytes() == b"the old one")
ok("the collision is reported, not swallowed", len(r2["conflicts"]) == 1, r2)
ok("it is not counted as moved", r2["moved"] == 0, r2)

print("\n=== 3. A dry run writes nothing ===")
root3, old3, new3 = fresh()
(old3 / "chat_memory.json").write_bytes(CHAT)
d = sm.run(dry_run=True)
ok("it counts", d["moved"] == 1, d)
ok("the source is still there", (old3 / "chat_memory.json").exists())
ok("the destination was not created", not (new3 / "chat_memory.json").exists())
ok("no marker was left", not sm.already_done())

print("\n=== 4. Same place in, nothing done ===")
# The read-only-install case: STATE_DIR already resolved to sage_data, so old
# and new are the same directory and there is nothing to move.
root4, old4, new4 = fresh()
new4.mkdir(parents=True, exist_ok=True)
sp.OLD_CHAT_MEMORY_FILE = sp.CHAT_MEMORY_FILE
sp.OLD_ARCHIVES_DIR = sp.ARCHIVES_DIR
sp.OLD_UPLOADS_DIR = sp.UPLOADS_DIR
sp.OLD_DOWNLOADS_DIR = sp.DOWNLOADS_DIR
sp.CHAT_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
sp.CHAT_MEMORY_FILE.write_bytes(CHAT)
r4 = sm.run()
ok("it notices old and new are the same", r4["same_place"] is True, r4)
ok("nothing was moved", r4["moved"] == 0, r4)
ok("the file is still readable where it was",
   sp.CHAT_MEMORY_FILE.read_bytes() == CHAT)

print("\n=== 5. It runs once, and re-running is harmless ===")
root5, old5, new5 = fresh()
(old5 / "chat_memory.json").write_bytes(CHAT)
sm.run()
ok("the marker records it", sm.already_done() is True)
before = (new5 / "chat_memory.json").read_bytes()
again = sm.run()
ok("a second run moves nothing", again["moved"] == 0, again)
ok("and the data is unchanged", (new5 / "chat_memory.json").read_bytes() == before)
ok("the startup entry point short-circuits",
   sm.migrate_owner_data_once().get("ran") is False)

print("\n=== 6. An unreadable marker means 'not done', not 'done' ===")
# Guessing "done" would strand files in the old location silently. Guessing
# "not done" costs one wasted scan.
mp = sp.CHAT_MEMORY_FILE.parent / sm.MARKER
mp.write_text("{ this is not json", encoding="utf-8")
ok("a corrupt marker does not claim completion", sm.already_done() is False)

print("\n=== 7. Empty old locations are not a migration ===")
root7, old7, new7 = fresh()
r7 = sm.run()
ok("nothing to do is reported as nothing to do", r7["ran"] is False, r7)
ok("summary says so", sm.summary(r7) == "nothing to move", sm.summary(r7))

for _r in (root, root2, root3, root4, root5, root7):
    shutil.rmtree(_r, ignore_errors=True)

_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
