#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_craiid_archive_dir.py -- CRAIID reads archives where they actually are.

WHAT WAS WRONG (Todd, 2026-08-23)

    "the OpsMan snapshot is skipped on any PC but my desktop, because it cannot
     find the archives dir, since it is looking in the root for it instead of
     in sage_data"

sage_daemon.py had:

    # CRAIID path constants (set at module init in Block B)
    _ARCHIVES_DIR = Path(__file__).resolve().parent.parent / "archives"

backend/ up to the install directory -- where archives lived until the
2026-08-13 move into sage_data. Assigned once, never reassigned, and there is
no "Block B" anywhere in that file. The comment said somebody else had handled
it, so nobody looked.

WHY IT WAS NOT COSMETIC

_job_ops_snapshot returns "archives dir not found -- skipping" and ops_mode
never goes active. The anticipatory pre-warm margin only lowers the fatigue
threshold WHEN ops_mode is active, so CRAIID never began the warm handoff
BEFORE the context cliff. Context ran to the cliff and handoffs fired back to
back, each slower than the last. Reported as a context-pruning problem; it was
a path problem two layers down.

WHY IT ONLY SHOWED UP ON OTHER MACHINES

The desktop still had <install>/archives from before the move -- 262 real
archive files -- so the wrong path found a real corpus and worked by accident.
A clean machine has nothing there. The most dangerous kind of wrong path is one
that happens to be right on the developer's box.

AND IT WAS SELF-PERPETUATING

archivist_compression_worker asked craiid_paths for a directory that CONTAINS
archives (require_content defaults True), got None on a machine that had not
archived anything yet, walked the tree, found nothing, and then CREATED
<install>/archives. craiid_paths' own walk-up looks for a folder named
"archives" -- so once that existed, every later resolution found it and
preferred it. A fresh machine talked itself into the pre-migration layout and
stayed there.

    python test_craiid_archive_dir.py
"""
import ast
import io
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "craiid"))

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


def _read(*parts):
    return io.open(os.path.join(_HERE, *parts), encoding="utf-8").read()


DAEMON = _read("sage_daemon.py")
CFD = _read("context_fatigue_detector.py")
ARCH = _read("craiid", "archivist_compression_worker.py")


# =============================================================================
print("=== 1. Nobody rebuilds the path from their own file location ===")
# =============================================================================
# The idiom itself, in the code (not the comments explaining its removal).
def _code_only(src):
    out = []
    for line in src.splitlines():
        out.append("" if line.lstrip().startswith("#") else line.split("#", 1)[0])
    return "\n".join(out)


_D = _code_only(DAEMON)
ok("the daemon no longer computes archives from __file__ at module level",
   "_ARCHIVES_DIR = Path(__file__)" not in _D,
   "that single line is the whole bug")
ok("...it resolves through a function instead",
   "_ARCHIVES_DIR = _resolve_archives_dir()" in _D)
ok("...which asks state_paths first",
   "from state_paths import ARCHIVES_DIR" in _D,
   "state_paths is the single source of truth for where user data lives")
# Checked against the CODE-ONLY view, and paired with the opposite assertion.
# The first version of this said '"Block B" not in DAEMON' and went red on the
# replacement comment, which quotes the false claim as history -- the same
# comment-vs-code trap this project keeps hitting, in the very test written to
# stop a comment from hiding a bug. Keeping the wrong label visible as history
# is the house rule (procedural_memory's SYSTEM TIER label was kept the same
# way); what must not survive is anyone reading it as still true.
ok("no live code relies on a 'Block B' that does not exist",
   "Block B" not in _D,
   "there is no Block B anywhere in sage_daemon.py")
ok("...and the false claim is preserved as history, not deleted",
   "Block B" in DAEMON,
   "deleting it loses the reason this went unnoticed for two releases")

ok("the fatigue detector's CLI default asks too",
   "_default_archives_dir()" in _code_only(CFD))
ok("...and that helper reads state_paths",
   "from state_paths import ARCHIVES_DIR" in CFD)

ok("the archivist asks for the CANONICAL dir before walking the tree",
   "require_content=False" in _code_only(ARCH),
   "without this it returns None on a machine with no archives yet, walks, "
   "finds nothing, and CREATES the install-directory folder -- which every "
   "later walk-up then finds and prefers")


# =============================================================================
print("\n=== 2. Every reader lands on the same directory, for real ===")
# =============================================================================
try:
    from state_paths import ARCHIVES_DIR
    _canon = str(ARCHIVES_DIR)
except Exception as _e:
    _canon = None
ok("state_paths resolves", bool(_canon), _e if not _canon else "")

if _canon:
    # The daemon's resolver, lifted from the shipped source so this cannot
    # test a copy that has drifted.
    from pathlib import Path
    _t = ast.parse(DAEMON)
    _fn = next((n for n in _t.body
                if isinstance(n, ast.FunctionDef)
                and n.name == "_resolve_archives_dir"), None)
    ok("_resolve_archives_dir was found in sage_daemon.py", _fn is not None)
    if _fn:
        _ns = {"Path": Path, "__file__": os.path.join(_HERE, "sage_daemon.py")}
        exec(compile(ast.get_source_segment(DAEMON, _fn), "<daemon>", "exec"), _ns)
        _got = str(_ns["_resolve_archives_dir"]())
        ok("the daemon's ops snapshot reads the canonical dir",
           _got == _canon, "%s != %s" % (_got, _canon))

    import context_fatigue_detector as _cfd
    ok("the fatigue detector's default matches",
       str(_cfd._default_archives_dir()) == _canon,
       str(_cfd._default_archives_dir()))

    import craiid_paths as _cp
    ok("craiid_paths agrees on where archives belong",
       str(_cp.archives_dir(require_content=False)) == _canon,
       str(_cp.archives_dir(require_content=False)))

    # THE ONE THAT MATTERS: the readers and the WRITER must agree. Two correct
    # halves that disagree is the same silence as one wrong half.
    try:
        import sage_engine as _se
        _writes = str(_se._archive_folder(None))
        ok("...and it is where sage_engine WRITES the owner's archives",
           _writes == _canon, "writes %s, readers expect %s" % (_writes, _canon))
    except Exception as _e2:
        ok("sage_engine could be asked where it writes", False, _e2)


# =============================================================================
print("\n=== 3. The install directory is not the answer any more ===")
# =============================================================================
_install_archives = os.path.join(os.path.dirname(_HERE), "archives")
if _canon:
    ok("the canonical dir is NOT inside the install directory",
       not os.path.abspath(_canon).startswith(
           os.path.abspath(os.path.dirname(_HERE)) + os.sep),
       "%s is under the install tree -- user content moved out of there on "
       "2026-08-13" % _canon)

# Present is not an error (a legacy copy is exactly what masked this), but it
# IS worth naming, because its presence is what makes the wrong path look
# right on one machine and fail everywhere else.
if os.path.isdir(_install_archives):
    import glob
    _n = len(glob.glob(os.path.join(_install_archives, "archive_*.json")))
    print("  ----  a legacy %s still exists with %d archive file(s)."
          % (_install_archives, _n))
    print("        Harmless now that nothing reads it, and it is what made")
    print("        this bug invisible on the machine that predates the move.")


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
