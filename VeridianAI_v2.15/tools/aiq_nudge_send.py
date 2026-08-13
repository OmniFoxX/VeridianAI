"""
aiq_nudge_send.py — compose and sign an AIQNudge for OracleAI
=============================================================

Use this when you want to guide Sage mid-run without aborting. The
script reads your guidance text, signs it with the AIQNudge HMAC key,
and writes it atomically into the watch directory where Sage will
pick it up on her next agentic-step iteration.

USAGE

    Direct text:
        python aiq_nudge_send.py "focus on the WCAG audit, skip the lint pass"

    From stdin (multi-line, piped):
        type guidance.txt | python aiq_nudge_send.py -

    Custom dry-run (sign but don't write):
        python aiq_nudge_send.py --dry-run "test message"

PRECONDITIONS

    - OracleAI must be set up (the .aiq_nudge_key is auto-created the
      first time AIQNudge() runs — either via this script or via the
      agentic loop on boot).
    - aiq_nudge_enabled must be `true` in config.json for Sage to
      actually pick up the nudge. Otherwise the file sits ignored.

EXIT CODES

    0  success — file written and signed
    1  bad invocation (missing args, etc.)
    2  AIQNudge setup error (couldn't load/create key)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path


def main(argv):
    # ── Argument parsing (deliberately minimal — no argparse to keep ──
    #    the dependency footprint zero for a tool that ships alongside
    #    OracleAI for personal use).
    if len(argv) < 2:
        _usage()
        return 1

    dry_run = False
    args = argv[1:]
    if args[0] == "--dry-run":
        dry_run = True
        args = args[1:]
        if not args:
            _usage()
            return 1

    if args[0] == "-":
        content = sys.stdin.read()
    else:
        content = " ".join(args)

    content = content.rstrip("\r\n")
    if not content.strip():
        print("[AIQ_NUDGE SEND] refusing to send empty nudge")
        return 1

    # ── Load AIQNudge using the project's own config paths. We import
    #    here (not at top) so a bad PYTHONPATH gives a clear error
    #    rather than failing during the `from x import y` step.
    here = Path(__file__).resolve()
    backend_dir = here.parent.parent / "backend"
    sys.path.insert(0, str(backend_dir))
    try:
        from aiq_nudge import AIQNudge, NudgeError
        from config import DATA_DIR, BACKEND_DIR
        from secret_locator import resolve_secret_file
    except ImportError as e:
        print(f"[AIQ_NUDGE SEND] cannot import backend modules: {e}")
        print(f"  expected backend at: {backend_dir}")
        return 2

    # v2.10 fix: resolve the key the SAME way the consumer (main.py) does —
    # via secret_locator -> sage_data (DATA_DIR), with a one-time migration
    # out of backend/. This script used to hardcode backend/.aiq_nudge_key,
    # so on first use after the sage_data migration it minted a SECOND,
    # different key in the project folder; nudges signed with it failed the
    # consumer's HMAC check (keyed off sage_data) and were silently
    # quarantined as .rejected_*. One resolver, one key, both ends agree.
    key_file  = resolve_secret_file(".aiq_nudge_key", DATA_DIR, BACKEND_DIR)
    watch_dir = Path(DATA_DIR) / "nudges"

    try:
        nudge = AIQNudge(key_file, watch_dir)
    except Exception as e:
        print(f"[AIQ_NUDGE SEND] could not initialise AIQNudge: {e}")
        return 2

    if dry_run:
        print("[AIQ_NUDGE SEND] DRY RUN — not writing. Signed blob:")
        print("-" * 60)
        print(nudge.sign(content))
        print("-" * 60)
        return 0

    # Sign + atomic-write via the shared AIQNudge.send() so this helper and
    # the /api/aiq-nudge UI endpoint go through one identical code path.
    try:
        target = nudge.send(content)
    except NudgeError as e:
        print(f"[AIQ_NUDGE SEND] {e}")
        return 2

    print(f"[AIQ_NUDGE SEND] wrote signed nudge: {target}")
    print(f"  content length: {len(content)} chars")
    return 0


def _usage():
    print("usage: aiq_nudge_send.py [--dry-run] 'content text'")
    print("       aiq_nudge_send.py [--dry-run] -    (read content from stdin)")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
