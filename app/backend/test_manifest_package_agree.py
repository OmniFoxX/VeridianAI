#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_manifest_package_agree.py -- the manifest may only expect what ships.

THE TRAP, WHICH THIS PROJECT HAS NOW FALLEN INTO SIX TIMES

build_integrity.py hashes the working tree. The installed app checks those
hashes against what it actually received. When a file is in the manifest and
not in the package, the app reports the ENTIRE build "modified".

The cruel part is the fix that does not work. Re-running genmanifest is the
obvious response and it changes nothing, because the manifest was never the
broken half -- regenerating it cannot conjure the file into the package.
Uninstall, rebuild, reinstall: still yellow. build_integrity.py has carried a
comment about this since v2.13.17, naming four files it caught this way:

    RULE: excluding from extraFiles and excluding from the manifest are two
    halves of one decision. Doing only the first silently arms the tamper
    switch.

The rule was written down. Nothing enforced it. So v2.16.1 added
_bust_cache.py to the project root -- build tooling, correctly not in
extraFiles -- and armed the switch again.

WHY THIS CHECKS THE PACKAGE AND NOT extraFiles

The first version of this test asserted "every root file is named in
extraFiles or excluded", and it was wrong on both sides. VeridianAI.exe,
resources.pak, the swiftshader DLLs and the rest of the Electron shell ship
WITHOUT appearing in extraFiles -- electron-builder emits them itself. And
build_manifest.json is legitimately both shipped and excluded from hashing,
because a manifest cannot contain its own hash. A rule that flags twenty-five
correct files to catch one wrong one gets switched off, and then catches
nothing.

So this asks the only question that actually matters, against the only thing
that can answer it: for every path the manifest hashes, is that file in the
built package? That is precisely what the installed app computes -- run here,
where it is cheap to fix, instead of after a build-install-uninstall cycle on
another machine.

NO BUILD, NO ANSWER -- and it says so rather than passing quietly.

    python test_manifest_package_agree.py
"""
import ast
import io
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


BI = io.open(os.path.join(_HERE, "build_integrity.py"), encoding="utf-8").read()


def _exclude_files():
    """EXCLUDE_FILES off the AST -- importing build_integrity drags in config
    and the data dir, and this only needs one literal."""
    for n in ast.walk(ast.parse(BI)):
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "EXCLUDE_FILES"
                for t in n.targets):
            return set(ast.literal_eval(n.value))
    return set()


EXCLUDED = _exclude_files()


# =============================================================================
print("=== 1. The rule is written down, and the known offenders are named ===")
# =============================================================================
ok("EXCLUDE_FILES was found and parsed", bool(EXCLUDED), len(EXCLUDED))
ok("the pairing rule is stated in build_integrity.py",
   "two halves of one decision" in BI,
   "that comment is what taught this test what to check; losing it loses "
   "the reason")
ok("_bust_cache.py is accounted for", "_bust_cache.py" in EXCLUDED,
   "the v2.16.1 instance -- a root-level build tool that armed the switch")
ok("...as are the earlier ones",
   {"_bump_version.py", "make_release.ps1", "bundle_python.ps1",
    "bundle_playwright.ps1"} <= EXCLUDED,
   sorted({"_bump_version.py", "make_release.ps1", "bundle_python.ps1",
           "bundle_playwright.ps1"} - EXCLUDED))


# =============================================================================
print("\n=== 2. Every hashed path exists in the built package ===")
# =============================================================================
_MANIFEST = os.path.join(_ROOT, "build_manifest.json")
_PKG = os.path.join(_ROOT, "dist", "win-unpacked")

ok("a manifest exists to check", os.path.exists(_MANIFEST), _MANIFEST)

if not os.path.isdir(_PKG):
    # Stated, not skipped silently. "No build here" is a real and common
    # answer -- the portable and personal trees never produce one -- but a
    # test that returns green for "I did not look" is how the check stops
    # meaning anything.
    print("  ----  no built package at dist/win-unpacked, so the packaged")
    print("        half cannot be checked from this tree. Run this in the")
    print("        Store tree after a build; that is the one that ships.")
else:
    # The file map is NESTED: {"manifest": {..., "files": {path: sha}},
    # "signature_b64": ...}. The first version of this guessed a top-level
    # "files" key, found nothing, and then reported "nothing is absent from
    # the package" -- passing because it had checked zero paths. An assertion
    # that cannot go red is worse than no assertion, so the count is checked
    # FIRST and the emptiness is the failure.
    _doc = json.load(io.open(_MANIFEST, encoding="utf-8"))
    _entries = ((_doc.get("manifest") or {}).get("files")
                or _doc.get("files") or {})
    _paths = [p for p in (_entries.keys() if isinstance(_entries, dict)
                          else [e.get("path") for e in _entries]) if p]
    ok("the manifest's file map was found and is not empty", len(_paths) > 50,
       "found %d paths -- if this is 0 the key moved, and every check below "
       "it would pass without looking at anything" % len(_paths))

    _missing = []
    for _p in _paths:
        _local = os.path.join(_PKG, _p.replace("/", os.sep))
        if not os.path.exists(_local):
            _missing.append(_p)

    # STALE PACKAGE vs REAL TRAP -- tell them apart instead of crying wolf.
    #
    # A file added to the source since the last build is missing from the
    # package for a completely ordinary reason, and reporting that as the
    # tamper-switch bug trains people to ignore this check. The distinction is
    # available: if every missing file is NEWER than the package itself, the
    # package is simply out of date. If even one predates it, the build had a
    # chance to include that file and did not -- which is the real thing.
    try:
        _pkg_mtime = os.path.getmtime(os.path.join(_PKG, "build_manifest.json"))
    except OSError:
        _pkg_mtime = os.path.getmtime(_PKG)

    _older, _newer = [], []
    for _p in _missing:
        _srcf = os.path.join(_ROOT, _p.replace("/", os.sep))
        try:
            (_newer if os.path.getmtime(_srcf) > _pkg_mtime else _older).append(_p)
        except OSError:
            _older.append(_p)        # cannot date it -> treat as the serious case

    if _newer and not _older:
        print("  ----  %d hashed file(s) are absent from dist/win-unpacked, and"
              % len(_newer))
        print("        every one is NEWER than the package -- it is simply out")
        print("        of date. Rebuild and this check answers properly.")
        for _p in _newer[:8]:
            print("          %s" % _p)

    ok("nothing the manifest hashes is missing from a CURRENT package",
       not _older,
       "%d file(s) predate the package and still are not in it, which means "
       "the build could have included them and did not:\n          %r\n"
       "          This is the trap: the installed app reports the whole build "
       "'modified', and re-running genmanifest will NOT fix it -- the manifest "
       "is not the broken half.\n"
       "          For each: SHIP it (add to extraFiles in "
       "electron/package.json) or EXCLUDE it (add to EXCLUDE_FILES in "
       "build_integrity.py)." % (len(_older), _older[:12]))


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
