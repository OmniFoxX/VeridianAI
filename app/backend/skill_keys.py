"""
OracleAI / Aether -- trusted author KEY STORE (Layer 5).

The receiver's explicit allow-list of author public keys. A skill's signature
proves AUTHENTICITY; this store is what makes an author TRUSTED (authorized for
promotion). Keys are imported out-of-band and verified by FINGERPRINT comparison
(trust-on-first-contact). Stored as JSON in sage_data, outside the project.

This is also where a future BitChat proximity exchange deposits a peer key -- the
import path is transport-agnostic (it just needs the b64 public key + a label).
"""
import base64
import json
import os
import time
from pathlib import Path

import skill_trust

_FILENAME = ".trusted_skill_keys.json"


def _path(key_dir=None):
    if key_dir is not None:
        return Path(key_dir) / _FILENAME
    try:
        from config import DATA_DIR
        return Path(DATA_DIR) / _FILENAME
    except Exception:
        return Path(os.path.expanduser("~")) / ".oracleai_sage_data" / _FILENAME


def _load(key_dir=None):
    p = _path(key_dir)
    try:
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, list):
                return d
    except Exception:
        pass
    return []


def _save(entries, key_dir=None):
    p = _path(key_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=True), encoding="utf-8")
    os.replace(str(tmp), str(p))


def _pub_of(e):
    if isinstance(e, str):
        return e
    if isinstance(e, dict):
        return e.get("pubkey")
    return None


def _valid_pub(pub_b64):
    """A real Ed25519 public key is exactly 32 bytes."""
    try:
        return len(base64.b64decode(pub_b64, validate=True)) == 32
    except Exception:
        return False


def list_keys(key_dir=None):
    """Trusted entries: [{pubkey, label, fingerprint, added}] (tolerates legacy
    bare-string entries)."""
    out = []
    for e in _load(key_dir):
        pub = _pub_of(e)
        if not pub:
            continue
        label = e.get("label", "") if isinstance(e, dict) else ""
        added = e.get("added", 0) if isinstance(e, dict) else 0
        out.append({"pubkey": pub, "label": label,
                    "fingerprint": skill_trust.fingerprint(pub), "added": added})
    return out


def trusted_pubkeys(key_dir=None):
    return [e["pubkey"] for e in list_keys(key_dir)]


def is_trusted(pubkey, key_dir=None):
    return (pubkey or "") in set(trusted_pubkeys(key_dir))


def add_key(pubkey, label="", key_dir=None):
    """Import a trusted author key (idempotent on pubkey; updates label if given).
    Returns {ok, fingerprint, reason, deduped}."""
    pubkey = (pubkey or "").strip()
    if not _valid_pub(pubkey):
        return {"ok": False, "reason": "invalid public key", "fingerprint": ""}
    entries = _load(key_dir)
    for e in entries:
        if _pub_of(e) == pubkey:
            if label and isinstance(e, dict):
                e["label"] = str(label)
                _save(entries, key_dir)
            return {"ok": True, "fingerprint": skill_trust.fingerprint(pubkey),
                    "reason": "already trusted", "deduped": True}
    entries.append({"pubkey": pubkey, "label": str(label), "added": int(time.time())})
    _save(entries, key_dir)
    return {"ok": True, "fingerprint": skill_trust.fingerprint(pubkey),
            "reason": "added", "deduped": False}


def remove_key(pubkey, key_dir=None):
    pubkey = (pubkey or "").strip()
    entries = _load(key_dir)
    kept = [e for e in entries if _pub_of(e) != pubkey]
    if len(kept) == len(entries):
        return {"ok": False, "reason": "not found"}
    _save(kept, key_dir)
    return {"ok": True, "reason": "removed"}


def self_identity(key_dir=None):
    """This Sage's own public key + fingerprint, to hand to peers out-of-band."""
    pub = skill_trust.public_key_b64(key_dir=key_dir)
    return {"pubkey": pub, "fingerprint": skill_trust.fingerprint(pub)}
