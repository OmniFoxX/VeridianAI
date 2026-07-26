#!/usr/bin/env python3
"""Argo-Net Identity + Direct-Message crypto — argonet_identity.py
MentiSphere Software LLC d.b.a VeridianAI — "Keep an open MentiSphere."

This is what turns Argo-Net from a broadcast-only group into something that can
carry PRIVATE messages, and what finally makes "Verify identity" cryptographic
instead of cosmetic.

Each node holds a persistent **X25519** static keypair (in sage_data, outside
the project tree). Its fingerprint -- the node_id you compare out-of-band -- is
the SHA-256 of its PUBLIC KEY, so the fingerprint is cryptographically bound to
the key. An impostor cannot announce your fingerprint with their own key.

Direct messages use an authenticated, per-message-ephemeral scheme (a 2-DH box):

    dh1 = ECDH(ephemeral_sender_priv, recipient_static_pub)   # per-message key
    dh2 = ECDH(sender_static_priv,    recipient_static_pub)   # sender auth
    key = HKDF-SHA256(dh1 || dh2, salt=random, info=eph||recip||sender)
    ciphertext = Fernet(key).encrypt(plaintext)

  * dh1 gives every DM a unique key (ephemeral sender key) -- forward secrecy on
    the sender side; a stolen sender key can't decrypt past DMs.
  * dh2 authenticates the sender: only the real holder of the sender static key
    can produce it, so a recipient knows who a DM is really from.
  * The sender's static public key travels in the box and is checked against the
    origin fingerprint, so the DM is self-contained (no prior handshake needed)
    and identity-bound.

Only the recipient's static private key can complete dh1+dh2, so other members
of the broadcast group -- even though they share the group key -- cannot read a
DM. NOTE (documented limitation): this is not a full double-ratchet; a recipient
whose static key is later compromised could decrypt captured DMs addressed to
them. A ratchet is future work.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger("sage.argonet.identity")

IDENTITY_FILENAME = "argonet_identity.key"   # raw 32-byte X25519 private key (DH / DMs)
SIGNING_FILENAME = "argonet_signing.key"     # raw 32-byte Ed25519 seed (signatures)
_DM_INFO_PREFIX = b"argonet-dm-v1"
REVOCATION_TYPE = "argonet-revocation-v1"


def fingerprint_of(pubkey_bytes: bytes) -> str:
    """Legacy single-key fingerprint helper (SHA-256[:16] of the given bytes).
    Kept for callers that hash one blob; identity binding uses bind_* below."""
    return hashlib.sha256(pubkey_bytes).hexdigest()[:16]


def bind_full(x25519_pub: bytes, ed25519_pub: bytes) -> str:
    """The node's FULL fingerprint: SHA-256 over BOTH public keys.

    Binding both keys into the fingerprint is what makes signed self-revocation
    safe: an attacker cannot announce a victim's fingerprint with the victim's
    (public) X25519 key but the attacker's OWN signing key -- that would change
    the fingerprint. So a valid revocation signature can only come from the true
    holder of the signing key bound to that fingerprint."""
    return hashlib.sha256(x25519_pub + ed25519_pub).hexdigest()


def bind_node_id(x25519_pub: bytes, ed25519_pub: bytes) -> str:
    """Short node_id (addressing): first 16 hex chars of the full fingerprint."""
    return bind_full(x25519_pub, ed25519_pub)[:16]


def _raw_pub(priv: X25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _raw_pub_ed(priv: Ed25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _canon(body: dict) -> bytes:
    """Canonical bytes for signing/verifying (stable key order, no whitespace)."""
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


class ArgoIdentity:
    """A node's persistent identity: an X25519 keypair (DH / DMs) plus an
    Ed25519 keypair (signatures / self-revocation). The fingerprint binds both.
    """

    def __init__(self, private_key: X25519PrivateKey,
                 signing_key: Optional[Ed25519PrivateKey] = None):
        self._priv = private_key
        # A signing key is required for a full identity; generate an ephemeral
        # one if a caller (e.g. a test) supplies only the X25519 key.
        self._sign_priv = signing_key or Ed25519PrivateKey.generate()
        self.public_bytes = _raw_pub(private_key)
        self.sign_public_bytes = _raw_pub_ed(self._sign_priv)
        self.full_fingerprint = bind_full(self.public_bytes, self.sign_public_bytes)
        self.fingerprint = self.full_fingerprint[:16]   # node_id (addressing)

    @property
    def pubkey_hex(self) -> str:
        return self.public_bytes.hex()

    @property
    def sign_pubkey_hex(self) -> str:
        return self.sign_public_bytes.hex()

    # -- persistence --------------------------------------------------
    @staticmethod
    def _load_or_make_raw(filename: str, make) -> bytes:
        """Load a 32-byte raw key from sage_data, or create + persist one."""
        from config import DATA_DIR, PROJECT_DIR
        from secret_locator import resolve_secret_file
        path = resolve_secret_file(filename, DATA_DIR, PROJECT_DIR, announce=False)
        if path.exists():
            raw = path.read_bytes().strip()
            if len(raw) == 32:
                return raw
            logger.warning("[ArgoIdentity] %s wrong size (%d); regenerating.",
                           filename, len(raw))
        raw = make()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return raw

    @classmethod
    def load_or_create(cls) -> "ArgoIdentity":
        """Load (or create) the persisted X25519 + Ed25519 keypairs from
        sage_data. Never raises: on any persistence failure returns an ephemeral
        identity so the node still functions (fingerprint just won't persist)."""
        try:
            def _mk_x():
                return X25519PrivateKey.generate().private_bytes(
                    serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                    serialization.NoEncryption())

            def _mk_s():
                return Ed25519PrivateKey.generate().private_bytes(
                    serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                    serialization.NoEncryption())

            x_raw = cls._load_or_make_raw(IDENTITY_FILENAME, _mk_x)
            s_raw = cls._load_or_make_raw(SIGNING_FILENAME, _mk_s)
            ident = cls(X25519PrivateKey.from_private_bytes(x_raw),
                        Ed25519PrivateKey.from_private_bytes(s_raw))
            logger.info("[ArgoIdentity] identity ready (fp=%s)", ident.fingerprint)
            return ident
        except Exception as exc:
            logger.warning("[ArgoIdentity] persistence unavailable (%s); using "
                           "an ephemeral identity this session.", exc)
            return cls(X25519PrivateKey.generate(), Ed25519PrivateKey.generate())

    @classmethod
    def reset(cls) -> bool:
        """Delete BOTH persisted keys so a fresh identity is generated next load.
        Returns True on success (removed or already absent)."""
        ok = True
        try:
            from config import DATA_DIR, PROJECT_DIR
            from secret_locator import resolve_secret_file
            for fn in (IDENTITY_FILENAME, SIGNING_FILENAME):
                path = resolve_secret_file(fn, DATA_DIR, PROJECT_DIR, announce=False)
                if path.exists():
                    path.unlink()
            logger.info("[ArgoIdentity] identity reset (both keys removed)")
        except Exception as exc:
            logger.warning("[ArgoIdentity] identity reset failed: %s", exc)
            ok = False
        return ok

    # -- signed self-revocation --------------------------------------
    def make_revocation(self, reason: str = "compromised") -> dict:
        """Produce a signed self-revocation of THIS identity. Anyone can verify
        it against the bound signing key; it only ever revokes our own key."""
        body = {
            "type": REVOCATION_TYPE,
            "node_id": self.fingerprint,
            "fingerprint": self.full_fingerprint,
            "x25519_pub": self.pubkey_hex,
            "sign_pubkey": self.sign_pubkey_hex,
            "reason": str(reason or "compromised")[:32],
            "issued_at": int(time.time()),
        }
        body["sig"] = self._sign_priv.sign(_canon(body)).hex()
        return body

    @staticmethod
    def verify_revocation(rev: dict) -> Optional[str]:
        """Verify a self-revocation. Returns the revoked FULL fingerprint if the
        signature is valid AND the keys bind to the claimed fingerprint; else
        None. This is what stops a third party from revoking someone else's key:
        the signature must come from the signing key bound into the fingerprint.
        """
        try:
            if rev.get("type") != REVOCATION_TYPE:
                return None
            x = bytes.fromhex(rev["x25519_pub"])
            s = bytes.fromhex(rev["sign_pubkey"])
            full = bind_full(x, s)
            if rev.get("fingerprint") != full or rev.get("node_id") != full[:16]:
                return None
            body = {k: rev[k] for k in (
                "type", "node_id", "fingerprint", "x25519_pub",
                "sign_pubkey", "reason", "issued_at")}
            Ed25519PublicKey.from_public_bytes(s).verify(
                bytes.fromhex(rev["sig"]), _canon(body))
            return full
        except Exception:
            return None

    # -- DM crypto ----------------------------------------------------
    @staticmethod
    def _derive(dh1: bytes, dh2: bytes, salt: bytes,
                eph_pub: bytes, recip_pub: bytes, sender_pub: bytes) -> bytes:
        info = (_DM_INFO_PREFIX + b"|" + eph_pub + b"|" + recip_pub
                + b"|" + sender_pub)
        raw = HKDF(algorithm=hashes.SHA256(), length=32,
                   salt=salt, info=info).derive(dh1 + dh2)
        return base64.urlsafe_b64encode(raw)   # a valid Fernet key

    def seal_dm(self, plaintext: str, recipient_pub_hex: str) -> dict:
        """Encrypt a DM to a recipient's static public key. Returns the box dict
        carried in the envelope's `dm` field."""
        recip_pub_bytes = bytes.fromhex(recipient_pub_hex)
        recip_pub = X25519PublicKey.from_public_bytes(recip_pub_bytes)
        eph = X25519PrivateKey.generate()
        eph_pub = _raw_pub(eph)
        dh1 = eph.exchange(recip_pub)
        dh2 = self._priv.exchange(recip_pub)
        salt = os.urandom(16)
        key = self._derive(dh1, dh2, salt, eph_pub, recip_pub_bytes,
                           self.public_bytes)
        ct = Fernet(key).encrypt(plaintext.encode("utf-8"))
        return {
            "eph": eph_pub.hex(),
            "spk": self.pubkey_hex,          # sender static X25519 pubkey
            "sspk": self.sign_pubkey_hex,    # sender Ed25519 pubkey (fingerprint binding)
            "salt": salt.hex(),
            "ct": ct.decode("ascii"),
        }

    def open_dm(self, box: dict, expected_origin_fp: Optional[str] = None) -> str:
        """Decrypt + authenticate a DM box addressed to us.

        Verifies the sender's static key hashes to the claimed origin
        fingerprint (identity binding). Raises on any failure -- callers treat a
        raise as "not for us / bad DM" and never deliver it."""
        sender_pub_bytes = bytes.fromhex(box["spk"])
        if expected_origin_fp is not None:
            # The origin fingerprint binds BOTH sender keys; recompute it from
            # the keys in the box and require a match (anti-spoof).
            sender_sign_bytes = bytes.fromhex(box.get("sspk", ""))
            if bind_node_id(sender_pub_bytes, sender_sign_bytes) != expected_origin_fp:
                raise ValueError("DM sender keys do not match origin "
                                 "fingerprint (identity spoof?)")
        sender_pub = X25519PublicKey.from_public_bytes(sender_pub_bytes)
        eph_pub_bytes = bytes.fromhex(box["eph"])
        eph_pub = X25519PublicKey.from_public_bytes(eph_pub_bytes)
        salt = bytes.fromhex(box["salt"])
        dh1 = self._priv.exchange(eph_pub)
        dh2 = self._priv.exchange(sender_pub)
        key = self._derive(dh1, dh2, salt, eph_pub_bytes, self.public_bytes,
                           sender_pub_bytes)
        return Fernet(key).decrypt(box["ct"].encode("ascii")).decode("utf-8")


# ---------------------------------------------------------------------------
# Self-test — crypto roundtrip, third-party isolation, no network
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== Argo-Net Identity / DM Sanity Check ===\n")

    alice = ArgoIdentity(X25519PrivateKey.generate())
    bob = ArgoIdentity(X25519PrivateKey.generate())
    eve = ArgoIdentity(X25519PrivateKey.generate())

    print(f"Alice fp : {alice.fingerprint}  (full {alice.full_fingerprint[:24]}...)")
    print(f"Bob   fp : {bob.fingerprint}")
    assert alice.fingerprint == bind_node_id(alice.public_bytes, alice.sign_public_bytes)
    assert len(alice.full_fingerprint) == 64

    box = alice.seal_dm("meet at the docks, midnight", bob.pubkey_hex)
    opened = bob.open_dm(box, expected_origin_fp=alice.fingerprint)
    print(f"\nBob decrypts: {opened!r}")
    assert opened == "meet at the docks, midnight"

    # Eve (a group member) cannot read a DM to Bob
    try:
        eve.open_dm(box, expected_origin_fp=alice.fingerprint)
        raise SystemExit("FAIL: Eve decrypted a DM not addressed to her!")
    except Exception:
        print("Eve cannot read Bob's DM: correct")

    # Spoofed origin fingerprint is rejected
    try:
        bob.open_dm(box, expected_origin_fp="deadbeefdeadbeef")
        raise SystemExit("FAIL: accepted a spoofed origin fingerprint!")
    except ValueError:
        print("Spoofed origin fingerprint rejected: correct")

    # Signed self-revocation: valid, and un-forgeable by a third party
    rev = alice.make_revocation("compromised")
    assert ArgoIdentity.verify_revocation(rev) == alice.full_fingerprint
    print("Alice's signed self-revocation verifies: correct")

    # Eve cannot forge a revocation of Alice's key: if she swaps in her own
    # signing key the fingerprint changes; if she keeps Alice's announced keys
    # she can't produce a valid signature.
    forged = dict(rev)
    forged["sign_pubkey"] = eve.sign_pubkey_hex
    assert ArgoIdentity.verify_revocation(forged) is None
    forged2 = dict(rev)
    forged2["reason"] = "retired"          # tamper the signed body
    assert ArgoIdentity.verify_revocation(forged2) is None
    print("Third-party / tampered revocation rejected: correct")

    print("\n=== Identity / DM / revocation check passed. ⚓ ===")
