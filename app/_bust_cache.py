#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_bust_cache.py -- derive every ?v= from the file's own contents.

WHY THIS EXISTS
---------------
A cache-bust answers exactly one question: has THIS FILE changed? It was being
answered by hand, with a version number, and hand-maintained answers drift.

By v2.16.0 index.html carried busts spanning 2.9.10 to 2.16.1, and one of them
was `chat.js?v=2.15.2a` -- a value typed mid-development purely to force a
browser past a stale copy. Worse than untidy: the failure mode of a bust that
did NOT get bumped is a browser serving yesterday's module, which looks exactly
like the feature you just shipped not working. That is an expensive hour every
time it happens.

Bumping every bust on every release is the other wrong answer: it forces a
re-download of every module including the ones that did not change.

A CONTENT HASH IS THE ONLY ANSWER THAT IS ALWAYS RIGHT. It changes when, and
only when, the file changes. Nobody has to remember, and nobody can be wrong.

WHY NOT READ THE HASHES OUT OF build_manifest.json
--------------------------------------------------
genmanifest already hashes every file, so reusing it looks like the obvious
move -- but it creates an ordering trap. Rewriting index.html CHANGES
index.html, so the manifest that supplied the hashes is stale the moment it is
used, and the correct sequence (bust, THEN genmanifest) is exactly the one a
manifest-reading tool would invite you to get backwards. Hashing the file
directly has no such dependency and gives an identical answer.

EVERY REFERENCE, NOT JUST index.html
------------------------------------
settings.js builds two of these URLs in JavaScript:

    "/static/css/hljs-github.css?v=11.9.0"

If index.html said one thing and settings.js another, the same file would be
fetched under two URLs -- a double download, and a theme swap pulling the copy
that was NOT refreshed. So this scans index.html AND frontend/js/*.js, and
every reference to a given file gets the same hash.

    python _bust_cache.py            rewrite what is stale
    python _bust_cache.py --check    report only; exit 1 if anything is stale
    python _bust_cache.py --verbose  also list what was already correct

Pure-ASCII source.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent
FRONTEND = PROJECT_ROOT / "frontend"

HASH_CHARS = 10

# src="/static/js/chat.js?v=2.16.1"  /  href="/static/css/styles.css?v=2.11.12"
# Also matches the same URLs written as JS string literals.
_REF = re.compile(
    r"(?P<url>/static/(?P<rel>(?:js|css)/[A-Za-z0-9._-]+))\?v=(?P<bust>[^\"'\s>]*)"
)

# A reference with NO ?v= at all. Reported, never rewritten: adding a bust
# changes a URL that something else may pin, and that is a decision rather
# than a tidy-up.
_NOBUST = re.compile(
    r"(?:src|href)=\"(?P<url>/static/(?:js|css)/[A-Za-z0-9._-]+)\""
)


def _scan_files() -> List[Path]:
    """index.html plus every frontend script. Nothing under dist/."""
    out = []
    idx = FRONTEND / "index.html"
    if idx.exists():
        out.append(idx)
    js = FRONTEND / "js"
    if js.is_dir():
        out.extend(sorted(p for p in js.glob("*.js") if p.is_file()))
    return out


def _hash_of(rel: str) -> str:
    """Short content hash of frontend/<rel>, or "" if it is not there."""
    p = FRONTEND / rel
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:HASH_CHARS]
    except OSError:
        return ""


def run(*, check: bool = False, verbose: bool = False) -> Tuple[int, int, int]:
    """Returns (rewritten, already_correct, problems)."""
    files = _scan_files()
    if not files:
        print("[bust] no frontend files found at %s" % FRONTEND)
        return 0, 0, 1

    hashes: Dict[str, str] = {}
    rewritten = correct = problems = 0
    missing: Dict[str, List[str]] = {}
    nobust: Dict[str, List[str]] = {}

    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as e:
            print("[bust] cannot read %s: %s" % (f.name, e))
            problems += 1
            continue
        original = text
        local_changes = []

        def _sub(m):
            rel = m.group("rel")
            if rel not in hashes:
                hashes[rel] = _hash_of(rel)
            h = hashes[rel]
            if not h:
                # A bust pointing at a file that is not there is a broken
                # reference, and inventing a hash for it would paper over a
                # 404. Left exactly as found, and reported.
                missing.setdefault(rel, []).append(f.name)
                return m.group(0)
            if m.group("bust") == h:
                return m.group(0)
            local_changes.append((rel, m.group("bust"), h))
            return "%s?v=%s" % (m.group("url"), h)

        text = _REF.sub(_sub, text)

        # The pattern requires a closing quote immediately after the URL, so a
        # match IS a reference with no ?v= -- no second check needed.
        for m in _NOBUST.finditer(original):
            nobust.setdefault(m.group("url"), []).append(f.name)

        if local_changes:
            for rel, old, new in local_changes:
                print("  [bust] %-22s %s -> %s   (%s)"
                      % (rel, old or "(none)", new, f.name))
            rewritten += len(local_changes)
            if not check and text != original:
                f.write_text(text, encoding="utf-8", newline="")
        elif verbose:
            n = len(_REF.findall(original))
            if n:
                print("  [ok]   %s: %d reference(s) already correct" % (f.name, n))
        correct += len(_REF.findall(original)) - len(local_changes)

    for rel, where in sorted(missing.items()):
        print("  [BROKEN] /static/%s is referenced by %s but the file is not "
              "there -- that reference 404s." % (rel, ", ".join(sorted(set(where)))))
        problems += 1

    for url, where in sorted(nobust.items()):
        print("  [unmanaged] %s has no ?v= (%s). It will be cached until the "
              "browser gives up on it; add one to bring it under this tool."
              % (url, ", ".join(sorted(set(where)))))

    return rewritten, correct, problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Derive every ?v= cache-bust from file contents.")
    ap.add_argument("--check", action="store_true",
                    help="report only; exit 1 if any bust is stale")
    ap.add_argument("--verbose", action="store_true",
                    help="also list references that were already correct")
    a = ap.parse_args(argv)

    print("Cache-busts from content hashes  (%s)" % FRONTEND)
    rewritten, correct, problems = run(check=a.check, verbose=a.verbose)

    if a.check:
        if rewritten:
            print("\n%d stale cache-bust(s). Run: python _bust_cache.py"
                  % rewritten)
            return 1
        print("\nAll %d cache-bust(s) match their file contents." % correct)
        return 1 if problems else 0

    # ONE PASS IS NOT ENOUGH, and the first version of this tool shipped
    # believing it was.
    #
    # settings.js both CARRIES busts (for two css files) and IS busted from
    # index.html. Rewriting it changes its own bytes, so its hash changes, so
    # index.html's reference to it becomes stale in the same pass that fixed
    # everything else. The re-check caught it: "26 rewritten" followed by
    # "1 stale". A tool that leaves one bust wrong is worse than no tool,
    # because now nobody is looking.
    #
    # So: iterate to a fixed point. The graph is tiny and acyclic in practice,
    # so this settles in two passes -- but a genuine cycle (a.js referencing
    # b.js while b.js references a.js) has NO fixed point at all, and would
    # spin here forever. Bounded, and reported rather than hidden.
    _MAX_PASSES = 8
    total = rewritten
    passes = 1
    while rewritten and passes < _MAX_PASSES:
        passes += 1
        print("  -- pass %d (a rewritten file changes its own hash) --" % passes)
        rewritten, correct, more = run(check=False, verbose=a.verbose)
        total += rewritten
        problems += more

    if rewritten:
        print("\nSTILL STALE after %d passes. Two files almost certainly "
              "reference each other's busts, which has no stable answer -- "
              "each rewrite invalidates the other. Break the cycle by hand."
              % _MAX_PASSES)
        return 1

    if total:
        print("\n%d rewritten over %d pass(es); all %d now match."
              % (total, passes, correct))
        print("Run build_integrity.py genmanifest AFTER this -- rewriting "
              "index.html changes it.")
    else:
        print("\nNothing to do; all %d already match." % correct)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
