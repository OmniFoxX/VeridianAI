"""
backend/time_manager.py — single source of truth for time across OracleAI.
==========================================================================

v2.1.6 unification. Every layer that emits a timestamp goes through this
module so format drift can never happen between the chain log, procedural
KB, chat memory, daemon logs, IPC events, and archives.

Canonical formats:

  iso_z          "2026-04-26T19:30:45.123456Z"
                 ISO 8601 UTC with `Z` suffix. Used in: memory_chain.log
                 entries, procedural.json, chain_digest.json, archives,
                 daemon log files. Matches the format already on disk
                 in pre-existing chain entries — chosen specifically so
                 new entries are byte-comparable against old ones for
                 ordering and dedupe.

  iso_offset     "2026-04-26T19:30:45.123456+00:00"
                 Python's isoformat() default. Same instant, different
                 string form. Provided for callers that explicitly want
                 the +00:00 suffix.

  epoch          1714159845.123
                 POSIX seconds since 1970 UTC. Drop-in for time.time().
                 Used in: IPC event timestamps, prioritiser deadlines,
                 daemon uptime math, internal timing.

  local_display  "2026-04-26 14:30:45"
                 Local-time formatted string for human-facing display
                 (chat UI message timestamps, status banners). Never
                 stored on disk — always derived from a stored UTC value.

Every UTC datetime returned is timezone-aware (datetime.timezone.utc).
Every parser is tolerant of both `Z` and `+00:00` suffixes so old data
written before the unification still round-trips cleanly.

Migration policy (v2.1.6):
  * Memory chain log: NEW entries use this module; old entries left
    UNCHANGED because the SHA3 chain hash is computed over the entire
    entry including the timestamp string. Re-stamping old entries
    would break verify_chain() and destroy provenance. The format
    happens to already be iso_z-compatible, so old and new entries
    co-mingle cleanly.
  * Procedural KB, chat memory, archives, chain digest: full migration
    is safe — no hash chain protects these files. Old entries get
    re-stamped to iso_z form on first read-then-write through the
    consumer, or via the one-shot migration helpers in this module.
"""

from __future__ import annotations

import datetime as _dt
import time as _time
from typing import Optional


class TimeManager:
    """Stateless utility — call as TimeManager.method(). No instances."""

    # ---------------- core "now" ---------------------------------- #
    @staticmethod
    def utc_now() -> _dt.datetime:
        """Current time as a timezone-aware UTC datetime."""
        return _dt.datetime.now(_dt.timezone.utc)

    @staticmethod
    def iso_z() -> str:
        """ISO 8601 UTC with `Z` suffix — chain-log compatible.

        Format example: '2026-04-26T19:30:45.123456Z'

        Python's isoformat() produces a '+00:00' suffix; we replace it
        with 'Z' so new entries match the format already on disk in
        memory_chain.log (which used `datetime.utcnow().isoformat() + "Z"`
        before unification).
        """
        return TimeManager.utc_now().isoformat().replace("+00:00", "Z")

    @staticmethod
    def iso_offset() -> str:
        """ISO 8601 UTC with `+00:00` suffix — Python default form."""
        return TimeManager.utc_now().isoformat()

    @staticmethod
    def epoch() -> float:
        """POSIX seconds since 1970 UTC. Drop-in for time.time()."""
        return _time.time()

    @staticmethod
    def epoch_int() -> int:
        """POSIX seconds as int — for filename suffixes etc."""
        return int(_time.time())

    # ---------------- formatting ---------------------------------- #
    @staticmethod
    def local_display(
        when: Optional[_dt.datetime] = None,
        fmt: str = "%Y-%m-%d %H:%M:%S",
    ) -> str:
        """Format a datetime in LOCAL time for human-facing display.

        If `when` is None, uses current UTC. If `when` is a naive
        datetime, it's assumed to be UTC. Output is in the system's
        local timezone — what the human sitting at the machine reads
        on a wall clock.
        """
        if when is None:
            when = TimeManager.utc_now()
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        local = when.astimezone()  # converts to system local tz
        return local.strftime(fmt)

    @staticmethod
    def local_display_short() -> str:
        """HH:MM:SS only — for compact chat-message stamps."""
        return TimeManager.local_display(fmt="%H:%M:%S")

    @staticmethod
    def session_banner() -> str:
        """Long-form local timestamp suitable for a session-start banner.
        Includes day-of-week + timezone abbreviation."""
        when = TimeManager.utc_now().astimezone()
        # %Z gives the local tz abbreviation (e.g. 'EDT', 'PST')
        return when.strftime("%A, %Y-%m-%d %H:%M:%S %Z").rstrip()

    # ---------------- parsing ------------------------------------- #
    @staticmethod
    def parse_iso(s: str) -> Optional[_dt.datetime]:
        """Parse an ISO 8601 string into a tz-aware datetime.

        Tolerant of both 'Z' and '+00:00' suffixes. Returns None if the
        input is empty, malformed, or naive (no timezone). Naive input
        is rejected by design — a timestamp without a timezone is
        ambiguous and we won't guess.
        """
        if not isinstance(s, str) or not s:
            return None
        s_norm = s.replace("Z", "+00:00") if s.endswith("Z") else s
        try:
            dt = _dt.datetime.fromisoformat(s_norm)
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            return None
        return dt

    @staticmethod
    def is_valid_iso(s: str) -> bool:
        """True iff s parses cleanly as a tz-aware ISO string."""
        return TimeManager.parse_iso(s) is not None

    # ---------------- conversions --------------------------------- #
    @staticmethod
    def epoch_to_iso_z(seconds: float) -> str:
        """Convert epoch seconds to iso_z string."""
        return (
            _dt.datetime.fromtimestamp(seconds, tz=_dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    @staticmethod
    def iso_to_epoch(s: str) -> Optional[float]:
        """Convert ISO string to epoch seconds. None if invalid."""
        dt = TimeManager.parse_iso(s)
        return dt.timestamp() if dt else None

    @staticmethod
    def normalise_iso(s: str) -> Optional[str]:
        """Take any ISO-ish string and return canonical iso_z form,
        or None if input was unparseable. Used by data-file migrations
        to re-stamp old entries into the v2.1.6 canonical format
        without re-deriving the underlying instant."""
        dt = TimeManager.parse_iso(s)
        if dt is None:
            return None
        return dt.astimezone(_dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )

    # ---------------- back-compat aliases ------------------------- #
    # The v2.1.6 draft API used these names; keeping them as aliases
    # so any consumer that imported from the original root file
    # continues to work after the move to backend/.
    @staticmethod
    def get_utc_now() -> _dt.datetime:
        return TimeManager.utc_now()

    @staticmethod
    def get_iso_timestamp() -> str:
        return TimeManager.iso_z()

    @staticmethod
    def validate_timestamp(s: str) -> Optional[_dt.datetime]:
        return TimeManager.parse_iso(s)


# Module-level convenience exports — short names for hot paths
def now_iso() -> str:
    """Shortcut for TimeManager.iso_z()."""
    return TimeManager.iso_z()


def now_epoch() -> float:
    """Shortcut for TimeManager.epoch()."""
    return _time.time()


def now_local() -> str:
    """Shortcut for TimeManager.local_display()."""
    return TimeManager.local_display()
