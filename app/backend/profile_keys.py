# -*- coding: utf-8 -*-
"""Per-profile keys: the namespace-aware layer over keywrap.py.

keywrap.py is deliberately pure -- paths in, key material out, no knowledge of
this application. This module is the part that knows what a *profile* is:
where its keywrap lives, which profiles get one at all, and where the machine's
recovery key is kept.

Kept separate so keywrap.py stays testable in isolation, and so the policy
decisions below sit in one readable place rather than being spread through
users.py and session.py.

THE OWNER HAS NO PROFILE KEY, AND THAT IS DELIBERATE
----------------------------------------------------
`DATA_DIR`'s root IS the shared store that sage_daemon and the overseer read.
Locking it behind the owner's password would stop background work every time
the owner logged out. In a commercial install the owner is the administrator
and this is the right shape; for a solo user, their *projects* are non-owner
profiles and those do get real keys.

So: the owner's data is protected by control of the machine, not by their
password. Said plainly here because it is the kind of limitation that is easy
to imply otherwise, and because every function below enforces it -- `ns=None`
returns None rather than quietly creating a key nothing would use.

THE RECOVERY KEY
----------------
Stored at `sage_data/.recovery_key`, encrypted with the system at-rest key.
That is honest rather than weak: "the owner can recover" already means
"whoever holds this machine can recover", and pretending the recovery key is
protected from the machine's administrator would be theatre.

Pure-ASCII source.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import keywrap
import ns_guard

__all__ = [
    "keywrap_path", "has_profile_key", "profile_key_info",
    "create_for_profile", "unlock", "unlock_with_recovery", "unlock_with_token",
    "rewrap_password", "enable_recovery", "disable_recovery",
    "add_token_wrap", "drop_token_wrap", "clear_token_wraps",
    "destroy_for_profile",
    "machine_recovery_key", "RECOVERY_KEY_NAME",
]

RECOVERY_KEY_NAME = ".recovery_key"


def _data_dir() -> Optional[Path]:
    try:
        from config import DATA_DIR
        return Path(DATA_DIR)
    except Exception:
        return None


def _user_dir(ns) -> Optional[Path]:
    """sage_data/users/<ns>, or None for the owner / shared store.

    FAILS CLOSED on a bad namespace. Until v2.15 the fallback below read
    `base / "users" / str(ns)` -- building a key path from an UNVALIDATED
    namespace whenever importing sage_engine happened to raise. Every
    keywrap.py path flows through this function, so that one `except` branch
    was the reason six keywrap alerts were real rather than noise. The
    namespace is now validated FIRST, so both branches are safe and the
    resilience the fallback was there to provide is kept.
    """
    if not ns:
        return None
    ns = ns_guard.safe_ns(ns)          # raises InvalidNamespace; never scrubs
    try:
        import sage_engine
        d = sage_engine.user_data_dir(ns)
        if d:
            return Path(d)
    except ns_guard.InvalidNamespace:
        raise                           # never downgrade a containment failure
    except Exception:
        pass                            # sage_engine unavailable -> fall back
    base = _data_dir()
    return (base / "users" / ns) if base else None


def keywrap_path(ns) -> Optional[Path]:
    """Where this profile's keywrap lives. None for the owner -- see module doc."""
    d = _user_dir(ns)
    return (d / keywrap.KEYWRAP_NAME) if d else None


def has_profile_key(ns) -> bool:
    p = keywrap_path(ns)
    return bool(p and keywrap.exists(p))


def profile_key_info(ns) -> Optional[dict]:
    """Metadata only, or None if this profile has no key."""
    p = keywrap_path(ns)
    if not p or not keywrap.exists(p):
        return None
    try:
        return keywrap.info(p)
    except keywrap.KeywrapError:
        return None


# ---------------------------------------------------------------------------
# Machine recovery key
# ---------------------------------------------------------------------------

def machine_recovery_key(create: bool = True) -> Optional[bytes]:
    """The owner's recovery key, at rest under the system key.

    Returns None if it does not exist and create=False, or if the data
    directory cannot be resolved. Callers treat None as "recovery unavailable"
    rather than an error: a profile without a recovery wrap is a legitimate
    and intended state.
    """
    base = _data_dir()
    if not base:
        return None
    p = base / RECOVERY_KEY_NAME
    try:
        import atrest
        # SYSTEM TIER, necessarily: this IS a key, and it must be readable
        # before any profile is unlocked. Wrapping it under a profile key
        # would be circular.
        if p.exists():
            return atrest.decrypt_bytes(p.read_bytes()).strip()
        if not create:
            return None
        key = keywrap.new_recovery_key()
        base.mkdir(parents=True, exist_ok=True)
        tmp = str(p) + ".tmp"
        with open(tmp, "wb") as f:
            f.write(atrest.encrypt_bytes(key))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        return key
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def create_for_profile(ns, password: str, *, recovery: bool = True,
                       adopt_dek: Optional[bytes] = None) -> Optional[bytes]:
    """Create this profile's key. Returns the DEK, or None for the owner.

    `recovery=True` wraps the DEK for the owner as well, so a lost password is
    recoverable. False means **nobody but this user can ever read it** -- the
    caller is responsible for having said so in words the user understood.

    `adopt_dek` lets migration bring an existing key under management instead
    of minting a new one.
    """
    p = keywrap_path(ns)
    if not p:
        return None                      # owner: system key, by design
    if keywrap.exists(p):
        raise keywrap.KeywrapError("profile %r already has a key" % (ns,))
    rk = machine_recovery_key() if recovery else None
    p.parent.mkdir(parents=True, exist_ok=True)
    return keywrap.create(p, password, recovery_key=rk, dek=adopt_dek)


def unlock(ns, password: str) -> Optional[bytes]:
    """Open a profile's DEK with its password. None if it has no key."""
    p = keywrap_path(ns)
    if not p or not keywrap.exists(p):
        return None
    return keywrap.load_dek(p, password=password)


def unlock_with_recovery(ns) -> Optional[bytes]:
    """Open a profile using the machine recovery key, if it was enabled."""
    p = keywrap_path(ns)
    if not p or not keywrap.exists(p):
        return None
    rk = machine_recovery_key(create=False)
    if not rk:
        raise keywrap.BadKey("no recovery key on this machine")
    return keywrap.load_dek(p, recovery_key=rk)


def unlock_with_token(ns, prefix: str, raw_token: str) -> Optional[bytes]:
    p = keywrap_path(ns)
    if not p or not keywrap.exists(p):
        return None
    return keywrap.load_dek(p, token=raw_token, token_prefix=prefix)


def rewrap_password(ns, dek: bytes, new_password: str) -> bool:
    p = keywrap_path(ns)
    if not p or not keywrap.exists(p):
        return False
    keywrap.rewrap_password(p, dek, new_password)
    return True


def enable_recovery(ns, dek: bytes) -> bool:
    """Grant the owner recovery. REQUIRES an already-open DEK, so it can only
    be done by somebody who can already read the profile."""
    p = keywrap_path(ns)
    rk = machine_recovery_key()
    if not p or not keywrap.exists(p) or not rk:
        return False
    keywrap.set_recovery(p, dek, rk)
    return True


def disable_recovery(ns) -> bool:
    """Remove the owner's recovery wrap. Needs no key: giving up access is
    unilateral. Getting it back is not -- that needs the user's password."""
    p = keywrap_path(ns)
    if not p or not keywrap.exists(p):
        return False
    return keywrap.drop_recovery(p)


def add_token_wrap(ns, dek: bytes, prefix: str, raw_token: str) -> bool:
    p = keywrap_path(ns)
    if not p or not keywrap.exists(p):
        return False
    keywrap.set_token_wrap(p, dek, prefix, raw_token)
    return True


def drop_token_wrap(ns, prefix: str) -> bool:
    p = keywrap_path(ns)
    if not p or not keywrap.exists(p):
        return False
    return keywrap.drop_token_wrap(p, prefix)


def clear_token_wraps(ns) -> list:
    """Drop all of a profile's token wraps. Returns the prefixes removed."""
    p = keywrap_path(ns)
    if not p or not keywrap.exists(p):
        return []
    return keywrap.clear_token_wraps(p)


def destroy_for_profile(ns) -> bool:
    """Destroy a profile's key.

    After this its data is unreadable by anyone, even from a disk image or an
    old backup -- which is what makes ZDR burn a stronger promise than deleting
    files. Call it BEFORE removing the files, so an interruption leaves data
    that cannot be read rather than a key with nothing to open.
    """
    p = keywrap_path(ns)
    if not p:
        return False
    return keywrap.destroy(p)
