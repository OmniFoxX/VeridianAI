"""OracleAI / Aether -- manual IP access control (denylist + lockdown allowlist).

A persistent, user-managed complement to wan_guard.AbuseGuard. AbuseGuard is
AUTOMATIC, in-memory and TEMPORARY (rate-limit + failed-auth backoff that resets
on restart). This module is the opposite axis: MANUAL, on-disk and PERMANENT
until the user removes an entry -- the "add and delete IPs as needed" control.

Stored in sage_data (OUTSIDE the project) so it survives restarts and never rides
a copied / OneDrive-synced / distributed project folder. Pure stdlib, thread-safe.

Model
-----
* denylist  -- IPs / CIDRs that are ALWAYS blocked, even on the Aether peer
               surface. The "evict a bad actor" tool. Compatible with Aether
               being public (you remove specific offenders, not enumerate peers).
* allowlist -- IPs / CIDRs trusted for LOCKDOWN mode.
* lockdown  -- when True, ONLY allowlisted remote IPs may reach the peer surface;
               every other remote IP is blocked. When False, the surface is
               public (normal Aether) and only the denylist applies.

Localhost is exempt -- but that is enforced by the CALLER (the request guard),
not here, so this module stays a pure data/decision unit. Single source of truth
for IP-access state: this one JSON file, nothing duplicated elsewhere.

Entries accept a single address ("203.0.113.5", IPv4 or IPv6) or a CIDR range
("203.0.113.0/24"), so you can swat a whole scanning subnet with one entry.
"""
from __future__ import annotations

import ipaddress
import json
import threading
from pathlib import Path


def _norm(entry):
    """Parse an IP or CIDR string -> ("ip", obj) | ("net", obj) | None if invalid.

    IPv4-mapped IPv6 (e.g. ::ffff:203.0.113.5) is folded to its IPv4 form so a
    plain "203.0.113.5" denylist entry still matches a mapped client address.
    """
    s = (entry or "").strip()
    if not s:
        return None
    try:
        if "/" in s:
            return ("net", ipaddress.ip_network(s, strict=False))
        ip = ipaddress.ip_address(s)
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        return ("ip", ip)
    except ValueError:
        return None


def _match(ip_obj, parsed_entries):
    """True if ip_obj equals any parsed 'ip' entry or sits inside any 'net'."""
    if ip_obj is None:
        return False
    for kind, obj in parsed_entries:
        if kind == "ip" and ip_obj == obj:
            return True
        if kind == "net" and ip_obj in obj:
            return True
    return False


class IPAccess:
    def __init__(self, store_path):
        self.path = Path(store_path)
        self._lock = threading.Lock()
        self._deny_raw = []     # original strings, for the UI + persistence
        self._allow_raw = []
        self._deny = []         # parsed [(kind, obj), ...]
        self._allow = []
        self._lockdown = False
        self._load()

    # ------------------------------------------------------------------ I/O
    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        with self._lock:
            self._deny_raw = [s for s in data.get("denylist", []) if _norm(s)]
            self._allow_raw = [s for s in data.get("allowlist", []) if _norm(s)]
            self._lockdown = bool(data.get("lockdown", False))
            self._reparse()

    def _reparse(self):
        self._deny = [_norm(s) for s in self._deny_raw]
        self._allow = [_norm(s) for s in self._allow_raw]

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({
            "denylist": self._deny_raw,
            "allowlist": self._allow_raw,
            "lockdown": self._lockdown,
        }, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # -------------------------------------------------------------- decision
    def remote_blocked(self, client_ip):
        """For a NON-local caller, return a short reason if blocked, else None.

        'denylisted' overrides everything; under lockdown a non-allowlisted IP
        (or an unparseable address) is 'lockdown'. Caller exempts localhost.
        """
        obj = _norm(client_ip)
        ip_obj = obj[1] if (obj and obj[0] == "ip") else None
        with self._lock:
            if _match(ip_obj, self._deny):
                return "denylisted"
            if self._lockdown and not _match(ip_obj, self._allow):
                return "lockdown"
        return None

    # ------------------------------------------------------------ management
    def add(self, which, entry):
        """Add to 'deny' or 'allow'. Raises ValueError on a bad IP/CIDR."""
        if _norm(entry) is None:
            raise ValueError("not a valid IP or CIDR: %r" % (entry,))
        s = entry.strip()
        with self._lock:
            raw = self._deny_raw if which == "deny" else self._allow_raw
            if s not in raw:
                raw.append(s)
            self._reparse()
            self._save()
        return self.snapshot()

    def remove(self, which, entry):
        s = (entry or "").strip()
        with self._lock:
            raw = self._deny_raw if which == "deny" else self._allow_raw
            if s in raw:
                raw.remove(s)
            self._reparse()
            self._save()
        return self.snapshot()

    def set_lockdown(self, enabled):
        with self._lock:
            self._lockdown = bool(enabled)
            self._save()
        return self.snapshot()

    def snapshot(self):
        with self._lock:
            return {
                "denylist": list(self._deny_raw),
                "allowlist": list(self._allow_raw),
                "lockdown": self._lockdown,
            }

    def summary(self):
        with self._lock:
            return "deny=%d allow=%d lockdown=%s" % (
                len(self._deny_raw), len(self._allow_raw),
                "on" if self._lockdown else "off")
