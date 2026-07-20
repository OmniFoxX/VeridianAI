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
