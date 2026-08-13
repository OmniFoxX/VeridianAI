"""Tiny local UI / runtime preferences store in sage_data (NOT config.json).

config.json is OracleConfig's allowlisted, distribution-synced schema; small
runtime UI prefs that aren't part of that schema — and that must be readable by
the daemons cross-process — live here instead, mirroring socials_config.py /
ip_access.json. Pure stdlib, thread-safe, never raises on read.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

_LOCK = threading.Lock()


def _data_dir() -> Path:
    try:
        from config import DATA_DIR as _DD
        return Path(_DD)
    except Exception:
        # backend/ -> project root -> sibling sage_data (matches real layout)
        return Path(__file__).resolve().parent.parent.parent / "sage_data"


def _path() -> Path:
    return _data_dir() / "ui_prefs.json"


def all_prefs() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get(key: str, default=None):
    try:
        return all_prefs().get(key, default)
    except Exception:
        return default


def set(key: str, value) -> dict:
    with _LOCK:
        data = all_prefs()
        data[key] = value
        p = _path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(p)
        except Exception:
            pass
        return data
