#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_craiid_archive_paths.py -- CRAIID reads archives from sage_data.

THE BUG THIS LOCKS OUT (v2.15.2)
On 2026-08-13 user content moved out of the install directory into sage_data.
state_paths.py records both locations. CRAIID did not follow: six places found
the archives by walking up looking for a folder named "archives", and
craiid_author.py hardcoded `_ROOT_DIR / "archives"`.

After the move every one of them resolved to the install directory's leftover
folder -- which the migration had emptied. So the Archivist built its
compression key from ZERO archives and reported no error, because an empty
corpus is not an error. It is just an empty corpus.

The tell was in craiid_author.py itself: chat memory, reconstructs, the VLTS
store and the logs had all been moved into the data dir. `_ARCHIVES_DIR` was
the one line left behind -- and it was the INPUT.

    python test_craiid_archive_paths.py
"""
import io
import os
import re
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_CRAIID = os.path.join(_HERE, "craiid")
sys.path.insert(0, _HERE)
sys.path.insert(0, _CRAIID)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


def read(rel):
    return io.open(os.path.join(_CRAIID, rel), encoding="utf-8").read()


# =============================================================================
print("=== 1. Every resolver asks state_paths before it searches ===")
# =============================================================================
SITES = [
    "craiid_author.py",
    "audit_archives_lite.py",
    "audit_archives_deep.py",
    "audit_archives_personal_v2.py",
    "archivist_compression_worker.py",
    "craiid_compression_validation_v4.py",
]
for f in SITES:
    src = read(f)
    ok(f"{f} consults craiid_paths", "craiid_paths" in src)

AUTHOR = read("craiid_author.py")
ok("craiid_author no longer trusts _ROOT_DIR / 'archives' as final",
   "_resolve_archives" in AUTHOR and "_ARCHIVES_DIR = Path(_resolved)" in AUTHOR)
# The old line stays as an initial default; what matters is that it is not the
# last word. Anything that reassigns it after the resolver would reopen the bug.
_after = AUTHOR.split("_ARCHIVES_DIR = Path(_resolved)", 1)[1]
ok("...and nothing overwrites it afterwards",
   not re.search(r"^_ARCHIVES_DIR\s*[:=]", _after, flags=re.M))


# =============================================================================
print("\n=== 2. The resolver picks the right directory ===")
# =============================================================================
import craiid_paths  # noqa: E402


def layout(new_files=0, old_files=0):
    """Build a fake install + sage_data pair and point craiid_paths at it."""
    d = Path(tempfile.mkdtemp(prefix="craiid_paths_"))
    new = d / "sage_data" / "archives"
    old = d / "install" / "archives"
    new.mkdir(parents=True)
    old.mkdir(parents=True)
    for i in range(new_files):
        (new / f"archive_{i:04d}.json").write_text("{}", encoding="utf-8")
    for i in range(old_files):
        (old / f"archive_{i:04d}.json").write_text("{}", encoding="utf-8")
    craiid_paths._from_state_paths = lambda: (new, old)   # noqa: E731
    return d, new, old


d, new, old = layout(new_files=3, old_files=0)
ok("archives in sage_data are found", craiid_paths.archives_dir() == new,
   craiid_paths.archives_dir())

# Todd's actual state on 2026-08-17: he copied archives BACK into the install
# directory to get CRAIID working again, so both locations are populated.
# sage_data must win, or the fix would quietly keep reading the copy.
d, new, old = layout(new_files=3, old_files=3)
ok("with BOTH populated, sage_data wins", craiid_paths.archives_dir() == new,
   craiid_paths.archives_dir())

# A move that started and stopped: the new folder exists but is empty.
d, new, old = layout(new_files=0, old_files=3)
ok("a half-finished migration still finds the archives",
   craiid_paths.archives_dir() == old, craiid_paths.archives_dir())

# The empty folder is the whole trap: "the directory exists" was never the
# question worth asking.
#
# The walk-up has to be stubbed out for this one. Run inside a real install it
# would climb out of the temp layout, find the machine's ACTUAL archives and
# pass for the wrong reason -- which is how a test starts depending on the
# machine it runs on. (It did exactly that on the first run here.)
d, new, old = layout(new_files=0, old_files=0)
_real_walk = craiid_paths._walk_up
craiid_paths._walk_up = lambda *a, **k: None    # noqa: E731
try:
    ok("an empty pair does not pretend to have found a corpus",
       not craiid_paths._has_archives(craiid_paths.archives_dir()))
    ok("...but still names the canonical location, not None",
       craiid_paths.archives_dir() == new, craiid_paths.archives_dir())
finally:
    craiid_paths._walk_up = _real_walk

d, new, old = layout(new_files=2, old_files=0)
ok("archives_root() is the PARENT, for callers that append 'archives'",
   craiid_paths.archives_root() == new.parent, craiid_paths.archives_root())


# =============================================================================
print("\n=== 3. 'Folder exists' is not 'folder has archives' ===")
# =============================================================================
d = Path(tempfile.mkdtemp(prefix="craiid_empty_"))
(d / "archives").mkdir()
ok("an empty archives folder does not count as populated",
   craiid_paths._has_archives(d / "archives") is False)
(d / "archives" / "notes.txt").write_text("x", encoding="utf-8")
ok("...nor does one holding unrelated files",
   craiid_paths._has_archives(d / "archives") is False)
(d / "archives" / "archive_0001.json").write_text("{}", encoding="utf-8")
ok("...but one real archive file does",
   craiid_paths._has_archives(d / "archives") is True)
ok("a missing directory is handled, not raised",
   craiid_paths._has_archives(d / "nope") is False)

# The glob has to match what sage_engine actually WRITES. An earlier draft of
# craiid_paths used "archives_*.json" -- taken from craiid_author's docstring,
# which is wrong -- and reported a directory holding 245 real archives as
# empty. A glob that matches nothing is indistinguishable from an empty folder,
# which is the failure this whole module exists to prevent.
ok("the glob matches sage_engine's real naming, archive_<ts>.json",
   craiid_paths._ARCHIVE_GLOB == "archive_*.json", craiid_paths._ARCHIVE_GLOB)
d2 = Path(tempfile.mkdtemp(prefix="craiid_naming_"))
(d2 / "archives").mkdir()
(d2 / "archives" / "archive_20260508_114756.json").write_text("{}", encoding="utf-8")
ok("...so a real archive filename is recognised",
   craiid_paths._has_archives(d2 / "archives") is True)
(d2 / "archives" / ".evidence_deadbeef.dat").write_text("x", encoding="utf-8")
(d2 / "archives" / ".archive_titles.dat").write_text("x", encoding="utf-8")
ok("...and the dot-prefixed sidecars are not mistaken for archives",
   len(list((d2 / "archives").glob(craiid_paths._ARCHIVE_GLOB))) == 1)


# =============================================================================
print("\n=== 4. Standalone runs still work ===")
# =============================================================================
# The audit scripts are also run by hand from a terminal, where state_paths may
# not import. The walk-up has to survive as the last resort.
craiid_paths._from_state_paths = lambda: (None, None)   # noqa: E731
d = Path(tempfile.mkdtemp(prefix="craiid_standalone_"))
(d / "archives").mkdir()
(d / "archives" / "archive_0001.json").write_text("{}", encoding="utf-8")
sub = d / "a" / "b"
sub.mkdir(parents=True)
ok("the walk-up still finds a project from a subdirectory",
   craiid_paths.archives_dir(str(sub)) == d / "archives",
   craiid_paths.archives_dir(str(sub)))


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
