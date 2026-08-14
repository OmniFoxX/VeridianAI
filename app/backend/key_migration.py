#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""key_migration.py -- bring a profile's existing files under its own key.

WHY THIS EXISTS
---------------
Per-profile encryption arrived after people already had data. Their files were
written under the SYSTEM key, and atrest's decrypt falls back to it, so
everything keeps reading and nothing looks wrong. What is wrong is the claim:
"your data is encrypted with your key" would be true only of what they wrote
after the upgrade, and silently false of their whole history.

So the conversion is explicit, it happens at first unlock while the key is in
hand, and it reports what it did.

SCOPE -- DELIBERATELY NARROW
----------------------------
Only files under the profile's OWN directory (sage_data/users/<ns>) are
touched. Not config.json, not the memory chain, not anything shared: those are
system tier, and re-keying them under one profile would take the app away from
everybody else. The containment is a path check, not a list to keep in sync.

WHAT IT WILL NOT DO
-------------------
- It never encrypts something that was plaintext. That would be an improvement
  in isolation and a format change in practice; it is counted and reported
  instead, so the decision stays visible rather than being made in passing.
- It never deletes or overwrites the original until the replacement has been
  written, fsynced, AND read back and decrypted with the profile's key. If the
  machine dies mid-file, the original is what survives.
- A file it cannot open with either key is left exactly as it is.

Run it twice and the second run converts nothing: files already under the
profile key are recognised and skipped, so an interrupted migration resumes
rather than restarting.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, Optional

MIGRATION_FILE = ".key_migration.json"
SCHEMA_VERSION = 1

# Keys and key metadata are never content. Re-encrypting a keywrap under the
# key it protects is the circular case; the rest simply are not user data.
NEVER = {
    MIGRATION_FILE,
    ".keywrap.json",
    ".atrest_key",
    ".fernet_key",
    "fernet.key",
    ".recovery_key",
    ".api_keystore.json",
}

_TMP_SUFFIX = ".keymig.tmp"


def _root(ns) -> Optional[Path]:
    """The profile's own directory, or None if there is not one."""
    if not ns:
        return None
    try:
        import sage_engine
        r = sage_engine.user_data_dir(ns)
        return Path(r) if r else None
    except Exception:
        return None


def _sentinel(ns) -> Optional[Path]:
    r = _root(ns)
    return (r / MIGRATION_FILE) if r else None


def state(ns) -> Dict:
    """What a previous run recorded, or {} if it has never run."""
    p = _sentinel(ns)
    if not p or not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        # An unreadable sentinel means "unknown", which must read as "not
        # done" -- rerunning is safe and idempotent; skipping is not.
        return {}


def is_done(ns) -> bool:
    return bool(state(ns).get("completed"))


def _candidates(root: Path):
    for f in sorted(root.rglob("*")):
        try:
            if not f.is_file():
                continue
        except OSError:
            continue
        if f.name in NEVER or f.name.endswith(_TMP_SUFFIX):
            continue
        yield f


def _rewrite(path: Path, plain: bytes, ns) -> None:
    """Replace `path` with `plain` encrypted under the profile key.

    The verify-before-replace is the point: a Fernet token that cannot be
    opened is indistinguishable from one that can until someone tries, and by
    then the original is gone.
    """
    import atrest
    tmp = path.with_name(path.name + _TMP_SUFFIX)
    try:
        blob = atrest.encrypt_bytes(plain, ns=ns)
        with open(tmp, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        back = atrest.decrypt_with_profile_key(tmp.read_bytes(), ns)
        if back != plain:
            raise ValueError("re-encrypted copy did not read back identical")
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def run(ns, *, dry_run: bool = False) -> Dict:
    """Convert one profile's files. Safe to call repeatedly.

    Returns counts: converted / already / plaintext / unreadable / failed.
    ``ok`` is False only for WRITE failures -- the retryable kind. A file
    nobody holds a key for is recorded and does not block completion, because
    running again cannot change the outcome.
    """
    out = {"ns": ns, "converted": 0, "already": 0, "plaintext": 0,
           "unreadable": 0, "failed": 0, "errors": [], "ok": False,
           "ran": False}
    root = _root(ns)
    if root is None or not root.exists():
        out["ok"] = True
        return out

    import atrest
    if not atrest.has_profile_key(ns):
        # Without the key there is nothing to convert TO. Loud, because a
        # caller that reaches here has an ordering bug.
        out["error"] = "profile key is not unlocked"
        return out

    out["ran"] = True
    for f in _candidates(root):
        try:
            blob = f.read_bytes()
        except OSError as e:
            out["failed"] += 1
            out["errors"].append("%s: %s" % (f.name, type(e).__name__))
            continue

        if not atrest.is_encrypted(blob):
            out["plaintext"] += 1
            continue
        try:
            atrest.decrypt_with_profile_key(blob, ns)
            out["already"] += 1
            continue
        except Exception:
            pass
        try:
            plain = atrest.decrypt_with_system_key(blob)
        except Exception:
            out["unreadable"] += 1
            continue

        if dry_run:
            out["converted"] += 1
            continue
        try:
            _rewrite(f, plain, ns)
            out["converted"] += 1
        except Exception as e:
            out["failed"] += 1
            out["errors"].append("%s: %s" % (f.name, type(e).__name__))

    out["ok"] = out["failed"] == 0
    if out["ok"] and not dry_run:
        _write_sentinel(ns, out)
    return out


def plan(ns) -> Dict:
    """What run() would do, without writing anything."""
    return run(ns, dry_run=True)


def _write_sentinel(ns, result: Dict) -> bool:
    p = _sentinel(ns)
    if not p:
        return False
    doc = {
        "version": SCHEMA_VERSION,
        "completed": True,
        "at": int(time.time()),
        "converted": result.get("converted", 0),
        "already": result.get("already", 0),
        "plaintext": result.get("plaintext", 0),
        "unreadable": result.get("unreadable", 0),
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + _TMP_SUFFIX)
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception:
        # Losing the marker costs one redundant scan next login, which is
        # harmless. It must never cost the migration itself.
        return False


def summary(result: Dict) -> str:
    """One line a person can read, for the console and the audit entry."""
    if not result or not result.get("ran"):
        return "nothing to convert"
    bits = ["%d converted to your key" % result.get("converted", 0)]
    if result.get("already"):
        bits.append("%d already yours" % result["already"])
    if result.get("plaintext"):
        bits.append("%d left unencrypted" % result["plaintext"])
    if result.get("unreadable"):
        bits.append("%d could not be opened" % result["unreadable"])
    if result.get("failed"):
        bits.append("%d FAILED to write" % result["failed"])
    return ", ".join(bits)
