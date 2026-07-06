"""
OracleAI / Aether -- skill-sharing TRUST CORE (Layer 1).

Zero-trust foundation for sharing learned skills + tools between Sages. This
module does crypto + verification ONLY: it never executes a skill, never touches
the network, and grants no trust on its own. Higher layers (store, catalog,
capability gating, transport) build on top.

Trust model: each Sage has an Ed25519 signing identity. Outgoing skills are
signed; incoming skills are verified by (a) content hash and (b) signature over
the declared metadata. A valid signature proves the artifact is authentic and
unmodified -- it does NOT make the author trusted. Trust is a separate decision
(the receiver's imported-key set). "Don't trust, verify."
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

_SCHEMA = "oracleai.skill/1"
# Domain separation: a signature produced here can never be valid in another
# protocol (node token, handoff, ...) and vice-versa.
_SIG_CONTEXT = b"oracleai-skill-signature-v1\n"
_KEY_FILENAME = ".skill_signing_key"   # raw 32-byte Ed25519 seed, 0600, in sage_data


# --------------------------------------------------------------------------
# content addressing
# --------------------------------------------------------------------------
def content_hash(data):
    """SHA-256 hex of the raw skill body. The artifact id == this hash, so the
    name proves the bytes (free dedupe + tamper-evidence)."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("content_hash expects bytes")
    return hashlib.sha256(bytes(data)).hexdigest()


def _canonical(obj):
    """Deterministic serialization so signing/verifying is stable across machines."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


# --------------------------------------------------------------------------
# identity (per-Sage signing keypair)
# --------------------------------------------------------------------------
def _data_dir(explicit=None):
    if explicit is not None:
        return Path(explicit)
    try:
        from config import DATA_DIR
        return Path(DATA_DIR)
    except Exception:
        # keeps the module importable/testable anywhere; production uses sage_data.
        return Path(os.path.expanduser("~")) / ".oracleai_sage_data"


def _key_path(key_dir=None):
    return _data_dir(key_dir) / _KEY_FILENAME


def load_or_create_identity(key_dir=None):
    """Load this Sage's Ed25519 private key, creating one on first use. The 32-byte
    seed lives 0600 in sage_data (outside the project) and never leaves the host."""
    p = _key_path(key_dir)
    try:
        if p.exists():
            seed = p.read_bytes()
            if len(seed) == 32:
                return ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        key = ed25519.Ed25519PrivateKey.generate()
        seed = key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption())
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(seed)
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600 (best-effort on Windows)
        except OSError:
            pass
        os.replace(tmp, p)
        return key
    except Exception as e:
        raise RuntimeError("skill identity unavailable: %s" % e)


def _as_public(key):
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return key.public_key()
    return key


def public_key_bytes(key=None, key_dir=None):
    if key is None:
        key = load_or_create_identity(key_dir)
    return _as_public(key).public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)


def public_key_b64(key=None, key_dir=None):
    return base64.b64encode(public_key_bytes(key, key_dir)).decode("ascii")


def fingerprint(pub_b64):
    """Human-comparable fingerprint of a public key, for OUT-OF-BAND verification
    (compare on screen / read aloud, Signal-safety-number style)."""
    try:
        raw = base64.b64decode(pub_b64)
    except Exception:
        return "INVALID"
    h = hashlib.sha256(raw).hexdigest()[:16].upper()
    return " ".join(h[i:i + 4] for i in range(0, len(h), 4))


# --------------------------------------------------------------------------
# sign / verify
# --------------------------------------------------------------------------
def sign_artifact(body, name, version="", capabilities=None, author="",
                  key=None, key_dir=None, extra=None):
    """Produce a signed skill envelope: {schema, payload, sig}. The signature
    covers the content hash AND the declared metadata, so tampering with EITHER
    the body OR the declared capabilities breaks verification."""
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("body must be bytes")
    if key is None:
        key = load_or_create_identity(key_dir)
    payload = {
        "schema": _SCHEMA,
        "id": content_hash(body),
        "name": str(name),
        "version": str(version),
        "capabilities": sorted(set(capabilities or [])),
        "author": str(author),
        "author_pub": public_key_b64(key),
        "created": int(time.time()),
    }
    if extra is not None:
        payload["extra"] = extra
    sig = key.sign(_SIG_CONTEXT + _canonical(payload))
    return {"schema": _SCHEMA,
            "payload": payload,
            "sig": base64.b64encode(sig).decode("ascii")}


def verify_artifact(envelope, body, trusted_pubkeys=None):
    """Verify a signed skill. NEVER raises -- returns a structured result.
    Checks: (1) envelope shape, (2) content_hash(body) == payload id,
    (3) signature valid for payload.author_pub over the canonical payload.
    If trusted_pubkeys is given, also reports whether the author is in that set
    -- a valid signature from an UNtrusted key is authentic but NOT authorized."""
    res = {"ok": False, "reason": "", "trusted": False, "author_fingerprint": "",
           "id": ""}
    try:
        if not isinstance(envelope, dict):
            res["reason"] = "envelope not a dict"; return res
        payload = envelope.get("payload")
        if not isinstance(payload, dict) or payload.get("schema") != _SCHEMA:
            res["reason"] = "unknown schema"; return res
        cid = payload.get("id", "")
        res["id"] = cid
        if content_hash(body) != cid:
            res["reason"] = "body hash mismatch"; return res
        pub_b64 = payload.get("author_pub", "")
        res["author_fingerprint"] = fingerprint(pub_b64)
        try:
            pub = ed25519.Ed25519PublicKey.from_public_bytes(
                base64.b64decode(pub_b64))
        except Exception:
            res["reason"] = "bad author_pub"; return res
        signed = _SIG_CONTEXT + _canonical(payload)
        try:
            pub.verify(base64.b64decode(envelope.get("sig", "")), signed)
        except Exception:
            res["reason"] = "bad signature"; return res
        res["ok"] = True
        res["reason"] = "valid"
        if trusted_pubkeys is not None:
            res["trusted"] = pub_b64 in set(trusted_pubkeys)
        return res
    except Exception as e:
        res["reason"] = "error: %s" % e
        return res
