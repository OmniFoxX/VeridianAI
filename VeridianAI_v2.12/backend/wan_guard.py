"""
OracleAI / Aether -- WAN abuse guard (rate limit + failed-auth backoff).

Table-stakes hardening for ANY internet-exposed surface (node compute, skill
serve): cheap, in-memory, per-key throttling that runs BEFORE expensive work, so
an exposed endpoint can't be flooded or brute-forced. No-regret -- direct-WAN,
hub, and relay paths all need it. Pure stdlib, thread-safe, memory-bounded.

The home token still provides the actual mutual auth; this only limits how fast
an unauthenticated stranger may try. Keyed by client IP (or any string).
"""
import threading
import time
from collections import defaultdict, deque


class AbuseGuard:
    def __init__(self, max_requests=120, window_sec=60, fail_threshold=8,
                 ban_sec=300, max_keys=4096, now_fn=None):
        self.max_requests = int(max_requests)
        self.window_sec = float(window_sec)
        self.fail_threshold = int(fail_threshold)
        self.ban_sec = float(ban_sec)
        self.max_keys = int(max_keys)
        self._now_fn = now_fn or time.monotonic
        self._hits = defaultdict(deque)   # key -> deque[timestamps]
        self._fails = defaultdict(int)    # key -> consecutive auth failures
        self._banned = {}                 # key -> ban_until
        self._lock = threading.Lock()

    def _now(self):
        return self._now_fn()

    def check(self, key):
        """Call BEFORE doing work. Returns {allowed, retry_after, reason}."""
        key = key or "?"
        now = self._now()
        with self._lock:
            until = self._banned.get(key)
            if until is not None:
                if now < until:
                    return {"allowed": False, "retry_after": round(until - now, 1), "reason": "banned"}
                self._banned.pop(key, None)
                self._fails[key] = 0
            dq = self._hits[key]
            cutoff = now - self.window_sec
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.max_requests:
                retry = round(dq[0] + self.window_sec - now, 1)
                return {"allowed": False, "retry_after": max(retry, 0.0), "reason": "rate_limited"}
            dq.append(now)
            self._maybe_prune(now)
            return {"allowed": True, "retry_after": 0.0, "reason": "ok"}

    def record_failure(self, key):
        """Call after an AUTH failure (bad token/sig). Bans the key at threshold."""
        key = key or "?"
        with self._lock:
            self._fails[key] += 1
            if self._fails[key] >= self.fail_threshold:
                self._banned[key] = self._now() + self.ban_sec

    def record_success(self, key):
        """Call after a successful auth -- clears the failure streak."""
        key = key or "?"
        with self._lock:
            self._fails[key] = 0

    def is_banned(self, key):
        with self._lock:
            until = self._banned.get(key or "?")
            return until is not None and self._now() < until

    def _maybe_prune(self, now):
        if len(self._hits) <= self.max_keys:
            return
        cutoff = now - self.window_sec
        for k in [k for k, dq in self._hits.items() if not dq or dq[-1] < cutoff]:
            self._hits.pop(k, None)
            if k not in self._banned:
                self._fails.pop(k, None)
