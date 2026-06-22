"""In-memory session store for OracleAI multi-user mode.

Maps an opaque 256-bit session token -> {username, ns, is_owner, created, expires}.

In-memory BY DESIGN: sessions clear on restart (everyone re-logs-in), which is the
right trade for a local desktop app -- it avoids persisting live session tokens to
disk at all, so a stolen disk yields no usable sessions. Tokens themselves are
cryptographically random and never derived from the password.
"""
import secrets
import threading
import time

_SESSIONS = {}
_LOCK = threading.Lock()
_DEFAULT_TTL = 7 * 24 * 3600  # 7 days


def _now():
    return int(time.time())


def create_session(user, ttl=_DEFAULT_TTL):
    """Create a session for a verified user dict ({username, ns, is_owner}). Returns
    the opaque token to hand back as an HttpOnly cookie."""
    token = secrets.token_urlsafe(32)
    with _LOCK:
        _SESSIONS[token] = {
            "username": user.get("username"),
            "ns": user.get("ns"),
            "is_owner": bool(user.get("is_owner", False)),
            "created": _now(),
            "expires": _now() + int(ttl),
        }
    return token


def get_session(token):
    """Return a COPY of the session dict if the token is valid and unexpired, else
    None. Expired tokens are pruned on access."""
    if not token:
        return None
    with _LOCK:
        s = _SESSIONS.get(token)
        if not s:
            return None
        if s["expires"] < _now():
            _SESSIONS.pop(token, None)
            return None
        return dict(s)


def destroy_session(token):
    with _LOCK:
        _SESSIONS.pop(token, None)


def destroy_user_sessions(username):
    """Invalidate every session for a username (e.g. after a password change)."""
    u = (username or "").lower()
    with _LOCK:
        for t in [t for t, s in _SESSIONS.items()
                  if (s.get("username") or "").lower() == u]:
            _SESSIONS.pop(t, None)


def active_count():
    with _LOCK:
        now = _now()
        # opportunistic prune
        for t in [t for t, s in _SESSIONS.items() if s["expires"] < now]:
            _SESSIONS.pop(t, None)
        return len(_SESSIONS)
