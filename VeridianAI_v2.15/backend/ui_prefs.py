"""Tiny local UI / runtime preferences store in sage_data (NOT config.json).

config.json is OracleConfig's allowlisted, distribution-synced schema; small
runtime UI prefs that aren't part of that schema -- and that must be readable
by the daemons cross-process -- live here instead, mirroring socials_config.py
/ ip_access.json. Pure stdlib, thread-safe, never raises on read.

TWO SCOPES, AND CONFLATING THEM IS THE BUG (2026-08-13)
-------------------------------------------------------
Every preference used to live in one file for the whole install. Todd found it
from the outside: he turned browser cookies on, switched profiles, and the
setting was still on for the next person. One person's choice became
everybody's default -- and worse, it did not come BACK when they signed in
again, because there was only ever one value.

So a key is now one of two things, declared and not inferred:

  MACHINE_KEYS   about this installation. Developer Mode spawns tier windows
                 and is read by tier_launcher in a daemon process where there
                 is no signed-in user to ask. A per-user answer there is not
                 merely wrong, it is unanswerable.

  everything     about a PERSON, stored under that profile's own directory.
  else           Owner / single-user keeps the existing file, so nothing about
                 an existing install changes.

The split is explicit because the failure is silent: a daemon reading a
per-user key gets the owner's value and behaves plausibly, which is exactly how
this survived until someone happened to switch profiles and look.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

_LOCK = threading.Lock()

# Preferences about the MACHINE, not about a person. Read by processes that
# have no user to ask. Add to this list only when a daemon genuinely needs the
# answer with nobody signed in.
MACHINE_KEYS = frozenset({
    "developer_mode",       # tier_launcher spawns windows from a daemon
    "backend_mode",         # which compute backend this box uses
})


def _data_dir() -> Path:
    try:
        from config import DATA_DIR as _DD
        return Path(_DD)
    except Exception:
        # backend/ -> project root -> sibling sage_data (matches real layout)
        return Path(__file__).resolve().parent.parent.parent / "sage_data"


def _path(ns=None, key: str = None) -> Path:
    """Where this preference lives.

    A machine key always lands in the shared file, whatever ns is passed --
    so a caller that threads ns through everything cannot accidentally give
    Developer Mode a per-person answer.
    """
    base = _data_dir()
    if key is not None and key in MACHINE_KEYS:
        return base / "ui_prefs.json"
    if ns:
        return base / "users" / str(ns) / "ui_prefs.json"
    return base / "ui_prefs.json"


def all_prefs(ns=None) -> dict:
    try:
        data = json.loads(_path(ns).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get(key: str, default=None, ns=None):
    """One profile's preference, or the machine's if the key is a machine key.

    Deliberately does NOT fall back to the shared file for a per-user key that
    a profile has never set. Inheriting the owner's choice on first sign-in is
    the leak this split exists to close; a new profile gets the default, which
    is the same thing the owner got on their first run.
    """
    try:
        if key in MACHINE_KEYS:
            ns = None
        return all_prefs(ns).get(key, default)
    except Exception:
        return default


def set(key: str, value, ns=None) -> dict:
    if key in MACHINE_KEYS:
        ns = None
    with _LOCK:
        data = all_prefs(ns)
        data[key] = value
        p = _path(ns, key)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(p)
        except Exception:
            pass
        return data
