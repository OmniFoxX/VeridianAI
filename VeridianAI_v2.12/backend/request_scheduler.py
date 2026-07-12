"""request_scheduler.py -- fair multi-tier request scheduling (v2.13, scaling).

STATUS: WIRED (v2.12.2). AsyncAgingGate (bottom of this file) is the live
per-server generation gate in model_manager.generate() — the drop-in
successor to the strict-precedence _PriorityGate. Config knobs (single
source of truth, config.json -> inference section): scheduler_enabled,
scheduler_aging_rate, scheduler_queue_limit. The sync RequestScheduler
class remains the reference implementation + simulation harness.

WHY
---
VeridianAI already routes by COMPLEXITY (sage_engine.route_query) and can
OFFLOAD opportunistically to peer nodes / the relay. What it lacks is a
FAIR arbiter for when concurrent demand exceeds one box's GPU -- observed
around ~8 concurrent users per install before latency climbs. Three tiers
compete:
    hyperlocal    -- this machine's own signed-in users (highest base)
    local_network -- peer nodes on the LAN (Sage Network)
    remote        -- relay-brokered requests (lowest base)

The naive fix, strict tier precedence, STARVES remote under sustained load
(simulated: remote requests waiting 700+ ticks, never served). This module
implements the standard cure -- AGING -- with a provable wait ceiling, plus
the two things aging alone doesn't solve: admission control and a separate
lane for urgent one-shots.

DESIGN (three independent mechanisms; keep them separate)
---------------------------------------------------------
1. AGING PRIORITY. score = tier_base + wait_seconds * aging_rate. The
   scheduler always serves the highest score, recomputed at pick time. A
   long-waiting remote request eventually out-scores a fresh hyperlocal one,
   so no tier starves. The ceiling is exact and tunable:
       max_wait ≈ (base[hyperlocal] - base[remote]) / aging_rate
   Pick aging_rate to hit whatever max-wait SLA you'll promise.

2. ADMISSION CONTROL. Aging distributes a finite resource fairly but can't
   manufacture capacity: if arrivals exceed service for long enough, EVERY
   policy backs up (physics, not a bug). So before enqueuing we check queue
   depth against a ceiling and shed gracefully -- reject the newcomer with a
   clear "busy, retry" rather than silently growing an unbounded queue. The
   caller can instead downshift to a lighter model (route_query already knows
   how) -- see should_admit()'s return.

3. URGENT LANE. The urgent one-shot is deliberately OUTSIDE the aging math:
   a per-profile token bucket (default 1 token/hour) that, when spent, jumps
   the request to the front regardless of score. Kept separate so urgent
   traffic can't interact with aging's edge cases or be starved by it.

Pure stdlib, no I/O, fully deterministic under an injected clock -> testable.
"""
from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Base weights: higher = served first, all else equal. The GAP between
# hyperlocal and remote, divided by aging_rate, is the max-wait ceiling.
TIER_BASE_WEIGHT: Dict[str, float] = {
    "hyperlocal": 3.0,
    "local_network": 2.0,
    "remote": 1.0,
}
DEFAULT_AGING_RATE = 0.05          # 40s ceiling ((3-1)/rate); sim-validated
DEFAULT_QUEUE_LIMIT = 24          # ~3x the ~8-user knee; tune per box
_URGENT_SCORE = float("inf")       # urgent always wins, by construction

_counter = itertools.count()       # FIFO tiebreaker for equal scores


@dataclass(order=False)
class Request:
    tier: str
    arrival_time: float
    urgent: bool = False
    ident: str = ""
    profile: str = ""
    _seq: int = field(default_factory=lambda: next(_counter))

    def base_weight(self) -> float:
        return TIER_BASE_WEIGHT.get(self.tier, TIER_BASE_WEIGHT["remote"])


def priority_score(req: Request, now: float, aging_rate: float) -> float:
    """Higher wins. Urgent short-circuits to +inf; otherwise tier base plus
    accrued wait * aging_rate. Never negative (clock skew guarded)."""
    if req.urgent:
        return _URGENT_SCORE
    wait = max(0.0, now - req.arrival_time)
    return req.base_weight() + wait * aging_rate


def max_wait_ceiling(aging_rate: float = DEFAULT_AGING_RATE) -> float:
    """The provable upper bound (seconds) a non-urgent request can wait before
    its aged score exceeds a freshly-arrived top-tier request."""
    span = TIER_BASE_WEIGHT["hyperlocal"] - TIER_BASE_WEIGHT["remote"]
    return span / aging_rate if aging_rate > 0 else float("inf")


class TokenBucket:
    """Per-key refilling bucket for the urgent lane. Default 1 token/hour."""

    def __init__(self, capacity: int = 1, refill_sec: float = 3600.0):
        self.capacity = int(capacity)
        self.refill_sec = float(refill_sec)
        self._state: Dict[str, tuple] = {}   # key -> (tokens, last_refill)

    def _refresh(self, key: str, now: float) -> float:
        tokens, last = self._state.get(key, (float(self.capacity), now))
        if now > last and self.refill_sec > 0:
            gained = (now - last) / self.refill_sec
            tokens = min(self.capacity, tokens + gained)
            last = now
        self._state[key] = (tokens, last)
        return tokens

    def spend(self, key: str, now: Optional[float] = None) -> bool:
        t = time.time() if now is None else now
        k = (key or "anon").strip().lower() or "anon"
        tokens = self._refresh(k, t)
        if tokens >= 1.0:
            self._state[k] = (tokens - 1.0, self._state[k][1])
            return True
        return False


class RequestScheduler:
    """Aging-fair scheduler with admission control and an urgent lane."""

    def __init__(self, aging_rate: float = DEFAULT_AGING_RATE,
                 queue_limit: int = DEFAULT_QUEUE_LIMIT,
                 urgent_per_hour: int = 1):
        self.aging_rate = float(aging_rate)
        self.queue_limit = int(queue_limit)
        self._q: List[Request] = []
        self._urgent = TokenBucket(capacity=urgent_per_hour, refill_sec=3600.0)

    # --- admission -----------------------------------------------------------
    def should_admit(self, tier: str) -> dict:
        """Decide before enqueue. Urgent traffic is admitted by spend() later,
        not here. Returns {admit, reason, suggest_downshift}."""
        depth = len(self._q)
        if depth < self.queue_limit:
            return {"admit": True, "reason": "ok", "suggest_downshift": False}
        # Over the limit: shed. Suggest the caller downshift to a lighter model
        # instead of hard-rejecting, when the tier is one we'd rather serve.
        return {"admit": False,
                "reason": f"queue full ({depth}/{self.queue_limit})",
                "suggest_downshift": tier in ("hyperlocal", "local_network")}

    # --- enqueue / dequeue ---------------------------------------------------
    def submit(self, tier: str, now: Optional[float] = None, *,
               urgent: bool = False, ident: str = "", profile: str = "",
               force: bool = False) -> dict:
        """Add a request. Urgent requests spend a token; on success they skip
        admission control (that's the point of urgent). Non-urgent requests
        pass admission control unless force=True."""
        t = time.time() if now is None else now
        is_urgent = False
        if urgent:
            is_urgent = self._urgent.spend(profile or ident, now=t)
            # A denied urgent token degrades to a normal request, not an error.
        if not is_urgent and not force:
            adm = self.should_admit(tier)
            if not adm["admit"]:
                return {"accepted": False, **adm}
        req = Request(tier=tier, arrival_time=t, urgent=is_urgent,
                      ident=ident, profile=profile)
        self._q.append(req)
        return {"accepted": True, "urgent": is_urgent, "queue_depth": len(self._q)}

    def pick_next(self, now: Optional[float] = None) -> Optional[Request]:
        """Remove and return the highest-priority request, or None if idle.
        Ties (equal score) break FIFO via the arrival sequence counter."""
        if not self._q:
            return None
        t = time.time() if now is None else now
        best_i, best_key = 0, None
        for i, r in enumerate(self._q):
            key = (priority_score(r, t, self.aging_rate), -r._seq)
            if best_key is None or key > best_key:
                best_key, best_i = key, i
        return self._q.pop(best_i)

    def depth(self) -> int:
        return len(self._q)

    def snapshot(self, now: Optional[float] = None) -> List[dict]:
        """Debug/telemetry view of the queue, highest score first."""
        t = time.time() if now is None else now
        rows = [{"ident": r.ident, "tier": r.tier, "urgent": r.urgent,
                 "wait": round(max(0.0, t - r.arrival_time), 2),
                 "score": priority_score(r, t, self.aging_rate)}
                for r in self._q]
        rows.sort(key=lambda d: d["score"], reverse=True)
        return rows


# ---------------------------------------------------------------------------
# LIVE WIRING (v2.12.2): async admission gate built on the scoring math above.
# ---------------------------------------------------------------------------

class AsyncAgingGate:
    """Async one-holder gate ordered by aging priority_score.

    Drop-in successor to model_manager._PriorityGate — same
    acquire()/release() surface, three differences that matter:

      1. FAIRNESS: waiters are picked by priority_score (recomputed at each
         release, because aging is a function of NOW — same reason
         RequestScheduler.pick_next scores at pick time), not by strict
         tier precedence. Remote work can no longer starve.
      2. ADMISSION: should_admit() bounds the queue; callers shed BEFORE
         enqueuing instead of growing an unbounded backlog.
      3. URGENT: authorized by the CALLER (ws-chat flags local urgent;
         node endpoints only after urgent_quota grants it), so this gate
         runs NO token bucket of its own — that would double-charge quota.

    A RUNNING generation is never preempted; scoring only reorders who goes
    NEXT. Cancelled waiters (client gone while queued) are skipped at
    release, mirroring _PriorityGate.
    """

    def __init__(self, aging_rate: float = DEFAULT_AGING_RATE,
                 queue_limit: int = DEFAULT_QUEUE_LIMIT):
        self.aging_rate = float(aging_rate)
        self.queue_limit = int(queue_limit)
        self._active = False
        self._waiters: List[tuple] = []      # (Request, asyncio.Future)

    def depth(self) -> int:
        return len(self._waiters)

    def should_admit(self, tier: str, urgent: bool = False) -> dict:
        """Bounded admission. Urgent always admits — its scarcity is already
        enforced upstream (urgent_quota / ws-chat), and shedding an urgent
        request would defeat the lane's purpose."""
        if urgent or len(self._waiters) < self.queue_limit:
            return {"admit": True, "reason": "ok", "suggest_downshift": False}
        return {"admit": False,
                "reason": f"queue full ({len(self._waiters)}/{self.queue_limit})",
                "suggest_downshift": tier in ("hyperlocal", "local_network")}

    async def acquire(self, tier: str = "hyperlocal", urgent: bool = False,
                      ident: str = "") -> None:
        if not self._active and not self._waiters:
            self._active = True
            return
        req = Request(tier=tier, arrival_time=time.time(),
                      urgent=urgent, ident=ident)
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append((req, fut))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                # Admitted between set_result and this await resuming:
                # pass the slot on so the gate can't deadlock.
                self.release()
            else:
                try:
                    self._waiters.remove((req, fut))
                except ValueError:
                    pass
            raise

    def release(self) -> None:
        now = time.time()
        while self._waiters:
            best_i, best_key = 0, None
            for i, (r, _f) in enumerate(self._waiters):
                key = (priority_score(r, now, self.aging_rate), -r._seq)
                if best_key is None or key > best_key:
                    best_key, best_i = key, i
            _req, fut = self._waiters.pop(best_i)
            if not fut.done():
                fut.set_result(None)     # gate stays active, handed over
                return
        self._active = False

    def snapshot(self) -> List[dict]:
        """Telemetry view of waiting requests, highest score first."""
        now = time.time()
        rows = [{"ident": r.ident, "tier": r.tier, "urgent": r.urgent,
                 "wait": round(max(0.0, now - r.arrival_time), 2),
                 "score": priority_score(r, now, self.aging_rate)}
                for r, _f in self._waiters]
        rows.sort(key=lambda d: d["score"], reverse=True)
        return rows


# ---------------------------------------------------------------------------
# ORIGINAL INSERTION-POINT NOTES (kept for history; wiring landed v2.12.2):
#   - Construct ONE RequestScheduler at app startup (module-level in main.py).
#   - In the inference entrypoint (the /api/chat + /api/node/infer paths),
#     BEFORE acquiring the model:
#         adm = SCHED.submit(tier, urgent=..., profile=...)
#         if not adm["accepted"]:
#             if adm["suggest_downshift"]:
#                 # fall back to a lighter model via route_query candidates
#             else:
#                 raise HTTPException(503, "busy, please retry")
#   - Replace the implicit "serve immediately" with a worker that loops
#     pick_next() under the GPU semaphore. Tier is derived from the request
#     envelope: local session -> hyperlocal, node_server -> local_network,
#     relay -> remote (node_server.py already carries the (user, session)
#     seam to tell these apart).
#   - Load-test with REAL arrival rates, then tune aging_rate to the max-wait
#     you'll promise (see max_wait_ceiling()).
# ---------------------------------------------------------------------------
