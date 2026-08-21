"""At-rest encryption for sensitive on-disk data.

Phase 1 target: chat archives (full saved conversations).

Design
------
* Uses its OWN Fernet key (``.atrest_key``), domain-separated from the
  memory-chain key (``.fernet_key``) so the two are independent -- compromising
  or rotating one does not affect the other.
* The key lives in ``sage_data`` (config.DATA_DIR), which is OUTSIDE the project,
  while the encrypted archives live INSIDE the project. So a leaked project
  folder yields ciphertext without the key, and a leaked sage_data yields the key
  without the data: you need BOTH to read an archive.
* Reads are mixed-state tolerant -- an encrypted blob is decrypted, a legacy
  plaintext JSON file is parsed as-is -- so migration can be gradual and a
  half-migrated folder keeps working.

This module only protects data at rest (offline disk access: a stolen laptop, a
leaked backup, a cloud-sync copy, another OS account). It does NOT protect a
running process or anyone who holds the key.

Audit-log note (v2.13)
----------------------
``handoff_guard.py`` encrypts its hash-chained audit-log ``detail`` fields with
THIS key. The key (``.atrest_key``) is co-located with the audit log in
sage_data, consistent with ``.handoff_key``'s existing co-location. That defends
against leaked-project-folder, lower-privilege, and corruption scenarios per the
threat model above; it does NOT defend against a same-user attacker with full
sage_data read access. Accepted, documented limitation -- if that threat model
changes, revisit key placement.
"""
import base64
import json
import os

_fernet = None
_KEY_NAME = ".atrest_key"
# Every Fernet token is urlsafe-base64 of a 0x80 version byte + 8-byte timestamp,
# which always encodes to this literal prefix. Plaintext JSON starts with { or [.
_FERNET_PREFIX = b"gAAAAA"


def _key_path():
    """Resolve the at-rest key path inside sage_data (OUTSIDE the project)."""
    base = None
    try:
        from config import DATA_DIR
        base = str(DATA_DIR)
    except Exception:
        base = None
    if not base:
        # Fallback: alongside the memory key, so we never fail to find a home.
        try:
            from config import FERNET_KEY_FILE
            base = os.path.dirname(str(FERNET_KEY_FILE))
        except Exception:
            base = os.path.dirname(os.path.abspath(__file__))
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        pass
    return os.path.join(base, _KEY_NAME)


def _get_fernet():
    """Load or create the at-rest Fernet key (atomic write + 0600), mirroring the
    memory-chain key pattern."""
    global _fernet
    if _fernet is not None:
        return _fernet
    from cryptography.fernet import Fernet
    kp = _key_path()
    if os.path.exists(kp):
        with open(kp, "rb") as f:
            key = f.read().strip()
    else:
        key = Fernet.generate_key()
        tmp = kp + ".tmp"
        with open(tmp, "wb") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, kp)
        try:
            os.chmod(kp, 0o600)  # best-effort on POSIX; no-op on Windows
        except Exception:
            pass
    _fernet = Fernet(key)
    return _fernet


# ---------------------------------------------------------------------------
# v2.14.3: per-profile keys
# ---------------------------------------------------------------------------
#
# One key becomes two TIERS. The system key (above) keeps auth material,
# integration secrets and the audit chain -- things that must be readable
# before anyone has signed in. A profile's own key covers its content.
#
# `ns=None` is the system key and is exactly the behaviour that existed before
# this, so every un-converted call site keeps working unchanged.
#
# NOTHING CALLS THIS WITH AN ns YET. The plumbing lands first, on its own, so
# it can be verified while it still cannot break anybody's data.

_PROFILE_FERNETS = {}          # ns -> Fernet, for profiles unlocked right now

# Audit hook. OFF by default and free when off. Tests turn it on, drive a
# per-profile operation, and assert every call carried the namespace it should
# have -- which is how a missed call site is caught HERE rather than as an
# archive somebody can no longer open.
_AUDIT = {"on": False, "log": []}


def register_profile_key(ns, dek) -> bool:
    """Make a profile's data key available for this process.

    Called when a profile is unlocked (login, or an API token presenting its
    wrap). In memory only -- nothing to scrub from disk, and it is gone when
    the process ends.
    """
    if not ns or not dek:
        return False
    from cryptography.fernet import Fernet
    _PROFILE_FERNETS[str(ns)] = Fernet(fernet_key_bytes(dek))
    return True


def fernet_key_bytes(dek) -> bytes:
    """The Fernet-shaped form of a data key.

    A DEK is 32 raw bytes; Fernet wants them urlsafe-base64'd, which is 44.
    Two places need this conversion -- registering a key for use, and writing
    one into an export -- and doing it twice by hand is how the two drift and
    one of them produces an archive nobody can open.
    """
    if isinstance(dek, bytes) and len(dek) == 44:
        return dek
    return base64.urlsafe_b64encode(dek)


def forget_profile_key(ns) -> bool:
    """Drop a profile's key -- logout, burn, deletion."""
    return _PROFILE_FERNETS.pop(str(ns), None) is not None


def has_profile_key(ns) -> bool:
    return str(ns) in _PROFILE_FERNETS if ns else False


def _audit(op, ns):
    if not _AUDIT["on"]:
        return
    import inspect
    caller = "?"
    try:
        for fr in inspect.stack()[2:6]:
            if not fr.filename.endswith("atrest.py"):
                caller = "%s:%d" % (os.path.basename(fr.filename), fr.lineno)
                break
    except Exception:
        pass
    _AUDIT["log"].append({"op": op, "ns": ns, "caller": caller})


def audit_start():
    _AUDIT["on"] = True
    _AUDIT["log"] = []


def audit_stop():
    _AUDIT["on"] = False
    return list(_AUDIT["log"])


def audit_log():
    return list(_AUDIT["log"])


def _fernet_for(ns):
    """The key to use. Falls back to the system key when a profile has none --
    which covers the owner, pre-migration profiles, and a profile that is not
    currently unlocked."""
    if ns:
        f = _PROFILE_FERNETS.get(str(ns))
        if f is not None:
            return f
    return _get_fernet()


_WRITE_FALLBACK_WARNED = set()      # ns values already reported, once each


def encrypt_bytes(data: bytes, ns=None) -> bytes:
    """Encrypt for a namespace. Falls back to the system key -- but says so.

    v2.15.2. The READ fallback in decrypt_bytes is deliberate and must stay
    silent: it is what lets pre-migration files keep opening, and what keeps a
    readable export working. This is the WRITE side, and it is different.

    Writing under the system key for a namespace that is supposed to have its
    own key means that profile's data lands under the same key the owner's
    does. _hold_profile_key's docstring already names the consequence -- "the
    profile's own key quietly protects nothing" -- so the risk was understood.
    What was missing was any way to notice it happening: no log line, no flag,
    and the audit hook is off by default.

    A profile reaches here when it has no keywrap at all (created before
    per-profile keys existed), or when key creation failed at profile
    creation. Both are recoverable -- main.py now creates the key at the next
    login, where a password exists -- but until then, this is the only signal.

    ns=None is the OWNER and is not warned about: for the owner the system key
    IS their key, by design. Warning there would be noise that teaches people
    to ignore the real case.

    Once per namespace per process. A warning on every write would be worse
    than none -- it would scroll the reason for it off the screen.
    """
    _audit("encrypt", ns)
    if ns and str(ns) not in _PROFILE_FERNETS:
        _k = str(ns)
        if _k not in _WRITE_FALLBACK_WARNED:
            _WRITE_FALLBACK_WARNED.add(_k)
            print(f"[ATREST] WARNING: writing data for profile {_k!r} under "
                  f"the SYSTEM key -- it has no profile key registered, so "
                  f"this data is readable with the owner's key. A key is "
                  f"created automatically at this profile's next password "
                  f"login, and its existing files are re-encrypted then. "
                  f"(Said once per profile per run.)", flush=True)
    return _fernet_for(ns).encrypt(data)


def write_fallback_warned():
    """Namespaces that have written under the system key this run.

    Exposed so a status surface can show it, and so tests can assert the
    warning fired without scraping stdout.
    """
    return set(_WRITE_FALLBACK_WARNED)


def decrypt_bytes(token: bytes, ns=None) -> bytes:
    """Decrypt, trying the profile key first and the system key second.

    The fallback is what makes migration possible: existing files were written
    under the system key and must keep opening, while new writes go under the
    profile key. It is tried SECOND so a profile's own key always wins -- the
    other order would quietly keep reading stale copies.
    """
    _audit("decrypt", ns)
    from cryptography.fernet import InvalidToken
    if ns:
        f = _PROFILE_FERNETS.get(str(ns))
        if f is not None:
            try:
                return f.decrypt(token)
            except InvalidToken:
                pass          # legacy file under the system key -- try that
    return _get_fernet().decrypt(token)


def decrypt_with_system_key(token: bytes) -> bytes:
    """Decrypt using ONLY the system key -- no profile fallback.

    decrypt_bytes is deliberately forgiving: it tries the profile key, then the
    system key, so a half-migrated store keeps working. That forgiveness hides
    the one fact migration needs, which is WHICH key a file is actually under.
    Raises InvalidToken if the system key does not open it.
    """
    return _get_fernet().decrypt(token)


def decrypt_with_profile_key(token: bytes, ns) -> bytes:
    """Decrypt using ONLY this profile's key. Raises if it is not unlocked.

    KeyError rather than a silent fallback: asking for a specific key and
    getting a different one is how a migration convinces itself it succeeded.
    """
    f = _PROFILE_FERNETS.get(str(ns)) if ns else None
    if f is None:
        raise KeyError("no profile key registered for ns=%r" % (ns,))
    return f.decrypt(token)


def is_encrypted(blob: bytes) -> bool:
    """True if `blob` looks like one of our Fernet tokens."""
    try:
        return blob.lstrip()[:6] == _FERNET_PREFIX
    except Exception:
        return False


def dump_json_encrypted(obj, ns=None) -> bytes:
    """JSON-serialize `obj` and return an encrypted blob ready to write to disk."""
    return encrypt_bytes(json.dumps(obj, indent=2).encode("utf-8"), ns=ns)


def load_json_auto(blob: bytes, ns=None):
    """Parse a file that may be an encrypted blob OR legacy plaintext JSON.
    Falls back to a plaintext parse if decryption fails, so a stray plaintext
    file is never lost."""
    if is_encrypted(blob):
        try:
            blob = decrypt_bytes(blob, ns=ns)
        except Exception:
            pass  # fall through and try to parse what we have
    if isinstance(blob, bytes):
        blob = blob.decode("utf-8", "replace")
    return json.loads(blob)


def migrate_image_folder(folder, *, quarantine=True) -> dict:
    """Encrypt plaintext IMAGE files in `folder` in place, round-trip verified
    before replacing (a file is only overwritten once its ciphertext is proven to
    decrypt back exactly). Optionally quarantines the plaintext original. Skips
    already-encrypted files, non-images, and the quarantine subfolder. Idempotent."""
    import shutil
    exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
            ".heic", ".heif", ".avif", ".tiff", ".tif"}
    folder = str(folder)
    out = {"migrated": 0, "skipped_encrypted": 0, "failed": 0, "errors": []}
    if not os.path.isdir(folder):
        return out
    qdir = os.path.join(folder, "_plaintext_quarantine")
    for name in sorted(os.listdir(folder)):
        if os.path.splitext(name)[1].lower() not in exts:
            continue
        fp = os.path.join(folder, name)
        if not os.path.isfile(fp):
            continue
        try:
            with open(fp, "rb") as f:
                original = f.read()
            if is_encrypted(original):
                out["skipped_encrypted"] += 1
                continue
            enc = encrypt_bytes(original)
            if decrypt_bytes(enc) != original:
                raise ValueError("round-trip verification failed")
            if quarantine:
                os.makedirs(qdir, exist_ok=True)
                shutil.copy2(fp, os.path.join(qdir, name))
            tmp = fp + ".tmp"
            with open(tmp, "wb") as f:
                f.write(enc)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, fp)
            out["migrated"] += 1
        except Exception as e:
            out["failed"] += 1
            out["errors"].append("%s: %s" % (name, e))
            continue
    return out


def read_file_auto(path, ns=None) -> bytes:
    """Read a file that may be an encrypted blob OR legacy plaintext, returning
    the usable (decrypted/plaintext) bytes. Lets at-rest images be served back to
    the UI whether they were written before or after encryption was enabled."""
    with open(str(path), "rb") as f:
        blob = f.read()
    if is_encrypted(blob):
        try:
            return decrypt_bytes(blob, ns=ns)
        except Exception:
            return blob
    return blob


def migrate_archive_folder(folder, *, quarantine=True) -> dict:
    """One-time migration: encrypt any plaintext ``archive_*.json`` in `folder`
    IN PLACE. Safety first:

    * each file is encrypted, then immediately decrypted and compared byte-for-byte
      to the original BEFORE the original is replaced -- a file is only ever
      overwritten once its ciphertext is proven to decrypt back exactly;
    * if `quarantine` is True the plaintext original is first copied into
      ``<folder>/_plaintext_quarantine/`` (never hard-deleted) so nothing is lost.
      NOTE: that quarantine is itself plaintext -- delete it once you have
      confirmed the migration, to actually gain the at-rest protection.

    Idempotent: already-encrypted files are skipped. Returns a summary dict.
    """
    import shutil
    folder = str(folder)
    out = {"migrated": 0, "skipped_encrypted": 0, "failed": 0, "errors": []}
    if not os.path.isdir(folder):
        return out
    qdir = os.path.join(folder, "_plaintext_quarantine")
    for name in sorted(os.listdir(folder)):
        if not (name.startswith("archive_") and name.endswith(".json")):
            continue
        fp = os.path.join(folder, name)
        if not os.path.isfile(fp):
            continue
        try:
            with open(fp, "rb") as f:
                original = f.read()
            if is_encrypted(original):
                out["skipped_encrypted"] += 1
                continue
            json.loads(original.decode("utf-8"))          # must be valid JSON
            enc = encrypt_bytes(original)
            if decrypt_bytes(enc) != original:            # PROVE round-trip
                raise ValueError("round-trip verification failed")
            if quarantine:
                os.makedirs(qdir, exist_ok=True)
                shutil.copy2(fp, os.path.join(qdir, name))
            tmp = fp + ".tmp"
            with open(tmp, "wb") as f:
                f.write(enc)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, fp)                            # atomic in-place
            out["migrated"] += 1
        except Exception as e:
            out["failed"] += 1
            out["errors"].append("%s: %s" % (name, e))
            continue
    return out
