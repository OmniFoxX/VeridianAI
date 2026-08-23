#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sage_daemon.py — VeridianAI background mechanics daemon
------------------------------------------------------
A separate long-running Python process that handles memory-log mechanics
(reading, verifying, summarizing) on behalf of Toga, so those operations
do NOT consume tokens in her agentic context.

v2.1.3 (April 2026) — Phase A: token-efficient mechanics

CURRENT CAPABILITIES (what this daemon ACTUALLY does today):
  * Hash-chain verification — full integrity walk via
    MemoryLogger.verify_chain()
  * Recent entry retrieval — chronological or sorted by surprise score
  * Extractive summarization — ranks entries by surprise_score and builds
    a compact bullet list. Zero external dependencies, zero token cost.
  * TCP server on 127.0.0.1:9998 (port 9999 is reserved for privacy
    browser IPC mirroring — see ipc_bridge.py / browser_tool.py)
  * Length-prefixed JSON protocol matching sage_daemon_client.py

v2.1.4 (April 21, 2026) -- Fernet encryption wired in.
  * memory_logger_surprise.py now encrypts the 'content' field of each
    entry with a Fernet key loaded from config.FERNET_KEY_FILE. Because
    the daemon uses the SAME MemoryLogger class that the main backend
    uses, decryption on read happens transparently inside get_recent()
    when the key file is reachable. No per-caller Fernet logic needed
    in this file. Hashes are computed over ciphertext so verify_chain()
    continues to work without the key -- tamper detection and
    confidentiality are independent guarantees.
  * Pre-v2.1.4 entries (plaintext 'content') are still readable: the
    decrypt helper detects the Fernet prefix and passes plaintext through
    unchanged. No migration required for existing logs.

NOT YET IMPLEMENTED — these are DESIGN GOALS, not current reality:
  * LLM-backed summarization (summary_type="llm"). The handler has a
    branch for it that currently falls back to extractive with a log
    note. Phase B will wire this to local Ollama without changing the
    client API or sage_engine call sites.
  * Procedural memory offload. Currently only memory log mechanics are
    handled here; procedural memory file I/O still runs in-process.

Supported actions:
  - "ping"             : liveness check
  - "status"           : report daemon + log health
  - "read_recent"      : return N most recent log entries (chronological)
  - "read_top_surprise": return N highest-surprise entries
  - "verify_chain"     : full hash-chain integrity walk
  - "generate_summary" : extractive summary of supplied entries (Phase A)
  - "count_entries"    : how many entries are in the log
  - "shutdown"         : graceful stop (for test harnesses)

Run directly:
    python sage_daemon.py

The backend launcher in main.py spawns this automatically on startup,
but it is safe to run standalone for debugging.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
# v2.1.6 unified time source — for ISO formatting + epoch helpers
sys.path.insert(0, str(Path(__file__).resolve().parent))
from time_manager import TimeManager
# Fernet is wired in inside memory_logger_surprise.py (v2.1.4). The daemon
# does NOT need to handle Fernet directly because MemoryLogger.get_recent()
# decrypts transparently when the shared key file at config.FERNET_KEY_FILE
# is readable. Import kept only so a missing 'cryptography' package fails
# loudly here at boot (rather than on first log read) for easier triage.
from cryptography.fernet import Fernet  # noqa: F401

# --- Paths ----------------
# Import from central config — works on any machine, any drive
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (
    DATA_DIR,
    MEMORY_DIR,
    LOG_DIR,
    DAEMON_LOG,
    PORT_DAEMON,
    PROCEDURAL_DIR,
)

DAEMON_LOG_FILE  = DAEMON_LOG
MEMORY_LOG_DIR   = MEMORY_DIR
# v2.15.2: the OWNER's procedural store, and only the owner's. Since the store
# became per-profile, each profile has its own under users/<ns>/. This daemon
# cannot consolidate those, and the reason is the encryption working correctly:
# a profile's store is sealed with that profile's key, which is registered only
# while that profile is unlocked. A background process has no business holding
# it, and could not decrypt the file if it tried.
#
# Accepted trade rather than a gap: consolidation prunes stale dead-ends, and
# the profile that benefits is the one actually in use -- which is the one
# whose key is available. A locked profile's store simply stops growing while
# nobody is using it.
PROCEDURAL_FILE  = PROCEDURAL_DIR / "procedural.json"
DIGEST_FILE      = MEMORY_DIR / "chain_digest.json"  # rolling summary
                                                      # (v2.1.5 Phase B3)

# --- Network config -----------
HOST        = "127.0.0.1"
PORT        = PORT_DAEMON   # 9998 — 9999 reserved for privacy browser IPC
HEADER_LEN  = 8
RECV_BUF    = 4096
SOCKET_TIMEOUT = 10.0

# --- Logging ------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(DAEMON_LOG_FILE), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("sage_daemon")

# ---------------------------------------------------------------------------
# CRAIID imports (v2.2.0) — placed after logger so warnings emit cleanly.
# Lazy-loaded so a missing audit file or detector script does NOT prevent
# the daemon from starting. Each import degrades gracefully with a warning.
# ---------------------------------------------------------------------------
import importlib.util as _ilu

def _load_ops_detector_module():
    """Load journalist_ops_detector.py via importlib.
    Uses spec_from_file_location so the .py extension and any future
    rename don't break the import."""
    _mod_path = Path(__file__).resolve().parent / "journalist_ops_detector.py"
    if not _mod_path.exists():
        logger.warning(
            "CRAIID: journalist_ops_detector.py not found — "
            "ops detector disabled"
        )
        return None
    spec = _ilu.spec_from_file_location("journalist_ops_detector", _mod_path)
    mod = _ilu.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        return mod
    except Exception as _e:
        logger.warning(f"CRAIID: ops detector module load failed: {_e}")
        return None

try:
    from context_fatigue_detector import (
        load_archives       as _cfd_load_archives,
        extract_user_texts  as _cfd_extract_texts,
        compute_metrics     as _cfd_compute_metrics,
    )
    _CFD_AVAILABLE = True
except ImportError as _e:
    _CFD_AVAILABLE = False
    logger.warning(f"CRAIID: context_fatigue_detector not importable: {_e} "
                   f"— fatigue job disabled")

try:
    from coordinator_signal import build_task as _build_coordinator_task
    _COORD_AVAILABLE = True
except ImportError as _e:
    _COORD_AVAILABLE = False
    logger.warning(f"CRAIID: coordinator_signal not importable: {_e} "
                   f"— task emit disabled")

# --- Memory logger integration -----
# We import Toga's existing MemoryLogger so the daemon reads the exact same
# log file the main backend writes to, and reuses its verification logic.
sys.path.insert(0, str(Path(__file__).parent))
try:
    from memory_logger_surprise import MemoryLogger
    _memory_logger: Optional[MemoryLogger] = MemoryLogger(
        storage_dir=str(MEMORY_LOG_DIR),
        baseline_temp=0.5,
    )
    logger.info(f"MemoryLogger initialized at {MEMORY_LOG_DIR}")
    logger.info(f"Current entries on disk: {_memory_logger.count_entries()}")
    # CRAIID fatigue baseline (2026-06-09): entries already on disk when THIS
    # instance booted. Live fatigue is measured against growth beyond this, so a
    # freshly spawned instance starts at ~0 (see _job_fatigue_check).
    _BOOT_ENTRY_COUNT = _memory_logger.count_entries()
except Exception as e:
    _memory_logger = None
    _BOOT_ENTRY_COUNT = 0
    logger.error(f"Failed to initialize MemoryLogger: {e}")

# Wall-clock boot time of this Toga daemon instance (fatigue boot-grace).
_DAEMON_BOOT_TS = time.time()
    
    
# ---------------------------------------------------------------------------
# CRAIID — ops detector init (v2.2.0)
# Runs once at daemon boot. If audit_personal_report.json is missing,
# the detector stays None and all ops jobs skip cleanly each tick.
# ---------------------------------------------------------------------------
_ops_detector       = None
_ops_lexicon: set   = set()
_ops_mod            = _load_ops_detector_module()

_AUDIT_REPORT    = Path(__file__).resolve().parent / "audit_personal_report.json"
_CRAIID_TASK_FILE = DATA_DIR / "craiid_task.json"

# --- Handoff hardening (#69) --------------------------------------------
# Signed, atomic, tamper-evident handoff artifacts (trigger + warm state +
# hash-chained audit) live in DATA_DIR (sage_data, outside the project tree).
# Degrades gracefully: if the guard cannot init, the daemon still runs, but
# the fatigue handoff falls back to logging only rather than firing a rotation.
try:
    from config import (
        HANDOFF_CADENCE_MAX as _HO_CAD_MAX,
        HANDOFF_CADENCE_WINDOW_SEC as _HO_CAD_WIN,
        HANDOFF_REQUIRE_SOCKET_AUTH as _HO_REQUIRE_AUTH,
    )
except Exception:
    _HO_CAD_MAX, _HO_CAD_WIN, _HO_REQUIRE_AUTH = 5, 300.0, False
try:
    from handoff_guard import (
        HandoffGuard,
        load_or_create_socket_token,
        frame_restored_context,
    )
    _handoff_guard: Optional[HandoffGuard] = HandoffGuard(
        DATA_DIR, cadence_max=_HO_CAD_MAX, cadence_window_sec=_HO_CAD_WIN
    )
    _HANDOFF_GUARD_AVAILABLE = True
    _SOCKET_TOKEN = load_or_create_socket_token(DATA_DIR)
    logger.info(f"Handoff guard ready - artifacts + audit log in {DATA_DIR}")
except Exception as _e:
    _handoff_guard = None
    _HANDOFF_GUARD_AVAILABLE = False
    _SOCKET_TOKEN = None
    logger.warning(f"Handoff guard unavailable: {_e} - handoff hardening disabled")

if _ops_mod is not None:
    if _AUDIT_REPORT.exists():
        try:
            _ops_lexicon  = _ops_mod.load_ops_lexicon(_AUDIT_REPORT, top_n=850)
            _ops_detector = _ops_mod.JournalistOpsDetector(_ops_lexicon)
            logger.info(
                f"CRAIID ops detector ready — "
                f"{len(_ops_lexicon)} lexicon terms loaded"
            )
        except Exception as _e:
            logger.warning(f"CRAIID ops detector init failed: {_e}")
    else:
        logger.warning(
            f"CRAIID: {_AUDIT_REPORT.name} not found — "
            f"run audit_archives_personal_v2.py first to enable ops detection"
        )    


# ---------------------------------
#  PROTOCOL — length-prefixed JSON
# ---------------------------------
def _send_response(sock: socket.socket, response: Dict[str, Any]) -> None:
    """Send a JSON response with an 8-byte length prefix."""
    data = json.dumps(response, separators=(",", ":")).encode("utf-8")
    header = f"{len(data):08d}".encode("utf-8")
    sock.sendall(header + data)


def _recv_request(sock: socket.socket) -> Optional[Dict[str, Any]]:
    """Read an 8-byte length-prefixed JSON request from the socket."""
    # Read the header
    header = b""
    while len(header) < HEADER_LEN:
        chunk = sock.recv(HEADER_LEN - len(header))
        if not chunk:
            return None
        header += chunk
    try:
        length = int(header.decode("utf-8"))
    except ValueError:
        logger.warning(f"Malformed header: {header!r}")
        return None

    # Read the payload
    received = 0
    chunks: List[bytes] = []
    while received < length:
        chunk = sock.recv(min(RECV_BUF, length - received))
        if not chunk:
            return None
        chunks.append(chunk)
        received += len(chunk)

    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except json.JSONDecodeError as e:
        logger.warning(f"Malformed JSON payload: {e}")
        return None


# --------------------
#  ACTION HANDLERS
# --------------------
def handle_ping(req: Dict[str, Any]) -> Dict[str, Any]:
    # v2.1.6: epoch via TimeManager so all time emissions go through
    # one source. Functionally identical to time.time().
    return {"status": "success", "pong": True,
            "timestamp": TimeManager.epoch()}


def handle_status(req: Dict[str, Any]) -> Dict[str, Any]:
    """Report daemon health and memory log stats."""
    log_ok = _memory_logger is not None
    entries = _memory_logger.count_entries() if log_ok else 0
    chain_head = (
        _memory_logger.chain_head[:16] + "..."
        if log_ok else "unavailable"
    )
    # v2.1.5: surface periodic-worker state so /api/daemon/status can
    # tell the user whether consolidation/digest/anomaly are healthy.
    # Holds the lock briefly to take a consistent snapshot.
    with _tick_lock:
        tick_snap = dict(_tick_state)
    return {
        "status": "success",
        "daemon": "running",
        "port": PORT,
        "memory_log_available": log_ok,
        "entries": entries,
        "chain_head_preview": chain_head,
        "log_file": str(MEMORY_LOG_DIR / "memory_chain.log"),
        "uptime_seconds": TimeManager.epoch() - _START_TIME,  # v2.1.6
        "phase": "B (out-of-band mechanics — v2.1.5)",
        "encryption": "fernet (content field) + hash-chain tamper detection",
        "summary_modes_available": ["extractive"],
        "summary_modes_planned": ["llm (Phase B)"],
        # v2.1.5 periodic-worker surfaces
        "periodic_worker": {
            "ticks_run":            tick_snap.get("ticks_run", 0),
            "last_consolidate_ts":  tick_snap.get("last_consolidate_ts"),
            "last_consolidate_msg": tick_snap.get("last_consolidate_msg", ""),
            "last_digest_ts":       tick_snap.get("last_digest_ts"),
            "last_digest_msg":      tick_snap.get("last_digest_msg", ""),
            "last_verify_ts":       tick_snap.get("last_verify_ts"),
            "last_verify_ok":       tick_snap.get("last_verify_ok"),
            "last_verify_msg":      tick_snap.get("last_verify_msg", ""),
            "anomaly_alert":        tick_snap.get("anomaly_alert", False),
            "anomaly_first_ts":     tick_snap.get("anomaly_first_ts"),
            "cadence_consolidate_sec": _CADENCE_CONSOLIDATE,
            "cadence_digest_sec":      _CADENCE_DIGEST,
            "cadence_verify_sec":      _CADENCE_VERIFY,
            # v2.15.2 liveness. Everything above reports what the worker LAST
            # DID, which is exactly the shape of information that cannot tell
            # you it has stopped -- a worker dead for 12 hours still has a
            # perfectly good last_digest_msg. These three say whether it is
            # still running RIGHT NOW.
            "worker_heartbeat_ts":   tick_snap.get("worker_heartbeat_ts"),
            "worker_heartbeat_age_sec": (
                round(TimeManager.epoch()
                      - tick_snap["worker_heartbeat_ts"], 1)
                if tick_snap.get("worker_heartbeat_ts") else None),
            # The loop sleeps 30s, so anything past ~90s is two missed passes
            # and not a scheduling wobble. None = has not run its first pass
            # yet (there is a 15s startup grace), which is not a fault.
            "worker_healthy": (
                None if not tick_snap.get("worker_heartbeat_ts")
                else (TimeManager.epoch()
                      - tick_snap["worker_heartbeat_ts"]) < 90),
            "worker_restarts":    tick_snap.get("worker_restarts", 0),
            "worker_last_death":  tick_snap.get("worker_last_death"),
        },
    }


def handle_read_recent(req: Dict[str, Any]) -> Dict[str, Any]:
    """Return the N most recent log entries."""
    if _memory_logger is None:
        return {"status": "error", "error": "memory_logger_unavailable"}
    try:
        n = int(req.get("count", 10))
        n = max(1, min(n, 500))  # clamp
        entries = _memory_logger.get_recent(n=n, sort_by_surprise=False)
        return {
            "status": "success",
            "entries": entries,
            "count": len(entries),
        }
    except Exception as e:
        logger.exception("read_recent failed")
        return {"status": "error", "error": str(e)}


def handle_read_top_surprise(req: Dict[str, Any]) -> Dict[str, Any]:
    """Return the top N entries by surprise score."""
    if _memory_logger is None:
        return {"status": "error", "error": "memory_logger_unavailable"}
    try:
        n = int(req.get("count", 10))
        n = max(1, min(n, 500))
        entries = _memory_logger.get_recent(n=n, sort_by_surprise=True)
        return {
            "status": "success",
            "entries": entries,
            "count": len(entries),
        }
    except Exception as e:
        logger.exception("read_top_surprise failed")
        return {"status": "error", "error": str(e)}


def handle_verify_chain(req: Dict[str, Any]) -> Dict[str, Any]:
    """Full integrity walk of the memory log."""
    if _memory_logger is None:
        return {"status": "error", "error": "memory_logger_unavailable"}
    try:
        is_valid, msg, count = _memory_logger.verify_chain()
        return {
            "status": "success",
            "valid": is_valid,
            "message": msg,
            "entry_count": count,
        }
    except Exception as e:
        logger.exception("verify_chain failed")
        return {"status": "error", "error": str(e)}


def handle_count_entries(req: Dict[str, Any]) -> Dict[str, Any]:
    if _memory_logger is None:
        return {"status": "error", "error": "memory_logger_unavailable"}
    try:
        return {"status": "success", "count": _memory_logger.count_entries()}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def handle_generate_summary(req: Dict[str, Any]) -> Dict[str, Any]:
    """Generate a summary from supplied entries.

    Phase A: extractive only. Picks the top entries by surprise_score
    and concatenates their content previews into a compact string.

    Future Phase B: add 'llm' summary_type that calls local Ollama.
    The client API does not need to change.
    """
    try:
        entries = req.get("entries", [])
        if not entries:
            return {"status": "success", "summary": ""}

        summary_type = req.get("summary_type", "extractive")
        max_length = int(req.get("max_length", 500))

        if summary_type == "extractive":
            summary = _extractive_summary(entries, max_length=max_length)
        elif summary_type == "llm":
            # Phase B (2026-06-09): LLM-backed summary via the Daemon tier
            # (llama-server, 11436), delegated to the Journalist's shared
            # _llm_summarize helper - the same Daemon-tier primitive a future
            # Symposium mode will use. On ANY failure (tier down / timeout /
            # journalist unavailable) we fall back to the extractive summary so
            # callers never break.
            summary = None
            try:
                if _JOURNALIST_AVAILABLE and hasattr(_journalist_mod, "llm_summarize"):
                    _turns = [
                        {"role": e.get("role", "memory"),
                         "content": str(e.get("content_preview") or e.get("content") or "")}
                        for e in entries
                        if (e.get("content_preview") or e.get("content"))
                    ]
                    summary = _journalist_mod.llm_summarize(_turns)
            except Exception:
                logger.exception("llm summary via Daemon tier failed; using extractive")
            if not summary:
                logger.info("llm summary unavailable; falling back to extractive")
                summary = _extractive_summary(entries, max_length=max_length)
        else:
            return {
                "status": "error",
                "error": f"unknown summary_type: {summary_type}",
            }

        return {
            "status": "success",
            "summary": summary,
            "summary_type": summary_type,
            "source_entry_count": len(entries),
        }
    except Exception as e:
        logger.exception("generate_summary failed")
        return {"status": "error", "error": str(e)}


def _extractive_summary(entries: List[Dict[str, Any]], max_length: int = 500) -> str:
    """Build a tight extractive summary from the highest-surprise entries.

    Strategy: sort by surprise_score descending, take the top entries,
    concatenate their content/content_preview into a bullet list that
    fits within max_length characters.
    """
    # Sort high → low surprise
    sorted_entries = sorted(
        entries,
        key=lambda e: e.get("surprise_score", 0.0),
        reverse=True,
    )

    lines: List[str] = []
    total_len = 0
    for e in sorted_entries:
        # The client sometimes sends "content_preview", sometimes raw "content"
        text = e.get("content_preview") or e.get("content") or ""
        text = str(text).strip().replace("\n", " ")
        if not text:
            continue
        score = e.get("surprise_score", 0.0)
        try:
            score_str = f"{float(score):.2f}"
        except (TypeError, ValueError):
            score_str = "?"

        # Compact bullet: "[0.87] content preview..."
        bullet = f"[{score_str}] {text}"
        # Leave room for ellipsis + newline
        remaining = max_length - total_len - 5
        if remaining <= 20:
            break
        if len(bullet) > remaining:
            bullet = bullet[:remaining] + "..."
        lines.append(bullet)
        total_len += len(bullet) + 1  # +1 for newline

        if total_len >= max_length:
            break

    if not lines:
        return "(no summarizable content)"
    return "\n".join(lines)


def handle_shutdown(req: Dict[str, Any]) -> Dict[str, Any]:
    """Graceful shutdown — used by test harnesses."""
    logger.info("Shutdown requested via IPC")
    _shutdown_event.set()
    return {"status": "success", "message": "shutting down"}


# ====================================================================
#  v2.1.5 PERIODIC WORKER — out-of-band mechanics
# ====================================================================
# Runs three scheduled jobs on independent cadences:
#   * KB consolidation     (Phase B2) — dedupe + age-out unsuccessful
#   * Chain log digest     (Phase B3) — rolling extractive summary
#   * Anomaly / tamper     (Phase B4) — periodic verify_chain()
#
# Design rules:
#   - NEVER modifies memory_chain.log. Reads only. Digest goes to a
#     separate file so it can be regenerated freely.
#   - NEVER deletes chain-witnessed (successful) procedures. Only
#     unsuccessful entries — which are local-only by design — can age
#     out. Memory-integrity-as-design-priority is preserved.
#   - All state changes go through atomic writes (temp + rename) so a
#     crash mid-write can't corrupt the file.
#   - Job failures are logged and the worker keeps running; one bad
#     tick must not take the daemon down.

# Cadences in seconds — overridable via env vars for testing
_CADENCE_CONSOLIDATE = int(os.environ.get(
    "SAGE_DAEMON_CONSOLIDATE_SEC", 600))   # 10 min
_CADENCE_DIGEST = int(os.environ.get(
    "SAGE_DAEMON_DIGEST_SEC",      300))   # 5 min
_CADENCE_VERIFY = int(os.environ.get(
    "SAGE_DAEMON_VERIFY_SEC",      300))   # 5 min
# CRAIID cadences (v2.2.0)
_CADENCE_OPS     = int(os.environ.get("SAGE_DAEMON_OPS_SEC",     60))  # 1 min
_CADENCE_FATIGUE = int(os.environ.get("SAGE_DAEMON_FATIGUE_SEC", 120)) # 2 min
_CADENCE_LLAMA   = int(os.environ.get("SAGE_DAEMON_LLAMA_SEC",    30)) # 30 sec
# #69 Archivist depth: rebuild the compressed VLTS store at a low cadence
# (default 6h) so the Author has historical depth to decompress. Bounded + safe.
_CADENCE_VLTS    = int(os.environ.get("SAGE_DAEMON_VLTS_SEC",   21600)) # 6 h
_last_vlts_build = 0.0   # assigned by _periodic_worker; 0 => build on first tick
# #69 Journalist janitor: bound growth (reconstructs / stale VLTS chunks / MLM
# CSV rotation / aged quarantine) at a low cadence. Cheap; runs on first tick.
_CADENCE_JOURNALIST = int(os.environ.get("SAGE_DAEMON_JOURNALIST_SEC", 3600)) # 1 h
_last_journalist = 0.0

# Stale-entry cutoff for unsuccessful procedures: prune after this many
# days of no updates, default 14. Successful (chain-witnessed) entries
# are NEVER pruned regardless of age.
_STALE_UNSUCCESSFUL_DAYS = int(os.environ.get(
    "SAGE_DAEMON_STALE_DAYS",      14))

_tick_state: Dict[str, Any] = {
    "last_consolidate_ts":  None,
    "last_consolidate_msg": "",
    "last_digest_ts":       None,
    "last_digest_msg":      "",
    "last_verify_ts":       None,
    "last_verify_ok":       None,
    "last_verify_msg":      "",
    "anomaly_alert":        False,     # set True if last verify failed
    "anomaly_first_ts":     None,      # first timestamp the alert flipped on
    "ticks_run":            0,         # --- CRAIID (v2.2.0) signals below ---
    "ops_mode":               False,   # True when journalist detector fires
    "ops_score":              0.0,     # last per-turn ops score
    "ops_last_ts":            None,    # epoch of last ops snapshot
    "fatigue_detected":       False,   # True when fatigue detector fires
    "fatigue_metrics":        {},      # last metrics dict from detector
    "fatigue_reasons":        [],      # human-readable reason strings
    "fatigue_last_ts":        None,    # epoch of last fatigue check
    "llama_progress":         0.0,     # 0.0–1.0 context window fill
    "llama_cached_tokens":    0,       # cached n_tokens from llama-server
    "llama_last_ts":          None,    # epoch of last llama log parse
    "craiid_task_emitted":    False,   # True after task written to disk
    "craiid_task_emitted_ts": None,    # epoch of last task emit
    # v2.15.2 worker liveness. See _periodic_worker and handle_status.
    "worker_heartbeat_ts":    None,    # epoch, bumped EVERY loop pass
    "worker_restarts":        0,       # times the supervisor revived it
    "worker_last_death":      None,    # human-readable cause of the last death
}
_tick_lock = threading.Lock()


def _atomic_write_json(path: Path, obj: Any) -> None:
    """Write JSON atomically: temp file + os.replace. Crash-safe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# v2.15.2: the procedural store is ENCRYPTED at rest (see procedural_memory.py).
#
# _atomic_write_json above stays plaintext, so the procedural store gets its
# own pair, and they are the ONLY way this daemon touches that file.
#
# CORRECTION (same day): the first version of this comment justified that by
# saying DIGEST_FILE and _CRAIID_TASK_FILE are "pipeline state, not user
# content". That is true of _CRAIID_TASK_FILE. It was NOT true of DIGEST_FILE:
# chain_digest.json carries recent_chrono -- up to 50 entries each with a
# 160-char `preview` of real message content -- plus a ~1200-char extractive
# `summary` of the same. Derived, but derived FROM user text, which makes it
# user content. It is now encrypted too.
#
# So _atomic_write_json is left with exactly one caller, _CRAIID_TASK_FILE,
# which really is pipeline state. Anything added here later that touches user
# text wants _write_encrypted_json instead -- and if you are unsure which a
# file is, open it and look, rather than reasoning from its name. Both of this
# file's plaintext stores looked like infrastructure from the outside.
#
# Both call sites matter. The read would have failed closed on ciphertext and
# quietly disabled consolidation ("read failed: ..."), which is the kind of
# silent degradation this release has spent its whole time hunting. The WRITE
# was worse: _atomic_write_json would have put the file back as PLAINTEXT and
# undone the encryption on the next consolidation tick.
#
# SYSTEM TIER (ns=None), matching procedural_memory.py. If these two ever
# disagree about the namespace, the app and the daemon cannot read each other's
# writes.
# ---------------------------------------------------------------------------
def _read_encrypted_json(path):
    """Read a JSON store that may be ciphertext OR legacy plaintext.

    Returns None on ANY failure. That is the important part of the contract:
    None means "a file is there and I could not read it", never "it is empty".
    A caller that rewrites the whole file must treat None as 'do nothing'.
    """
    # SYSTEM TIER: these stores are install-wide, written by background work
    # with no signed-in user, alongside the audit chain. Matches
    # procedural_memory.py -- if the two disagree on namespace, the app and
    # this daemon cannot read each other's writes.
    try:
        import atrest as _atrest
        with open(path, "rb") as f:
            obj = _atrest.load_json_auto(f.read())
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _write_encrypted_json(path, obj) -> None:
    """Atomically write a JSON store as ciphertext."""
    # SYSTEM TIER: same classification as the reader above. Not a default.
    import atrest as _atrest
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(_atrest.dump_json_encrypted(obj))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_procedural_kb():
    """The OWNER's procedural store: verbatim user_request / answer previews.

    v2.15.2: the keys line up, and that is not luck. ProceduralMemory encrypts
    with ns=self.owner_ns, and the owner's owner_ns is None, which atrest
    resolves to the system key -- the same key _read_encrypted_json uses here.
    A PROFILE's store is sealed with that profile's key and is deliberately not
    reachable from this process; see the PROCEDURAL_FILE note above.
    """
    return _read_encrypted_json(PROCEDURAL_FILE)


def _write_procedural_kb(kb) -> None:
    return _write_encrypted_json(PROCEDURAL_FILE, kb)


def _read_digest():
    """The rolling chain digest: message previews + an extractive summary."""
    return _read_encrypted_json(DIGEST_FILE)


def _write_digest(digest) -> None:
    return _write_encrypted_json(DIGEST_FILE, digest)


def _log_mlm_training_row(action: str) -> None:
    try:
        row = f"0.0,0.0,0.0,0.0,1.0,{action}\n"
        log_path = (
            Path(__file__).resolve().parent
            / "mlm_training_data"
            / "daemon_calls.csv"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(row)
    except Exception:
        pass



def _job_consolidate_procedural() -> str:
    """Phase B2: dedupe and age out the unsuccessful bucket.

    Read procedural.json, walk both buckets, and:
      - Successful: leave intact (chain-witnessed, never pruned).
      - Unsuccessful: remove entries with timestamp older than the
        configured stale-window AND no recent metadata.attempts bump.
      - Both: collapse near-duplicate keys (case-insensitive exact
        match after slug normalization). Successful wins on tie.

    Returns a human-readable summary string.
    """
    if not PROCEDURAL_FILE.exists():
        return "no procedural.json yet"

    # v2.15.2: encrypted at rest. A None here means the file EXISTS and could
    # not be read -- never "it's empty". Returning early leaves it untouched,
    # which is the only safe response: consolidation rewrites the whole file,
    # so proceeding on a misread would delete everything it could not parse.
    kb = _read_procedural_kb()
    if kb is None:
        return ("read failed: procedural store present but unreadable "
                "(at-rest key mismatch?) -- left untouched")

    succ = kb.get("successful", {})
    unsucc = kb.get("unsuccessful", {})

    pruned = 0
    deduped = 0

    # --- Age-out unsuccessful entries -----------------------------
    # v2.1.6: TimeManager.epoch_to_iso_z gives us the canonical Z-suffix
    # form so cutoff_iso is byte-comparable with stored timestamps that
    # were also written via TimeManager.iso_z().
    cutoff_iso = TimeManager.epoch_to_iso_z(
        TimeManager.epoch() - _STALE_UNSUCCESSFUL_DAYS * 86400
    )
    keep: Dict[str, Any] = {}
    for k, entry in unsucc.items():
        if not isinstance(entry, dict):
            keep[k] = entry
            continue
        ts = str(entry.get("timestamp", ""))
        if ts and ts < cutoff_iso:
            pruned += 1
            continue
        keep[k] = entry
    unsucc = keep

    # --- Dedupe within each bucket --------------------------------
    def _norm(s: str) -> str:
        return "".join(ch.lower() for ch in s if ch.isalnum() or ch in "_:")

    def _dedupe(bucket: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal deduped
        seen: Dict[str, str] = {}     # norm_key -> canonical_key
        out: Dict[str, Any] = {}
        for k, entry in bucket.items():
            nk = _norm(k)
            if nk in seen:
                # Keep the entry with the more recent timestamp
                canon = seen[nk]
                e_old = out.get(canon, {})
                if (isinstance(entry, dict)
                        and isinstance(e_old, dict)
                        and str(entry.get("timestamp", ""))
                        > str(e_old.get("timestamp", ""))):
                    out[canon] = entry
                deduped += 1
                continue
            seen[nk] = k
            out[k] = entry
        return out

    succ = _dedupe(succ)
    unsucc = _dedupe(unsucc)

    # --- Cross-bucket: if a key exists in both, successful wins ---
    cross_demoted = 0
    for k in list(unsucc.keys()):
        if k in succ:
            del unsucc[k]
            cross_demoted += 1

    kb["successful"] = succ
    kb["unsuccessful"] = unsucc
    
    try:
        # v2.15.2: encrypted. _atomic_write_json here would have written the
        # store back as PLAINTEXT and silently undone the at-rest protection
        # on the first consolidation tick after migration.
        _write_procedural_kb(kb)
    except Exception as e:
        return f"write failed: {e}"
        
    _log_mlm_training_row("consolidate_now")

    return (
        f"pruned {pruned} stale unsuccessful, "
        f"deduped {deduped}, "
        f"removed {cross_demoted} cross-bucket dupes "
        f"(successful={len(succ)}, unsuccessful={len(unsucc)})"
    )


def _job_chain_digest() -> str:
    """Phase B3: rolling extractive summary of recent chain entries.

    Reads (does NOT modify) the chain log via MemoryLogger, picks the
    top-surprise entries, builds a compact bullet-list digest, writes
    to DIGEST_FILE. The digest is a derived artifact — safe to delete,
    fully regenerable.
    """
    if _memory_logger is None:
        return "memory_logger unavailable"
    try:
        # Pull a manageable slice — last 50 chronological + top 20 by
        # surprise. The summary is built from the surprise-ranked set;
        # the chronological set is included so consumers can see what's
        # 'recent' in addition to what's 'memorable'.
        recent_chrono = _memory_logger.get_recent(
            n=50, sort_by_surprise=False)
        recent_surprise = _memory_logger.get_recent(
            n=20, sort_by_surprise=True)
        summary = _extractive_summary(
            recent_surprise, max_length=1200)
        digest = {
            "generated_at":   TimeManager.iso_z(),  # v2.1.6 unified
            "chain_head":     getattr(
                _memory_logger, "chain_head", "unknown"),
            "entry_count":    _memory_logger.count_entries(),
            "summary":        summary,
            "recent_chrono":  [
                {
                    "role": e.get("role", ""),
                    "ts":   e.get("timestamp", ""),
                    "preview": (
                        str(e.get("content", ""))[:160]
                    ),
                }
                for e in recent_chrono
            ],
        }
        # v2.15.2: encrypted. recent_chrono carries up to 50 x 160-char
        # previews of real message content, and `summary` is an extractive
        # digest of the same -- derived, but derived FROM user text, which
        # makes it user content. It was plaintext until this change.
        _write_digest(digest)
        _log_mlm_training_row("run_digest_now")
        return (f"digest written, {len(summary)} chars, "
                f"{len(recent_chrono)} recent entries")
    except Exception as e:
        return f"digest failed: {e}"


def _job_anomaly_check() -> str:
    """Phase B4: periodic verify_chain() for tamper detection.

    Cheap sanity-check on the chain log. Sets _tick_state['anomaly_alert']
    on failure so the next /api/daemon/status surface picks it up.
    """
    if _memory_logger is None:
        return "memory_logger unavailable"
    try:
        ok, msg, count = _memory_logger.verify_chain()
        with _tick_lock:
            _tick_state["last_verify_ok"] = ok
            _tick_state["last_verify_msg"] = msg
            if not ok and not _tick_state.get("anomaly_alert"):
                _tick_state["anomaly_alert"] = True
                _tick_state["anomaly_first_ts"] = TimeManager.iso_z()
                logger.error(
                    f"ANOMALY DETECTED: chain verify failed: {msg}")
            elif ok and _tick_state.get("anomaly_alert"):
                # Recovery (e.g., user restored from backup)
                logger.info("Chain verify recovered (anomaly cleared)")
                _tick_state["anomaly_alert"] = False
                _tick_state["anomaly_first_ts"] = None
        _log_mlm_training_row("verify_chain")
        return f"verify ok={ok} entries={count} ({msg})"
    except Exception as e:
        return f"verify exception: {e}"


_CANARY_WARN_INTERVAL = 300.0  # per-job: do not repeat an alarm more often than this
_canary_last_warn: Dict[str, float] = {}


def _craiid_canary_check(now: float, worker_start: float) -> None:
    """BUG#1 regression guard (#69 dry-run follow-up).

    BUG#1 mis-indented the CRAIID jobs into the worker's `except` block, so
    they never ran in healthy operation and fatigue detection was silently
    dead (0 [fatigue_check] lines vs 2200+ others). This canary makes any
    recurrence LOUD: if a CRAIID job has not run within a generous multiple of
    its cadence (after a startup grace), it logs a warning. Rate-limited per
    job so it never spams the log.
    """
    with _tick_lock:
        snapshot = (
            ("fatigue_check",  _tick_state.get("fatigue_last_ts"), _CADENCE_FATIGUE),
            ("ops_snapshot",   _tick_state.get("ops_last_ts"),     _CADENCE_OPS),
            ("llama_progress", _tick_state.get("llama_last_ts"),   _CADENCE_LLAMA),
        )
    for name, last, cadence in snapshot:
        threshold = max(float(cadence) * 3.0, 90.0)
        if (now - worker_start) < threshold:
            continue  # still within startup grace for this job
        stale = (last is None) or ((now - last) > threshold)
        if not stale:
            continue
        if (now - _canary_last_warn.get(name, 0.0)) < _CANARY_WARN_INTERVAL:
            continue
        _canary_last_warn[name] = now
        age = "EVER" if last is None else f"{int(now - last)}s ago"
        logger.warning(
            f"[CANARY] CRAIID job '{name}' last ran {age} "
            f"(threshold {int(threshold)}s) - detection may be silently "
            f"disabled. See #69 BUG#1 (jobs mis-indented out of the try body)."
        )


def _periodic_worker() -> None:
    """Long-running worker thread that runs the three scheduled jobs.

    Loops every 30s, checks each cadence, runs jobs whose interval has
    elapsed. Uses _shutdown_event.wait() so the daemon can exit
    promptly when SIGINT/SIGTERM lands.
    """
    logger.info(
        f"Periodic worker started "
        f"(consolidate={_CADENCE_CONSOLIDATE}s, "
        f"digest={_CADENCE_DIGEST}s, "
        f"verify={_CADENCE_VERIFY}s, "
        f"stale_days={_STALE_UNSUCCESSFUL_DAYS})"
    )
    # Run each job once shortly after start so the daemon is
    # immediately useful rather than waiting a full cadence.
    _shutdown_event.wait(timeout=15)
    _worker_start = time.time()  # #69 canary: startup-grace reference (self-consistent with `now`)
    global _last_vlts_build  # #69 Archivist depth: low-cadence VLTS rebuild
    global _last_journalist  # #69 Journalist janitor: low-cadence bloat control
    while not _shutdown_event.is_set():
        now = time.time()
        # v2.15.2: heartbeat FIRST, every pass, before any job can throw.
        #
        # On 2026-08-20 this thread ran one consolidate at 06:56:28 and then
        # stopped for 12.5 hours. The daemon stayed up, kept its socket, kept
        # answering `status` -- and CRAIID's digest, verify, ops, fatigue and
        # context-fill jobs were all simply not running. Nothing said so.
        # It surfaced only because someone looked at log timestamps.
        #
        # The staleness canary below cannot catch that: it runs INSIDE this
        # loop, so a dead worker takes its own watchdog with it. A heartbeat
        # written here is observable from OUTSIDE the thread -- by handle_status
        # and by the supervisor in run_server -- which is the whole point.
        with _tick_lock:
            _tick_state["worker_heartbeat_ts"] = now
            last_c = _tick_state["last_consolidate_ts"] or 0
            last_d = _tick_state["last_digest_ts"] or 0
            last_v = _tick_state["last_verify_ts"] or 0
            last_ll = _tick_state["llama_last_ts"] or 0
            last_op = _tick_state["ops_last_ts"] or 0
            last_ft = _tick_state["fatigue_last_ts"] or 0

        try:
            if now - last_c >= _CADENCE_CONSOLIDATE:
                msg = _job_consolidate_procedural()
                with _tick_lock:
                    _tick_state["last_consolidate_ts"] = now
                    _tick_state["last_consolidate_msg"] = msg
                    _tick_state["ticks_run"] += 1
                logger.info(f"[consolidate] {msg}")

            if now - last_d >= _CADENCE_DIGEST:
                msg = _job_chain_digest()
                with _tick_lock:
                    _tick_state["last_digest_ts"] = now
                    _tick_state["last_digest_msg"] = msg
                    _tick_state["ticks_run"] += 1
                logger.info(f"[digest] {msg}")

            if now - last_v >= _CADENCE_VERIFY:
                msg = _job_anomaly_check()
                with _tick_lock:
                    _tick_state["last_verify_ts"] = now
                    _tick_state["ticks_run"] += 1
                logger.info(f"[verify] {msg}")

            # CRAIID jobs (v2.2.0): llama_progress, ops_snapshot, fatigue_check.
            # FIX (#69 dry-run, Hermes 2026-06-08): these three were previously
            # mis-indented INSIDE the `except` block below, so they only ran when
            # a consolidate/digest/verify exception fired — i.e. never, during
            # healthy operation. That silently disabled CRAIID fatigue detection
            # end-to-end (sage_daemon.log showed 0 [fatigue_check] lines against
            # 2200+ [consolidate]/[digest]/[verify]). They belong in the try body
            # so they run every cadence and remain covered by the catch-all below.
            if now - last_ll >= _CADENCE_LLAMA:
                msg = _job_llama_progress()
                with _tick_lock:
                    _tick_state["llama_last_ts"] = now
                    _tick_state["ticks_run"] += 1
                # v2.15.2: an empty string means "nothing worth saying this
                # tick" -- the job still ran and still updated the tick state,
                # it simply has no news. Logging it anyway is how a diagnostic
                # turns into 2,880 identical lines a day.
                if msg:
                    logger.info(f"[llama_progress] {msg}")

            if now - last_op >= _CADENCE_OPS:
                msg = _job_ops_snapshot()
                with _tick_lock:
                    _tick_state["ops_last_ts"] = now
                    _tick_state["ticks_run"] += 1
                logger.info(f"[ops_snapshot] {msg}")

            if now - last_ft >= _CADENCE_FATIGUE:
                msg = _job_fatigue_check()
                with _tick_lock:
                    _tick_state["fatigue_last_ts"] = now
                    _tick_state["ticks_run"] += 1
                logger.info(f"[fatigue_check] {msg}")

            # #69 dry-run canary: lives in the try body on purpose - the same
            # place the jobs run - so a recurrence of BUG#1 (jobs indented into
            # the except) makes the timestamps go stale and this screams.
            _craiid_canary_check(now, _worker_start)

            # #69 Archivist depth: rebuild the compressed VLTS store at a low
            # cadence so the Author has history to decompress. Defensive; a
            # failure here never takes the worker down.
            if (now - _last_vlts_build) >= _CADENCE_VLTS:
                _last_vlts_build = now
                logger.info(f"[vlts_build] {_job_build_vlts()}")

            # #69 Journalist janitor: bound growth so long uptimes + data spikes
            # stay healthy. Defensive; a failure never takes the worker down.
            if (now - _last_journalist) >= _CADENCE_JOURNALIST:
                _last_journalist = now
                logger.info(f"[journalist] {_job_journalist()}")
        except Exception as e:
            logger.exception(f"Periodic tick crashed: {e}")
        except BaseException as e:
            # v2.15.2: `except Exception` does not catch BaseException --
            # MemoryError, SystemExit, KeyboardInterrupt, or anything a C
            # extension raises outside the Exception hierarchy. One of those
            # escaping here ends the thread, and a Python thread that dies
            # from an unhandled BaseException writes to STDERR, which for a
            # windowless child process goes nowhere at all. That is the
            # difference between "the log records a crash" and "the log simply
            # stops", and on 2026-08-20 what we got was the second one.
            #
            # Recorded and re-raised rather than swallowed: these are not
            # conditions to keep looping through, but they must not vanish.
            # The supervisor in run_server sees the thread die and restarts it.
            try:
                with _tick_lock:
                    _tick_state["worker_last_death"] = (
                        f"{type(e).__name__}: {e}")
                logger.critical(
                    f"Periodic worker hit a BaseException and is exiting: "
                    f"{type(e).__name__}: {e}", exc_info=True)
            except BaseException:
                pass          # never let the reporting path mask the cause
            raise

        # Wait up to 30s, breaking early on shutdown
        _shutdown_event.wait(timeout=30)
    logger.info("Periodic worker stopped.")
    
    
# ====================================================================
#  v2.2.0 CRAIID PERIODIC JOBS
# ====================================================================
# Three new scheduled jobs feeding the CRAIID signal pipeline.
# Same design rules as the existing three jobs above:
#   - Never crash the worker thread — all exceptions caught and logged
#   - Never modify memory_chain.log
#   - Atomic writes for any file output
#   - Return a human-readable summary string for the logger

# CRAIID path constants.
#
# v2.16.1 -- THIS POINTED AT THE PROJECT ROOT, AND THE COMMENT SAID OTHERWISE.
#
# It read `Path(__file__).parent.parent / "archives"` -- backend/ up to the
# install directory -- which is where archives lived until the 2026-08-13 move
# into sage_data. It was assigned exactly once, here, and never reassigned,
# while the comment claimed "set at module init in Block B". There is no Block
# B anywhere in this file. Nobody looked closer because the comment said
# somebody else had already handled it.
#
# WHAT IT COST, which is not what it looks like. The ops snapshot is not
# cosmetic: _job_ops_snapshot returns "archives dir not found -- skipping" and
# ops_mode never goes active, so the anticipatory pre-warm margin below
# (_CRAIID_PREWARM_MARGIN, see the note at its definition) never lowers the
# fatigue threshold, so CRAIID never starts the warm handoff BEFORE the
# context cliff. Context then runs to the cliff and handoffs fire back to
# back, each one slower than the last.
#
# It only showed up away from the machine that predates the move: an install
# directory still holding a legacy archives/ folder makes this line work by
# accident, and a clean machine does not.
#
# OWNER SCOPE, DELIBERATELY. This is a background worker with no session and
# no profile key, so a non-owner's archives are unreadable to it by design --
# the same accepted trade already written down for the procedural store. It
# reads the owner's corpus to compute one aggregate behavioural signal; it
# does not read, mix, or surface any profile's conversation content.
def _resolve_archives_dir() -> Path:
    """sage_data/archives, via the app's own resolver -- never rebuilt here."""
    try:
        from state_paths import ARCHIVES_DIR
        return Path(ARCHIVES_DIR)
    except Exception:
        pass
    try:
        # Second opinion, and the one that knows about the half-finished-move
        # case. require_content=False: "where should this live", not "where is
        # there something to read" -- a fresh install has nowhere yet and still
        # needs the right answer.
        from craiid.craiid_paths import archives_dir as _craiid_archives_dir
        _p = _craiid_archives_dir(require_content=False)
        if _p:
            return Path(_p)
    except Exception:
        pass
    # Historical location. Reached only if both resolvers are unavailable,
    # which in the app means something is very wrong -- but returning a path
    # keeps the worker alive, and the job reports the miss rather than raising.
    return Path(__file__).resolve().parent.parent / "archives"


_ARCHIVES_DIR = _resolve_archives_dir()

# Fatigue thresholds — match context_fatigue_detector.py defaults.
# Overridable via env vars so they can be tuned without code changes.
_FATIGUE_TOKEN_THRESH      = float(os.environ.get("CRAIID_TOKEN_THRESH",      0.7))
_FATIGUE_REPETITION_THRESH = float(os.environ.get("CRAIID_REPETITION_THRESH", 0.6))
_FATIGUE_ENTROPY_THRESH    = float(os.environ.get("CRAIID_ENTROPY_THRESH",    0.4))

# CRAIID live-fatigue knobs (2026-06-09 repoint). Fatigue is measured from the
# LIVE instance (conversation accumulated since boot + llama kv-cache), NOT the
# static archives/. All env-tunable for slow local rigs / long deep-dives.
_CRAIID_CONTEXT_BUDGET_TOKENS  = int(os.environ.get("CRAIID_CONTEXT_BUDGET_TOKENS",  24000))
_CRAIID_MIN_TURNS              = int(os.environ.get("CRAIID_FATIGUE_MIN_TURNS",      6))
_CRAIID_AVG_TOKENS_PER_TURN    = int(os.environ.get("CRAIID_AVG_TOKENS_PER_TURN",    300))
_CRAIID_FATIGUE_BOOT_GRACE_SEC = int(os.environ.get("CRAIID_FATIGUE_BOOT_GRACE_SEC", 180))
_CRAIID_FATIGUE_COOLDOWN_SEC   = int(os.environ.get("CRAIID_FATIGUE_COOLDOWN_SEC",   180))
# Anticipatory pre-warm margin: when ops_mode (the user behavioral profile) is
# active, the fatigue threshold is lowered by this much so CRAIID begins the
# warm handoff BEFORE the context cliff. A soft nudge on a legit signal - not a
# gate and not an interval. 0.0 disables (detect-only).
_CRAIID_OPS_PREWARM_MARGIN     = float(os.environ.get("CRAIID_OPS_PREWARM_MARGIN", 0.15))

# kv_cache_usage_ratio thresholds (from handoff summary)
_LLAMA_WARN_THRESHOLD  = float(os.environ.get("CRAIID_LLAMA_WARN",  0.90))
_LLAMA_CLIFF_THRESHOLD = float(os.environ.get("CRAIID_LLAMA_CLIFF", 0.95))


def _job_ops_snapshot() -> str:
    """CRAIID Phase 1: Run the journalist ops detector against recent
    archive turns and update _tick_state with the current ops signal.

    Reads the last 20 user turns from the most recent archive file,
    feeds them through JournalistOpsDetector.update() one at a time,
    and records the final ops_mode state and score. Does NOT modify
    any chain-witnessed files — read-only operation.

    Returns a human-readable summary string for the logger.
    """
    if _ops_detector is None:
        return "ops detector unavailable (audit report missing or init failed)"

    if not _ARCHIVES_DIR.exists():
        return "archives dir not found — skipping ops snapshot"

    try:
        # Load archives and extract recent user turns
        archives = _cfd_load_archives(_ARCHIVES_DIR)
        if not archives:
            return "no archives found — skipping ops snapshot"

        all_messages: list = []
        for msg_list in archives:
            if isinstance(msg_list, list):
                all_messages.extend(msg_list)

        user_texts = _cfd_extract_texts(all_messages)
        if not user_texts:
            return "no user turns in archives — skipping ops snapshot"

        # Feed last 20 turns through the detector (sliding window)
        recent = user_texts[-20:]
        last_flag = False
        last_score = 0.0
        last_details: dict = {}

        for turn in recent:
            last_flag, last_score, last_details = _ops_detector.update(turn)

        summary = _ops_detector.session_summary()

        with _tick_lock:
            _tick_state["ops_mode"]     = last_flag
            _tick_state["ops_score"]    = round(last_score, 4)
            _tick_state["ops_last_ts"]  = time.time()

        _log_mlm_training_row("ops_snapshot")

        return (
            f"ops_mode={last_flag} score={last_score:.4f} "
            f"turns_fed={len(recent)} "
            f"unique_terms={summary['unique_ops_terms']} "
            f"distribution={summary['hit_distribution']}"
        )

    except Exception as e:
        return f"ops snapshot failed: {type(e).__name__}: {e}"


def _load_author_module():
    """Load craiid/craiid_author.py via importlib so a missing/broken Author
    never prevents the daemon from running (mirrors the ops-detector loader)."""
    _mod_path = Path(__file__).resolve().parent / "craiid" / "craiid_author.py"
    if not _mod_path.exists():
        logger.warning("CRAIID: craiid_author.py not found - warm reconstruction disabled")
        return None
    try:
        spec = _ilu.spec_from_file_location("craiid_author", _mod_path)
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as _e:
        logger.warning(f"CRAIID: author module load failed: {_e} - warm reconstruction disabled")
        return None


_author_mod = _load_author_module()
_AUTHOR_AVAILABLE = _author_mod is not None and hasattr(_author_mod, "prepare_warm_instance")


def _load_archivist_worker():
    """Load craiid/archivist_compression_worker.py via importlib (graceful).
    Adds craiid/ to sys.path first so the worker's sibling imports resolve."""
    craiid_dir = Path(__file__).resolve().parent / "craiid"
    _mod_path = craiid_dir / "archivist_compression_worker.py"
    if not _mod_path.exists():
        logger.warning("CRAIID: archivist_compression_worker.py not found - VLTS build disabled")
        return None
    try:
        if str(craiid_dir) not in sys.path:
            sys.path.insert(0, str(craiid_dir))
        spec = _ilu.spec_from_file_location("archivist_compression_worker", _mod_path)
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as _e:
        logger.warning(f"CRAIID: archivist worker load failed: {_e} - VLTS build disabled")
        return None


_archivist_mod = _load_archivist_worker()
_ARCHIVIST_AVAILABLE = _archivist_mod is not None and hasattr(_archivist_mod, "ArchivistCompressionWorker")


def _load_journalist_module():
    """Load craiid/journalist.py via importlib (graceful)."""
    craiid_dir = Path(__file__).resolve().parent / "craiid"
    _mod_path = craiid_dir / "journalist.py"
    if not _mod_path.exists():
        logger.warning("CRAIID: journalist.py not found - janitor disabled")
        return None
    try:
        if str(craiid_dir) not in sys.path:
            sys.path.insert(0, str(craiid_dir))
        spec = _ilu.spec_from_file_location("journalist", _mod_path)
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as _e:
        logger.warning(f"CRAIID: journalist load failed: {_e} - janitor disabled")
        return None


_journalist_mod = _load_journalist_module()
_JOURNALIST_AVAILABLE = _journalist_mod is not None and hasattr(_journalist_mod, "run_maintenance")


def _job_journalist() -> str:
    """Low-cadence: run the Journalist janitor to bound growth. FULLY DEFENSIVE
    - never raises; empty install -> clean no-op."""
    if not _JOURNALIST_AVAILABLE:
        return "journalist unavailable - skipped"
    try:
        rep = _journalist_mod.run_maintenance()
        acts = rep.get("actions") or []
        errs = rep.get("errors") or []
        if errs:
            return f"journalist: {len(acts)} actions, {len(errs)} errors: {errs[:2]}"
        return f"journalist: {'; '.join(acts) if acts else 'nothing to prune'}"
    except Exception as e:
        return f"journalist failed: {type(e).__name__}: {e}"


def _job_build_vlts() -> str:
    """Low-cadence: rebuild the compressed VLTS store from archives so the Author
    has historical depth to decompress. FULLY DEFENSIVE - never raises; empty or
    sparse archives are a clean no-op, not an error."""
    if not _ARCHIVIST_AVAILABLE:
        return "archivist worker unavailable - skipped"
    try:
        worker = _archivist_mod.ArchivistCompressionWorker(
            vlts_archive_dir=str(DATA_DIR / "vlts_archives"))
        res = worker.compress_archives_to_vlts()
        if res.get("error"):
            return f"vlts build error: {res['error']}"
        return (f"vlts: {res.get('chunks_written', 0)} chunks, "
                f"{res.get('entries_compressed', 0)} entries, "
                f"{res.get('symbols', 0)} symbols")
    except Exception as e:
        return f"vlts build failed: {type(e).__name__}: {e}"


def _build_author_digest(metrics: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Run the Author reconstruction and return a BOUNDED, prompt-safe digest
    for the warm-context. FULLY DEFENSIVE: returns None on ANY failure so the
    handoff/rotation is never blocked. Empty or sparse archives are NORMAL
    (every fresh install) and yield a minimal digest (total_entries=0, empty
    preview), never an error or stall."""
    if not _AUTHOR_AVAILABLE:
        return None
    try:
        task = {
            "trigger":       "context_fatigue",
            "fatigue_score": (metrics or {}).get("token_ratio"),
            "requested_by":  "sage_daemon",
        }
        result = _author_mod.prepare_warm_instance(task)
        if not isinstance(result, dict):
            return None
        digest: Dict[str, Any] = {
            "status":          result.get("status"),
            "total_entries":   (result.get("summary") or {}).get("total_entries", 0),
            "tail_preview":    [],
            "history_preview": [],   # #69: bounded sample of DEEPER history (VLTS/archives)
            "history_source":  None,
        }
        out_path = result.get("output_path")
        if out_path:
            try:
                doc = json.loads(Path(out_path).read_text(encoding="utf-8"))
                ctx = doc.get("context") or {}
                # last few conversation turns (verbatim, truncated)
                for m in (ctx.get("chat_tail") or [])[-6:]:
                    if isinstance(m, dict):
                        digest["tail_preview"].append({
                            "role":    str(m.get("role", "?"))[:24],
                            "content": str(m.get("content", ""))[:280],
                        })
                # deeper history: prefer decompressed VLTS (clean text), else
                # fall back to recent archive entries. Bounded; never raises.
                # v2.13.18: preview truncation that keeps the particulars.
                #
                # These previews are what a recovering instance reads to know
                # where the conversation was. Cut to a fixed length they keep
                # the narrative and drop the specifics -- the addresses, times
                # and figures that cannot be reconstructed from the gist. The
                # extractor re-appends whatever fell outside the window.
                def _keep(txt, budget):
                    try:
                        from craiid import particulars as _pt
                        return _pt.preserve(str(txt), budget)
                    except Exception:
                        return " ".join(str(txt).split())[:budget]

                hist = []
                vlts = ctx.get("vlts") or []
                if vlts:
                    digest["history_source"] = "vlts"
                    for e in vlts[:8]:
                        t = e.get("text") if isinstance(e, dict) else str(e)
                        if t and str(t).strip():
                            hist.append(_keep(str(t).strip(), 500))
                else:
                    for e in (ctx.get("archives") or [])[:8]:
                        t = ""
                        if isinstance(e, dict):
                            for k in ("content", "text", "message",
                                      "user_input", "assistant_response", "summary"):
                                v = e.get(k)
                                if isinstance(v, str) and v.strip():
                                    t = v
                                    break
                            if not t:
                                t = json.dumps(e)[:220]
                        else:
                            t = str(e)
                        if t and str(t).strip():
                            hist.append(_keep(str(t).strip(), 500))
                    if hist:
                        digest["history_source"] = "archives"
                digest["history_preview"] = hist
                # Journalist (#69): a clean, theme-focused summary of the
                # conversation - noise / off-topic / redundant turns stripped,
                # the running THEME extracted. This is the richest part of the
                # warm-context (replaces raw excerpts). Defensive; the embed
                # tier sharpens it when up, lexical fallback when not.
                try:
                    if _JOURNALIST_AVAILABLE:
                        convo = (ctx.get("chat_history") or []) + (ctx.get("chat_tail") or [])
                        if convo:
                            js = _journalist_mod.summarize_stream(convo, max_turns=14)
                            digest["theme"] = (js.get("theme") or [])[:10]
                            # 2000 -> 6000. The summary now carries verbatim
                            # particulars per turn, so the cap must leave room
                            # for them or it clips exactly what was rescued.
                            try:
                                _tcap = int(os.environ.get("CRAIID_THEME_CHARS", "6000"))
                            except Exception:
                                _tcap = 6000
                            digest["theme_summary"] = (js.get("text") or "")[:_tcap]
                            digest["summary_method"] = js.get("method")

                            # --- CRAIID evidence ledger (v2.13.18) ----------
                            # Keyed "evidence", NOT "sources".
                            #
                            # coordinator_signal already emits a `sources` key
                            # and the overseer reads it as a source-HEALTH map
                            # ({name: {status, entries_included}}) for its logs.
                            # Different concept, different consumer, different
                            # file. Reusing the name would have worked today --
                            # they travel in separate payloads -- and become a
                            # confusing collision the first time someone merged
                            # the two paths. Deliberately OUTSIDE theme_summary's cap:
                            # these are verbatim extracts and clipping them
                            # would defeat the point of preserving them.
                            #
                            # Ranked by how often the assistant referenced each
                            # URL in its own prose -- the sources it has been
                            # citing are the ones it is about to cite again.
                            try:
                                from craiid import evidence_ledger as _el
                                _convo_text = " ".join(
                                    str(m.get("content", "")) for m in convo
                                    if isinstance(m, dict))
                                _src = _el.for_handoff(
                                    conversation_text=_convo_text)
                                if _src.get("sources"):
                                    digest["evidence"] = _src
                                    digest["evidence_rule"] = _el.CITATION_RULE
                                    logger.info(
                                        f"[CRAIID] evidence ledger: "
                                        f"{_src['preserved']} preserved, "
                                        f"{_src['gaps']} gap(s), "
                                        f"{_src['total']} total source(s)")
                            except Exception as _ee:
                                # No ledger -> sources absent -> the handoff
                                # behaves exactly as it did before this existed.
                                logger.debug(f"[CRAIID] no evidence ledger: {_ee}")
                except Exception:
                    pass
            except Exception:
                pass  # a digest without previews is still valid
        return digest
    except Exception as _e:
        logger.warning(f"[CRAIID] author reconstruction failed (non-fatal): {_e}")
        return None


def _job_fatigue_check() -> str:
    """CRAIID Phase 2: detect context fatigue from the LIVE instance and emit a
    coordinator + signed handoff task when the current Toga has genuinely
    filled up.

    Repointed 2026-06-09: fatigue is measured from what THIS instance has
    accumulated SINCE BOOT (conversation entries via MemoryLogger + a token
    estimate) plus the live llama kv-cache ratio - NOT the static archives/
    folder. Measuring the static archives made every freshly-spawned instance
    read identical, ever-present 'fatigue', which drove an endless rotation
    loop (a fresh instance was born fatigued). The live signal resets naturally
    on every rotation, so a fresh instance reads ~0 and only a genuinely
    long-running one triggers.

    Runaway guards: a just-booted instance cannot declare fatigue for
    CRAIID_FATIGUE_BOOT_GRACE_SEC, and after emitting it will not emit again
    for CRAIID_FATIGUE_COOLDOWN_SEC. FULLY DEFENSIVE: never raises.
    """
    try:
        now = time.time()
        with _tick_lock:
            llama_progress = float(_tick_state.get("llama_progress", 0.0) or 0.0)
            ops_active = bool(_tick_state.get("ops_mode", False))

        # Conversation THIS instance has accumulated since boot.
        since_boot_turns = 0
        since_boot_tokens = 0
        if _memory_logger is not None:
            try:
                now_count = int(_memory_logger.count_entries())
                since_boot_turns = max(0, now_count - _BOOT_ENTRY_COUNT)
            except Exception:
                since_boot_turns = 0
            if since_boot_turns > 0:
                try:
                    recent = _memory_logger.get_recent(min(since_boot_turns, 256)) or []
                    for _e in recent:
                        _c = _e.get("content") if isinstance(_e, dict) else None
                        if _c:
                            since_boot_tokens += max(1, len(str(_c)) // 4)
                except Exception:
                    since_boot_tokens = since_boot_turns * _CRAIID_AVG_TOKENS_PER_TURN

        budget = _CRAIID_CONTEXT_BUDGET_TOKENS
        token_fill = min(since_boot_tokens / budget, 1.0) if budget > 0 else 0.0
        # 0..1 context fill: the higher of the live KV-cache (precise when the
        # llama tier serves chat) and the since-boot token estimate (carries the
        # signal when chat runs through Ollama and the KV tier is idle ~0).
        context_fill = max(llama_progress, token_fill)

        # Anticipatory pre-warm: ops_mode is a behavioral leading indicator (the
        # user's profile shows operations activity, which tends to precede a
        # token surge), so when it is active we LOWER the fatigue threshold by a
        # margin to begin the warm handoff BEFORE the cliff instead of at it. A
        # soft nudge, never a gate: the since-boot floor + budget + boot-grace +
        # cooldown still apply, so it cannot fire on a fresh/idle instance and a
        # stuck ops_mode still cannot loop it. Env: CRAIID_OPS_PREWARM_MARGIN.
        eff_thresh = _FATIGUE_TOKEN_THRESH
        prewarm = bool(ops_active) and _CRAIID_OPS_PREWARM_MARGIN > 0.0
        if prewarm:
            eff_thresh = max(0.25, _FATIGUE_TOKEN_THRESH - _CRAIID_OPS_PREWARM_MARGIN)

        reasons: list = []
        if context_fill >= eff_thresh:
            reasons.append(
                f"context_fill {context_fill:.3f} >= {eff_thresh:.3f}"
                f"{' (ops pre-warm)' if prewarm else ''} "
                f"(kv={llama_progress:.3f}, since_boot={since_boot_turns} turns / "
                f"~{since_boot_tokens} tok)"
            )
        if llama_progress >= _LLAMA_CLIFF_THRESHOLD:
            reasons.append(
                f"llama_progress {llama_progress:.3f} >= cliff "
                f"{_LLAMA_CLIFF_THRESHOLD}"
            )

        # A fresh/sparse instance must NEVER be 'fatigued': require a floor of
        # real since-boot conversation unless the KV cache itself is at the cliff.
        has_floor = (since_boot_turns >= _CRAIID_MIN_TURNS
                     or llama_progress >= _LLAMA_CLIFF_THRESHOLD)
        fatigue_detected = bool(reasons) and has_floor

        metrics = {
            "context_fill":      round(context_fill, 3),
            "kv_cache_ratio":    round(llama_progress, 3),
            "since_boot_turns":  since_boot_turns,
            "since_boot_tokens": since_boot_tokens,
            "token_ratio":       round(context_fill, 3),
        }

        # Runaway guards: boot grace + post-emit cooldown.
        boot_age = now - _DAEMON_BOOT_TS
        with _tick_lock:
            last_emit = _tick_state.get("craiid_task_emitted_ts") or 0.0
        cooldown_ok = (boot_age >= _CRAIID_FATIGUE_BOOT_GRACE_SEC
                       and (now - last_emit) >= _CRAIID_FATIGUE_COOLDOWN_SEC)
        should_emit = fatigue_detected and cooldown_ok
        if fatigue_detected and not cooldown_ok:
            reasons.append(
                f"(suppressed: boot_age={boot_age:.0f}s, "
                f"since_last_emit={now - last_emit:.0f}s - within grace/cooldown)"
            )

        with _tick_lock:
            _tick_state["fatigue_detected"] = fatigue_detected
            _tick_state["fatigue_metrics"]  = metrics
            _tick_state["fatigue_reasons"]  = reasons
            _tick_state["fatigue_last_ts"]  = now

        if should_emit and _COORD_AVAILABLE:
            fatigue_info = {
                "fatigue_detected": True,
                "metrics":          metrics,
                "timestamp":        TimeManager.iso_z(),
                "reasons":          reasons,
            }
            task = _build_coordinator_task(fatigue_info)
            try:
                _atomic_write_json(_CRAIID_TASK_FILE, task)
                with _tick_lock:
                    _tick_state["craiid_task_emitted"]    = True
                    _tick_state["craiid_task_emitted_ts"] = time.time()
                logger.warning(
                    f"[CRAIID] prepare_warm_instance task emitted "
                    f"(ops_mode={ops_active}, context_fill={context_fill:.3f})"
                )
                _log_mlm_training_row("prepare_warm_instance")
            except Exception as write_err:
                logger.error(f"[CRAIID] task write failed: {write_err}")

        if should_emit and _HANDOFF_GUARD_AVAILABLE and _handoff_guard is not None:
            try:
                with _tick_lock:
                    tick_snapshot = {
                        "ops_mode":       _tick_state.get("ops_mode"),
                        "ops_score":      _tick_state.get("ops_score"),
                        "llama_progress": _tick_state.get("llama_progress"),
                    }
                _author_digest = _build_author_digest(metrics)
                _handoff_guard.write_state({
                    "reason":         "context_fatigue",
                    "metrics":        metrics,
                    "reasons":        reasons,
                    "tick":           tick_snapshot,
                    "reconstruction": _author_digest,
                    "emitted_ts":     TimeManager.iso_z(),
                })
                _handoff_guard.write_trigger(
                    "; ".join(reasons) or "context_fatigue", metrics
                )
                alarm, count = _handoff_guard.cadence_alarm()
                if alarm:
                    logger.error(
                        f"[CRAIID] handoff cadence ALARM - {count} handoffs in "
                        f"the cadence window. Possible restart loop or forced "
                        f"rotation; see handoff_audit.log."
                    )
                else:
                    logger.warning(
                        f"[CRAIID] signed handoff trigger + warm state written "
                        f"({count} in cadence window)."
                    )
            except Exception as hg_err:
                logger.error(f"[CRAIID] handoff guard write failed: {hg_err}")

        _log_mlm_training_row("fatigue_check")

        return (
            f"fatigue={fatigue_detected} ops={ops_active} emit={should_emit} "
            f"context_fill={context_fill:.3f} "
            f"since_boot={since_boot_turns}turns/~{since_boot_tokens}tok "
            f"kv={llama_progress:.3f} reasons={len(reasons)}"
        )

    except Exception as e:
        return f"fatigue check failed: {type(e).__name__}: {e}"


# v2.15.2: one-shot latch so the "metric is gone" notice is said once per
# process instead of every 30 seconds for the life of the daemon.
_KV_ABSENT_LOGGED = False


def _job_llama_progress() -> str:
    """CRAIID Phase 3: the 0.0-1.0 context-window fill.

    v2.15.2: this used to scrape llamacpp:kv_cache_usage_ratio from
    LLAMA_SAGE_URL/metrics. llama.cpp REMOVED that metric. Build 8639 exposes
    eleven metrics and not one is a KV gauge, and /slots was trimmed to
    {id, n_ctx, speculative, is_processing}. So the scrape could never succeed,
    llama_progress sat at 0.0 forever, and `llama_progress >=
    _LLAMA_CLIFF_THRESHOLD` in the fatigue check became unreachable code
    against a constant zero. The detector had quietly been running on one
    signal instead of two.

    We never needed the gauge. Both halves of the ratio are ours already:
    n_ctx is what we launch the tier with, and the token counts come from the
    server's own tokenizer on every completed turn. main.py computes the ratio
    and publishes it at /api/context-fill; this polls that.

    Why an HTTP hop for something in the same product: sage_daemon runs in its
    OWN PROCESS. main.py does not import it and cannot hand it a module global.

    A null ratio means NO DATA -- no turn has completed yet -- and is NOT the
    same as an empty cache. The two readings look identical as 0.000 and mean
    opposite things, so a null is never coerced to zero here.

    Thresholds (unchanged):
      >= 0.90 : warning territory — fatigue check urgency increases
      >= 0.95 : cliff territory — unconditional task emit on next
                fatigue check regardless of ops_mode

    Uses synchronous httpx (this runs in a thread, not an async context).

    Returns a human-readable summary string for the logger.
    """
    # Imported OUTSIDE the try below on purpose. The except clauses name
    # _httpx.ConnectError / _httpx.TimeoutException, and an except clause is
    # EVALUATED when an exception fires -- so if the import lived inside the
    # try and failed, _httpx would be unbound and the handler itself would
    # raise NameError, replacing a tidy return with a traceback in the daemon.
    try:
        import httpx as _httpx
    except Exception as _imp_err:
        return (f"llama progress check unavailable: httpx not importable "
                f"({type(_imp_err).__name__}: {_imp_err})")

    try:
        try:
            from config import PORT_APP as _APP_PORT
        except Exception:
            _APP_PORT = 8000
        _FILL_URL = f"http://127.0.0.1:{_APP_PORT}"

        resp = _httpx.get(f"{_FILL_URL}/api/context-fill", timeout=3.0)

        if resp.status_code != 200:
            return f"context-fill endpoint returned {resp.status_code}"

        doc = resp.json() or {}
        kv_ratio = doc.get("ratio")
        cached_toks = doc.get("prompt_tokens")

        if kv_ratio is None:
            # No turn has completed since boot, so there is genuinely nothing
            # to measure yet. That is a real "not yet" -- unlike the old
            # message, which said "not yet" about a metric llama.cpp had
            # DELETED and would never send. Said once, then quiet.
            global _KV_ABSENT_LOGGED
            if not _KV_ABSENT_LOGGED:
                _KV_ABSENT_LOGGED = True
                return ("no context-fill reading yet -- waiting for the first "
                        "completed turn. (Logged once.)")
            return ""
        _KV_ABSENT_LOGGED = False        # a reading arrived; re-arm the notice

        # Determine alert level (thresholds unchanged from the metric era --
        # this is the same 0.0-1.0 quantity, derived from a source that exists).
        if kv_ratio >= _LLAMA_CLIFF_THRESHOLD:
            level = "CLIFF"
            logger.error(
                f"[CRAIID] context window at CLIFF level: "
                f"{kv_ratio:.3f} >= {_LLAMA_CLIFF_THRESHOLD} — "
                f"context reconstruction urgently needed"
            )
        elif kv_ratio >= _LLAMA_WARN_THRESHOLD:
            level = "WARN"
            logger.warning(
                f"[CRAIID] context window warning: "
                f"{kv_ratio:.3f} >= {_LLAMA_WARN_THRESHOLD}"
            )
        else:
            level = "ok"

        with _tick_lock:
            _tick_state["llama_progress"]      = round(kv_ratio, 4)
            _tick_state["llama_cached_tokens"] = cached_toks or 0
            _tick_state["llama_last_ts"]       = time.time()

        _log_mlm_training_row("llama_progress")

        return (
            f"context_fill={kv_ratio:.4f} level={level} "
            f"prompt_tokens={cached_toks or 'n/a'} "
            f"n_ctx={doc.get('n_ctx') or 'n/a'} "
            f"tier={doc.get('tier') or 'n/a'}"
        )

    except _httpx.ConnectError:
        return "backend not reachable on /api/context-fill (starting?)"
    except _httpx.TimeoutException:
        return "/api/context-fill timed out (backend under load)"
    except Exception as e:
        return f"llama progress check failed: {type(e).__name__}: {e}"


# ----- Action handlers for the new periodic-worker outputs ------

def handle_consolidate_now(req: Dict[str, Any]) -> Dict[str, Any]:
    """Run KB consolidation immediately (out-of-cadence)."""
    msg = _job_consolidate_procedural()
    with _tick_lock:
        _tick_state["last_consolidate_ts"] = time.time()
        _tick_state["last_consolidate_msg"] = msg
    return {"status": "success", "message": msg}


def handle_read_digest(req: Dict[str, Any]) -> Dict[str, Any]:
    """Return the latest rolling chain digest (or empty if none yet)."""
    if not DIGEST_FILE.exists():
        return {
            "status": "success",
            "digest": None,
            "message": "no digest yet — run job or wait for first tick",
        }
    # v2.15.2: encrypted at rest. Reads either form, so a digest written
    # before this change still opens.
    digest = _read_digest()
    if digest is None:
        return {"status": "error",
                "error": "digest present but unreadable (at-rest key "
                         "mismatch?). It is a derived artifact -- deleting it "
                         "lets the next tick regenerate one."}
    return {"status": "success", "digest": digest}


def handle_anomaly_status(req: Dict[str, Any]) -> Dict[str, Any]:
    """Return tamper-monitor state."""
    with _tick_lock:
        snap = dict(_tick_state)
    return {
        "status":           "success",
        "anomaly_alert":    snap.get("anomaly_alert", False),
        "anomaly_first_ts": snap.get("anomaly_first_ts"),
        "last_verify_ok":   snap.get("last_verify_ok"),
        "last_verify_msg":  snap.get("last_verify_msg", ""),
        "last_verify_ts":   snap.get("last_verify_ts"),
    }


def handle_run_digest_now(req: Dict[str, Any]) -> Dict[str, Any]:
    """Run the chain digest immediately (out-of-cadence)."""
    msg = _job_chain_digest()
    with _tick_lock:
        _tick_state["last_digest_ts"] = time.time()
        _tick_state["last_digest_msg"] = msg
    return {"status": "success", "message": msg}


# --- Action dispatch table ------------
def handle_consume_warm_handoff(request: Dict[str, Any]) -> Dict[str, Any]:
    """Hand the verified + FRAMED warm-context (if any) to the inference side
    exactly ONCE, then clear it so it is injected only once. The daemon read,
    HMAC-verified, and framed it at startup (#69); main.py injects warm_framed
    as reference context on the next turn after a fatigue rotation. Returns
    present=False (and an empty string) when there is nothing to resume."""
    with _tick_lock:
        framed = _tick_state.get("warm_handoff_framed")
        present = bool(framed)
        _tick_state["warm_handoff_framed"] = None
        _tick_state["warm_handoff"] = None
    if present:
        logger.info("[CRAIID] warm-context handed to inference side (one-shot).")
    return {
        "status": "success",
        "present": present,
        "warm_framed": framed if present else "",
    }


HANDLERS = {
    "ping":                 handle_ping,
    "status":               handle_status,
    "read_recent":          handle_read_recent,
    "read_top_surprise":    handle_read_top_surprise,
    "verify_chain":         handle_verify_chain,
    "count_entries":        handle_count_entries,
    "generate_summary":     handle_generate_summary,
    "shutdown":             handle_shutdown,
    # v2.1.5 additions — out-of-band mechanics
    "consolidate_now":      handle_consolidate_now,
    "read_digest":          handle_read_digest,
    "run_digest_now":       handle_run_digest_now,
    "anomaly_status":       handle_anomaly_status,
    "consume_warm_handoff": handle_consume_warm_handoff,
}


# ----------
#  SERVER
# ----------
_START_TIME = TimeManager.epoch()  # v2.1.6 unified
_shutdown_event = threading.Event()


def _handle_client(conn: socket.socket, addr) -> None:
    """Handle a single client connection. Supports multiple requests per connection."""
    try:
        conn.settimeout(SOCKET_TIMEOUT)
        while not _shutdown_event.is_set():
            request = _recv_request(conn)
            if request is None:
                break  # client closed or sent garbage

            # Handoff hardening (F5 / #69): optional shared-secret gate. OFF by
            # default (config require_socket_auth=false). When ON, a local
            # process that cannot read .socket_token cannot issue commands.
            if _HO_REQUIRE_AUTH and _SOCKET_TOKEN is not None:
                _tok = request.get("auth_token")
                if not (isinstance(_tok, str)
                        and hmac.compare_digest(_tok, _SOCKET_TOKEN)):
                    _send_response(conn, {"status": "error", "error": "unauthorized"})
                    continue

            action = request.get("action", "")
            handler = HANDLERS.get(action)
            if handler is None:
                response = {
                    "status": "error",
                    "error": f"unknown action: {action}",
                }
            else:
                try:
                    response = handler(request)
                except Exception as e:
                    logger.exception(f"Handler {action} crashed")
                    response = {"status": "error", "error": str(e)}

            try:
                _send_response(conn, response)
            except Exception as e:
                logger.warning(f"Failed to send response to {addr}: {e}")
                break

            if action == "shutdown":
                break
    except socket.timeout:
        pass  # idle client — just close the connection
    except Exception as e:
        logger.warning(f"Client {addr} error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_server() -> None:
    """Main server loop. Blocks until shutdown."""
    # Ensure memory_log directory exists
    MEMORY_LOG_DIR.mkdir(parents=True, exist_ok=True)
    PROCEDURAL_DIR.mkdir(parents=True, exist_ok=True)

    # v2.1.5: spin up the periodic worker thread BEFORE we bind, so the
    # first tick fires while clients are warming up rather than blocking
    # them. Daemon thread => exits cleanly with the process.
    worker = _spawn_periodic_worker()

    # Handoff hardening (#69 / F6+F3): if the overseer rotated us in after a
    # fatigue handoff, a SIGNED warm-context file is waiting in sage_data.
    # Verify it (reject forged/tampered/stale), then stash the payload for the
    # engine to resume from. An unverified handoff file is NEVER trusted.
    if _HANDOFF_GUARD_AVAILABLE and _handoff_guard is not None:
        try:
            ok, warm, reason = _handoff_guard.consume_state(max_age_sec=900)
            if ok and warm:
                with _tick_lock:
                    _tick_state["warm_handoff"] = warm
                    # #69 dry-run follow-up: also store a prompt-safe FRAMED
                    # form so wherever this is later surfaced to the model it is
                    # inert reference data, not obeyable instructions (signing
                    # proves origin, not content-safety).
                    _tick_state["warm_handoff_framed"] = frame_restored_context(warm)
                logger.warning(
                    f"[CRAIID] warm-context handoff loaded "
                    f"(reason={warm.get('reason')!r}) - resuming warm."
                )
            elif reason and reason != "absent":
                logger.error(f"[CRAIID] warm-context handoff REJECTED: {reason}")
        except Exception as _hg_err:
            logger.error(f"[CRAIID] warm-context read failed: {_hg_err}")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
        # Handoff hardening (F4 / #69): on Windows, SO_REUSEADDR lets a
        # *different* local process bind this same in-use 127.0.0.1:PORT and
        # hijack the brief handoff window. SO_EXCLUSIVEADDR_USE (Windows-only)
        # forbids that takeover. On POSIX the attr does not exist; SO_REUSEADDR
        # there only affects TIME_WAIT (no hijack risk) and is kept so the
        # overseer fast respawn can rebind immediately.
        if hasattr(socket, "SO_EXCLUSIVEADDR_USE"):
            try:
                server_sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDR_USE, 1
                )
            except OSError:
                server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        else:
            server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server_sock.bind((HOST, PORT))
        except OSError as e:
            logger.error(f"Failed to bind {HOST}:{PORT} — {e}")
            logger.error("Is another daemon instance already running?")
            sys.exit(1)

        server_sock.listen(5)
        server_sock.settimeout(1.0)  # so we can check _shutdown_event
        logger.info(f"Toga daemon listening on {HOST}:{PORT}")
        logger.info(f"Log file: {DAEMON_LOG_FILE}")
        logger.info("Ready to accept requests.")

        while not _shutdown_event.is_set():
            # v2.15.2 worker supervision. This loop already wakes once a second
            # for the shutdown check, so watching the worker costs nothing.
            #
            # Why it is needed: on 2026-08-20 the worker stopped after a single
            # tick and the daemon carried on for 12.5 hours -- socket up,
            # `status` answering, every CRAIID job silently not running. The
            # daemon looked healthy from every angle except the one nobody was
            # checking. A background thread dying should not be survivable in
            # silence.
            try:
                if worker is not None and not worker.is_alive():
                    with _tick_lock:
                        _tick_state["worker_restarts"] += 1
                        _n = _tick_state["worker_restarts"]
                        _why = _tick_state.get("worker_last_death") or "unknown"
                    logger.critical(
                        f"Periodic worker thread is DEAD (cause: {_why}); "
                        f"restarting it (restart #{_n}). Jobs did not run "
                        f"while it was down.")
                    worker = _spawn_periodic_worker()
            except Exception:
                logger.exception("Worker supervision check failed")

            try:
                conn, addr = server_sock.accept()
            except socket.timeout:
                continue
            except Exception as e:
                logger.warning(f"Accept failed: {e}")
                continue

            t = threading.Thread(
                target=_handle_client,
                args=(conn, addr),
                daemon=True,
            )
            t.start()

        logger.info("Toga daemon shut down cleanly.")


# --- Signal handling for clean Ctrl+C / SIGTERM ---------
def _signal_handler(signum, frame):
    logger.info(f"Received signal {signum}, shutting down...")
    _shutdown_event.set()


def _spawn_periodic_worker() -> threading.Thread:
    """Start the periodic worker. Daemon thread -> exits with the process."""
    t = threading.Thread(
        target=_periodic_worker,
        name="sage_daemon_periodic_worker",
        daemon=True,
    )
    t.start()
    return t


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal_handler)
    try:
        run_server()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception:
        logger.exception("Fatal daemon error")
        sys.exit(1)
