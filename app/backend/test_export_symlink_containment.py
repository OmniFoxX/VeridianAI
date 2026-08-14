# -*- coding: utf-8 -*-
"""v2.15 -- an export must not follow a symlink out of its own profile.

data_export._files_under walks with rglob + is_file(), and is_file() FOLLOWS
links. A symlink planted inside a profile's directory would be followed into
whatever it points at -- another profile's store, the recovery key -- and packed
into that profile's export. Same shape as the export-containment leak this
module exists to prevent, arriving by a different route.

Planting one needs filesystem access rather than the app, so this is hardening
rather than a reachable hole. It is still worth a test: the failure mode is
silent, and "the export contains someone else's data" is the exact claim the
per-profile encryption work is built to make impossible.

Windows needs Developer Mode or elevation to create symlinks. If that fails,
the symlink cases SKIP rather than pass -- a skipped test that says so is
honest; a passing test that never created a link is not.

    python test_export_symlink_containment.py
"""
import os
import sys
import tempfile
from pathlib import Path

import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data_export as de

_results = []
_skipped = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


def skip(name, why):
    _skipped.append(name)
    print("  SKIP  " + name + "  (" + why + ")")


tmp = Path(tempfile.mkdtemp(prefix="exp_sym_"))
profile = tmp / "profile"; profile.mkdir()
outside = tmp / "somebody_else"; outside.mkdir()

(profile / "mine.txt").write_text("my own data", encoding="utf-8")
(profile / "sub").mkdir()
(profile / "sub" / "nested.txt").write_text("also mine", encoding="utf-8")
(outside / "SECRET.key").write_text("another profile's key", encoding="utf-8")


def _try_symlink(link: Path, target: Path, dir_link=False):
    try:
        link.symlink_to(target, target_is_directory=dir_link)
        return link.is_symlink()
    except (OSError, NotImplementedError):
        return False


print("=== the ordinary case still works ===")
found = de._files_under(profile)
names = sorted(f.name for f in found)
ok("real files are still collected", names == ["mine.txt", "nested.txt"], names)

print("\n=== a symlink to a file outside the profile ===")
link = profile / "innocent.txt"
if _try_symlink(link, outside / "SECRET.key"):
    found = de._files_under(profile)
    names = sorted(f.name for f in found)
    ok("the symlink is not collected", "innocent.txt" not in names, names)
    ok("nothing resolves outside the profile",
       all(str(profile.resolve()) in str(f.resolve()) for f in found),
       [str(f.resolve()) for f in found])
    ok("the real files are still there", names == ["mine.txt", "nested.txt"], names)
    link.unlink()
else:
    skip("symlink-to-file cases", "symlink creation not permitted here")

print("\n=== a symlink to a DIRECTORY outside the profile ===")
dlink = profile / "shortcut"
if _try_symlink(dlink, outside, dir_link=True):
    found = de._files_under(profile)
    names = sorted(f.name for f in found)
    ok("the linked directory's contents are not collected",
       "SECRET.key" not in names, names)
    ok("still exactly the two real files", names == ["mine.txt", "nested.txt"], names)
    try:
        dlink.unlink()
    except OSError:
        os.rmdir(dlink)
else:
    skip("symlink-to-directory cases", "symlink creation not permitted here")

print("\n=== a symlink whose target does not exist ===")
broken = profile / "broken.txt"
if _try_symlink(broken, outside / "does_not_exist"):
    found = de._files_under(profile)
    ok("a broken link neither crashes the walk nor appears",
       "broken.txt" not in [f.name for f in found], [f.name for f in found])
    broken.unlink()
else:
    skip("broken-symlink case", "symlink creation not permitted here")

print("\n=== the profile root itself being a link ===")
rootlink = tmp / "root_shortcut"
if _try_symlink(rootlink, outside, dir_link=True):
    ok("a symlinked root exports nothing", de._files_under(rootlink) == [],
       de._files_under(rootlink))
    try:
        rootlink.unlink()
    except OSError:
        os.rmdir(rootlink)
else:
    skip("symlinked-root case", "symlink creation not permitted here")

_p = sum(1 for _, c in _results if c)
_f = len(_results) - _p
print("\n%d/%d passed, %d skipped." % (_p, len(_results), len(_skipped)))
if _skipped:
    print("NOTE: %d group(s) skipped because this account cannot create symlinks."
          % len(_skipped))
    print("      On Windows that needs Developer Mode or an elevated shell.")
sys.exit(1 if _f else 0)
