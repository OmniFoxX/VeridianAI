"""bitchat_drift.py -- BitChat protocol-drift detection (v2.12.2).

WHY THIS EXISTS
---------------
BitChat's Swift app owns the protocol constants; our Python gateways carry
copies. When upstream shifts a constant (the feared case: a service UUID,
where a ONE-character change makes phones and Sage silently invisible to
each other), the first symptom is "why isn't BitChat working" -- days later.
This module turns that into "BitChat protocol update detected" -- minutes
later, with an explicit diff and an owner-approval gate.

(Ironically, the incident that commissioned this module turned out to be a
Bluetooth DRIVER failure, not UUID drift -- the checker confirmed upstream
matched. That's the point: five minutes to rule drift IN or OUT.)

DESIGN (deliberately NOT an auto-updater)
-----------------------------------------
* We fetch ONE known public source file over HTTPS (raw.githubusercontent),
  hash it, and regex-extract three specific constants. Fetched content is
  never executed, evaluated, or written anywhere except as extracted UUID
  strings into our own JSON -- and only after validation that they ARE
  UUIDs.
* check() is cheap + safe to run on every BitChat connect. Unchanged hash =
  instant no-op. Changed hash but same constants = hash updated silently
  (comments/whitespace churn upstream is not the owner's problem).
* Constant changes are ALWAYS-ASK: the owner sees old -> new per field and
  approves or declines. Declines are remembered per content-hash, so the
  same upstream change never nags twice; a NEW upstream change asks again.
* apply() writes bitchat_protocol_constants.json (atomic) -- the same file
  the gateways read at spawn -- then the caller restarts the gateway.

stdlib-only (urllib, not requests): the drift checker must import cleanly
in the main app process regardless of gateway-side dependencies.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.request
from net_guard import safe_urlopen
from pathlib import Path
from typing import Optional

_BACKEND_DIR = Path(__file__).resolve().parent
CONSTANTS_PATH = _BACKEND_DIR / "bitchat_protocol_constants.json"

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                      r"[0-9a-f]{4}-[0-9a-f]{12}$")

_BUILTIN_DEFAULTS = {
    "active_network": "mainnet",
    "service_uuid_mainnet": "f47b5e2d-4a9e-4c5a-9b3f-8e1d2c3a4b5c",
    "service_uuid_testnet": "f47b5e2d-4a9e-4c5a-9b3f-8e1d2c3a4b5a",
    "characteristic_uuid": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d",
    "upstream": {
        "tracked_file": ("https://raw.githubusercontent.com/permissionlesstech/"
                         "bitchat/main/bitchat/Services/BLE/BLEService.swift"),
        "last_checked": None,
        "last_hash": None,
        "declined_hash": None,
    },
}

# The three constants we track, mapped to their JSON keys.
_TRACKED_FIELDS = ("service_uuid_mainnet", "service_uuid_testnet",
                   "characteristic_uuid")


# ---------------------------------------------------------------------------
# Constants store
# ---------------------------------------------------------------------------

def load_constants() -> dict:
    """The constants record, defaults merged under whatever the file has.
    Never raises: a missing/corrupt file yields the built-in (known-good as
    of 2026-07-11) values, so the gateways always have SOMETHING sane."""
    merged = json.loads(json.dumps(_BUILTIN_DEFAULTS))   # deep copy
    try:
        with open(CONSTANTS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for k in ("active_network",) + _TRACKED_FIELDS:
            v = raw.get(k)
            if isinstance(v, str) and v:
                merged[k] = v.lower() if k != "active_network" else v
        up = raw.get("upstream")
        if isinstance(up, dict):
            merged["upstream"].update(up)
    except Exception:
        pass
    return merged


def active_service_uuid(constants: Optional[dict] = None) -> str:
    c = constants or load_constants()
    return (c["service_uuid_testnet"]
            if c.get("active_network") == "testnet"
            else c["service_uuid_mainnet"])


def _save_constants(data: dict) -> None:
    tmp = str(CONSTANTS_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONSTANTS_PATH)


# ---------------------------------------------------------------------------
# Upstream fetch + parse
# ---------------------------------------------------------------------------

def _fetch_upstream(url: str, timeout: float = 6.0) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "VeridianAI-drift-check"})
    with safe_urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_swift_constants(source: str) -> dict:
    """Extract the tracked constants from BLEService.swift.

    Tiny #if DEBUG / #else / #endif state machine: upstream declares the
    testnet serviceUUID inside the DEBUG branch and mainnet in #else (their
    layout as of 2026-07). characteristicUUID is unconditional. Only values
    that look like UUIDs are accepted -- a refactored file that we can no
    longer parse yields {} (reported as 'parse failed', never a bad write)."""
    found: dict = {}
    branch = None                      # None | "debug" | "else"
    uuid_in = re.compile(r'CBUUID\(string:\s*"([0-9A-Fa-f-]{36})"\)')
    for line in source.splitlines():
        s = line.strip()
        if s.startswith("#if DEBUG"):
            branch = "debug"
            continue
        if s.startswith("#else"):
            branch = "else" if branch == "debug" else branch
            continue
        if s.startswith("#endif"):
            branch = None
            continue
        m = uuid_in.search(s)
        if not m:
            continue
        val = m.group(1).lower()
        if not _UUID_RE.match(val):
            continue
        if "serviceUUID" in s:
            if branch == "debug" or "testnet" in s.lower():
                found["service_uuid_testnet"] = val
            else:
                found["service_uuid_mainnet"] = val
        elif "characteristicUUID" in s:
            found["characteristic_uuid"] = val
    return found


# ---------------------------------------------------------------------------
# Check / apply / decline
# ---------------------------------------------------------------------------

def check(force: bool = False, timeout: float = 6.0) -> dict:
    """Compare upstream constants against ours.

    Returns one of:
      {"status": "unreachable", "error": ...}       network trouble; no-op
      {"status": "unchanged"}                        same hash as last check
      {"status": "declined"}                         owner already said no
      {"status": "parse_failed", "found": {...}}     file changed shape
      {"status": "in_sync"}                          content churn, constants equal
      {"status": "drift", "hash": ..., "changes": {field: {"ours":..,"upstream":..}}}

    Side effects: updates last_checked always; updates last_hash only for
    benign outcomes (unchanged / in_sync), NEVER for drift -- a pending
    drift must keep re-announcing until applied or declined."""
    c = load_constants()
    up = c["upstream"]
    try:
        content = _fetch_upstream(up["tracked_file"], timeout=timeout)
    except Exception as exc:
        return {"status": "unreachable", "error": str(exc)[:200]}

    new_hash = hashlib.sha256(content.encode()).hexdigest()
    c["upstream"]["last_checked"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    if not force and new_hash == up.get("last_hash"):
        _save_constants(c)
        return {"status": "unchanged"}
    if not force and new_hash == up.get("declined_hash"):
        _save_constants(c)
        return {"status": "declined"}

    found = parse_swift_constants(content)
    missing = [k for k in _TRACKED_FIELDS if k not in found]
    if missing:
        _save_constants(c)   # record the attempt; do NOT bless the hash
        return {"status": "parse_failed", "missing": missing, "found": found}

    changes = {}
    for k in _TRACKED_FIELDS:
        if found[k] != c[k]:
            changes[k] = {"ours": c[k], "upstream": found[k]}

    if not changes:
        c["upstream"]["last_hash"] = new_hash     # benign churn: bless it
        _save_constants(c)
        return {"status": "in_sync"}

    _save_constants(c)
    return {"status": "drift", "hash": new_hash, "changes": changes}


def apply_changes(changes: dict, new_hash: str) -> dict:
    """Owner approved: write the new constants. `changes` is the exact dict
    check() returned -- we re-validate every value before writing."""
    c = load_constants()
    for k, pair in (changes or {}).items():
        if k not in _TRACKED_FIELDS:
            return {"success": False, "error": f"unknown field: {k}"}
        v = str(pair.get("upstream", "")).lower()
        if not _UUID_RE.match(v):
            return {"success": False, "error": f"not a UUID: {v!r}"}
        c[k] = v
    c["upstream"]["last_hash"] = str(new_hash)
    c["upstream"]["declined_hash"] = None
    _save_constants(c)
    return {"success": True, "constants": load_constants()}


def decline_changes(new_hash: str) -> dict:
    """Owner declined: remember this hash so we don't nag until upstream
    changes AGAIN."""
    c = load_constants()
    c["upstream"]["declined_hash"] = str(new_hash)
    _save_constants(c)
    return {"success": True}
