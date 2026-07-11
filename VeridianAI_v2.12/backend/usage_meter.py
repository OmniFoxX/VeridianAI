"""usage_meter.py -- daily screen-time accounting for Access Controls (v2.13.1).

THE PROBLEM
-----------
`session_minutes` caps one login; a profile with a DAILY budget needs the app
to know how long it was actually used today, across any number of sign-ins,
surviving a backend restart, and NOT counting hours where the app was closed.

THE TRICK: heartbeat accounting
-------------------------------
The frontend polls /api/auth/status every ~30s while the app is open (the
session watchdog in auth.js). Each poll for a signed-in, daily-capped,
non-owner profile calls tick(). A tick charges the wall-clock gap since the
PREVIOUS tick, capped at _MAX_TICK_GAP -- so:

  * app open + signed in  -> gaps of ~30s accrue continuously;
  * app closed / signed out -> no polls, and the first tick after coming
    back charges at most _MAX_TICK_GAP, not the whole absence;
  * backend restart -> in-memory last_tick resets; worst case the user
    loses nothing and we under-charge one gap. Fail-friendly, never
    fail-punitive.

STORAGE
-------
sage_data/.access_usage.json -- {username: {"date": "YYYY-MM-DD", "seconds": N}}
Only TODAY's total per user; yesterday is overwritten at local-midnight
rollover. Deliberately no history: this is a budget meter, not surveillance
(same privacy stance as access_policy.py -- we never store ages either).
Writes are throttled (_SAVE_INTERVAL) + atomic (tmp/fsync/replace, 0600),
matching the users.py pattern. A restart can forget at most the last
unsaved interval -- again, errs in the user's favour.

stdlib-only, import-safe-early, like the rest of the auth stack.
"""
from __future__ import annotations

import json
import os
import threading
import time

_STORE_NAME = ".access_usage.json"

_MAX_TICK_GAP = 90        # seconds; > watchdog cadence (30s), < "left for lunch"
_SAVE_INTERVAL = 60       # throttle disk writes to at most one per minute

_LOCK = threading.Lock()
_MEM: dict = {}           # username(lower) -> {"date", "seconds", "last_tick"}
_LAST_SAVE = 0.0
_LOADED = False


def _store_path() -> str:
    try:
        from config import DATA_DIR
        return os.path.join(str(DATA_DIR), _STORE_NAME)
    except Exception:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), _STORE_NAME)


def _today(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _load_once() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        with open(_store_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for u, rec in raw.items():
                if isinstance(rec, dict):
                    _MEM[str(u).lower()] = {
                        "date": str(rec.get("date", "")),
                        "seconds": float(rec.get("seconds", 0) or 0),
                        "last_tick": 0.0,   # never resurrect a pre-restart gap
                    }
    except Exception:
        pass  # missing/corrupt file = fresh meter; never blocks auth


def _save(now: float) -> None:
    global _LAST_SAVE
    _LAST_SAVE = now
    p = _store_path()
    data = {u: {"date": r["date"], "seconds": int(r["seconds"])}
            for u, r in _MEM.items() if r.get("seconds", 0) >= 1}
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
    except Exception as exc:
        print(f"[USAGE] persist failed (kept in memory): {exc}")


def _rec(username: str, now: float) -> dict:
    """Today's record for the user, rolling over at local midnight."""
    u = (username or "").strip().lower()
    r = _MEM.get(u)
    d = _today(now)
    if r is None or r.get("date") != d:
        r = {"date": d, "seconds": 0.0, "last_tick": 0.0}
        _MEM[u] = r
    return r


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def tick(username: str, now: float | None = None) -> int:
    """Register a heartbeat; returns TOTAL seconds used today (after charge).
    The first tick of a stretch charges nothing (it only arms last_tick)."""
    t = time.time() if now is None else now
    with _LOCK:
        _load_once()
        r = _rec(username, t)
        lt = r["last_tick"]
        if lt and t > lt:
            r["seconds"] += min(t - lt, _MAX_TICK_GAP)
        r["last_tick"] = t
        if t - _LAST_SAVE >= _SAVE_INTERVAL:
            _save(t)
        return int(r["seconds"])


def used_today(username: str, now: float | None = None) -> int:
    """Seconds used today, WITHOUT charging a heartbeat (login checks, the
    owner panel's readout)."""
    t = time.time() if now is None else now
    with _LOCK:
        _load_once()
        return int(_rec(username, t)["seconds"])


def reset_today(username: str) -> None:
    """Owner mercy button: hand today's minutes back (not currently surfaced
    in the UI, but trivially wired to an endpoint when wanted)."""
    with _LOCK:
        _load_once()
        u = (username or "").strip().lower()
        _MEM.pop(u, None)
        _save(time.time())
