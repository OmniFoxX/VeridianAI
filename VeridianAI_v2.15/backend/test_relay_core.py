"""Self-running unit tests for relay_core (Aether relay broker).

Run:  python test_relay_core.py    (pure stdlib; deterministic via injected clock)
Covers: full submit/serve/respond/collect round-trip, empty inbox, per-peer
isolation, response consumed-once, request + response TTL expiry, inbox cap.

NOTE (2026-08-13): RelayHub's methods are `async def` -- they take an
asyncio.Lock. These tests called them synchronously, so every assertion ran
against a coroutine object and the whole file failed with
"'coroutine' object is not subscriptable". Seven tests reporting FAIL for one
calling-convention drift is indistinguishable, at a glance, from a broken
broker; the tests are now awaited so a real failure means what it says.
"""
import asyncio
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_core import RelayHub


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def adv(self, d): self.t += d


async def test_roundtrip():
    h = RelayHub()
    rid = await h.submit_request("peerA", {"x": 1})
    assert rid
    req = await h.next_request("peerA")
    assert req and req["id"] == rid and req["payload"] == {"x": 1}
    assert (await h.get_response(rid))["ready"] is False
    await h.submit_response(rid, {"ok": True})
    got = await h.get_response(rid)
    assert got["ready"] and got["response"] == {"ok": True}


async def test_next_request_empty():
    assert await RelayHub().next_request("nobody") is None


async def test_per_peer_isolation():
    h = RelayHub()
    await h.submit_request("A", {"a": 1})
    assert await h.next_request("B") is None
    assert (await h.next_request("A"))["payload"] == {"a": 1}


async def test_response_consumed_once():
    h = RelayHub()
    rid = await h.submit_request("A", {})
    await h.next_request("A")
    await h.submit_response(rid, "R")
    assert (await h.get_response(rid))["ready"] is True
    assert (await h.get_response(rid))["ready"] is False


async def test_request_ttl_expiry():
    clk = Clock(); h = RelayHub(request_ttl=10, now_fn=clk)
    await h.submit_request("A", {"x": 1})
    clk.adv(11)
    assert await h.next_request("A") is None


async def test_response_ttl_expiry():
    clk = Clock(); h = RelayHub(response_ttl=10, now_fn=clk)
    rid = await h.submit_request("A", {})
    await h.next_request("A")
    await h.submit_response(rid, "R")
    clk.adv(11)
    assert (await h.get_response(rid))["ready"] is False


async def test_max_pending_cap():
    h = RelayHub(max_pending_per_peer=2)
    assert await h.submit_request("A", 1) and await h.submit_request("A", 2)
    assert await h.submit_request("A", 3) is None


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    p = f = 0
    for fn in fns:
        try:
            # A test that is not a coroutine function would silently pass here
            # by never running, so require it.
            assert asyncio.iscoroutinefunction(fn), fn.__name__ + " is not async"
            asyncio.run(fn()); p += 1; print("PASS", fn.__name__)
        except Exception:
            f += 1; print("FAIL", fn.__name__); traceback.print_exc()
    print("\n%d passed, %d failed" % (p, f)); sys.exit(1 if f else 0)
