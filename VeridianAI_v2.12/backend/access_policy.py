"""access_policy.py -- per-profile Access Controls (v2.13, Phase AC).

One policy engine, two audiences: "parental controls" on the free/personal
tier and "manager access controls" on a commercial tier are the SAME record
with a different lock_reason string. The policy applies to NON-OWNER profiles
only; the owner account is never restricted (mirrors delete_user's owner
protection in users.py).

STORAGE
-------
The policy lives inside each user's entry in sage_data/.users.json under the
"access" key (via users.get_access / users.set_access). No new secret files,
no schema migration: an absent/empty record means "no restrictions", so every
pre-AC install behaves exactly as before this module existed.

RECORD (all keys optional; defaults = unrestricted)
---------------------------------------------------
    session_minutes : int   0 = no cap, else 1..1440. Caps the login-session
                            TTL, so "timed sessions" ride the EXISTING expiry
                            mechanism in session.py -- no new timer machinery.
    daily_minutes   : int   0 = no cap, else 1..1440. Cumulative screen-time
                            budget per LOCAL day, metered by usage_meter.py
                            off the /api/auth/status heartbeat. Enforced at
                            login (remaining budget also caps the session TTL)
                            and mid-session (budget exhaustion ends the
                            session on the next heartbeat).
    allowed_hours   : str   "" = anytime, else "HH:MM-HH:MM" local time.
                            Overnight windows ("20:00-06:00") are supported.
                            Enforced at login; when inside the window the
                            session TTL is additionally capped so the session
                            expires when the window closes.
    locked          : bool  Deny sign-in (temp ban / "See Manager"). Existing
                            sessions are revoked by the API layer at set time.
    lock_reason     : str   Shown verbatim on the login screen.
    lock_until      : int|null  Epoch seconds; auto-unlocks (self-healing) on
                            the first login attempt after expiry. null =
                            locked until the owner unlocks.
    socials_allowed : bool  False cuts every /api/socials/* route for this
                            profile (default-off is recommended for child /
                            restricted profiles; see Microsoft Store policy
                            on minors + parental controls).

DESIGN NOTES
------------
- Evaluation is pure + stdlib-only; the single side effect is the self-healing
  auto-unlock (persists the cleared lock so the store reflects reality).
- COPPA-adjacent care: we deliberately store NO ages or birthdates. The owner
  flags a profile restricted; we never profile the human behind it.
- Times use the machine's local clock: this is a desktop app and the parent /
  manager sets rules in the timezone the machine lives in.
"""
from __future__ import annotations

import time
from typing import Optional, Tuple

DEFAULTS = {
    "session_minutes": 0,
    "daily_minutes": 0,
    "allowed_hours": "",
    "locked": False,
    "lock_reason": "",
    "lock_until": None,
    "socials_allowed": True,
}

_MAX_SESSION_MINUTES = 1440          # one day; anything longer is "no cap"
_MAX_REASON_LEN = 300
_MAX_LOCK_DAYS = 366                 # sanity ceiling for lock_until


# ---------------------------------------------------------------------------
# Store access (users.py owns the file; we own the semantics)
# ---------------------------------------------------------------------------

def get_policy(username: str) -> dict:
    """The user's policy merged over DEFAULTS. Unknown user -> pure defaults
    (callers that care about existence check users.user_exists themselves)."""
    import users
    raw = users.get_access(username) or {}
    pol = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in raw:
            pol[k] = raw[k]
    return pol


def set_policy(username: str, patch: dict) -> dict:
    """Validate `patch`, merge it over the stored record, persist.
    Returns {"success": True, "access": <merged policy>} or
            {"success": False, "error": <human-readable reason>}."""
    clean, err = validate_patch(patch)
    if err:
        return {"success": False, "error": err}
    import users
    current = users.get_access(username)
    if current is None:
        return {"success": False, "error": "no such user"}
    merged = dict(current)
    merged.update(clean)
    # Unlocking always clears the stale reason/deadline so a later re-lock
    # never accidentally resurrects last month's message.
    if "locked" in clean and not clean["locked"]:
        merged["lock_reason"] = ""
        merged["lock_until"] = None
    r = users.set_access(username, merged)
    if not r.get("success"):
        return r
    return {"success": True, "access": get_policy(username)}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_patch(patch: dict) -> Tuple[dict, Optional[str]]:
    """Whitelist + sanity-check a policy patch. Returns (clean, error)."""
    if not isinstance(patch, dict):
        return {}, "policy must be an object"
    clean: dict = {}
    for k, v in patch.items():
        if k not in DEFAULTS:
            continue  # unknown keys are dropped, never stored
        if k in ("session_minutes", "daily_minutes"):
            try:
                m = int(v)
            except (TypeError, ValueError):
                return {}, f"{k} must be a number"
            if m < 0 or m > _MAX_SESSION_MINUTES:
                return {}, f"{k} must be 0..{_MAX_SESSION_MINUTES}"
            clean[k] = m
        elif k == "allowed_hours":
            s = (v or "").strip() if isinstance(v, (str, type(None))) else None
            if s is None:
                return {}, "allowed_hours must be a string"
            if s and _parse_window(s) is None:
                return {}, 'allowed_hours must be "HH:MM-HH:MM" (or empty for anytime)'
            clean[k] = s
        elif k in ("locked", "socials_allowed"):
            clean[k] = bool(v)
        elif k == "lock_reason":
            if not isinstance(v, (str, type(None))):
                return {}, "lock_reason must be a string"
            clean[k] = (v or "").strip()[:_MAX_REASON_LEN]
        elif k == "lock_until":
            if v in (None, "", 0):
                clean[k] = None
                continue
            try:
                ts = int(v)
            except (TypeError, ValueError):
                return {}, "lock_until must be an epoch timestamp or null"
            if ts <= int(time.time()):
                return {}, "lock_until is already in the past"
            if ts > int(time.time()) + _MAX_LOCK_DAYS * 86400:
                return {}, f"lock_until is more than {_MAX_LOCK_DAYS} days away"
            clean[k] = ts
    return clean, None


# ---------------------------------------------------------------------------
# Time-window helpers ("HH:MM-HH:MM", overnight-aware)
# ---------------------------------------------------------------------------

def _parse_window(s: str) -> Optional[Tuple[int, int]]:
    """"07:30-21:00" -> (450, 1260) minutes-of-day, or None if malformed.
    start == end is rejected (a zero-length window is a config mistake, not
    a 24h allowance -- use "" for anytime)."""
    try:
        a, b = s.split("-", 1)
        ah, am = a.strip().split(":", 1)
        bh, bm = b.strip().split(":", 1)
        start = int(ah) * 60 + int(am)
        end = int(bh) * 60 + int(bm)
        if not (0 <= int(ah) <= 23 and 0 <= int(bh) <= 23):
            return None
        if not (0 <= int(am) <= 59 and 0 <= int(bm) <= 59):
            return None
        if start == end:
            return None
        return start, end
    except (ValueError, AttributeError):
        return None


def _window_state(window: Tuple[int, int], now_min: int) -> Tuple[bool, int]:
    """(inside?, minutes_until_window_end). Overnight windows wrap midnight:
    (1200, 360) = 20:00-06:00 means 20:00..24:00 and 00:00..06:00 are inside."""
    start, end = window
    if start < end:                       # same-day window
        inside = start <= now_min < end
        remaining = end - now_min
    else:                                 # overnight window
        inside = now_min >= start or now_min < end
        remaining = (end - now_min) if now_min < end else (1440 - now_min + end)
    return inside, max(remaining, 0)


def _fmt_window(s: str) -> str:
    return s.replace("-", " to ")


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------

def check_login(username: str, now: Optional[float] = None) -> dict:
    """Gate a NON-OWNER login attempt against the user's policy.

    Returns:
      {"allowed": True,  "ttl_cap": <seconds or None>}
      {"allowed": False, "reason": <string for the login screen>}

    ttl_cap is the tightest of (session_minutes, time-until-window-close);
    the caller min()s it against its own default session TTL.
    """
    pol = get_policy(username)
    t = time.time() if now is None else now

    # --- lock / temp ban ---------------------------------------------------
    if pol["locked"]:
        until = pol["lock_until"]
        if until is not None and t >= int(until):
            # Self-healing: the ban served its time; clear it in the store so
            # the admin panel shows the truth, then fall through to the other
            # checks.
            set_policy(username, {"locked": False})
            pol = get_policy(username)
        else:
            reason = pol["lock_reason"] or "This account is temporarily unavailable."
            if until is not None:
                reason += " (until " + time.strftime(
                    "%b %d, %H:%M", time.localtime(int(until))) + ")"
            return {"allowed": False, "reason": reason}

    ttl_cap: Optional[int] = None

    # --- allowed hours -------------------------------------------------------
    if pol["allowed_hours"]:
        window = _parse_window(pol["allowed_hours"])
        if window is not None:            # malformed = ignored, never a lockout
            lt = time.localtime(t)
            inside, remaining_min = _window_state(window, lt.tm_hour * 60 + lt.tm_min)
            if not inside:
                return {"allowed": False,
                        "reason": "Sign-in for this profile is available "
                                  + _fmt_window(pol["allowed_hours"]) + "."}
            ttl_cap = remaining_min * 60

    # --- per-session time cap ------------------------------------------------
    if pol["session_minutes"]:
        cap = int(pol["session_minutes"]) * 60
        ttl_cap = cap if ttl_cap is None else min(ttl_cap, cap)

    # --- daily budget ----------------------------------------------------------
    if pol["daily_minutes"]:
        import usage_meter
        remaining = int(pol["daily_minutes"]) * 60 - usage_meter.used_today(username, now=t)
        if remaining <= 0:
            return {"allowed": False,
                    "reason": "Daily time for this profile is used up. "
                              "It resets at midnight."}
        ttl_cap = remaining if ttl_cap is None else min(ttl_cap, remaining)

    return {"allowed": True, "ttl_cap": ttl_cap}


def tick_usage(username: str, now: Optional[float] = None) -> bool:
    """Heartbeat hook for /api/auth/status: advance the daily meter for a
    signed-in NON-owner. Returns True when the budget is exhausted (the
    caller ends the session). Profiles without a daily cap cost nothing --
    no meter entry, no disk writes."""
    try:
        pol = get_policy(username)
        if not pol["daily_minutes"]:
            return False
        import usage_meter
        used = usage_meter.tick(username, now=now)
        return used >= int(pol["daily_minutes"]) * 60
    except Exception as exc:
        print(f"[ACCESS] usage tick failed for {username}: {exc}")
        return False   # metering trouble must never lock anyone out


def socials_allowed(username: str) -> bool:
    """False only when an explicit policy turns socials off for this profile."""
    try:
        return bool(get_policy(username)["socials_allowed"])
    except Exception:
        return True   # a broken store must never brick the owner's own app
