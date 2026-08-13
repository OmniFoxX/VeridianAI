#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
state_paths.py - where MUTABLE state lives.

v2.13 (2026-08-07). Historically every piece of mutable project state lived in
the project root: config.json, .oracle_pids.json, chat_memory.json,
backend/hash_chain.log, the overseer lock files. That was fine for a decade of
portable installs because the folder was always writable.

MSIX broke it. A Store-installed app runs from
C:\\Program Files\\WindowsApps\\<package>, which is READ-ONLY at runtime. Every
one of those writes fails, and the first one that matters -- config.json, which
the backend writes on first boot -- takes the backend down before it binds a
port. Symptom: "backend not running yet", forever.

The rule here is deliberately behavioural, not build-flag-based:

    if the project directory is writable, use it  (portable = unchanged)
    otherwise use the data directory              (MSIX, Program Files, ...)

Keying off a Store flag would have fixed only the Store. This also covers
anyone who installs the NSIS build into Program Files, which has exactly the
same problem and would have failed exactly the same way.

This module deliberately imports NOTHING from the project. config.py imports
config_store, so neither can own this without creating a cycle; a leaf module
can be imported from anywhere.
"""

from pathlib import Path
import os

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent


def data_dir() -> Path:
    """Same resolution as config.DATA_DIR. Duplicated rather than imported to
    keep this module dependency-free -- if the two ever disagree, config.py is
    the one to change, and it reads the same env var."""
    env = os.environ.get("VERIDIAN_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return PROJECT_DIR.parent / "sage_data"


# Paths that are read-only by construction. Checked BEFORE touching the disk,
# because probing them is not merely slow -- it can hang (see is_writable).
_READONLY_MARKERS = ("\\windowsapps\\", "/windowsapps/")


def is_writable(path: Path) -> bool:
    r"""Can we create a file here?

    v2.13: this used tempfile.NamedTemporaryFile and HUNG FOR MINUTES on an
    MSIX install, taking the whole backend down with it. CPython's
    _mkstemp_inner has a Windows-specific retry loop:

        except PermissionError:
            if _os.name == 'nt' and _os.path.isdir(dir) and _os.access(dir, _os.W_OK):
                continue        # up to TMP_MAX == 10,000 times
            raise

    On Windows os.access() only reports the read-only ATTRIBUTE, never the
    ACL -- so for C:\Program Files\WindowsApps it answers True, and tempfile
    cheerfully retries ten thousand failing opens against a slow virtualised
    filesystem. (The irony is not lost: the docstring this replaces warned
    that os.access lies on Windows, and then used a helper that trusts it.)

    Now: a marker check first, then ONE open attempt. No loop, no tempfile,
    fails in microseconds.
    """
    sp = str(path).lower()
    if any(m in sp for m in _READONLY_MARKERS):
        return False
    probe = None
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".veridian_write_test"
        # O_EXCL so we never clobber, and exactly one attempt either way.
        fd = os.open(str(probe), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        return True
    except Exception:
        return False
    finally:
        try:
            if probe is not None and probe.exists():
                probe.unlink()
        except Exception:
            pass


def user_data_fallback() -> Path:
    """Last-resort writable location, used when the configured data dir cannot
    be written (MSIX install, Program Files, a read-only USB stick).

    Kept OUT of the project tree on purpose -- the sage_data-outside-the-project
    rule exists for watchdog correctness and sync-corruption avoidance, and a
    fallback that violated it would be worse than the crash it replaces."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "VeridianAI" / "sage_data"
    return Path.home() / ".veridianai" / "sage_data"


def _resolve_state_dir() -> Path:
    if is_writable(PROJECT_DIR):
        return PROJECT_DIR
    d = data_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


# Resolved once at import. Cheap (one temp file), and a process that starts in
# a read-only directory is not going to become writable mid-run.
STATE_DIR = _resolve_state_dir()

# True when we had to relocate -- useful for logging and for tests.
STATE_RELOCATED = (STATE_DIR != PROJECT_DIR)

CONFIG_FILE      = STATE_DIR / "config.json"
PID_REGISTRY     = STATE_DIR / ".oracle_pids.json"
CHAT_MEMORY_FILE = STATE_DIR / "chat_memory.json"
# Shared (non-namespaced) user data. Historically these sat in the project
# root, which is fine while it is writable and impossible when it is not.
# Per-user namespaced copies live under DATA_DIR/users/<ns>/ and are
# unaffected.
ARCHIVES_DIR     = STATE_DIR / "archives"
UPLOADS_DIR      = STATE_DIR / "uploads"
DOWNLOADS_DIR    = STATE_DIR / "downloads"
LOCK_DIR         = STATE_DIR / "locks" if STATE_RELOCATED else BACKEND_DIR
HASH_CHAIN_LOG   = (STATE_DIR / "hash_chain.log") if STATE_RELOCATED \
                   else (BACKEND_DIR / "hash_chain.log")

if STATE_RELOCATED:
    print(f"[state_paths] project dir is read-only; mutable state -> {STATE_DIR}",
          flush=True)
