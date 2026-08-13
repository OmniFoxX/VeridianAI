"""Self-running tests for relay_api + relay_client (Aether relay HTTP layer).

Run:  python test_relay.py    (needs fastapi + httpx, which the app uses)
Covers: feature-disabled 404, the HTTP broker round-trip (request/poll/respond/
response), an in-process source<->relay<->client round-trip over a real ASGI
transport, and the empty-poll case.
"""
import os, sys, asyncio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import relay_api
from relay_client import RelayClient, RelaySource


def _app():
    app = FastAPI(); app.include_router(relay_api.relay_router)
    relay_api.set_config({"relay_server_enabled": True})
    relay_api._hub = relay_api.RelayHub()
    return app


def test_disabled_404():
    app = FastAPI(); app.include_router(relay_api.relay_router)
    relay_api.set_config({"relay_server_enabled": False})
    c = TestClient(app)
    assert c.post("/api/relay/request", json={"target": "A", "payload": {}}).status_code == 404


def test_http_broker_sequential():
    c = TestClient(_app())
    rid = c.post("/api/relay/request", json={"target": "A", "payload": {"hi": 1}}).json()["request_id"]
    req = c.get("/api/relay/poll/A").json()
    assert req["id"] == rid and req["payload"] == {"hi": 1}
    assert c.get("/api/relay/poll/A").json() == {}      # drained
    c.post("/api/relay/respond", json={"request_id": rid, "response": {"ok": True}})
    d = c.get("/api/relay/response/" + rid).json()
    assert d["ready"] and d["response"] == {"ok": True}


def test_inprocess_relay_roundtrip():
    app = _app()
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://relay") as client:
            cli = RelayClient("http://relay")
            rid = await cli.submit(client, "peerA", {"q": "catalog"})
            assert rid
            async def handler(payload):
                return {"echo": payload, "served": True}
            src = RelaySource("http://relay", "peerA", handler)
            assert await src.serve_once(client) is True
            return await cli.await_response(client, rid, timeout=5, poll_interval=0.05)
    res = asyncio.run(run())
    assert res["ok"] and res["response"] == {"echo": {"q": "catalog"}, "served": True}


def test_source_no_request_returns_false():
    app = _app()
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://relay") as client:
            async def handler(p):
                return {}
            return await RelaySource("http://relay", "peerX", handler).serve_once(client)
    assert asyncio.run(run()) is False


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
