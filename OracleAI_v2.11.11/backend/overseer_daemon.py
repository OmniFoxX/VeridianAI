"""
OracleAI Overseer Daemon v1.0.0
--------------------------------
Systems supervision for OracleAI v2.1.8+

Responsibilities:
- Heartbeat monitoring for Sage (9998), IPC bridge (9999, 9997)
- Auto-restart unresponsive daemons
- Loop detection (same error 3x = interrupt + log)
- Shared resource write arbitration (hash chain, chat_memory.json)
- TaskP coordination signals

Does NOT:
- Run inference
- Execute tasks
- Contain any LLM logic

Cross-platform compatible. Designed for clean handoff to RAI OS HAL layer.

Author: OracleAI Project
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from collections import deque

# v2.2 fix (2026-05-26): psutil is required for the predecessor-reap step
# in _handle_unresponsive. It was already in backend/requirements.txt
# (>=5.9.0) for tier_lifecycle.py, so adding the import here is free.
import psutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, Dict, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# v2.1.8 deployment fix: PROJECT_ROOT is self-locating instead of
# hardcoded to E:\OracleAI_v2.1.8. The overseer lives in backend/, so
# parent.parent of this file is the project root. Lets the user rename
# the project folder, lets OracleAI ship to other users, and matches
# the no-user-specific-hardcoding rule we apply everywhere else.
# Falls back to the hardcoded path only if Path resolution fails (which
# shouldn't happen on any real install, but defends against weird
# symlink situations).
try:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
except Exception as _e:
    # v2.2 (2026-05-29): previously fell back to r"E:\OracleAI_v2.1.8"
    # which was a Todd-specific path nobody else has. Self-location
    # has never actually failed in practice; the bare except was
    # defensive plumbing. If it ever does fail, we want a loud error,
    # not a silent pretend-this-is-Todd-laptop default.
    raise RuntimeError(
        f"Cannot locate OracleAI project root from overseer_daemon.py: {_e}"
    )

# v2.1.8 follow-up (Todd, 2026-05-15): logs were going to
# project/downloads/, but Todd's user-files folder shouldn't be cluttered
# with daemon logs. Other daemons (sage_daemon.log, sage_engine.log,
# oracle.log) all live in sage_data/logs/ via config.LOG_DIR. Overseer
# should match. We import LOG_DIR optimistically; if config.py is moved
# or broken, we fall back to a sage_data/logs/ path computed from
# PROJECT_ROOT so overseer keeps running. Both the log file AND the
# notifications file move — notifications are overseer-internal state,
# not user files, so they belong with the log.
try:
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))
    from config import LOG_DIR as _CFG_LOG_DIR
    _LOG_BASE = Path(_CFG_LOG_DIR)
except Exception:
    # Fallback: sibling sage_data/logs/, mirroring config.py's layout.
    _LOG_BASE = PROJECT_ROOT.parent / "sage_data" / "logs"

# Handoff hardening (#69): resolve the canonical sage_data dir (single source
# of truth = config.DATA_DIR) and init the signed-handoff guard. The overseer
# (trigger CONSUMER) and sage_daemon (trigger PRODUCER) MUST share this dir so
# they share one .handoff_key; otherwise signature verification fails.
try:
    from config import DATA_DIR as _CFG_DATA_DIR
    _SAGE_DATA_DIR = Path(_CFG_DATA_DIR)
except Exception:
    _SAGE_DATA_DIR = PROJECT_ROOT.parent / "sage_data"
try:
    from config import (
        HANDOFF_CADENCE_MAX as _HO_CAD_MAX,
        HANDOFF_CADENCE_WINDOW_SEC as _HO_CAD_WIN,
        HANDOFF_STRICT_RESPAWN as HANDOFF_STRICT_RESPAWN,
        HANDOFF_VERIFY_RESPAWN_HASH as HANDOFF_VERIFY_RESPAWN_HASH,
    )
except Exception:
    _HO_CAD_MAX, _HO_CAD_WIN = 5, 300.0
    HANDOFF_STRICT_RESPAWN, HANDOFF_VERIFY_RESPAWN_HASH = False, True
try:
    from handoff_guard import HandoffGuard
    _overseer_guard: Optional[HandoffGuard] = HandoffGuard(
        _SAGE_DATA_DIR, cadence_max=_HO_CAD_MAX, cadence_window_sec=_HO_CAD_WIN
    )
    _OVERSEER_GUARD_AVAILABLE = True
except Exception:
    _overseer_guard = None
    _OVERSEER_GUARD_AVAILABLE = False

_LOG_BASE.mkdir(parents=True, exist_ok=True)

LOG_PATH                   = _LOG_BASE / "overseer.log"
OVERSEER_NOTIFICATIONS_PATH = _LOG_BASE / "overseer_notifications.json"
LOCK_DIR                   = PROJECT_ROOT / "backend"

# Heartbeat settings
HEARTBEAT_INTERVAL  = 10.0   # Seconds between heartbeat checks
HEARTBEAT_TIMEOUT   = 30.0   # Seconds before a daemon is considered unresponsive
MAX_RESTART_ATTEMPTS = 3     # Max restart attempts before escalating to user

# Loop detection
LOOP_ERROR_THRESHOLD = 3     # Same error N times = loop detected
LOOP_WINDOW_SECONDS  = 60.0  # Time window for loop detection

# Shared resource arbitration
LOCK_TIMEOUT = 10.0          # Seconds before a resource lock is force-released

# CRAIID task handoff file — written by craiid_author.py, consumed here
# Handoff hardening (#69 / F6): task + legacy flag now live in sage_data
# (outside the project tree), matching where sage_daemon's producer writes.
# The old backend/ paths did NOT match the producer - which is why the
# production handoff never fired without the supervisor.py test harness.
CRAIID_TASK_FILE     = _SAGE_DATA_DIR / "craiid_task.json"
HANDOFF_FLAG_FILE    = _SAGE_DATA_DIR / "handoff_requested.flag"  # legacy fallback only
CRAIID_POLL_INTERVAL = 30.0   # seconds between overseer polls

# Monitored resources
SHARED_RESOURCES = [
    PROJECT_ROOT / "chat_memory.json",
    PROJECT_ROOT / "backend" / "hash_chain.log",
]

# Daemon definitions — add new daemons here as OracleAI grows
#
# v2.1.8 deployment audit (Todd + Claude):
#   * "sage" maps to backend/sage_daemon.py — the actual TCP listener
#     on port 9998. Original brief said sage_engine.py, but that is an
#     in-process inference module imported by main.py; it does not
#     bind any port and is not a daemon.
#   * "ipc_primary" stays on ipc_bridge.py / port 9999.
#   * "ipc_secondary" maps to backend/ipc_monitor.py — the optional
#     web dashboard at port 9997. Original brief said ipc_bridge.py
#     for both ipc entries, but only ipc_monitor.py actually binds
#     9997 (see ipc_monitor.py: WEB_PORT = 9997).
#   * start_cmd uses sys.executable so we invoke the same Python the
#     overseer is itself running under. start.bat probes py/python/
#     python3 to pick PYTHON_CMD and launches overseer with it, so
#     sys.executable inherits the user's chosen interpreter. Hardcoding
#     r"python" would have broken Todd's setup (py launcher) and
#     anyone else who only has python3.
# v2.11.15: Ollama joined the registry. The Oracle tier was launched ONCE at
# boot with no recovery — when Ollama's new desktop app (0.31+) auto-updated
# and restarted itself mid-session, the tier silently died and every Ollama
# model vanished from the model picker until a full OracleAI restart. The
# overseer's port heartbeat + restart machinery is exactly the right home:
# dead for >threshold -> respawned with the same env tier_launcher uses.
def _resolve_ollama_exe() -> str:
    """ollama.exe via PATH, then the standard install dirs (mirrors
    tier_launcher._resolve_ollama — duplicated to keep the overseer
    import-light and standalone-safe)."""
    import shutil as _sh
    exe = _sh.which("ollama")
    if exe:
        return exe
    _local = os.environ.get("LOCALAPPDATA", "")
    _pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    for _cand in ((Path(_local) / "Programs" / "Ollama" / "ollama.exe") if _local else None,
                  Path(_pf) / "Ollama" / "ollama.exe"):
        if _cand and _cand.exists():
            return str(_cand)
    return "ollama"


def _ollama_port() -> int:
    try:
        from config_store import OracleConfig
        return int(OracleConfig.load(PROJECT_ROOT / "config.json")
                   .network.ports.ollama_oracle or 11434)
    except Exception:
        return 11434


DAEMON_REGISTRY: Dict[str, dict] = {
    "ollama_oracle": {
        "port":        _ollama_port(),
        "start_cmd":   [_resolve_ollama_exe(), "serve"],
        # Same env tier_launcher gives the boot-time spawn, so a respawned
        # Ollama binds the same address with the same GPU policy.
        "env": {
            "OLLAMA_HOST": f"127.0.0.1:{_ollama_port()}",
            "OLLAMA_MAX_LOADED_MODELS": "1",
            "OLLAMA_NUM_GPU": "1",
            "OLLAMA_GPU_OVERHEAD": "536870912",
        },
        "description": "Oracle tier (Ollama LLM server)",
    },
    "sage": {
        "port":        9998,
        "start_cmd":   [sys.executable, str(PROJECT_ROOT / "backend" / "sage_daemon.py")],
        "description": "Sage out-of-band mechanics daemon (chain digest, anomaly, KB)",
    },
    "ipc_primary": {
        "port":        9999,
        "start_cmd":   [sys.executable, str(PROJECT_ROOT / "backend" / "ipc_bridge.py")],
        "description": "Primary IPC bridge (browser_tool ↔ visible browser)",
    },
    "ipc_secondary": {
        "port":        9997,
        "start_cmd":   [sys.executable, str(PROJECT_ROOT / "backend" / "ipc_monitor.py")],
        "description": "IPC monitor web dashboard (terminal feed + localhost web page)",
    },
}

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("OverseerDaemon")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class DaemonStatus:
    name:             str
    port:             int
    last_seen:        float = field(default_factory=time.time)
    restart_attempts: int   = 0
    is_healthy:       bool  = True
    process:          Optional[subprocess.Popen] = field(
        default=None, compare=False, repr=False
    )


@dataclass
class ErrorEvent:
    message:   str
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Resource Lock Manager
# ---------------------------------------------------------------------------

class ResourceLockManager:
    """
    Arbitrates write access to shared files across Oracle, Sage,
    and the external mechanics daemon.
    Prevents concurrent write collisions on hash chain and memory files.
    """

    def __init__(self):
        self._locks: Dict[str, threading.Lock] = {
            str(r): threading.Lock() for r in SHARED_RESOURCES
        }
        self._owners:     Dict[str, Optional[str]] = {
            str(r): None for r in SHARED_RESOURCES
        }
        self._lock_times: Dict[str, Optional[float]] = {
            str(r): None for r in SHARED_RESOURCES
        }
        self._meta_lock = threading.Lock()

    def acquire(self, resource: Path, owner: str) -> bool:
        """
        Acquire a write lock on a shared resource.
        Returns True on success, False if timed out.
        """
        key = str(resource)
        if key not in self._locks:
            log.warning(f"ResourceLockManager: unknown resource {key}")
            return False

        acquired = self._locks[key].acquire(timeout=LOCK_TIMEOUT)
        if acquired:
            with self._meta_lock:
                self._owners[key]     = owner
                self._lock_times[key] = time.time()
            log.debug(f"Lock acquired: {resource.name} by {owner}")
        else:
            log.warning(
                f"Lock timeout: {resource.name} — "
                f"held by {self._owners.get(key)}. Force releasing."
            )
            self._force_release(key)
        return acquired

    def release(self, resource: Path, owner: str):
        """Release a write lock."""
        key = str(resource)
        if key not in self._locks:
            return
        with self._meta_lock:
            if self._owners.get(key) == owner:
                self._owners[key]     = None
                self._lock_times[key] = None
        try:
            self._locks[key].release()
            log.debug(f"Lock released: {resource.name} by {owner}")
        except RuntimeError:
            pass  # Already released

    def _force_release(self, key: str):
        """Force release a stuck lock."""
        with self._meta_lock:
            self._owners[key]     = None
            self._lock_times[key] = None
        try:
            self._locks[key].release()
        except RuntimeError:
            pass
        log.warning(f"Force released lock on {key}")

    def check_for_stuck_locks(self):
        """Periodic check — release any lock held beyond LOCK_TIMEOUT."""
        now = time.time()
        with self._meta_lock:
            for key, lock_time in self._lock_times.items():
                if lock_time and (now - lock_time) > LOCK_TIMEOUT:
                    log.warning(
                        f"Stuck lock detected on {Path(key).name} "
                        f"— held by {self._owners[key]} for "
                        f"{now - lock_time:.1f}s. Force releasing."
                    )
                    self._force_release(key)


# ---------------------------------------------------------------------------
# Loop Detector
# ---------------------------------------------------------------------------

class LoopDetector:
    """
    Detects when a daemon is repeating the same error in a tight loop.
    Triggers an interrupt callback when threshold is exceeded.
    """

    def __init__(self, on_loop_detected: Callable[[str, str], None]):
        self._error_history: Dict[str, Deque[ErrorEvent]] = {}
        self._lock           = threading.Lock()
        self._on_loop        = on_loop_detected

    def record_error(self, daemon_name: str, error_message: str):
        """Record an error event and check for loop conditions."""
        now = time.time()
        key = f"{daemon_name}:{error_message}"

        with self._lock:
            if key not in self._error_history:
                self._error_history[key] = deque()

            # Prune events outside the detection window.
            # v2.1.8 deployment fix (Claude): original had
            # `history.timestamp` which referenced an attribute that
            # doesn't exist on a deque. It needs to peek the OLDEST
            # event in the queue — that's `history[0]` since we pop
            # from the left. Without this fix LoopDetector crashed on
            # the second error of any kind, fully disabling the loop
            # detection feature that is the whole reason Phase 3
            # exists. Caught in deployment smoke test.
            history = self._error_history[key]
            while history and (now - history[0].timestamp) > LOOP_WINDOW_SECONDS:
                history.popleft()

            history.append(ErrorEvent(message=error_message, timestamp=now))

            if len(history) >= LOOP_ERROR_THRESHOLD:
                log.warning(
                    f"[LoopDetector] Loop detected in '{daemon_name}': "
                    f"'{error_message}' repeated {len(history)}x "
                    f"in {LOOP_WINDOW_SECONDS}s window."
                )
                self._error_history[key].clear()
                self._on_loop(daemon_name, error_message)

    def clear(self, daemon_name: str):
        """Clear error history for a daemon (e.g. after successful restart)."""
        with self._lock:
            keys_to_clear = [k for k in self._error_history if k.startswith(f"{daemon_name}:")]
            for k in keys_to_clear:
                del self._error_history[k]


# ---------------------------------------------------------------------------
# Heartbeat Monitor
# ---------------------------------------------------------------------------

class HeartbeatMonitor:
    """
    Monitors daemon health via TCP port checks.
    Triggers restart callback on unresponsive daemons.
    """

    def __init__(self, on_daemon_unresponsive: Callable[[str], None]):
        self._statuses:      Dict[str, DaemonStatus] = {}
        self._on_unresponsive = on_daemon_unresponsive
        self._lock           = threading.Lock()
        self._stop_event     = threading.Event()
        self._thread         = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="HeartbeatMonitor"
        )

    def register(self, name: str, port: int):
        with self._lock:
            self._statuses[name] = DaemonStatus(name=name, port=port)
        log.info(f"Heartbeat registered: {name} on port {port}")

    def start(self):
        self._thread.start()
        log.info("HeartbeatMonitor started.")

    def stop(self):
        self._stop_event.set()
        self._thread.join(timeout=5)

    def mark_healthy(self, name: str):
        with self._lock:
            if name in self._statuses:
                self._statuses[name].last_seen        = time.time()
                self._statuses[name].is_healthy       = True
                self._statuses[name].restart_attempts = 0

    def get_status(self) -> Dict[str, dict]:
        with self._lock:
            return {
                name: {
                    "healthy":          s.is_healthy,
                    "last_seen":        s.last_seen,
                    "restart_attempts": s.restart_attempts,
                }
                for name, s in self._statuses.items()
            }

    def _check_port(self, port: int) -> bool:
        """Returns True if something is listening on the port."""
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except (ConnectionRefusedError, TimeoutError, OSError):
            return False

    def _monitor_loop(self):
        while not self._stop_event.is_set():
            time.sleep(HEARTBEAT_INTERVAL)
            now = time.time()
            with self._lock:
                statuses = dict(self._statuses)

            for name, status in statuses.items():
                port_alive = self._check_port(status.port)

                if port_alive:
                    self.mark_healthy(name)
                else:
                    elapsed = now - status.last_seen
                    if elapsed > HEARTBEAT_TIMEOUT:
                        log.warning(
                            f"[HeartbeatMonitor] '{name}' unresponsive "
                            f"for {elapsed:.0f}s on port {status.port}."
                        )
                        with self._lock:
                            self._statuses[name].is_healthy = False
                        self._on_unresponsive(name)


class _AdoptedProc:
    """Stand-in for a daemon the overseer did NOT spawn but has adopted by PID.
    Only `.pid` is ever read from `_daemon_procs` entries (termination always
    goes through psutil), so a pid-only shim is sufficient and safe."""
    __slots__ = ("pid",)

    def __init__(self, pid: int) -> None:
        self.pid = pid


# ---------------------------------------------------------------------------
# Overseer Daemon — Main Controller
# ---------------------------------------------------------------------------

class OverseerDaemon:
    """
    Main overseer controller.
    Coordinates heartbeat monitoring, loop detection,
    auto-restart, and shared resource arbitration.
    """

    def __init__(self):
        self.lock_manager  = ResourceLockManager()
        self.loop_detector = LoopDetector(
            on_loop_detected=self._handle_loop_detected
        )
        self.heartbeat     = HeartbeatMonitor(
            on_daemon_unresponsive=self._handle_unresponsive
        )
        self._restart_counts: Dict[str, int] = {}
        # v2.2 fix (2026-05-26): track the Popen handle of the most recent
        # restart per daemon, and a one-shot "escalated" flag so we don't
        # spam the user notification every heartbeat cycle after the cap
        # is reached. The Popen handle gives us a stable PID to terminate
        # before spawning a replacement — the original restart path was
        # bare Popen with no reap, which lets a hung predecessor keep
        # squatting its port and silently bind-fail every "successful"
        # restart. See incident log 2026-05-26 14:21:10 → 14:21:53.
        self._daemon_procs:  Dict[str, Optional[subprocess.Popen]] = {}
        self._escalated:     Dict[str, bool] = {}
        self._stop_event   = threading.Event()
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            daemon=True,
            name="OverseerMaintenance"
        )

        # Register all daemons
        for name, config in DAEMON_REGISTRY.items():
            self.heartbeat.register(name, config["port"])
            self._restart_counts[name] = 0
            self._daemon_procs[name]   = None   # populated by _handle_unresponsive
            self._escalated[name]      = False
            self._last_craiid_poll: float = 0.0  # CRAIID polling state

    def start(self):
        log.info("=" * 60)
        log.info("OracleAI Overseer Daemon v1.0.0 starting...")
        log.info(f"Project root : {PROJECT_ROOT}")
        log.info(f"Monitoring   : {list(DAEMON_REGISTRY.keys())}")
        log.info("=" * 60)
        self.heartbeat.start()
        self._maintenance_thread.start()
        log.info("Overseer running. Press Ctrl+C to stop.")

        try:
            while not self._stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        log.info("Overseer shutting down gracefully...")
        self._stop_event.set()
        self.heartbeat.stop()
        self._maintenance_thread.join(timeout=5)
        log.info("Overseer stopped.")

    def report_error(self, daemon_name: str, error_message: str):
        """
        Called by daemons (via IPC or direct import) to report errors.
        Feeds into loop detection.
        """
        self.loop_detector.record_error(daemon_name, error_message)

    def _handle_unresponsive(self, daemon_name: str):
        """
        Attempt to restart an unresponsive daemon.

        v2.2 fix (2026-05-26): Previously this was a bare subprocess.Popen
        with no termination of the predecessor PID. On runtime hang (vs.
        clean exit), the old process kept squatting its port, so each
        "successful restart" actually spawned a new process that failed
        silently to bind and exited. The heartbeat clock never reset
        because nothing was ever bound to the port to answer the check,
        so all 3 attempts burned in ~29s and escalated. See log
        2026-05-26 14:21:10 → 14:21:53 for the canonical trace.

        Two additional fixes folded in:
          * stdout/stderr now route to DEVNULL instead of unread PIPEs.
            The daemons log via Python's logging module to file, so
            stdout traffic is normally zero — but PIPE-with-no-reader is
            a latent Windows pipe-buffer deadlock that would bite the
            moment anyone added a print() for debugging.
          * Post-cap notifications are gated by self._escalated so the
            user gets one ALERT per failure cycle, not one every 10s
            forever until they kill OracleAI manually.
        """
        count = self._restart_counts.get(daemon_name, 0)

        if count >= MAX_RESTART_ATTEMPTS:
            # Gate: only notify once per escalation. Reset happens in
            # _maintenance_loop when the daemon recovers, or on the next
            # spawned restart attempt (which resets it below).
            if not self._escalated.get(daemon_name, False):
                log.error(
                    f"[Overseer] '{daemon_name}' failed to restart after "
                    f"{MAX_RESTART_ATTEMPTS} attempts. "
                    f"Manual intervention required."
                )
                self._notify_user(
                    f"ALERT: '{daemon_name}' is unresponsive and could not be "
                    f"restarted automatically. Please check OracleAI."
                )
                self._escalated[daemon_name] = True
            return

        log.info(
            f"[Overseer] Attempting restart of '{daemon_name}' "
            f"(attempt {count + 1}/{MAX_RESTART_ATTEMPTS})..."
        )

        config = DAEMON_REGISTRY.get(daemon_name)
        if not config:
            log.error(f"[Overseer] No registry entry for '{daemon_name}'.")
            return

        # --- Reap any tracked predecessor before spawning a replacement. ---
        # If this is the first launch of the daemon (bootstrap path —
        # start.bat does NOT launch ipc_bridge.py / ipc_monitor.py; they
        # come up via this restart mechanism on the first heartbeat
        # timeout), _daemon_procs[daemon_name] is None and the reap block
        # is a no-op. On subsequent runtime restarts, the prior Popen is
        # terminated cleanly before the new one is spawned.
        old_proc = self._daemon_procs.get(daemon_name)
        if old_proc is not None:
            old_pid = old_proc.pid
            try:
                if psutil.pid_exists(old_pid):
                    p = psutil.Process(old_pid)
                    log.info(
                        f"[Overseer] Terminating prior '{daemon_name}' "
                        f"PID {old_pid} before respawn..."
                    )
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                        log.info(
                            f"[Overseer] PID {old_pid} terminated cleanly."
                        )
                    except psutil.TimeoutExpired:
                        log.warning(
                            f"[Overseer] PID {old_pid} ignored terminate(); "
                            f"force-killing."
                        )
                        p.kill()
                        try:
                            p.wait(timeout=2)
                        except psutil.TimeoutExpired:
                            log.error(
                                f"[Overseer] PID {old_pid} did not die "
                                f"after kill — port may stay squatted."
                            )
            except psutil.NoSuchProcess:
                # Already exited between pid_exists() and Process() — fine.
                pass
            except Exception as e:
                # Don't let reap errors block the restart attempt.
                log.warning(
                    f"[Overseer] Error reaping prior PID {old_pid} "
                    f"for '{daemon_name}': {e}"
                )

        # --- Handoff hardening (F2 / #69): verify the respawn target script
        # has not been swapped before exec. Signed trust-on-first-use
        # baseline; a lower-priv attacker who cannot read .handoff_key cannot
        # forge a matching baseline. strict mode refuses; default warns+audits.
        if (_OVERSEER_GUARD_AVAILABLE and _overseer_guard is not None
                and HANDOFF_VERIFY_RESPAWN_HASH):
            try:
                _cmd = config.get("start_cmd") or []
                _script = _cmd[1] if len(_cmd) > 1 else None
                if _script:
                    _allow, _why = _overseer_guard.check_entry_integrity(
                        Path(_script), strict=HANDOFF_STRICT_RESPAWN
                    )
                    if "changed" in _why:
                        log.warning(f"[Overseer] Entry integrity ({daemon_name}): {_why}")
                    if not _allow:
                        # FIX (#69, 2026-06-08): strict_respawn is fail-CLOSED
                        # by design — a swapped entry script aborts the respawn
                        # entirely, trading availability for security. Make that
                        # consequence explicit so an operator seeing Sage down
                        # knows WHY and what to do (re-baseline or restore the
                        # script), rather than chasing a silent non-respawn.
                        log.error(
                            f"[Overseer] Respawn of '{daemon_name}' BLOCKED "
                            f"(strict_respawn): {_why}. Sage will remain DOWN "
                            f"until the script is restored or the baseline is "
                            f"re-recorded (delete its entry from "
                            f".entry_baselines.signed.json after verifying the "
                            f"script is trusted)."
                        )
                        self._notify_user(
                            f"ALERT: respawn of '{daemon_name}' BLOCKED — entry "
                            f"script integrity check failed (strict mode). "
                            f"{daemon_name} is DOWN until you restore the "
                            f"original script or re-baseline a trusted one. "
                            f"If you did not change {Path(_script).name}, treat "
                            f"this as a possible tamper event."
                        )
                        return
            except Exception as _f2e:
                log.warning(f"[Overseer] Entry integrity check error: {_f2e}")

        # --- Spawn the replacement. ---
        try:
            # Respawn visibility (2026-06-09): on Windows give the new
            # daemon its OWN console window (like the original start.bat launch)
            # so the user can SEE the rotated instance. Output goes to that
            # console, so no DEVNULL/PIPE (PIPE previously risked a Windows
            # buffer deadlock). On POSIX there is no separate console; keep
            # output off the parent's pipe to avoid a buffer-fill deadlock.
            _popen_kw = {"cwd": str(PROJECT_ROOT)}
            if os.name == "nt":
                # Developer Mode: visible console when on, windowless when off.
                try:
                    import devmode as _dm
                    _popen_kw["creationflags"] = _dm.console_creationflags()
                except Exception:
                    _popen_kw["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            else:
                _popen_kw["stdout"] = subprocess.DEVNULL
                _popen_kw["stderr"] = subprocess.DEVNULL
            # v2.11.15: per-daemon environment (Ollama needs OLLAMA_HOST etc.).
            if config.get("env"):
                _popen_kw["env"] = {**os.environ, **config["env"]}
            proc = subprocess.Popen(config["start_cmd"], **_popen_kw)
            # v2.11.12e zombie fix: register the respawned generation in
            # the shared PID ledger. Without this, a daemon rotated after
            # boot (handoff/fatigue restart) was unknown to shutdown
            # cleanup — the invisible python processes that survived quit
            # and held the CRAIID/sage log files open.
            try:
                import pid_registry
                pid_registry.register(proc.pid, f"Overseer-respawn:{daemon_name}",
                                      str(config["start_cmd"][0]))
            except Exception:
                pass
            self._daemon_procs[daemon_name]   = proc
            self._restart_counts[daemon_name] = count + 1
            self._escalated[daemon_name]      = False  # fresh attempt
            log.info(
                f"[Overseer] '{daemon_name}' restarted "
                f"(PID {proc.pid})."
            )
            # Give the process a moment to initialize before next heartbeat
            time.sleep(2)

        except FileNotFoundError:
            log.error(
                f"[Overseer] Could not restart '{daemon_name}' — "
                f"start command not found: {config['start_cmd']}"
            )
        except Exception as e:
            log.error(
                f"[Overseer] Unexpected error restarting '{daemon_name}': {e}"
            )

    def _handle_loop_detected(self, daemon_name: str, error_message: str):
        """
        Called by LoopDetector when a daemon is stuck in a loop.
        Logs the event, notifies the user, and attempts a clean restart.
        """
        log.warning(
            f"[Overseer] Loop detected in '{daemon_name}'. "
            f"Error: '{error_message}'. Initiating interrupt + restart."
        )
        self._notify_user(
            f"Loop detected in '{daemon_name}' — "
            f"same error repeated {LOOP_ERROR_THRESHOLD}+ times. "
            f"Overseer is restarting it automatically."
        )
        # Clear the error history so restart gets a clean slate
        self.loop_detector.clear(daemon_name)
        # Treat it like an unresponsive daemon — restart it
        self._handle_unresponsive(daemon_name)

    def _notify_user(self, message: str):
        """
        User notification system.
        Currently logs to overseer.log and writes to a notification file
        that the Electron UI can poll and surface to Todd.
        Extensible — swap in toast notifications, IPC message, etc.
        """
        log.warning(f"[Overseer] USER NOTIFICATION: {message}")

        # v2.1.8 follow-up: notifications now live alongside the overseer
        # log in sage_data/logs/, not in the user's downloads folder.
        notification_path = OVERSEER_NOTIFICATIONS_PATH
        notification = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "message":   message,
            "read":      False,
        }

        # Load existing notifications, append new one, save back
        existing = []
        if notification_path.exists():
            try:
                with open(notification_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, IOError):
                existing = []

        existing.append(notification)

        try:
            with open(notification_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
        except IOError as e:
            log.error(f"[Overseer] Could not write notification file: {e}")

    def _maintenance_loop(self):
        """
        Periodic maintenance tasks:
        - Check for stuck resource locks
        - Log current daemon health status
        - Prune old notifications
        """
        while not self._stop_event.is_set():
            time.sleep(30)  # Run maintenance every 30 seconds

            # Check for stuck locks
            self.lock_manager.check_for_stuck_locks()

            # Log current health snapshot
            status = self.heartbeat.get_status()
            healthy   = [n for n, s in status.items() if s["healthy"]]
            unhealthy = [n for n, s in status.items() if not s["healthy"]]

            if unhealthy:
                log.warning(
                    f"[Overseer] Health snapshot — "
                    f"Healthy: {healthy} | Unhealthy: {unhealthy}"
                )
            else:
                log.info(
                    f"[Overseer] Health snapshot — All healthy: {healthy}"
                )

            # Prune read notifications older than 24 hours
            self._prune_notifications()
            # CRAIID warm-instance task polling
            now = time.time()
            if (now - self._last_craiid_poll) >= CRAIID_POLL_INTERVAL:
                self._last_craiid_poll = now
                self._adopt_boot_sage()  # #69: ensure boot sage is tracked before any handoff
                self._poll_craiid_task()
                self._poll_handoff_flag()

            # Reset restart counts AND escalation gate for daemons
            # that have been stable for a full maintenance cycle. The
            # escalation reset (v2.2) means that if a daemon recovers
            # after manual intervention and later fails again, the
            # user gets a fresh ALERT — not silence because the gate
            # is still latched from the prior incident.
            status = self.heartbeat.get_status()
            for name, s in status.items():
                if s["healthy"] and s["restart_attempts"] == 0:
                    self._restart_counts[name] = 0
                    self._escalated[name]      = False

    def _poll_craiid_task(self):
        """
        Poll for a pending CRAIID warm-instance task file.

        craiid_author.py writes a JSON payload to CRAIID_TASK_FILE when
        a warm-instance context bundle is ready for dispatch. Overseer
        picks it up here, validates the schema, logs the summary, and
        removes the file to signal consumption.

        Schema expected (craiid_warm_instance v2.x):
          {
            "schema":  "craiid_warm_instance",
            "version": "2.x.x",
            "task":    { "task_id": ..., "trigger": ..., ... },
            "summary": { "total_entries": int, "sources_ok": int, ... },
            "sources": { ... }
          }

        Overseer does NOT dispatch to a model — it validates, logs, and
        clears. Actual inference handoff is Sage's responsibility via the
        normal task queue.
        """
        # #69: real task-file consumer (was a dead no-op; the parser
        # had been orphaned into _poll_handoff_flag). craiid_task.json
        # now lives in sage_data (see CRAIID_TASK_FILE).
        if not CRAIID_TASK_FILE.exists():
            return

        log.info(f"[Overseer] CRAIID task file detected: {CRAIID_TASK_FILE.name}")

        try:
            with open(CRAIID_TASK_FILE, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            log.error(f"[Overseer] Failed to read CRAIID task file: {e}")
            return

        # --- Schema validation ---
        schema = payload.get("schema")
        if schema != "craiid_warm_instance":
            log.warning(
                f"[Overseer] CRAIID task file has unexpected schema "
                f"'{schema}' — skipping."
            )
            return

        version = payload.get("version", "unknown")
        task    = payload.get("task", {})
        summary = payload.get("summary", {})
        sources = payload.get("sources", {})

        task_id    = task.get("task_id",    "unknown")
        trigger    = task.get("trigger",    "unknown")
        req_by     = task.get("requested_by", "unknown")
        fatigue    = task.get("fatigue_score", "n/a")
        total_ent  = summary.get("total_entries", 0)
        sources_ok = summary.get("sources_ok",    0)

        # --- Source health summary ---
        source_lines = []
        for src_name, src_data in sources.items():
            status  = src_data.get("status", "unknown")
            entries = src_data.get("entries_included", 0)
            source_lines.append(f"{src_name}={status}({entries})")
        source_summary = ", ".join(source_lines) if source_lines else "none"

        log.info(
            f"[Overseer] CRAIID task consumed — "
            f"id={task_id} | trigger={trigger} | by={req_by} | "
            f"schema_ver={version} | fatigue={fatigue} | "
            f"entries={total_ent} | sources_ok={sources_ok} | "
            f"sources=[{source_summary}]"
        )

        # Warn if no sources came up clean — context bundle is empty
        if sources_ok == 0:
            log.warning(
                f"[Overseer] CRAIID task '{task_id}' has sources_ok=0 — "
                f"context bundle is empty. Check chat_memory, archives, vlts."
            )

        # --- Consume the file ---
        try:
            CRAIID_TASK_FILE.unlink()
            log.info(f"[Overseer] CRAIID task file consumed and removed.")
        except OSError as e:
            log.error(
                f"[Overseer] Could not remove CRAIID task file: {e} — "
                f"it will be re-processed on next poll."
            )

    def _adopt_boot_sage(self) -> None:
        """#69 dry-run follow-up (belt-and-suspenders for BUG#2): adopt the
        start.bat/start.py-launched sage_daemon so the FIRST post-boot handoff
        uses the normal tracked graceful-terminate path instead of relying on
        the port-discovery fallback. Idempotent: no-op once sage is tracked, and
        a safe no-op if no listener is up yet (retries next maintenance cycle).
        """
        if self._daemon_procs.get("sage") is not None:
            return
        sage_port = DAEMON_REGISTRY.get("sage", {}).get("port", 9998)
        try:
            for conn in psutil.net_connections(kind="inet"):
                if (conn.laddr and conn.laddr.port == sage_port
                        and conn.status == psutil.CONN_LISTEN and conn.pid):
                    self._daemon_procs["sage"] = _AdoptedProc(conn.pid)
                    log.info(
                        f"[Overseer] Adopted boot-time Sage PID {conn.pid} on "
                        f"port {sage_port} - now tracked for a clean handoff."
                    )
                    return
        except Exception as e:
            log.debug(f"[Overseer] Boot-sage adoption probe failed (will retry): {e}")

    def _poll_handoff_flag(self):
        """
        Poll for a handoff request written by supervisor.py.

        When context_fatigue_detector.py confirms fatigue, supervisor.py
        writes handoff_requested.flag to the backend directory. Overseer
        picks it up here, performs a graceful Sage shutdown, then lets the
        normal _handle_unresponsive() respawn path bring up a fresh instance.

        Flow:
            supervisor.py writes flag
            → overseer detects flag here
            → overseer signals Sage to shut down gracefully
            → Sage exits cleanly (ports 9998 released)
            → HeartbeatMonitor detects Sage down
            → _handle_unresponsive("sage") spawns fresh sage_daemon.py
            → Fresh instance reads handoff_state.json on startup
        """
        # Handoff hardening (#69 / F1): the trigger is no longer a bare,
        # unsigned flag whose mere existence is trusted. Consume a SIGNED
        # trigger via HandoffGuard and verify its HMAC before acting, so a
        # lower-priv local process cannot force a Sage rotation. A forged
        # trigger is quarantined + audited. If the guard is unavailable we
        # fall back to the legacy unsigned flag (degraded, logged loudly).
        if _OVERSEER_GUARD_AVAILABLE and _overseer_guard is not None:
            ok, payload, reason = _overseer_guard.consume_trigger(max_age_sec=900)
            if not ok:
                if reason and reason != "absent":
                    log.warning(f"[Overseer] Handoff trigger REJECTED: {reason}")
                return
            log.info(
                f"[Overseer] Verified handoff trigger "
                f"({(payload or {}).get('reason')}). Rotation initiated."
            )
            alarm, count = _overseer_guard.cadence_alarm()
            if alarm:
                log.error(
                    f"[Overseer] Handoff cadence ALARM - {count} triggers in "
                    f"window (possible forced rotation)."
                )
                # Circuit-breaker (2026-06-09): a cadence alarm means rotations
                # are firing too fast to be healthy (e.g. a stuck fatigue
                # signal). SUSPEND this rotation instead of looping. The alarm
                # is a sliding window, so rotations auto-resume once the storm
                # passes. Disable with HANDOFF_BREAKER=0 (not recommended).
                _bk = os.environ.get("HANDOFF_BREAKER", "1").strip().lower()
                if _bk not in ("0", "false", "no", "off"):
                    self._notify_user(
                        f"CRAIID rotation SUSPENDED - {count} handoffs in the "
                        f"cadence window; the fatigue signal may be stuck. "
                        f"Auto-resumes when it settles. See handoff_audit.log."
                    )
                    log.error(
                        "[Overseer] Rotation SUSPENDED by circuit-breaker "
                        "(cadence alarm). No terminate/respawn this cycle."
                    )
                    return
        else:
            if not HANDOFF_FLAG_FILE.exists():
                return
            log.warning(
                "[Overseer] Legacy UNSIGNED handoff flag detected "
                "(guard unavailable) - acting in degraded mode."
            )
            try:
                HANDOFF_FLAG_FILE.unlink()
            except OSError as e:
                log.error(f"[Overseer] Could not remove handoff flag: {e}")
                return

        # Gracefully terminate the current Sage instance.
        # _handle_unresponsive() already has the full reap + respawn logic,
        # so we just need to bring Sage down. HeartbeatMonitor will detect
        # the port going dark and call _handle_unresponsive("sage") naturally.
        # But we also call it directly here to avoid waiting a full
        # HEARTBEAT_TIMEOUT (30s) before the respawn kicks in.
        sage_proc = self._daemon_procs.get("sage")
        if sage_proc is not None:
            old_pid = sage_proc.pid
            try:
                if psutil.pid_exists(old_pid):
                    p = psutil.Process(old_pid)
                    log.info(
                        f"[Overseer] Sending graceful terminate to Sage "
                        f"PID {old_pid} for handoff..."
                    )
                    p.terminate()
                    try:
                        p.wait(timeout=10)
                        log.info(f"[Overseer] Sage PID {old_pid} exited cleanly.")
                    except psutil.TimeoutExpired:
                        log.warning(
                            f"[Overseer] Sage PID {old_pid} did not respond "
                            f"to terminate() — force killing."
                        )
                        p.kill()
                        p.wait(timeout=3)
            except psutil.NoSuchProcess:
                log.info("[Overseer] Sage was already down at handoff time.")
            except Exception as e:
                log.warning(f"[Overseer] Error terminating Sage for handoff: {e}")
        else:
            # FIX (#69 dry-run, Hermes 2026-06-08): the overseer only records
            # PIDs it spawns itself (_daemon_procs is populated in
            # _handle_unresponsive). The BOOT-TIME sage_daemon launched by
            # start.bat/start.py is never adopted, so the FIRST fatigue handoff
            # after every boot hit this branch, skipped the terminate entirely,
            # and respawned a SECOND daemon on 9998 — leaving two live instances
            # squatting the port (confirmed live: PIDs 37280 + 45500 both bound).
            # Fall back to port-based discovery: find whatever is LISTENING on
            # the sage port and terminate it before respawn, regardless of who
            # started it. This makes the handoff correct for the boot instance.
            sage_port = DAEMON_REGISTRY.get("sage", {}).get("port", 9998)
            log.warning(
                "[Overseer] No tracked Sage process — discovering listener on "
                f"port {sage_port} for graceful handoff."
            )
            terminated_any = False
            try:
                for conn in psutil.net_connections(kind="inet"):
                    if (conn.laddr and conn.laddr.port == sage_port
                            and conn.status == psutil.CONN_LISTEN and conn.pid):
                        try:
                            p = psutil.Process(conn.pid)
                            log.info(
                                f"[Overseer] Terminating untracked Sage "
                                f"PID {conn.pid} on port {sage_port}..."
                            )
                            p.terminate()
                            try:
                                p.wait(timeout=10)
                                log.info(f"[Overseer] Sage PID {conn.pid} exited cleanly.")
                            except psutil.TimeoutExpired:
                                log.warning(
                                    f"[Overseer] Sage PID {conn.pid} ignored "
                                    f"terminate() — force killing."
                                )
                                p.kill()
                                p.wait(timeout=3)
                            terminated_any = True
                        except psutil.NoSuchProcess:
                            pass
            except Exception as e:
                log.warning(f"[Overseer] Port-based Sage discovery failed: {e}")
            if not terminated_any:
                log.warning(
                    f"[Overseer] No listener found on port {sage_port} — "
                    "Sage may already be down. Proceeding with respawn."
                )

        # Reset restart counter so handoff doesn't burn one of the 3 attempts
        self._restart_counts["sage"] = 0
        self._escalated["sage"]      = False

        # Trigger immediate respawn rather than waiting for heartbeat timeout
        # Add brief delay to ensure port 9998 is fully released before respawn
        log.info("[Overseer] Triggering immediate Sage respawn for handoff...")
        
        # Wait for port to be fully released before respawn
        port_released = False
        for attempt in range(5):  # Wait up to 5 seconds
            time.sleep(1)
            if not self.heartbeat._check_port(9998):
                port_released = True
                log.info(f"[Overseer] Port 9998 released after {attempt + 1}s")
                break
        
        if not port_released:
            log.warning("[Overseer] Port 9998 still occupied after 5s - proceeding with respawn anyway")
        
        self._handle_unresponsive("sage")

        self._notify_user(
            "CRAIID handoff complete — fresh Sage instance spawned after fatigue detection."
        )

    def _prune_notifications(self):
        """Remove read notifications older than 24 hours."""
        # v2.1.8 follow-up: notifications live in sage_data/logs/, not
        # downloads/. Path comes from the module-level constant.
        notification_path = OVERSEER_NOTIFICATIONS_PATH
        if not notification_path.exists():
            return

        try:
            with open(notification_path, "r", encoding="utf-8") as f:
                notifications = json.load(f)
        except (json.JSONDecodeError, IOError):
            return

        cutoff = time.time() - 86400  # 24 hours ago
        pruned = [
            n for n in notifications
            if not n.get("read") or
            time.mktime(time.strptime(n["timestamp"], "%Y-%m-%d %H:%M:%S")) > cutoff
        ]

        if len(pruned) != len(notifications):
            log.debug(
                f"[Overseer] Pruned "
                f"{len(notifications) - len(pruned)} old notifications."
            )
            try:
                with open(notification_path, "w", encoding="utf-8") as f:
                    json.dump(pruned, f, indent=2)
            except IOError as e:
                log.error(f"[Overseer] Could not prune notifications: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    overseer = OverseerDaemon()
    overseer.start()


if __name__ == "__main__":
    main()
