"""migrate_at_rest.py -- one-time: encrypt EXISTING plaintext archives and images
using THIS install's own .atrest_key (resolved via config at runtime), so they
decrypt correctly when OracleAI runs.

Why a script (and not done remotely): the at-rest key must be the SAME one the
running app resolves from config.DATA_DIR. Running this on the machine where
OracleAI lives guarantees that; a remote/mounted view of sage_data can diverge.

HOW TO RUN  (with OracleAI stopped), from the project's backend folder:
    python migrate_at_rest.py

Safety: each file is encrypted, then decrypted and compared byte-for-byte BEFORE
the original is replaced (atomic write); the plaintext originals are copied into a
_plaintext_quarantine subfolder first (never hard-deleted). Idempotent --
already-encrypted files are skipped. New archives/images the app saves are already
encrypted automatically; this only sweeps the pre-existing plaintext ones.

After it finishes: start OracleAI, confirm your archives load and images display,
then delete the _plaintext_quarantine folders to complete the at-rest protection.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import atrest
from config import DATA_DIR, PROJECT_DIR


def main():
    print("[migrate_at_rest] at-rest key directory for THIS install:", DATA_DIR)
    jobs = [
        ("archives",  os.path.join(str(PROJECT_DIR), "archives"),  atrest.migrate_archive_folder),
        ("downloads", os.path.join(str(PROJECT_DIR), "downloads"), atrest.migrate_image_folder),
        ("uploads",   os.path.join(str(PROJECT_DIR), "uploads"),   atrest.migrate_image_folder),
    ]
    total_migrated = total_failed = 0
    for label, folder, fn in jobs:
        if not os.path.isdir(folder):
            print(f"  {label:9s}: (folder not present) skipped")
            continue
        res = fn(folder, quarantine=True)
        total_migrated += res.get("migrated", 0)
        total_failed += res.get("failed", 0)
        print(f"  {label:9s}: {res}")
    print(f"[migrate_at_rest] done -- {total_migrated} encrypted, {total_failed} failed.")
    if total_failed:
        print("[migrate_at_rest] FAILURES above were NOT replaced (originals intact).")
    print("[migrate_at_rest] Start OracleAI, confirm archives load + images display,")
    print("                  then delete the _plaintext_quarantine folder(s).")


if __name__ == "__main__":
    main()
