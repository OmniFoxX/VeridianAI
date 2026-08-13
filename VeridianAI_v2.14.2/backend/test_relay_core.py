"""Self-running unit tests for relay_core (Aether relay broker).

Run:  python test_relay_core.py    (pure stdlib; deterministic via injected clock)
Covers: full submit/serve/respond/collect round-trip, empty inbox, per-peer
isolation, response consumed-once, request + response TTL expiry, inbox cap.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_core import RelayHub


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def adv(self, d): self.t += d


def test_roundtrip():
    h = RelayHub()
    rid = h.submit_request("peerA", {"x": 1})
    assert rid
    req = h.next_request("peerA")
    assert req and req["id"] == rid and req["payload"] == {"x": 1}
    assert h.get_response(rid)["ready"] is False
    h.submit_response(rid, {"ok": True})
    got = h.get_response(rid)
    assert got["ready"] and got["response"] == {"ok": True}


def test_next_request_empty():
    assert RelayHub().next_request("nobody") is None


def test_per_peer_isolation():
    h = RelayHub()
    h.submit_request("A", {"a": 1})
    assert h.next_request("B") is None
    assert h.next_request("A")["payload"] == {"a": 1}


def test_response_consumed_once():
    h = RelayHub()
    rid = h.submit_request("A", {}); h.next_request("A"); h.submit_response(rid, "R")
    assert h.get_response(rid)["ready"] is True
    assert h.get_response(rid)["ready"] is False


def test_request_ttl_expiry():
    clk = Clock(); h = RelayHub(request_ttl=10, now_fn=clk)
    h.submit_request("A", {"x": 1})
    clk.adv(11)
    assert h.next_request("A") is None


def test_response_ttl_expiry():
    clk = Clock(); h = RelayHub(response_ttl=10, now_fn=clk)
    rid = h.submit_request("A", {}); h.next_request("A"); h.submit_response(rid, "R")
    clk.adv(11)
    assert h.get_response(rid)["ready"] is False


def test_max_pending_cap():
    h = RelayHub(max_pending_per_peer=2)
    assert h.submit_request("A", 1) and h.submit_request("A", 2)
    assert h.submit_request("A", 3) is None


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    p = f = 0
    for fn in fns:
        try:
            fn(); p += 1; print("PASS", fn.__name__)
        except Exception:
            f += 1; print("FAIL", fn.__name__); traceback.print_exc()
    print("\n%d passed, %d failed" % (p, f)); sys.exit(1 if f else 0)
