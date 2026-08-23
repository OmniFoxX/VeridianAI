"""
VeridianAI AIQNudge — HMAC-signed mid-run side-channel
=====================================================
v2.1.10 #44 implementation.

What this is
------------
Todd's mid-run side-channel for guiding Toga during long agentic runs
without aborting. Without code, the pattern was: Todd would rename a
file or paste content somewhere Toga could see it on her next step.
That works, but it means ANYONE who can write a file with the right
name into the watch directory can inject prompts into Toga's active
run — a self-prompt-injection vector, exactly the thing #44 was
queued to address.

The fix: every nudge file carries an HMAC-SHA256 signature. The
consumer verifies the signature before forwarding the content to
Toga. Unsigned or tampered files get quarantined (renamed to
.rejected_<timestamp>) and never reach the agentic loop. Toga only
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
    @staticmethod
    def ns_token(ns) -> str:
        """One spelling of a namespace, for signing and comparison.

        The owner is a real value here, not an absence: "(owner)" rather than
        an empty line, so a blank cannot be confused with a missing field by
        either the signer or the reader.
        """
        s = str(ns).strip() if ns else ""
        return s or "(owner)"

    def _hmac_hex(self, timestamp: str, body: str, ns_tok: str) -> str:
        """v2.16.1: the NAMESPACE IS SIGNED.

        It has to be. If the owning profile lived in the filename, or beside
        the signature instead of inside it, then renaming a file would
        re-target somebody else's nudge at your session -- and the whole point
        of the HMAC is that a file on disk cannot make Toga do something a
        person did not ask for. Signing the timestamp already stops replay of
        a captured blob; signing the namespace stops re-aiming it.
        """
        msg = f"{timestamp}\n{ns_tok}\n{body}".encode("utf-8")
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def sign(self, content: str, ns=None,
             timestamp: Optional[str] = None) -> str:
        """Return a signed blob ready to be written to a nudge file.

        Format (v2.16.1): sig \n timestamp \n namespace \n body
        The signature covers timestamp, namespace and body.

        If timestamp is None, ISO-8601 local time is used. Caller may
        pass an explicit timestamp for deterministic signing in tests.
        """
        if timestamp is None:
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        ns_tok = self.ns_token(ns)
        sig = self._hmac_hex(timestamp, content, ns_tok)
        return f"{sig}\n{timestamp}\n{ns_tok}\n{content}"

    def verify(self, signed_blob: str):
        """Return (ok, content, namespace, reason_or_timestamp).

        On success the fourth item is the signed TIMESTAMP, so the caller can
        age a nudge out without re-parsing the blob. On failure it is a short
        reason suitable for logging when we discard or quarantine the file.

        v2.16.1 widened this from a 3-tuple; every caller in the tree was
        updated with it.
        """
        # Split into 4 parts max — body can contain newlines
        parts = signed_blob.split("\n", 3)
        if len(parts) < 4:
            # v2.16.1: the PRE-NAMESPACE format. Refused rather than honoured.
            #
            # A 3-line blob carries no owning profile, so there is no way to
            # tell whose session it was meant for -- and honouring it is
            # exactly the bug this version exists to close: a nudge queued by
            # one person arriving in the next person's turn. Any such file is
            # stale by definition (it predates this build), and nudges are
            # one-shot directives measured in seconds, so discarding it costs
            # nothing and guessing costs a great deal.
            return False, None, None, "legacy unscoped nudge (pre-2.16.1)"
        sig_line, ts_line, ns_line, body = (
            parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3])
        if len(sig_line) != _HEX_SIG_LEN:
            return False, None, None, f"signature length wrong (got {len(sig_line)}, want {_HEX_SIG_LEN})"
        if not ts_line:
            return False, None, None, "timestamp line empty"
        if not ns_line:
            return False, None, None, "namespace line empty"
        try:
            expected = self._hmac_hex(ts_line, body, ns_line)
        except Exception as e:
            return False, None, None, f"hmac compute error: {e}"
        if not hmac.compare_digest(sig_line, expected):
            return False, None, None, "signature mismatch"
        return True, body, ns_line, ts_line

    # ------------------------------------------------------------------
    # Watch-directory scan
    # ------------------------------------------------------------------
    def read_pending(self, pattern: str = "nudge_*.txt", ns=None,
                     max_age_sec: int = 300) -> List[dict]:
        """Walk watch_dir for `pattern`, verify each, return verified.

        Verified files are DELETED on consume (single-use — Todd's
        nudges are not idempotent state, they're one-shot directives).
        Rejected files are renamed to `<name>.rejected_<unix_ts>` so
        repeated bad attempts are visible without being re-processed.

        Failure to read or rename a file is silently logged to stdout
        and skipped — we never raise from this function because we're
        called from inside the agentic loop and one bad file should
        not break Toga's run.
        """
        results: List[dict] = []
        try:
            candidates = sorted(self.watch_dir.glob(pattern))
        except OSError as e:
            print(f"[AIQ_NUDGE] watch_dir glob failed: {e}")
            return results

        want_ns = self.ns_token(ns)

        for path in candidates:
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                print(f"[AIQ_NUDGE] could not read {path.name}: {e}")
                continue

            ok, content, got_ns, extra = self.verify(raw)
            if ok:
                # --- EXPIRY -------------------------------------------------
                # A nudge steers the run that is happening NOW. Todd's report:
                # queue one, walk away before it fires, and it ambushes the
                # next prompt -- possibly the next PERSON's. Age is checked
                # before ownership so a forgotten nudge cannot sit in the
                # directory indefinitely waiting for its author to come back.
                _age = self._age_seconds(extra)
                if max_age_sec and _age is not None and _age > max_age_sec:
                    try:
                        path.unlink()
                        print(f"[AIQ_NUDGE] discarded {path.name}: "
                              f"{int(_age)}s old (limit {max_age_sec}s)")
                    except OSError as e:
                        print(f"[AIQ_NUDGE] could not discard stale "
                              f"{path.name}: {e}")
                    continue

                # --- OWNERSHIP ----------------------------------------------
                # Somebody else's nudge is LEFT ALONE, not consumed and not
                # deleted: its author may be one keystroke away from the turn
                # it was meant for. Expiry above is what stops it lingering,
                # so nothing is needed here except restraint.
                if got_ns != want_ns:
                    continue

                results.append({
                    "path":    str(path),
                    "content": content,
                    "ns":      got_ns,
                })
                # Delete consumed file
                try:
                    path.unlink()
                except OSError as e:
                    print(f"[AIQ_NUDGE] could not delete consumed {path.name}: {e}")
            elif extra and extra.startswith("legacy"):
                # Not tampering -- just a format this build cannot attribute.
                # Deleted rather than quarantined so an upgrade does not leave
                # a drift of .rejected_ files nobody will ever read.
                try:
                    path.unlink()
                    print(f"[AIQ_NUDGE] discarded {path.name}: {extra}")
                except OSError as e:
                    print(f"[AIQ_NUDGE] could not discard {path.name}: {e}")
            else:
                # Quarantine — rename so it stays visible but isn't reprocessed
                reason = extra
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

    @staticmethod
    def _age_seconds(ts_line):
        """Seconds since the SIGNED timestamp, or None if it cannot be read.

        The signed timestamp, never the file's mtime: mtime is trivially
        changed by a copy, a sync tool or a backup restore, and this is the
        value the freshness decision rests on. None means "cannot tell", and
        the caller treats that as not-expired -- a clock we cannot read must
        not silently throw away a directive somebody just typed.
        """
        try:
            return time.time() - time.mktime(
                time.strptime(str(ts_line).strip(), "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            return None

    def flush(self, ns=None, pattern: str = "nudge_*.txt") -> int:
        """Drop this profile's pending nudges. Returns how many went.

        Called when a session ends. Todd's scenario, exactly: person A queues
        a nudge, logs off before it fires, person B signs in and types a
        prompt -- and A's directive arrives first, addressed to nobody in
        particular and read by the model as B's own instruction.

        Namespace scoping already prevents the delivery. This prevents the
        LOITERING: the nudge is gone at logout rather than waiting out its
        expiry in a directory shared by everyone on the machine.

        ns=None flushes the OWNER's nudges, not everyone's -- the same meaning
        ns=None carries everywhere else in this codebase. There is deliberately
        no flush-all: "clear every profile's pending directives" is not
        something one signing-out user should be able to do to the others.
        """
        want = self.ns_token(ns)
        gone = 0
        try:
            candidates = sorted(self.watch_dir.glob(pattern))
        except OSError as e:
            print(f"[AIQ_NUDGE] flush glob failed: {e}")
            return 0
        for path in candidates:
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            ok, _content, got_ns, _extra = self.verify(raw)
            if ok and got_ns == want:
                try:
                    path.unlink()
                    gone += 1
                except OSError as e:
                    print(f"[AIQ_NUDGE] flush could not remove {path.name}: {e}")
        if gone:
            print(f"[AIQ_NUDGE] flushed {gone} pending nudge(s) for {want}")
        return gone

    # ------------------------------------------------------------------
    # Compose + write (sender side)
    # ------------------------------------------------------------------
    def send(self, content: str, ns=None) -> Path:
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
        signed = self.sign(body, ns=ns)
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
