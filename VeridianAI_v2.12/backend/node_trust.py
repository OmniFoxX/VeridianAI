#!/usr/bin/env python3
"""
node_trust.py - trust core for the OracleAI node network ("Sage network").
==========================================================================

A single per-user HOME TOKEN is the root secret. It NEVER crosses the network.
Each node derives a Fernet key from it, and ALL node-to-node traffic is
Fernet-encrypted, which gives, in one primitive:

  * confidentiality - payloads are ciphertext on the wire;
  * authentication  - only a holder of the token can produce valid ciphertext,
                      so a peer that cannot be decrypted is rejected;
  * tamper-evidence - Fernet is authenticated encryption (HMAC);
  * a replay window - Fernet timestamps every message; decrypt(ttl=...) rejects
                      stale/replayed messages.

No token is ever transmitted, and no TLS certificate is needed on the LAN. For
the future internet step ("SageNet"), TLS would be layered ON TOP - but this
Fernet layer still protects payloads even over a plaintext or hostile transport,
so the trust model is correct from day one.

The token is stored like the project's other secrets (.handoff_key / .fernet_key
/ .socket_token): a protected, per-user, load-or-create file. Anyone who can read
it can talk to your nodes - protect it with the same care.

This module has ZERO network surface. It is pure key + crypto helpers.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any, Tuple

try:
    from cryptography.fernet import Fernet, InvalidToken
except Exception:  # pragma: no cover - cryptography is a project dependency
    Fernet = None

    class InvalidToken(Exception):
        pass


_TOKEN_FILE = ".home_token"
_DOMAIN = "oracleai-node-v1:"   # domain separation for the key derivation
_DEFAULT_TTL = 60               # seconds: replay / staleness window


def load_or_create_home_token(data_dir) -> str:
    """Load the per-user home token, creating a high-entropy one on first use.
    Stored at <data_dir>/.home_token. Copy this value to your OTHER nodes so they
    share the secret. NEVER transmitted over the network. NEVER raises."""
    p = Path(data_dir) / _TOKEN_FILE
    try:
        if p.exists():
            t = p.read_text(encoding="utf-8").strip()
            if t:
                return t
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(token, encoding="utf-8")
        try:
            import os
            import stat
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)  # 0600 where supported
        except Exception:
            pass
    except OSError:
        pass
    return token


def _fernet_for(token: str):
    if Fernet is None:
        raise RuntimeError("cryptography/Fernet unavailable")
    if not token:
        raise ValueError("empty token")
    digest = hashlib.sha256((_DOMAIN + token).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_payload(obj: Any, token: str) -> bytes:
    """JSON-serialize then Fernet-encrypt a payload. Returns ciphertext bytes."""
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return _fernet_for(token).encrypt(raw)


def decrypt_payload(blob, token: str, ttl: int = _DEFAULT_TTL) -> Tuple[bool, Any]:
    """Decrypt + verify a payload. Returns (ok, obj) on success, or (False,
    reason) on a wrong token, tampering, or a message older than ttl. NEVER
    raises - a failure to decrypt IS the authentication failure."""
    try:
        data = blob if isinstance(blob, (bytes, bytearray)) else str(blob).encode("utf-8")
        raw = _fernet_for(token).decrypt(bytes(data), ttl=ttl)
        return True, json.loads(raw.decode("utf-8"))
    except InvalidToken:
        return False, "rejected: wrong token, tampered, or stale message"
    except Exception as e:
        return False, f"rejected: {type(e).__name__}: {e}"


def token_fingerprint(token: str) -> str:
    """A short, NON-secret fingerprint of a token, so two nodes (or a setup
    screen) can confirm they share the same token WITHOUT revealing it."""
    if not token:
        return ""
    return hashlib.sha256(("fp:" + token).encode("utf-8")).hexdigest()[:12]


def effective_token(home_token: str, user: str = None) -> str:
    """Multi-user SEAM (reserved). Returns the key material used to derive a
    node's Fernet key for a given user. TODAY: the shared network home token (one
    effective user, "owner"). LATER: replaced by a per-user enrolled SECRET so
    members of the same network cannot read each other - additive, no rewrite.
    NOTE: a real per-user key must be a per-user SECRET, NOT merely derived from
    the shared token + a public user id (which every admitted node could
    recompute). This function is where that secret plugs in."""
    return home_token


def set_home_token(data_dir, token: str) -> bool:
    """Overwrite the home token (pairing: paste the value from another node so
    this node SHARES it). Returns True on success. Takes effect immediately for
    subsequent encrypt/decrypt. NEVER raises."""
    token = (token or "").strip()
    if not token:
        return False
    p = Path(data_dir) / _TOKEN_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(token, encoding="utf-8")
        try:
            import os
            import stat
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except Exception:
            pass
        return True
    except OSError:
        return False


def reset_home_token(data_dir) -> str:
    """Generate a FRESH random home token and write it (clears a swapped/messy
    token). Returns the new token. Breaks any existing pairing - all nodes must
    then share this new value. NEVER raises (returns '' on failure)."""
    new = secrets.token_urlsafe(32)
    return new if set_home_token(data_dir, new) else ""
