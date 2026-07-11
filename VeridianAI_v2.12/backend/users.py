"""Per-user account store for OracleAI multi-user mode (identity layer).

Accounts live in sage_data/.users.json (OUTSIDE the project, 0600). Each entry is
{username, salt, hash, algo, created, is_owner, ns}. Passwords are NEVER stored --
only a salted scrypt hash (pbkdf2 fallback), and verification is constant-time.

`ns` is a filesystem-safe per-user namespace id, reserved here and consumed by the
Phase B data-isolation layer to keep each user's conversations / archives / images
separate. The first account created is the OWNER (the existing single-user data
maps to it).

stdlib + secret_locator only, so it imports safely and early.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time

_STORE_NAME = ".users.json"

# scrypt work factors (memory-hard). n*r*p drives cost; these are a sane desktop
# default. Stored per-entry so factors can change without breaking old hashes.
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
_PBKDF2_ITERS = 240000


def _store_path():
    """sage_data/.users.json, migrated out of the project if a legacy copy exists."""
    try:
        from config import DATA_DIR, BACKEND_DIR
        from secret_locator import resolve_secret_file
        return str(resolve_secret_file(_STORE_NAME, DATA_DIR, BACKEND_DIR,
                                       announce=False))
    except Exception:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), _STORE_NAME)


def _hash_password(password, salt):
    pw = (password or "").encode("utf-8")
    try:
        h = hashlib.scrypt(pw, salt=salt, **_SCRYPT)
        return h.hex(), "scrypt"
    except Exception:
        h = hashlib.pbkdf2_hmac("sha256", pw, salt, _PBKDF2_ITERS, dklen=32)
        return h.hex(), "pbkdf2_240k"


def _verify_password(password, salt_hex, hash_hex, algo):
    try:
        salt = bytes.fromhex(salt_hex)
        pw = (password or "").encode("utf-8")
        if algo == "scrypt":
            h = hashlib.scrypt(pw, salt=salt, **_SCRYPT).hex()
        else:
            h = hashlib.pbkdf2_hmac("sha256", pw, salt, _PBKDF2_ITERS, dklen=32).hex()
        return hmac.compare_digest(h, hash_hex)  # constant-time
    except Exception:
        return False


def _ns_for(username):
    base = re.sub(r"[^a-zA-Z0-9_-]", "_", (username or "").strip().lower())[:40] or "user"
    return base + "_" + secrets.token_hex(4)  # fs-safe + collision-resistant


def _load():
    p = _store_path()
    if not os.path.exists(p):
        return {"users": []}
    try:
        with open(p, "r", encoding="utf-8") as f:
            store = json.load(f)
        if not isinstance(store, dict) or "users" not in store:
            return {"users": []}
        return store
    except Exception:
        return {"users": []}


def _save(store):
    p = _store_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def _find(store, username):
    u = (username or "").strip().lower()
    for x in store.get("users", []):
        if x.get("username", "").lower() == u:
            return x
    return None


def any_users():
    """True if at least one account exists (drives the first-run owner-setup flow)."""
    return bool(_load().get("users"))


def user_exists(username):
    return _find(_load(), username) is not None


def list_users():
    return [{"username": x.get("username"), "is_owner": x.get("is_owner", False),
             "created": x.get("created"), "ns": x.get("ns"),
             # v2.13 Access Controls: surfaced so the owner panel can render
             # lock state / restrictions at a glance. {} = unrestricted.
             "access": dict(x["access"]) if isinstance(x.get("access"), dict) else {}}
            for x in _load().get("users", [])]


# --- v2.13 Access Controls (parental / manager) --------------------------------
# users.py owns the FILE; access_policy.py owns the SEMANTICS (defaults,
# validation, login gating). These two accessors are the only bridge.

def get_access(username):
    """The raw stored access record ({} if none). None = no such user."""
    x = _find(_load(), username)
    if x is None:
        return None
    a = x.get("access")
    return dict(a) if isinstance(a, dict) else {}


def set_access(username, access):
    """Persist an access record. Refuses owner accounts -- the owner is never
    restricted (same protection stance as delete_user)."""
    if not isinstance(access, dict):
        return {"success": False, "error": "access must be an object"}
    store = _load()
    x = _find(store, username)
    if x is None:
        return {"success": False, "error": "no such user"}
    if x.get("is_owner"):
        return {"success": False, "error": "access controls do not apply to the owner account"}
    x["access"] = dict(access)
    _save(store)
    return {"success": True, "username": x.get("username")}


def create_user(username, password, *, is_owner=False):
    username = (username or "").strip()
    if not username:
        return {"success": False, "error": "username required"}
    if not (password or ""):
        return {"success": False, "error": "password required"}
    store = _load()
    if _find(store, username) is not None:
        return {"success": False, "error": "user already exists"}
    salt = secrets.token_bytes(16)
    hsh, algo = _hash_password(password, salt)
    owner = bool(is_owner) or not store.get("users")  # first account is the owner
    entry = {"username": username, "salt": salt.hex(), "hash": hsh, "algo": algo,
             "created": int(time.time()), "is_owner": owner, "ns": _ns_for(username)}
    store.setdefault("users", []).append(entry)
    _save(store)
    return {"success": True, "username": username, "is_owner": owner, "ns": entry["ns"]}


def verify_user(username, password):
    x = _find(_load(), username)
    if x is None:
        # Hash anyway to keep timing ~constant for unknown vs known users.
        _hash_password(password, b"0" * 16)
        return {"success": False, "error": "invalid credentials"}
    if _verify_password(password, x["salt"], x["hash"], x.get("algo", "scrypt")):
        return {"success": True, "username": x["username"],
                "is_owner": x.get("is_owner", False), "ns": x.get("ns")}
    return {"success": False, "error": "invalid credentials"}


def set_password(username, new_password):
    if not (new_password or ""):
        return {"success": False, "error": "password required"}
    store = _load()
    x = _find(store, username)
    if x is None:
        return {"success": False, "error": "no such user"}
    salt = secrets.token_bytes(16)
    x["salt"], (h, a) = salt.hex(), _hash_password(new_password, salt)
    x["hash"], x["algo"] = h, a
    _save(store)
    return {"success": True}


def delete_user(username):
    """Remove an account. Refuses to delete an owner (protects the install).
    Returns the deleted entry's ns so the caller can optionally wipe its data."""
    store = _load()
    x = _find(store, username)
    if x is None:
        return {"success": False, "error": "no such user"}
    if x.get("is_owner"):
        return {"success": False, "error": "cannot delete the owner account"}
    u = (username or "").strip().lower()
    store["users"] = [e for e in store.get("users", [])
                      if (e.get("username", "").lower() != u)]
    _save(store)
    return {"success": True, "username": x.get("username"), "ns": x.get("ns")}
