"""Locate secret files in the vault (sage_data, OUTSIDE the project), migrating
them once from a legacy in-project location.

Why: the project folder is the thing that gets zipped, copied, OneDrive-synced,
and distributed. A secret that lives in the project rides every copy out the door;
a secret in sage_data (which stays put) does not. This module moves each secret
into sage_data the first time it's resolved, so a leaked/synced project folder
never carries live keys -- without relying on a manual packaging step.

Leaf module: only the standard library, so config / auth / main / sage_engine can
all import it with no circular-import risk. The caller passes both directories, so
this module needs no knowledge of the project layout.

Safety: a migration is an atomic MOVE (the file is never lost), permissions are
tightened to 0600, and on ANY error it falls back to whichever location currently
holds the file -- so the app can never lose access to its own key.
"""
import os
import shutil
from pathlib import Path


def resolve_secret_file(filename, prefer_dir, legacy_dir, *, announce=True):
    """Return the Path where `filename` should live (prefer_dir = sage_data).

    * already in prefer_dir            -> return it (steady state, no I/O move);
    * only in legacy_dir (the project) -> MOVE it to prefer_dir once, return new;
    * in neither (fresh install)       -> return the prefer_dir path so the caller
                                          creates it there.
    Never raises; on error returns whichever path currently has the file.
    """
    prefer = Path(prefer_dir) / filename
    legacy = Path(legacy_dir) / filename
    try:
        if prefer.exists():
            return prefer
        if legacy.exists():
            Path(prefer_dir).mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(legacy), str(prefer))
            except Exception:
                # A race (another process moved it) or a cross-device move:
                # prefer the already-migrated copy, else copy + best-effort remove.
                if prefer.exists():
                    return prefer
                shutil.copy2(str(legacy), str(prefer))
                try:
                    os.remove(str(legacy))
                except Exception:
                    pass
            try:
                os.chmod(str(prefer), 0o600)  # best-effort on POSIX; no-op on Windows
            except Exception:
                pass
            if announce:
                try:
                    print("[SECRETS] migrated %s out of the project -> %s"
                          % (filename, prefer_dir))
                except Exception:
                    pass
            return prefer
        # Fresh install: create it in the vault.
        Path(prefer_dir).mkdir(parents=True, exist_ok=True)
        return prefer
    except Exception:
        # Last resort: never break the app over key relocation.
        return legacy if legacy.exists() else prefer
