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
"""
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


def encrypt_bytes(data: bytes) -> bytes:
    return _get_fernet().encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    return _get_fernet().decrypt(token)


def is_encrypted(blob: bytes) -> bool:
    """True if `blob` looks like one of our Fernet tokens."""
    try:
        return blob.lstrip()[:6] == _FERNET_PREFIX
    except Exception:
        return False


def dump_json_encrypted(obj) -> bytes:
    """JSON-serialize `obj` and return an encrypted blob ready to write to disk."""
    return encrypt_bytes(json.dumps(obj, indent=2).encode("utf-8"))


def load_json_auto(blob: bytes):
    """Parse a file that may be an encrypted blob OR legacy plaintext JSON.
    Falls back to a plaintext parse if decryption fails, so a stray plaintext
    file is never lost."""
    if is_encrypted(blob):
        try:
            blob = decrypt_bytes(blob)
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


def read_file_auto(path) -> bytes:
    """Read a file that may be an encrypted blob OR legacy plaintext, returning
    the usable (decrypted/plaintext) bytes. Lets at-rest images be served back to
    the UI whether they were written before or after encryption was enabled."""
    with open(str(path), "rb") as f:
        blob = f.read()
    if is_encrypted(blob):
        try:
            return decrypt_bytes(blob)
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
