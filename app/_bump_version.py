#!/usr/bin/env python3
r"""
_bump_version.py -- VeridianAI version-bump worker
==================================================

Bumps every canonical version-string location across the project in a
single pass. Invoked by bump_version.bat (top-level wrapper).

USAGE
-----
    py _bump_version.py OLD NEW [--dry-run] [--verbose]

Where OLD / NEW accept any of:
    "2.3"        "v2.3"        "2.3.0"
All forms internally normalize to:
    short_form    = "v2.3"
    semver_form   = "2.3.0"
    folder_form   = "VeridianAI_v2.3"

WHAT GETS BUMPED
----------------
1. start.bat
   - title bar lines and box-art display headers
   - layout-description comments referring to VeridianAI_vX.Y
   - user-facing Toga-model preflight error message
2. backend/main.py
   - FastAPI app version="X.Y.0"
3. backend/mcp_handlers.py
   - MCP_SERVER_VERSION = "X.Y.0"
4. VeridianAI_REFERENCE.md
   - **Version:** vX.Y header
   - file-tree path references (E:\VeridianAI_vX.Y and TogaCopy mirror)
5. BEFORE_RUNNING.txt
   - All folder-name references (recipient instructions)

WHAT INTENTIONALLY ISN'T TOUCHED
--------------------------------
- Dated historical fix-marker comments (`:: v2.2 fix (2026-05-29):`
  and similar) -- these document WHEN a fix landed; rewriting them
  would falsify the timeline.
- Backup files (.bak_*, .repaired, .truncated_*).
- The project folder itself (rename is a separate intentional step;
  the script reports the rename it RECOMMENDS but does not execute
  it -- folder rename breaks Continue.dev configs, cowork mounts,
  shortcuts, and other external references that the user must
  update by hand).

EXIT CODES
----------
0   success, all expected anchors found and replaced
1   bad arguments or version-string format
2   one or more expected anchors not found in their file
        (suggests files have drifted from the script's known shape)
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Version-form normalization
# ---------------------------------------------------------------------------

def _normalize(raw: str) -> Tuple[str, str, str]:
    """Return (short_form, semver_form, folder_form) from any input form.

    Accepts: "2.3", "v2.3", "2.3.0", "V2.3.0", etc.
    Validates that it's a major.minor (with optional patch) pattern.
    """
    raw = raw.strip().lstrip("vV")
    parts = raw.split(".")
    if not (2 <= len(parts) <= 3):
        raise ValueError(
            f"expected MAJOR.MINOR[.PATCH], got {raw!r}"
        )
    if not all(p.isdigit() for p in parts):
        raise ValueError(
            f"all version components must be integers, got {raw!r}"
        )
    major, minor = parts[0], parts[1]
    patch = parts[2] if len(parts) == 3 else "0"
    short  = f"v{major}.{minor}"
    semver = f"{major}.{minor}.{patch}"
    folder = f"VeridianAI_v{major}.{minor}"
    return short, semver, folder


# ---------------------------------------------------------------------------
# Replacement specification
# ---------------------------------------------------------------------------

@dataclass
class Replacement:
    """One targeted edit. `find` and `replace_with` are computed lazily
    from old/new version forms so the same spec works for any bump.
    """
    file: str                            # relative to PROJECT_ROOT
    build_find:    Callable[[dict], str]  # called with version-forms dict
    build_replace: Callable[[dict], str]
    required: bool = True                # if False, missing anchor is OK
    description: str = ""                # human-readable label

    # v2.16.0 -- DRIFT REPAIR.
    #
    # A literal anchor carries the version we are bumping FROM. That makes
    # drift SELF-SEALING: the moment one line is missed, its anchor can never
    # match again, so every later bump misses it too and reports the same
    # routine-looking WARN. start.bat's post-choice title sat at v2.14 through
    # two releases exactly this way -- the window said v2.14 while the app said
    # v2.16, and the warning that should have caught it said only "no
    # version-like string found nearby".
    #
    # `build_version_pattern` is a regex (MULTILINE) for the SAME shape
    # carrying ANY version, with the version in group 1. When set it REPLACES
    # the literal find rather than backing it up, because a version-agnostic
    # match is strictly better: it cannot be sealed off by drift, and it does
    # not depend on an earlier anchor in the list having already run to stay
    # unambiguous. The whole match is rewritten to build_replace(new_forms),
    # and a version that was not the expected one is reported as a repair.
    build_version_pattern: Optional[Callable[[dict], str]] = None


def _forms(short: str, semver: str, folder: str) -> dict:
    return {"short": short, "semver": semver, "folder": folder}


REPLACEMENTS: List[Replacement] = [
    # ----- start.bat -----
    #
    # NOT CRLF. This header said "(Windows CRLF)" and start.bat contains ZERO
    # CRLF pairs -- it is LF-only and has been for some time. That was not a
    # cosmetic inaccuracy: the post-choice title anchor below required a
    # literal "\r\n", so it could never match, and the title stayed at v2.14
    # while everything else advanced. The engine reads bytes and decodes, so
    # line endings are preserved either way; nothing here should assume which
    # kind a file uses.
    Replacement(
        file="start.bat",
        build_find=lambda f: f"title VeridianAI {f['short']} - Startup",
        build_replace=lambda f: f"title VeridianAI {f['short']} - Startup",
        description="start.bat title bar (startup)",
    ),
    Replacement(
        file="start.bat",
        build_find=lambda f: f"V E R I D I A N  A I  {f['short']}",
        build_replace=lambda f: f"V E R I D I A N  A I  {f['short']}",
        description="start.bat box-art header (2x occurrences)",
    ),
    Replacement(
        file="start.bat",
        # End-of-line assertion instead of a literal newline, so this matches
        # whether the file is LF or CRLF -- and so it stays matching if that
        # ever changes again. The lookahead is what distinguishes this line
        # from the startup title above, which continues with " - Startup".
        build_find=lambda f: f"title VeridianAI {f['short']}",
        build_replace=lambda f: f"title VeridianAI {f['short']}",
        build_version_pattern=lambda f: (
            r"title VeridianAI v(\d+\.\d+(?:\.\d+)?)(?=[ \t]*\r?$)"),
        description="start.bat runtime title (post-choice)",
    ),
    # RETIRED, not drifted. These two comments used to name the versioned
    # folder ("sage_data lives ALONGSIDE VeridianAI_v2.14, not ...") and were
    # rewritten to say "the app folder" instead. That is strictly better: a
    # comment that names no version cannot go stale, and needs no anchor here.
    #
    # Kept as required=False rather than deleted, so the two WARNs they used to
    # produce every single bump are explained rather than merely gone. Reading
    # a spec and finding nothing is a fine outcome; reading a warning and not
    # knowing whether it matters is not.
    Replacement(
        file="start.bat",
        build_find=lambda f: f":: walks up one level (sage_data lives ALONGSIDE {f['folder']}, not",
        build_replace=lambda f: f":: walks up one level (sage_data lives ALONGSIDE {f['folder']}, not",
        required=False,
        description=("start.bat layout comment (line ~105) -- genericised to "
                     "'the app folder'; no longer version-bearing"),
    ),
    Replacement(
        file="start.bat",
        build_find=lambda f: f":: intuitively create sage_data inside {f['folder']} (which would",
        build_replace=lambda f: f":: intuitively create sage_data inside {f['folder']} (which would",
        required=False,
        description=("start.bat layout comment (line ~429) -- genericised to "
                     "'the app folder'; no longer version-bearing"),
    ),
    Replacement(
        file="start.bat",
        build_find=lambda f: f"echo    The sage_data folder lives ALONGSIDE the {f['folder']}",
        build_replace=lambda f: f"echo    The sage_data folder lives ALONGSIDE the {f['folder']}",
        required=False,   # this echo was removed from start.bat; keep the spec
                          # in case it returns, but do not fail the run over it
        description="start.bat user-facing Toga-preflight error message",
    ),

    # ----- backend/main.py -----
    Replacement(
        file="backend/main.py",
        build_find=lambda f: f'app = FastAPI(title="VeridianAI", version="{f["semver"]}", docs_url=None, redoc_url=None)',
        build_replace=lambda f: f'app = FastAPI(title="VeridianAI", version="{f["semver"]}", docs_url=None, redoc_url=None)',
        description="main.py FastAPI app version=",
    ),

    # ----- backend/mcp_handlers.py -----
    Replacement(
        file="backend/mcp_handlers.py",
        build_find=lambda f: f'MCP_SERVER_VERSION = "{f["semver"]}"',
        build_replace=lambda f: f'MCP_SERVER_VERSION = "{f["semver"]}"',
        description="mcp_handlers.py MCP_SERVER_VERSION",
    ),

    # ----- VeridianAI_REFERENCE.md -----
    Replacement(
        file="VeridianAI_REFERENCE.md",
        build_find=lambda f: f"**Version:** {f['short']}",
        build_replace=lambda f: f"**Version:** {f['short']}",
        description="VeridianAI_REFERENCE.md Version header",
    ),
    Replacement(
        file="VeridianAI_REFERENCE.md",
        build_find=lambda f: f"E:\\{f['folder']}",
        build_replace=lambda f: f"E:\\{f['folder']}",
        required=False,   # path tree might already be edited away
        description="VeridianAI_REFERENCE.md file-tree path",
    ),
    Replacement(
        file="VeridianAI_REFERENCE.md",
        build_find=lambda f: f"{f['folder']}_TogaCopy",
        build_replace=lambda f: f"{f['folder']}_TogaCopy",
        required=False,
        description="VeridianAI_REFERENCE.md TogaCopy mirror folder",
    ),

    # ----- electron/package.json -----
    # THE version that becomes the MSIX/AppX package version, and the number the
    # installed app reports. Its absence from this list is why a bump could
    # report success while every subsequent build still identified itself as the
    # PREVIOUS version -- the one symptom that makes "is this the new build?"
    # unanswerable, which is exactly the question you are asking when a fix does
    # not appear to have taken.
    #
    # Both trees carry this file (portable and WinStoreApp); the anchor is the
    # same in each.
    Replacement(
        file="electron/package.json",
        build_find=lambda f: f'"version": "{f["semver"]}"',
        build_replace=lambda f: f'"version": "{f["semver"]}"',
        description="electron/package.json version (drives the MSIX package version)",
    ),

    # ----- BEFORE_RUNNING.txt -----
    # Multiple occurrences -- we use replace_all by counting
    # occurrences below. The find/replace function returns the
    # SINGLE folder string; the harness handles multi-replace.
    Replacement(
        file="BEFORE_RUNNING.txt",
        build_find=lambda f: f["folder"],
        build_replace=lambda f: f["folder"],
        required=False,   # the download instructions name the zip by SEMVER
                          # (VeridianAI-X.Y.Z-portable), not by folder form, so
                          # this anchor legitimately finds nothing
        description=("BEFORE_RUNNING.txt folder references "
                     "(all occurrences)"),
    ),
    Replacement(
        file="BEFORE_RUNNING.txt",
        build_find=lambda f: f'VeridianAI-{f["semver"]}-portable',
        build_replace=lambda f: f'VeridianAI-{f["semver"]}-portable',
        description="BEFORE_RUNNING.txt portable zip name (all occurrences)",
    ),
]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def _read_file_preserving_endings(path: Path) -> Tuple[str, bool]:
    """Return (text, has_crlf). text preserves whatever line endings the
    file uses; the caller will use has_crlf only to write back correctly.
    """
    raw = path.read_bytes()
    has_crlf = b"\r\n" in raw
    return raw.decode("utf-8"), has_crlf


def _write_file_preserving_endings(path: Path, text: str, had_crlf: bool):
    """Write back. If the original file used CRLF, we preserve any CRLF
    sequences already in `text` (Python doesn't translate by default
    when we use 'wb'); we only need to be careful to ENCODE without
    Python's universal-newlines layer.
    """
    path.write_bytes(text.encode("utf-8"))


def _version_hint(text, find_str):
    """What version does this file appear to be at?

    Given the anchor we failed to find, look for the same shape carrying a
    DIFFERENT version. `"version": "2.13.18"` not matching is uninformative;
    `"version": "2.14.0"` being present instead is the whole answer.

    Written by splitting the anchor around its version number rather than by
    rewriting an escaped pattern -- the escaped-regex-of-an-escaped-regex
    version of this was wrong in a way that silently returned None for every
    input, i.e. it looked like "no hint available" instead of like a bug.
    """
    m = re.search(r"\d+\.\d+(?:\.\d+)?", find_str)
    if not m:
        return None                     # anchor carries no version to compare
    prefix, suffix = find_str[:m.start()], find_str[m.end():]
    pat = re.escape(prefix) + r"(\d+\.\d+(?:\.\d+)?)" + re.escape(suffix)
    hit = re.search(pat, text)
    return hit.group(1) if hit else None


def apply_replacements(
    old_forms: dict,
    new_forms: dict,
    *,
    dry_run: bool = False,
    verbose: bool = False,
) -> Tuple[int, int]:
    """Apply all REPLACEMENTS. Returns (success_count, missing_count).

    A "missing" required anchor is reported but does not abort -- the
    user gets the full report so they can decide whether to bail.
    """
    success = 0
    missing = 0

    by_file: dict = {}
    for r in REPLACEMENTS:
        by_file.setdefault(r.file, []).append(r)

    for rel_path, repls in by_file.items():
        path = PROJECT_ROOT / rel_path
        if not path.exists():
            print(f"  [skip] {rel_path}: file not present")
            continue

        text, had_crlf = _read_file_preserving_endings(path)
        original_text = text
        local_success = 0

        for r in repls:
            find_str    = r.build_find(old_forms)
            replace_str = r.build_replace(new_forms)

            # v2.16.0: version-agnostic anchors run here and skip the literal
            # path entirely. They also skip the no-op shortcut below, because
            # "FROM equals TO" does not mean there is nothing to do -- a line
            # that drifted BEHIND both of them is exactly what this exists to
            # find, and bailing early would hide it.
            if r.build_version_pattern is not None:
                pat = r.build_version_pattern(new_forms)
                _seen = []

                def _sub(m, _rep=replace_str, _seen=_seen):
                    _seen.append(m.group(1))
                    return _rep

                new_text, n = re.subn(pat, _sub, text, flags=re.MULTILINE)
                if n == 0:
                    tag = "WARN" if r.required else "skip"
                    print(f"  [{tag}] {rel_path}: shape NOT FOUND -- "
                          f"{r.description}")
                    print(f"           nothing in this file matches the shape "
                          f"at all, at any version. This is real drift.")
                    if verbose:
                        print(f"           pattern: {pat!r}")
                    if r.required:
                        missing += 1
                    continue
                _stale = sorted({v for v in _seen
                                 if v not in (old_forms["short"].lstrip("v"),
                                              old_forms["semver"],
                                              new_forms["short"].lstrip("v"),
                                              new_forms["semver"])})
                # A version-agnostic anchor MATCHES on a healthy tree too --
                # it finds the already-correct line and rewrites it to the
                # identical text. That is a match, not a change, and counting
                # it reported "Total replacements: 1" for a run that touched
                # nothing. Count the CHANGE, never the match.
                _changed = (text != new_text)
                if _stale:
                    # LOUD. This line missed one or more previous bumps and has
                    # been claiming the wrong version ever since.
                    print(f"  [REPAIR x{n}] {rel_path}: {r.description}")
                    print(f"           was at {', '.join('v' + v for v in _stale)}"
                          f" -- BEHIND the version you asked to bump from "
                          f"({old_forms['semver']}). It missed at least one "
                          f"earlier bump. Corrected.")
                elif _changed:
                    print(f"  [bump x{n}] {rel_path}: {r.description}")
                elif verbose:
                    print(f"  [noop] {rel_path}: {r.description}")
                text = new_text
                if _changed:
                    # local_success ONLY -- it is rolled into `success` once
                    # per file at the end of this loop. Adding to both counted
                    # every repair twice.
                    local_success += n
                continue

            if find_str == replace_str:
                # Same version, no-op. Don't count as missing.
                if verbose:
                    print(f"  [noop] {rel_path}: {r.description}")
                continue

            count = text.count(find_str)
            if count == 0:
                tag = "WARN" if r.required else "skip"
                print(f"  [{tag}] {rel_path}: anchor NOT FOUND -- {r.description}")

                # WHY it was not found matters more than THAT it was not found.
                #
                # Running this against a tree that is ALREADY at the target
                # produces a full screen of "anchor NOT FOUND" and the summary
                # "files may have drifted from expected shape" -- which reads as
                # corruption when the truth is simply that there was nothing to
                # bump. That happened on 2026-08-13 across two trees and cost an
                # afternoon, because the message described the wrong problem.
                #
                # So: look for what version this file IS at, and say so.
                hint = _version_hint(text, find_str)
                if hint:
                    print(f"           this file is at {hint} -- you asked to "
                          f"bump FROM {old_forms['semver']}.")
                    if hint == new_forms['semver']:
                        print(f"           it is ALREADY at the target version. "
                              f"Nothing to do here.")
                    else:
                        print(f"           re-run with the current version as "
                              f"FROM, or leave it alone.")
                elif verbose:
                    print(f"           expected: {find_str!r}")
                else:
                    print(f"           no version-like string found nearby -- "
                          f"this one may be real drift. Re-run with --verbose.")
                if r.required:
                    missing += 1
                continue

            text = text.replace(find_str, replace_str)
            print(f"  [bump x{count}] {rel_path}: {r.description}")
            local_success += count

        if text != original_text and not dry_run:
            _write_file_preserving_endings(path, text, had_crlf)
        elif text != original_text and dry_run:
            print(f"  [DRY-RUN] would write {rel_path} ({local_success} changes)")

        success += local_success

    return success, missing


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bump VeridianAI version strings across the project."
    )
    parser.add_argument("old", help="Current version (e.g. 2.3, v2.3, 2.3.0)")
    parser.add_argument("new", help="New version (e.g. 2.4, v2.4, 2.4.0)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing files")
    parser.add_argument("--verbose", action="store_true",
                        help="Show no-op replacements and details")
    args = parser.parse_args(argv)

    try:
        old_short, old_semver, old_folder = _normalize(args.old)
        new_short, new_semver, new_folder = _normalize(args.new)
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    old_forms = _forms(old_short, old_semver, old_folder)
    new_forms = _forms(new_short, new_semver, new_folder)

    print(f"Bumping VeridianAI version strings")
    print(f"  FROM: short={old_short!r}  semver={old_semver!r}  "
          f"folder={old_folder!r}")
    print(f"  TO:   short={new_short!r}  semver={new_semver!r}  "
          f"folder={new_folder!r}")
    if args.dry_run:
        print(f"  (DRY RUN -- no files will be written)")
    print()

    success, missing = apply_replacements(
        old_forms, new_forms,
        dry_run=args.dry_run, verbose=args.verbose,
    )

    print()
    print(f"Total replacements: {success}")
    if missing:
        print(f"WARN: {missing} required anchor(s) not found "
              f"-- files may have drifted from expected shape.")
        print(f"      Re-inspect manually before relying on this bump.")
    else:
        print(f"All required anchors found and bumped cleanly.")

    if not args.dry_run:
        print()
        print(f"NEXT STEPS (manual):")
        # A patch bump leaves the folder form unchanged, and printing
        # "Rename VeridianAI_v2.14 -> VeridianAI_v2.14" as step 1 invites
        # someone to go looking for a difference that is not there.
        if old_folder != new_folder:
            print(f"  1. Rename project folder: {old_folder} -> {new_folder}")
            print(f"     (intentional separate step -- breaks external")
            print(f"     references like Continue.dev configs, cowork mounts,")
            print(f"     shortcuts; update those by hand after rename.)")
        else:
            print(f"  1. No folder rename needed -- {old_folder} is unchanged")
            print(f"     (patch-level bump; the folder carries only MAJOR.MINOR).")
        print(f"  2. Re-grant Claude folder access through the Claude app")
        print(f"     if Claude is in the loop.")
        print(f"  3. Run prep_distribution.bat -- dist folder will be")
        print(f"     auto-named {new_folder}_dist.")

    return 2 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
