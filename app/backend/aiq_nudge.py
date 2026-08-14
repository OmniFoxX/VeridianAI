"""
OracleAI AIQNudge — HMAC-signed mid-run side-channel
=====================================================
v2.1.10 #44 implementation.

What this is
------------
Todd's mid-run side-channel for guiding Sage during long agentic runs
without aborting. Without code, the pattern was: Todd would rename a
file or paste content somewhere Sage could see it on her next step.
That works, but it means ANYONE who can write a file with the right
name into the watch directory can inject prompts into Sage's active
run — a self-prompt-injection vector, exactly the thing #44 was
queued to address.

The fix: every nudge file carries an HMAC-SHA256 signature. The
consumer verifies the signature before forwarding the content to
Sage. Unsigned or tampered files get quarantined (renamed to
.rejected_<timestamp>) and never reach the agentic loop. Sage only
sees verified nudges, injected as a system-role priority directive.

File format
-----------
Plain text, line-oriented:

    <line 1>  HMAC-SHA256 hex of (timestamp + "\\n" + body), 64 chars
    <line 2>  ISO-8601 timestamp (informational + part of HMAC input)
    <line 3+> nudge body — free text, arbitrary length

Including the timestamp in the HMAC input means a captured-and-
replayed nudge file with a different timestamp has a different
signature, so simple replay attacks fail without us having to track
seen-nonces. Anti-stale-nudge cutoffs (e.g. reject older than N min)
can be added by the consumer side later.

Key management
--------------
- Key lives at backend/.aiq_nudge_key (paralleling .fernet_key)
- 32 random bytes, base64-urlsafe encoded for portable storage
- Auto-generated on first AIQNudge() instantiation if missing
- File permissions set to 0600 on Unix; on Windows we rely on
  per-user profile ACLs (best effort)
- DO NOT reuse the Fernet key. Different concerns, different
  compromise blast radius. Backup BOTH alongside each other but
  treat them as separate trust roots.

Security model
--------------
Threat: attacker can write files into the watch directory.
Defense: without the key, attacker cannot forge a valid signature
on a new nudge. constant-time HMAC comparison prevents timing
oracles.

Not a defense against: an attacker who already has read access to
the key file. If they have the key, they ARE Todd as far as the
nudge channel is concerned. Protect the key file with the same care
as .fernet_key.
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import os
import secrets
import time
from pathlib import Path
from typing import List, Optional, Tuple


# Signature is hex-encoded SHA-256 -> 64 chars
_HEX_SIG_LEN = 64


class NudgeError(Exception):
    """Raised for unrecoverable AIQNudge setup problems (missing dir,
    unreadable key, etc.). Verification failures do NOT raise — they
    return (False, None, reason) so callers can quarantine cleanly."""


class AIQNudge:
    """File-based HMAC-signed nudge channel.

    Typical usage from the agentic loop:

        nudge = AIQNudge(key_file, watch_dir)  # singleton at module
        # ... later, between agentic steps:
        for entry in nudge.read_pending():
            messages.append({"role": "system",
                             "content": f"[VERIFIED USER NUDGE] {entry['content']}"})

    Typical usage from the helper script (Todd composing a nudge):

        nudge = AIQNudge(key_file, watch_dir)
        signed = nudge.sign("focus on the WCAG 2.2 audit, skip the lint pass")
        # write `signed` to watch_dir / f"nudge_{ms_timestamp}.txt"
    """

    def __init__(self, key_file: Path, watch_dir: Path):
        self.key_file  = Path(key_file)
        self.watch_dir = Path(watch_dir)
        self.watch_dir.mkdir(parents=True, exist_ok=True)
        self._key = self._load_or_create_key()

    # ------------------------------------------------------------------
    # Key management
    # ------------------------------------------------------------------
    def _load_or_create_key(self) -> bytes:
        """Load the key bytes from disk, or create + persist a new key.

        Stored on disk as base64-urlsafe text (so it can be cat'd /
        viewed safely if someone needs to debug). Returns the raw
        decoded key bytes.
        """
        if self.key_file.exists():
            try:
                raw = self.key_file.read_bytes().strip()
            except OSError as e:
                raise NudgeError(f"could not read key file {self.key_file}: {e}")
            if len(raw) < 16:
                raise NudgeError(
                    f"key file {self.key_file} is too short to be a real "
                    f"32-byte key ({len(raw)} bytes). Delete it and retry."
                )
            try:
                return base64.urlsafe_b64decode(raw)
            except Exception:
                # Fall back to treating the file as raw bytes — covers the
                # case where a user hand-wrote a key file without base64.
                return raw

        # Create a fresh key
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        tmp = self.key_file.with_suffix(self.key_file.suffix + ".tmp")
        tmp.write_bytes(base64.urlsafe_b64encode(key))
        try:
            os.chmod(tmp, 0o600)  # Unix-only; harmless on Windows
        except (OSError, NotImplementedError):
            pass
        tmp.replace(self.key_file)
        return key

    # ------------------------------------------------------------------
    # Sign / verify primitives
    # ------------------------------------------------------------------
    def _hmac_hex(self, timestamp: str, body: str) -> str:
        msg = f"{timestamp}\n{body}".encode("utf-8")
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def sign(self, content: str, timestamp: Optional[str] = None) -> str:
        """Return a signed blob ready to be written to a nudge file.

        If timestamp is None, ISO-8601 local time is used. Caller may
        pass an explicit timestamp for deterministic signing in tests.
        """
        if timestamp is None:
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        sig = self._hmac_hex(timestamp, content)
        return f"{sig}\n{timestamp}\n{content}"

    def verify(self, signed_blob: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """Return (ok, content_or_None, reason_or_None).

        Reasons are short strings suitable for logging when we
        quarantine a rejected file.
        """
        # Split into 3 parts max — body can contain newlines
        parts = signed_blob.split("\n", 2)
        if len(parts) < 3:
            return False, None, "malformed (need sig + timestamp + body, separated by \\n)"
        sig_line, ts_line, body = parts[0].strip(), parts[1].strip(), parts[2]
        if len(sig_line) != _HEX_SIG_LEN:
            return False, None, f"signature length wrong (got {len(sig_line)}, want {_HEX_SIG_LEN})"
        if not ts_line:
            return False, None, "timestamp line empty"
        try:
            expected = self._hmac_hex(ts_line, body)
        except Exception as e:
            return False, None, f"hmac compute error: {e}"
        if not hmac.compare_digest(sig_line, expected):
            return False, None, "signature mismatch"
        return True, body, None

    # ------------------------------------------------------------------
    # Watch-directory scan
    # ------------------------------------------------------------------
    def read_pending(self, pattern: str = "nudge_*.txt") -> List[dict]:
        """Walk watch_dir for `pattern`, verify each, return verified.

        Verified files are DELETED on consume (single-use — Todd's
        nudges are not idempotent state, they're one-shot directives).
        Rejected files are renamed to `<name>.rejected_<unix_ts>` so
        repeated bad attempts are visible without being re-processed.

        Failure to read or rename a file is silently logged to stdout
        and skipped — we never raise from this function because we're
        called from inside the agentic loop and one bad file should
        not break Sage's run.
        """
        results: List[dict] = []
        try:
            candidates = sorted(self.watch_dir.glob(pattern))
        except OSError as e:
            print(f"[AIQ_NUDGE] watch_dir glob failed: {e}")
            return results

        for path in candidates:
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                print(f"[AIQ_NUDGE] could not read {path.name}: {e}")
                continue

            ok, content, reason = self.verify(raw)
            if ok:
                results.append({
                    "path":    str(path),
                    "content": content,
                })
                # Delete consumed file
                try:
                    path.unlink()
                except OSError as e:
                    print(f"[AIQ_NUDGE] could not delete consumed {path.name}: {e}")
            else:
                # Quarantine — rename so it stays visible but isn't reprocessed
                quarantine = path.with_name(
                    f"{path.name}.rejected_{int(time.time())}"
                )
                try:
                    path.rename(quarantine)
                except OSError as e:
                    print(
                        f"[AIQ_NUDGE REJECT] {path.name}: {reason} "
                        f"(could not quarantine: {e})"
                    )
                else:
                    print(
                        f"[AIQ_NUDGE REJECT] {path.name}: {reason} "
                        f"(renamed to {quarantine.name})"
                    )

        return results

    # ------------------------------------------------------------------
    # Compose + write (sender side)
    # ------------------------------------------------------------------
    def send(self, content: str) -> Path:
        """Sign `content` and atomically write it as a nudge file in
        watch_dir; return the written Path.

        Shared by the aiq_nudge_send.py terminal helper and the
        /api/aiq-nudge UI endpoint so both compose nudges through ONE
        code path (same key, same atomic write, same filename scheme).
        Raises NudgeError on empty content or write failure.
        """
        body = (content or "").rstrip("\r\n")
        if not body.strip():
            raise NudgeError("refusing to send an empty nudge")
        signed = self.sign(body)
        self.watch_dir.mkdir(parents=True, exist_ok=True)
        target = self.watch_dir / f"nudge_{int(time.time() * 1000)}.txt"
        tmp = target.with_suffix(".txt.tmp")
        try:
            tmp.write_text(signed, encoding="utf-8")
            tmp.replace(target)
        except OSError as e:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise NudgeError(f"could not write nudge file: {e}")
        return target
