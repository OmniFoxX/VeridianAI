"""In-memory session store for VeridianAI multi-user mode.

Maps an opaque 256-bit session token -> {username, ns, is_owner, created, expires}.

In-memory BY DESIGN: sessions clear on restart (everyone re-logs-in), which is the
right trade for a local desktop app -- it avoids persisting live session tokens to
disk at all, so a stolen disk yields no usable sessions. Tokens themselves are
cryptographically random and never derived from the password.
"""
import secrets
import threading
import time
from fastapi import Request, HTTPException

# Canonical auth-cookie name (single source of truth). main.py and any
# router doing its own cookie check (e.g. skill_api._owner_guard) import
# THIS constant rather than re-declaring the literal.
AUTH_COOKIE = "oai_session"

_SESSIONS = {}
_LOCK = threading.Lock()
_DEFAULT_TTL = 7 * 24 * 3600  # 7 days

# v2.14.3 -- per-profile data keys, held for the life of a session.
#
# KEPT IN A SEPARATE MAP, NOT IN THE SESSION RECORD. get_session() returns the
# record, and records get logged, echoed into debug output and serialised into
# responses. A key riding inside one will eventually be printed by code that
# had no idea it was carrying a secret. Here it can only be reached by asking
# for it by name.
#
# In memory only, like _SESSIONS itself: nothing to scrub from disk, and the
# key is gone the moment the process ends or the session is dropped.
_DEKS = {}


def set_session_dek(token, dek):
    """Attach an unwrapped data key to a live session."""
    if not token or not dek:
        return False
    with _LOCK:
        if token not in _SESSIONS:
            return False
        _DEKS[token] = dek
    return True


def get_session_dek(token):
    """The data key for this session, or None if there is none.

    None is a normal answer, not an error: the owner has no profile key, and
    a profile predating per-profile keys has none either.
    """
    if not token:
        return None
    with _LOCK:
        return _DEKS.get(token)


def has_session_dek(token) -> bool:
    with _LOCK:
        return token in _DEKS


def _forget_locked(token):
    """Pop one token from BOTH maps. The caller must hold _LOCK.

    Returns the namespace that token belonged to, so the caller can decide
    whether that profile's key may now leave the process entirely. Every path
    that removes a session goes through here -- including expiry, which used
    to drop the session record and leave the data key resident in memory.
    """
    s = _SESSIONS.pop(token, None)
    _DEKS.pop(token, None)
    return (s or {}).get("ns")


def ns_has_live_dek(ns) -> bool:
    """Is any unexpired session still holding this profile's data key?"""
    if not ns:
        return False
    key = str(ns)
    now = _now()
    with _LOCK:
        for t, s in _SESSIONS.items():
            if s.get("expires", 0) < now:
                continue
            if str(s.get("ns") or "") != key:
                continue
            if t in _DEKS:
                return True
    return False


def _release_keys(namespaces):
    """Drop a profile's key from the at-rest layer once nobody holds it.

    Registration lasts for the length of a LOGIN, not a request, so the only
    correct moment to drop it is when the LAST session for that profile goes
    away -- logout, expiry, or invalidation. Releasing per session would sign
    one tab out and quietly break another.

    Must not be called while holding _LOCK: ns_has_live_dek takes it.
    """
    for ns in {n for n in namespaces if n}:
        if ns_has_live_dek(ns):
            continue
        try:
            import atrest
            atrest.forget_profile_key(ns)
        except Exception:
            # atrest may be absent in standalone use of this module. A key
            # that cannot be dropped is a memory-residency question, never a
            # correctness one, and must never raise into a logout path.
            pass


def _now():
    return int(time.time())


def owner_or_granted(request: Request, cookie_name: str, cap: str = None) -> bool:
    """True if this request's session belongs to the owner, or (when cap is
    given) belongs to a non-owner explicitly granted that capability via
    Access Controls. Fetches the session directly from the cookie every
    call — no dependency on middleware-stamped request state, so this works
    identically for HTTP requests, WebSocket handshakes, or anything else
    that carries the cookie."""
    s = get_session(request.cookies.get(cookie_name))
    if not s:
        return False
    if s.get("is_owner"):
        return True
    if cap:
        try:
            import access_policy as _ap
            return bool(_ap.admin_granted(s.get("username"), cap))
        except Exception:
            return False  # fail-closed: a broken policy store never mints admin power
    return False


def create_session(user, ttl=_DEFAULT_TTL, must_change=False):
    """Create a session for a verified user dict ({username, ns, is_owner}). Returns
    the opaque token to hand back as an HttpOnly cookie.

    must_change=True marks a session whose password FAILED the current policy
    at login (legacy weak password): the session is valid but the middleware
    confines it to the auth surface until the password is changed."""
    token = secrets.token_urlsafe(32)
    with _LOCK:
        _SESSIONS[token] = {
            "username": user.get("username"),
            "ns": user.get("ns"),
            "is_owner": bool(user.get("is_owner", False)),
            "created": _now(),
            "expires": _now() + int(ttl),
            "must_change": bool(must_change),
        }
    return token


def get_session(token):
    """Return a COPY of the session dict if the token is valid and unexpired, else
    None. Expired tokens are pruned on access."""
    if not token:
        return None
    expired_ns = None
    with _LOCK:
        s = _SESSIONS.get(token)
        if not s:
            return None
        if s["expires"] < _now():
            expired_ns = _forget_locked(token)   # the key expires with it
            out = None
        else:
            out = dict(s)
    if expired_ns:
        _release_keys([expired_ns])
    return out


def destroy_session(token):
    with _LOCK:
        ns = _forget_locked(token)          # the key dies with the session
    _release_keys([ns])


def destroy_user_sessions(username):
    """Invalidate every session for a username (e.g. after a password change)."""
    u = (username or "").lower()
    with _LOCK:
        gone = [_forget_locked(t) for t in
                [t for t, s in _SESSIONS.items()
                 if (s.get("username") or "").lower() == u]]
    _release_keys(gone)


def active_count():
    with _LOCK:
        now = _now()
        # opportunistic prune
        gone = [_forget_locked(t) for t in
                [t for t, s in _SESSIONS.items() if s["expires"] < now]]
        n = len(_SESSIONS)
    _release_keys(gone)
    return n
