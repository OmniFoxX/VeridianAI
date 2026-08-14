# -*- coding: utf-8 -*-
"""Per-profile key wrapping.

WHAT THIS IS
------------
Each profile gets a random 32-byte DATA KEY (DEK) that encrypts its content.
The DEK itself is never stored in the clear -- it is WRAPPED under one or more
key-encryption keys (KEKs), and any one of them opens it:

    KEK_user      scrypt(password, salt)      -- always present
    KEK_recovery  a machine-held random key   -- only if recovery was chosen
    KEK_token     scrypt(raw API token, salt) -- one per API key, see below

Wrapping rather than deriving directly is what makes a password change cheap:
re-wrap the same DEK under a new KEK and not one byte of user data is touched.
Deriving the data key from the password directly would mean re-encrypting a
person's entire history every time they changed it.

THE ASYMMETRY THAT MATTERS
--------------------------
Dropping a wrap is unilateral: delete it. Adding one requires the PLAINTEXT
DEK, which requires somebody who can already open it.

So an owner can surrender their recovery capability alone and instantly, and
cannot grant it back to themselves -- that needs the user's password. "Giving
up the right is easy; reclaiming it needs the other party" is therefore
arithmetic rather than a rule this code has to enforce. Nothing here has to be
trusted for that property to hold.

WHAT THIS DOES NOT DO
---------------------
It does not prevent destruction. Encryption governs reading, not deleting;
anyone with filesystem access can delete the file this module writes. Dual
control on the Burn button is worth having, but it is POLICY, and should never
be described as though the mathematics enforced it.

DESIGN NOTES
------------
- Path-based and dependency-free (stdlib + cryptography). No config, no users,
  no sage_engine -- so it is fully testable in isolation, and so a bug here is
  a bug here rather than an interaction.
- Fernet for the wraps: authenticated, so a wrong password FAILS rather than
  yielding plausible garbage that would corrupt data downstream.
- The KDF salt is stored in the file and is SEPARATE from the password-hash
  salt in users.py. Deriving a key and a login verifier from the same
  input+salt would make the stored verifier a head start on the key.
- KDF parameters are stored alongside, so a future parameter change can still
  read old files.
- Atomic write + 0600, mirroring atrest.py and auth.py.

Pure-ASCII source.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from typing import Dict, Optional

__all__ = [
    "KeywrapError", "BadKey", "NoKeywrap",
    "create", "load_dek", "info", "exists", "destroy",
    "rewrap_password", "set_recovery", "drop_recovery", "has_recovery",
    "set_token_wrap", "drop_token_wrap",
    "new_recovery_key", "KEYWRAP_NAME",
]

KEYWRAP_NAME = ".keywrap.json"
VERSION = 1
DEK_BYTES = 32
SALT_BYTES = 16

# Same primitive and cost as users.py password hashing, deliberately: it is
# already vetted here and the threat (offline guessing of a user password) is
# identical. maxmem is set explicitly because n=2**14, r=8 needs ~16 MB and
# Python's default cap can be lower on some builds.
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32, "maxmem": 64 * 1024 * 1024}


class KeywrapError(Exception):
    """Base class for this module."""


class NoKeywrap(KeywrapError):
    """No keywrap file exists for this profile."""


class BadKey(KeywrapError):
    """The supplied password / key did not open any wrap.

    Deliberately does not say WHICH wrap failed or whether others exist:
    a caller distinguishing "wrong password" from "no recovery configured"
    would leak the profile's configuration to whoever is guessing.
    """


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def _kek_from_secret(secret: str, salt: bytes, params: Optional[Dict] = None) -> bytes:
    """Derive a Fernet-shaped key from a password or token."""
    p = dict(_SCRYPT)
    if params:
        for k in ("n", "r", "p", "dklen"):
            if k in params:
                p[k] = int(params[k])
    raw = hashlib.scrypt((secret or "").encode("utf-8"), salt=salt, **p)
    return base64.urlsafe_b64encode(raw)


def _fernet(key: bytes):
    from cryptography.fernet import Fernet
    return Fernet(key)


def new_recovery_key() -> bytes:
    """A fresh machine recovery key, Fernet-shaped.

    Where this is STORED is the caller's problem, and the honest answer is
    'encrypted with the system at-rest key' -- the owner controls the machine,
    so 'the owner can recover' already means 'whoever holds the machine can
    recover'. Pretending otherwise would be theatre.
    """
    from cryptography.fernet import Fernet
    return Fernet.generate_key()


def wrap_key_with_password(key: bytes, password: str) -> Dict:
    """Wrap key material under a passphrase, as a self-describing document.

    For data export: the archive carries the WRAP, not the key, so the file is
    inert without the passphrase. Same scrypt parameters and the same shape as
    a profile's own keywrap on purpose -- one key-derivation path in this
    codebase, not a second one invented for exports that nobody remembers to
    strengthen when the first is strengthened.
    """
    if not password:
        raise KeywrapError("a passphrase is required to wrap a key")
    if not key:
        raise KeywrapError("there is no key material to wrap")
    salt = secrets.token_bytes(SALT_BYTES)
    return {
        "version": VERSION,
        "kdf": "scrypt",
        "n": _SCRYPT["n"], "r": _SCRYPT["r"], "p": _SCRYPT["p"],
        "dklen": _SCRYPT["dklen"],
        "salt": salt.hex(),
        "wrapped": _fernet(_kek_from_secret(password, salt))
                   .encrypt(key).decode("ascii"),
    }


def unwrap_key_with_password(doc: Dict, password: str) -> bytes:
    """Open a wrap_key_with_password document.

    Raises BadKey on a wrong passphrase and KeywrapError on a malformed
    document -- never returns None. A caller that got None would have to guess
    which of the two happened, and would guess wrong under pressure.
    """
    from cryptography.fernet import InvalidToken
    d = doc or {}
    try:
        salt = bytes.fromhex(str(d.get("salt", "")))
    except ValueError:
        raise KeywrapError("this does not look like a wrapped-key document")
    blob = d.get("wrapped")
    if not salt or not blob:
        raise KeywrapError("this does not look like a wrapped-key document")
    params = {k: d[k] for k in ("n", "r", "p", "dklen") if k in d}
    try:
        return _fernet(_kek_from_secret(password, salt, params)) \
               .decrypt(str(blob).encode("ascii"))
    except InvalidToken:
        raise BadKey("the passphrase does not open this export")


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def _read(path) -> Dict:
    if not os.path.exists(path):
        raise NoKeywrap("no keywrap at %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise KeywrapError("keywrap unreadable: %s" % e)
    if not isinstance(data, dict) or "wraps" not in data:
        raise KeywrapError("keywrap malformed")
    return data


def _write(path, data: Dict) -> None:
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)      # POSIX; best-effort no-op on Windows
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def exists(path) -> bool:
    return os.path.exists(path)


def create(path, password: str, *, recovery_key: Optional[bytes] = None,
           dek: Optional[bytes] = None) -> bytes:
    """Create a keywrap and return the DEK.

    `dek` is injectable for migration -- adopting an existing key rather than
    minting one -- and for tests. Left None, a fresh key is generated.
    """
    if not password:
        raise KeywrapError("a password is required to create a keywrap")
    if exists(path):
        raise KeywrapError("keywrap already exists at %s" % path)

    key = dek or secrets.token_bytes(DEK_BYTES)
    salt = secrets.token_bytes(SALT_BYTES)
    kek = _kek_from_secret(password, salt)

    data = {
        "version": VERSION,
        "kdf": "scrypt",
        "n": _SCRYPT["n"], "r": _SCRYPT["r"], "p": _SCRYPT["p"],
        "dklen": _SCRYPT["dklen"],
        "salt": salt.hex(),
        "recovery_enabled": bool(recovery_key),
        "wraps": {"user": _fernet(kek).encrypt(key).decode("ascii")},
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if recovery_key:
        data["wraps"]["recovery"] = _fernet(recovery_key).encrypt(key).decode("ascii")
    _write(path, data)
    return key


def _unwrap(data: Dict, kek: bytes, which: str) -> Optional[bytes]:
    """Try one wrap. None = this credential did not open it.

    Catches InvalidToken ONLY. `except Exception` here would swallow a
    programming error -- a TypeError, a bad key length -- and report it as
    "wrong password", which is precisely the class of silent failure that
    makes a security bug invisible. A real fault should crash loudly.
    """
    from cryptography.fernet import InvalidToken
    blob = data.get("wraps", {}).get(which)
    if not blob:
        return None
    try:
        return _fernet(kek).decrypt(blob.encode("ascii"))
    except InvalidToken:
        return None


def load_dek(path, *, password: Optional[str] = None,
             recovery_key: Optional[bytes] = None,
             token: Optional[str] = None,
             token_prefix: Optional[str] = None) -> bytes:
    """Open the DEK with whichever credential is supplied.

    Exactly one of password / recovery_key / (token + token_prefix).
    Raises BadKey on failure -- never returns None, so a caller cannot
    accidentally treat a failed unwrap as an empty key.
    """
    data = _read(path)
    salt = bytes.fromhex(data.get("salt", ""))
    params = data

    if password is not None:
        dek = _unwrap(data, _kek_from_secret(password, salt, params), "user")
    elif recovery_key is not None:
        dek = _unwrap(data, recovery_key, "recovery")
    elif token is not None and token_prefix:
        tw = data.get("wraps", {}).get("tokens", {}) or {}
        blob = tw.get(token_prefix)
        if not blob:
            raise BadKey("no wrap for that token")
        from cryptography.fernet import InvalidToken
        try:
            dek = _fernet(_kek_from_secret(token, salt, params)).decrypt(
                blob.encode("ascii"))
        except InvalidToken:
            dek = None
    else:
        raise KeywrapError("supply a password, a recovery key, or a token")

    if not dek:
        raise BadKey("could not open the keywrap with the supplied credential")
    return dek


def info(path) -> Dict:
    """Metadata only. Never returns key material or a wrap blob."""
    data = _read(path)
    tw = data.get("wraps", {}).get("tokens", {}) or {}
    return {
        "version": data.get("version"),
        "created": data.get("created"),
        "recovery_enabled": bool(data.get("wraps", {}).get("recovery")),
        "token_wraps": sorted(tw.keys()),
        "kdf": data.get("kdf"),
    }


def rewrap_password(path, dek: bytes, new_password: str) -> None:
    """Re-wrap an ALREADY-OPEN DEK under a new password.

    The caller must have opened the DEK first -- normally from the live
    session, which is why changing your own password needs no old password
    here: the session already holds the key.

    A fresh salt is generated, so the new wrap shares nothing with the old.
    """
    if not new_password:
        raise KeywrapError("a password is required")
    data = _read(path)
    salt = secrets.token_bytes(SALT_BYTES)
    data["salt"] = salt.hex()
    data["wraps"]["user"] = _fernet(
        _kek_from_secret(new_password, salt)).encrypt(dek).decode("ascii")

    # Token wraps are derived from the SAME salt, so they must be rebuilt --
    # and they cannot be, because the raw tokens are not recoverable. Drop
    # them: the keys stop working and have to be re-issued, which is correct
    # and much better than leaving wraps that silently fail to open.
    dropped = sorted((data["wraps"].get("tokens") or {}).keys())
    if dropped:
        data["wraps"]["tokens"] = {}
    data["token_wraps_invalidated"] = dropped or data.get("token_wraps_invalidated", [])
    _write(path, data)


def set_recovery(path, dek: bytes, recovery_key: bytes) -> None:
    """Add or replace the recovery wrap. Requires an already-open DEK --
    which is the whole point: recovery cannot be granted by someone who
    cannot already read the data."""
    data = _read(path)
    data["wraps"]["recovery"] = _fernet(recovery_key).encrypt(dek).decode("ascii")
    data["recovery_enabled"] = True
    _write(path, data)


def drop_recovery(path) -> bool:
    """Remove the recovery wrap. Unilateral, needs no key, irreversible
    without the user's password. Returns True if one was removed."""
    data = _read(path)
    had = bool(data.get("wraps", {}).pop("recovery", None))
    data["recovery_enabled"] = False
    if had:
        data["recovery_dropped"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _write(path, data)
    return had


def has_recovery(path) -> bool:
    try:
        return bool(_read(path).get("wraps", {}).get("recovery"))
    except KeywrapError:
        return False


def set_token_wrap(path, dek: bytes, prefix: str, raw_token: str) -> None:
    """Wrap the DEK for an API token, at ISSUE time.

    An API caller has no session and no password, so without this a bearer
    token reaches a namespace whose data it cannot read. The raw token exists
    exactly once -- here -- so this is the only moment it can be done.
    """
    if not prefix or not raw_token:
        raise KeywrapError("prefix and raw token are required")
    data = _read(path)
    salt = bytes.fromhex(data.get("salt", ""))
    data["wraps"].setdefault("tokens", {})[prefix] = _fernet(
        _kek_from_secret(raw_token, salt, data)).encrypt(dek).decode("ascii")
    _write(path, data)


def clear_token_wraps(path) -> list:
    """Drop EVERY token wrap. Returns the prefixes removed.

    Used when a profile's tokens are rotated wholesale: the old keys are gone,
    so wraps that could still open the DEK for them must go with them. Leaving
    a wrap whose token no longer exists is harmless today and is exactly the
    kind of orphan that becomes a question nobody can answer later.
    """
    data = _read(path)
    tw = data.get("wraps", {}).get("tokens", {}) or {}
    dropped = sorted(tw.keys())
    if dropped:
        data["wraps"]["tokens"] = {}
        _write(path, data)
    return dropped


def drop_token_wrap(path, prefix: str) -> bool:
    """Remove one token's wrap -- called when that key is revoked."""
    data = _read(path)
    tw = data.get("wraps", {}).get("tokens", {}) or {}
    had = bool(tw.pop(prefix, None))
    if had:
        data["wraps"]["tokens"] = tw
        _write(path, data)
    return had


def destroy(path) -> bool:
    """Delete the keywrap.

    After this the profile's data is unreadable by anyone, even if the files
    are later recovered from a disk image or a backup -- which is what makes
    ZDR burn a stronger promise than deleting files alone.
    """
    if not os.path.exists(path):
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False
