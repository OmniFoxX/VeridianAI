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


def test_pinned_base_keeps_host_and_encodes_path():
    """v2.15 SSRF fix. Two properties in one round-trip:

    1. The PIN -- RelayClient dials the base it was handed (in production an IP
       literal that net_guard.pinned_base already validated) while the Host header
       still carries the original name, so the peer's virtual hosting keeps working
       and the name is never re-resolved between check and request.
    2. The QUOTE -- request_id arrives from the RELAY, not from us. A hostile relay
       that answers with a slash/query/fragment must not be able to steer the path.
    """
    seen = {}

    async def echo_app(scope, receive, send):
        seen["path"] = scope["path"]
        seen["raw_path"] = scope.get("raw_path") or b""
        seen["query_string"] = scope.get("query_string") or b""
        seen["host"] = dict((k.decode(), v.decode())
                            for k, v in scope["headers"]).get("host")
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body",
                    "body": b'{"ready": true, "response": {"pinned": true}}'})

    async def run():
        transport = httpx.ASGITransport(app=echo_app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://198.51.100.7:80") as client:
            cli = RelayClient("http://198.51.100.7:80",
                              host_header="relay.example.com")
            return await cli.await_response(client, "abc/def?x=1",
                                            timeout=2, poll_interval=0.05)

    res = asyncio.run(run())
    assert res["ok"] and res["response"] == {"pinned": True}
    assert seen["host"] == "relay.example.com", seen["host"]
    assert b"%2F" in seen["raw_path"] and b"%3F" in seen["raw_path"], seen["raw_path"]
    assert seen["query_string"] == b"", seen["query_string"]


def test_unpinned_caller_unchanged():
    """The source-loop and LAN relays pass a bare URL with no pin -- no Host override,
    so behaviour is byte-identical to pre-v2.15."""
    cli = RelayClient("http://relay/")
    assert cli.relay == "http://relay"
    assert cli.host_header is None and cli.sni_hostname is None
    src = RelaySource("http://relay/", "peerA", None)
    assert src.relay == "http://relay" and src.host_header is None


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
