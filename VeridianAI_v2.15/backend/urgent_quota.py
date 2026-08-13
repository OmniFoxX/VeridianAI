"""
urgent_quota.py — v2.11.13 per-peer Urgent budget for shared compute.

Design (Todd, 2026-07-03): remote peers on the Sage/Aether network may flag a
request URGENT to jump ahead of other REMOTE work (never ahead of local work,
and never preempting a running job). To keep anyone from Bogarting the urgent
lane, each peer gets a small rolling budget — the budget IS the ceiling:

    allow_urgent(peer)  ->  True  (budget consumed, request runs at urgent
                                   priority)
                            False (budget exhausted; caller demotes the
                                   request to normal priority and logs it —
                                   the request still runs, just not jumped)

Identity: the node envelope's `user` field (peers already authenticate with
the shared home token before this is consulted, so the trust gate is token
auth; this module only meters). State lives in sage_data (outside the
project tree) as a plain JSON map of peer -> [unix timestamps].

Thread-safe, never raises: any internal failure returns False (deny urgent,
serve as normal) — metering must never take the whole request down.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Dict, List

DEFAULT_LIMIT = 3          # urgent requests ...
DEFAULT_WINDOW_SEC = 3600  # ... per rolling hour, per peer

_lock = threading.Lock()


def _state_file() -> Path:
    try:
        from config import DATA_DIR
        return Path(DATA_DIR) / "node_urgent_quota.json"
    except Exception:
        return Path(__file__).resolve().parent.parent / "node_urgent_quota.json"


def _load() -> Dict[str, List[float]]:
    try:
        f = _state_file()
        if f.exists():
            raw = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(k): [float(t) for t in v]
                        for k, v in raw.items() if isinstance(v, list)}
    except Exception:
        pass
    return {}


def _save(state: Dict[str, List[float]]) -> None:
    try:
        f = _state_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(f)
    except Exception:
        pass


def allow_urgent(peer: str, limit: int = DEFAULT_LIMIT,
                 window_sec: int = DEFAULT_WINDOW_SEC) -> bool:
    """Consume one urgent slot for `peer` if any remain in the rolling
    window. True = granted. False = budget exhausted (caller demotes)."""
    peer = (peer or "unknown").strip() or "unknown"
    try:
        with _lock:
            now = time.time()
            state = _load()
            recent = [t for t in state.get(peer, []) if now - t < window_sec]
            if len(recent) >= max(1, int(limit)):
                state[peer] = recent
                _save(state)
                return False
            recent.append(now)
            state[peer] = recent
            # Opportunistic prune of other peers' stale entries.
            for k in list(state.keys()):
                if k != peer:
                    kept = [t for t in state[k] if now - t < window_sec]
                    if kept:
                        state[k] = kept
                    else:
                        del state[k]
            _save(state)
            return True
    except Exception:
        return False


def remaining(peer: str, limit: int = DEFAULT_LIMIT,
              window_sec: int = DEFAULT_WINDOW_SEC) -> int:
    """How many urgent slots `peer` has left (for status displays)."""
    try:
        with _lock:
            now = time.time()
            recent = [t for t in _load().get(peer or "unknown", [])
                      if now - t < window_sec]
            return max(0, int(limit) - len(recent))
    except Exception:
        return 0
