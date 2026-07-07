"""Self-running unit tests for wan_guard (WAN abuse guard).

Run:  python test_wan_guard.py    (pure stdlib; deterministic via an injected clock)
Covers: sliding-window rate limit + recovery, failed-auth ban + expiry, streak
reset on success, per-key isolation, bounded retry_after, None-key safety.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wan_guard import AbuseGuard


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def adv(self, d): self.t += d


def test_rate_limit_then_window_slides():
    clk = Clock(); g = AbuseGuard(max_requests=3, window_sec=10, now_fn=clk)
    for _ in range(3):
        assert g.check("a")["allowed"]
    r = g.check("a")
    assert not r["allowed"] and r["reason"] == "rate_limited" and r["retry_after"] > 0
    clk.adv(11)
    assert g.check("a")["allowed"]


def test_failed_auth_ban_and_expiry():
    clk = Clock(); g = AbuseGuard(fail_threshold=3, ban_sec=60, now_fn=clk)
    for _ in range(3):
        g.record_failure("a")
    assert g.is_banned("a")
    r = g.check("a")
    assert not r["allowed"] and r["reason"] == "banned" and r["retry_after"] > 0
    clk.adv(61)
    assert g.check("a")["allowed"] and not g.is_banned("a")


def test_success_resets_streak():
    clk = Clock(); g = AbuseGuard(fail_threshold=3, now_fn=clk)
    g.record_failure("a"); g.record_failure("a")
    g.record_success("a")
    g.record_failure("a"); g.record_failure("a")
    assert not g.is_banned("a")


def test_per_key_isolation():
    clk = Clock(); g = AbuseGuard(max_requests=2, window_sec=10, now_fn=clk)
    assert g.check("a")["allowed"] and g.check("a")["allowed"]
    assert not g.check("a")["allowed"]
    assert g.check("b")["allowed"]


def test_retry_after_bounded():
    clk = Clock(); g = AbuseGuard(max_requests=1, window_sec=10, now_fn=clk)
    g.check("a")
    r = g.check("a")
    assert 0 < r["retry_after"] <= 10


def test_none_key_safe():
    g = AbuseGuard(max_requests=1)
    assert g.check(None)["allowed"]
    assert not g.check(None)["allowed"]


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    p = f = 0
    for fn in fns:
        try:
            fn(); p += 1; print("PASS", fn.__name__)
        except Exception:
            f += 1; print("FAIL", fn.__name__); traceback.print_exc()
    print("\n%d passed, %d failed" % (p, f))
    sys.exit(1 if f else 0)
