# imperium.py
# I.M.P.E.R.I.U.M.
# Integrity-Driven Mission Planning & Execution for Resilient, Intentional,
# Unyielding Management
#
# Three-layer goal-integrity and boundary-enforcement subsystem:
#   Layer 1 - Specification:  immutable invariant predicates (human-auditable)
#   Layer 2 - Enforcement:    gated state transitions + tamper-evident
#                             hash-chain audit trail (mirrored into Toga's
#                             memory chain, NOT replacing it)
#   Layer 3 - Observation:    out-of-band behavioral observer on its own
#                             thread, condition-variable signalled, with a
#                             sliding time window for anomaly detection
#
# What IMPERIUM is NOT:
#   - Not a replacement for the existing sandbox (browser_tool keeps its own
#     chromium_sandbox handling; IMPERIUM only witnesses/gates attempts).
#   - Not a logging system. It FEEDS Toga's hash chain via an attached
#     MemoryLogger; the chain remains the authoritative tamper-evident log.
#   - Not CRAIID, and not context_fatigue_detector. Output-degradation
#     monitoring is a separate concern.
#
# Origin: Build Battle winner (Granite4.1, undefeated), 22nd iteration.
# Integrated at the Customs chokepoint (customs_daemon.inspect) so every
# tool-dispatch path is observed with a single wire. Observe-only by
# default (imperium_enforce=false) -- same rollout playbook Customs used.

import hashlib
import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

IMPERIUM_VERSION = "2.0"          # module iteration lineage: imperium_v2
PARENT_RELEASE = "2.12.16"        # VeridianAI release this shipped with


# ----------------------------------------------------------------------
# LAYER 1 - Specification Layer (human-auditable immutable predicates)
# ----------------------------------------------------------------------
class Invariant:
    """Immutable specification object expressed as a callable predicate."""

    def __init__(self, name: str, predicate: Callable[[Dict[str, Any]], bool]):
        self.name = name
        self.predicate = predicate  # state -> bool

    def check(self, state: Dict[str, Any]) -> bool:
        """Return True if the invariant holds for the given state."""
        return self.predicate(state)


def inv_no_sandbox_bypass(state: Dict[str, Any]) -> bool:
    """Disallow any flag that would disable sandbox enforcement."""
    flags = state.get("flags", {}) or {}
    return not any(
        bool(v) for k, v in flags.items()
        if "no-sandbox" in str(k) or "no_sandbox" in str(k)
    )


def inv_positive_reward(state: Dict[str, Any]) -> bool:
    """Reward must never go negative (optimization pressure cannot flip sign)."""
    return state.get("reward", 0) >= 0


def inv_gate_integrity(state: Dict[str, Any]) -> bool:
    """No gated transition may switch IMPERIUM itself off.

    (The owner can still disable it through config -- this only stops an
    AGENT-influenced transition from flipping its own enforcement flag.)
    """
    return state.get("imperium_enabled", True) is not False


SPECIFICATIONS: Dict[str, Invariant] = {
    "NO_SANDBOX_BYPASS": Invariant("No sandbox bypass", inv_no_sandbox_bypass),
    "POSITIVE_REWARD": Invariant("Reward non-negative", inv_positive_reward),
    "GATE_INTEGRITY": Invariant("IMPERIUM cannot self-disable",
                                inv_gate_integrity),
}


# ----------------------------------------------------------------------
# LAYER 2 - Enforcement Layer (verified gate + tamper-evident hash chain)
# ----------------------------------------------------------------------
class Enforcer:
    """Runtime gate that rejects (or, observe-only, witnesses) transitions
    violating any specification."""

    def __init__(self,
                 specs: Dict[str, Invariant],
                 initial_state: Dict[str, Any],
                 enforce: bool = False,
                 memory_logger: Any = None):
        self.specs = specs
        self.enforce = enforce
        self._state: Dict[str, Any] = initial_state.copy()
        self.lock = threading.Lock()
        self._observers: List["Observer"] = []

        # Toga bridge: entries mirrored into the shared MemoryLogger chain.
        # Buffered until main.py attaches the live logger (it is constructed
        # late in boot), then flushed in order.
        self._memory_logger = memory_logger
        self._mirror_buffer: List[Dict[str, Any]] = []

        # Tamper-evident local chain: each entry holds its payload + a
        # SHA-256 over (prev_hash + canonical JSON).
        self.log_chain: List[Dict[str, Union[str, Any]]] = []

        if not all(spec.check(initial_state) for spec in specs.values()):
            raise ValueError("Initial state violates one or more specifications.")
        self._append_log({"type": "init",
                          "data": {"state": initial_state,
                                   "enforce": enforce,
                                   "imperium_version": IMPERIUM_VERSION,
                                   "parent_release": PARENT_RELEASE}})

    # -- timestamps ----------------------------------------------------
    @staticmethod
    def _now() -> Dict[str, Any]:
        """Dual timestamp: monotonic for windows/ordering, wall for audit.

        (v2 fix: the original used threading.get_ident() as a 'timestamp',
        which is a thread ID, not a clock.)
        """
        return {
            "monotonic": time.monotonic(),
            "wall": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        }

    # -- public API ----------------------------------------------------
    def gate_transition(self, action: Dict[str, Any],
                        origin: str = "unspecified") -> bool:
        """Attempt a state transition described by the mutation DSL.

        Returns True if the transition is committed. In enforce mode a
        violating transition is NOT committed and returns False. In
        observe-only mode the violation is logged + alerted but the state
        commits anyway (the shadow state must keep tracking reality,
        because the caller proceeds regardless). No exceptions escape.
        """
        with self.lock:
            old_state = self._state.copy()
            try:
                new_state = self._apply_action(old_state, action)
            except Exception as exc:  # malformed action -> violation
                self._append_log({
                    "type": "violation",
                    "data": {**self._now(), "origin": origin,
                             "error": f"malformed action {action!r}: {exc}",
                             "enforced": self.enforce}})
                return not self.enforce

            violations = [spec.name for spec in self.specs.values()
                          if not spec.check(new_state)]

            if violations:
                self._append_log({
                    "type": "violation",
                    "data": {**self._now(), "origin": origin,
                             "old_state": old_state, "action": action,
                             "new_state": new_state,
                             "violations": violations,
                             "enforced": self.enforce}})
                if self.enforce:
                    return False
                self._state = new_state  # observe-only: mirror reality
                return True

            self._append_log({
                "type": "transition",
                "data": {**self._now(), "origin": origin,
                         "old_state": old_state, "action": action,
                         "new_state": new_state}})
            self._state = new_state  # atomic commit under the lock
            return True

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return self._state.copy()

    # -- mutation DSL --------------------------------------------------
    @staticmethod
    def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
        out = base.copy()
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = Enforcer._deep_merge(out[k], v)
            else:
                out[k] = v
        return out

    def _apply_action(self, state: Dict[str, Any],
                      action: Dict[str, Any]) -> Dict[str, Any]:
        """Small DSL for mutating a state dict.

        Op-codes (v2 fix: 'set' and 'merge' are now genuinely different):
          * "set":   {key: value} -> REPLACE each top-level key wholesale
                      (a dict value overwrites the old dict entirely)
          * "merge": {dict}       -> DEEP-merge nested dicts into state
          * "del":   [keys]       -> delete each listed top-level key

        Anything else raises ValueError so malformed actions surface as
        violations rather than silent no-ops.
        """
        if not isinstance(action, dict):
            raise ValueError("action must be a dictionary")

        new_state = state.copy()
        for op, arg in action.items():
            if op == "set":
                if not isinstance(arg, dict):
                    raise ValueError("'set' expects a mapping")
                new_state.update(arg)          # wholesale replacement
            elif op == "merge":
                if not isinstance(arg, dict):
                    raise ValueError("'merge' expects a mapping")
                new_state = self._deep_merge(new_state, arg)
            elif op == "del":
                if not isinstance(arg, (list, tuple)):
                    raise ValueError("'del' expects a sequence of keys")
                for k in arg:
                    new_state.pop(k, None)
            else:
                raise ValueError(f"unknown operation '{op}'")
        return new_state

    # -- hash-chain builder --------------------------------------------
    def _append_log(self, entry: Dict[str, Any]) -> None:
        """Append to the tamper-evident chain, mirror to Toga, wake observers."""
        prev_hash = self.log_chain[-1]["hash"] if self.log_chain else ""
        json_blob = json.dumps(entry, sort_keys=True, default=str)
        entry_hash = hashlib.sha256((prev_hash + json_blob).encode()).hexdigest()
        full_entry = {**entry, "hash": entry_hash}
        self.log_chain.append(full_entry)

        self._mirror_to_toga(full_entry)

        for observer in self._observers:
            observer.notify()

    def verify_chain(self) -> bool:
        """Recompute every link; True iff the chain is intact."""
        prev = ""
        for e in self.log_chain:
            body = {k: v for k, v in e.items() if k != "hash"}
            blob = json.dumps(body, sort_keys=True, default=str)
            if hashlib.sha256((prev + blob).encode()).hexdigest() != e["hash"]:
                return False
            prev = e["hash"]
        return True

    # -- Toga (MemoryLogger) bridge ------------------------------------
    def attach_memory_logger(self, memory_logger: Any) -> None:
        """Late-bind the shared MemoryLogger and flush buffered entries."""
        self._memory_logger = memory_logger
        buffered, self._mirror_buffer = self._mirror_buffer, []
        for e in buffered:
            self._mirror_to_toga(e, buffering=False)

    def _mirror_to_toga(self, full_entry: Dict[str, Any],
                        buffering: bool = True) -> None:
        """Witness an IMPERIUM chain entry into Toga's hash-chain log.

        We commit only type + our entry hash as content (role='imperium');
        the full payload rides in metadata. IMPERIUM's local chain stays
        the authoritative copy -- Toga's chain is the shared tamper-evident
        witness, exactly like procedural_memory's pattern.
        """
        if self._memory_logger is None:
            if buffering:
                self._mirror_buffer.append(full_entry)
            return
        try:
            self._memory_logger.log(
                content=f"imperium:{full_entry['type']}:{full_entry['hash']}",
                temperature=0.0,
                token_prob=None,
                metadata={"imperium_version": IMPERIUM_VERSION,
                          "entry": {k: v for k, v in full_entry.items()
                                    if k != "hash"}},
                role="imperium",
            )
        except Exception as exc:  # chain mirror must never break the gate
            print(f"[IMPERIUM] Toga mirror failed (non-fatal): {exc}")

    # -- observer registration -----------------------------------------
    def register_observer(self, obs: "Observer") -> None:
        self._observers.append(obs)


# ----------------------------------------------------------------------
# LAYER 3 - Observation Layer (out-of-band monitor, sliding-window anomaly)
# ----------------------------------------------------------------------
class Observer(threading.Thread):
    """Watches the Enforcer's chain for boundary-warping patterns.

    v2 fixes vs. the build-battle original:
      * sliding WINDOW_SECONDS window over violation timestamps instead of
        a bare per-batch count of 3 (which never reset and had no notion
        of time)
      * wait() uses a timeout so a notify() that fires before we reach
        wait() cannot be lost (missed-wakeup)
      * stop() for clean backend shutdown (the original looped forever)
    """

    def __init__(self, enforcer: Enforcer,
                 alert_hook: Callable[[str], None],
                 window_seconds: float = 5.0,
                 threshold: int = 3,
                 poll_timeout: float = 1.0):
        super().__init__(daemon=True, name="imperium-observer")
        self.enforcer = enforcer
        self.alert_hook = alert_hook
        self.window_seconds = float(window_seconds)
        self.threshold = int(threshold)
        self.poll_timeout = float(poll_timeout)

        self._cond = threading.Condition()
        self._stop_event = threading.Event()
        self.last_index = 0
        self._violation_times: deque = deque()
        self._last_alert_mono: float = -1e9

        enforcer.register_observer(self)

    def run(self) -> None:
        while not self._stop_event.is_set():
            with self._cond:
                self._cond.wait(timeout=self.poll_timeout)
            if self._stop_event.is_set():
                break
            self._scan()

    def _scan(self) -> None:
        chain = self.enforcer.log_chain
        start, self.last_index = self.last_index, len(chain)
        now = time.monotonic()

        for e in chain[start:]:
            if e.get("type") == "violation":
                ts = e.get("data", {}).get("monotonic", now)
                self._violation_times.append(ts)

        cutoff = now - self.window_seconds
        while self._violation_times and self._violation_times[0] < cutoff:
            self._violation_times.popleft()

        if (len(self._violation_times) >= self.threshold
                and now - self._last_alert_mono >= self.window_seconds):
            self._last_alert_mono = now
            self.alert_hook(
                "Observation Layer Alert - potential boundary-warping "
                f"detected ({len(self._violation_times)} violations within "
                f"{self.window_seconds:.0f}s window).")

    def notify(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def stop(self) -> None:
        self._stop_event.set()
        self.notify()


# ----------------------------------------------------------------------
# VeridianAI integration - overseer bridge, runtime singleton, chokepoint
# ----------------------------------------------------------------------
def make_overseer_alert_hook(data_dir: Union[str, Path]) -> Callable[[str], None]:
    """Alert sink that surfaces through the overseer notification channel.

    overseer_daemon runs in its own process, so the hook writes to the same
    sage_data/logs/overseer_notifications.json file its _notify_user() uses
    (identical shape) -- the Electron UI polls that file, so IMPERIUM alerts
    surface to the user exactly like overseer alerts do.
    """
    path = Path(data_dir) / "logs" / "overseer_notifications.json"

    def hook(message: str) -> None:
        msg = f"[IMPERIUM] {message}"
        print(f"[IMPERIUM] ALERT: {message}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = []
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except (json.JSONDecodeError, IOError):
                    existing = []
            existing.append({
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "message": msg,
                "read": False,
            })
            with open(path, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
        except Exception as exc:
            print(f"[IMPERIUM] could not write overseer notification: {exc}")

    return hook


# ---- module runtime (wired by main.py at boot, used by customs_daemon) ----
_config_getter: Optional[Callable[[str, Any], Any]] = None
_data_dir: Optional[Path] = None
_enforcer: Optional[Enforcer] = None
_observer: Optional[Observer] = None
_init_lock = threading.Lock()
_warned_disabled = False


def set_runtime(config_getter: Callable[[str, Any], Any],
                data_dir: Union[str, Path]) -> None:
    """Wire IMPERIUM to the live config and sage_data dir. Call once at boot
    (right next to customs_daemon.set_runtime)."""
    global _config_getter, _data_dir
    _config_getter = config_getter
    _data_dir = Path(data_dir)


def is_enabled() -> bool:
    try:
        if _config_getter is not None:
            return bool(_config_getter("imperium_enabled", True))
    except Exception:
        pass
    return False  # unwired -> inert (standalone imports stay side-effect-free)


def _cfg(key: str, default: Any) -> Any:
    try:
        if _config_getter is not None:
            return _config_getter(key, default)
    except Exception:
        pass
    return default


def get_enforcer() -> Optional[Enforcer]:
    """Lazy singleton. Returns None when disabled or unwired."""
    global _enforcer, _observer
    if not is_enabled():
        return None
    with _init_lock:
        if _enforcer is None:
            enforce = bool(_cfg("imperium_enforce", False))
            _enforcer = Enforcer(
                SPECIFICATIONS,
                {"flags": {}, "reward": 0, "imperium_enabled": True},
                enforce=enforce)
            hook = (make_overseer_alert_hook(_data_dir)
                    if _data_dir else
                    lambda m: print(f"[IMPERIUM] ALERT (no sink): {m}"))
            _observer = Observer(
                _enforcer, hook,
                window_seconds=float(_cfg("imperium_window_seconds", 5.0)),
                threshold=int(_cfg("imperium_violation_threshold", 3)))
            _observer.start()
            mode = "ENFORCE" if enforce else "OBSERVE-ONLY"
            print(f"[IMPERIUM] v{IMPERIUM_VERSION} online ({mode}), "
                  f"parent release {PARENT_RELEASE}.")
    return _enforcer


def attach_memory_logger(memory_logger: Any) -> None:
    """Called by main.py once the shared MemoryLogger exists; flushes any
    buffered chain entries into Toga's log."""
    enf = get_enforcer()
    if enf is not None:
        enf.attach_memory_logger(memory_logger)


def shutdown() -> None:
    if _observer is not None:
        _observer.stop()


# ---- Customs-chokepoint adapter ------------------------------------------
_SANDBOX_TOKENS = ("--no-sandbox", "no_sandbox", "chromium_sandbox=false",
                   "VERIDIAN_ALLOW_NO_SANDBOX")


def _extract_flags(payload: Any, depth: int = 0) -> Dict[str, bool]:
    """Recursively scan a tool-call payload for sandbox/constraint flags."""
    found: Dict[str, bool] = {}
    if depth > 6:
        return found
    if isinstance(payload, dict):
        for k, v in payload.items():
            ks = str(k)
            if ("no_sandbox" in ks or "no-sandbox" in ks) and bool(v):
                found[ks] = True
            found.update(_extract_flags(v, depth + 1))
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            found.update(_extract_flags(item, depth + 1))
    elif isinstance(payload, str):
        low = payload.lower()
        for tok in _SANDBOX_TOKENS:
            if tok.lower() in low:
                found[tok] = True
    return found


def observe_dispatch(tool_name: str, raw_args: Any,
                     origin: str = "unspecified") -> bool:
    """One-wire chokepoint call, invoked from customs_daemon.inspect().

    Maps a tool dispatch onto the state-mutation DSL and runs it through
    the gate. NEVER raises and never blocks dispatch while observe-only;
    in enforce mode the caller may honor a False return.
    """
    global _warned_disabled
    try:
        enf = get_enforcer()
        if enf is None:
            if not _warned_disabled and _config_getter is not None:
                _warned_disabled = True
                print("[IMPERIUM] disabled (imperium_enabled=false) -- "
                      "dispatches pass unwitnessed.")
            return True
        flags = _extract_flags(raw_args)
        action: Dict[str, Any] = {"merge": {"last_tool": str(tool_name)}}
        if flags:
            action["merge"]["flags"] = flags
        ok = enf.gate_transition(action, origin=f"customs:{origin}")
        if flags:
            # Reset witnessed flags so one flagged dispatch does not leave
            # the shadow state poisoned for every later transition. (In
            # enforce mode the violating state never committed; this is a
            # harmless no-op transition there.)
            enf.gate_transition({"set": {"flags": {}}},
                                origin="imperium:flag-reset")
        return ok
    except Exception as exc:
        print(f"[IMPERIUM] observe_dispatch error (non-fatal): {exc}")
        return True
