"""Self-running tests for skill_api (Aether skill-share Layers 4-5 HTTP surface).

Run:  python test_skill_api.py     (needs fastapi + httpx, which the app uses)
Covers: feature-disabled 404s, serve catalog + object, publish endpoint, gated
promotion over HTTP, graceful handling of an unreachable peer, an in-process
serve+fetch roundtrip over a real ASGI transport, the identity + trusted-key
endpoints, and end-to-end promotion enforcement via the trusted-key store.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi import FastAPI
from fastapi.testclient import TestClient
import skill_trust as st
import skill_gate as g
import skill_service as svc
import skill_api


def _fresh_app(enabled=True):
    app = FastAPI()
    app.include_router(skill_api.skill_router)
    skill_api.set_config({"skill_share_enabled": enabled})
    skill_api._service_err = None
    skill_api._service = svc.SkillService(
        base_dir=tempfile.mkdtemp(), key_dir=tempfile.mkdtemp(),
        policy=g.default_policy(require_trusted_author=False))
    return app, skill_api._service


def test_disabled_returns_404():
    app = FastAPI(); app.include_router(skill_api.skill_router)
    skill_api.set_config({"skill_share_enabled": False})
    skill_api._service = None
    c = TestClient(app)
    assert c.get("/api/skills/catalog").status_code == 404


def test_serve_catalog_and_object():
    app, s = _fresh_app()
    c = TestClient(app)
    r = s.publish(b'{"hooks":{"append_footer":"hi"}}', name="F",
                  capabilities=["hook.append_footer"], author="A")
    hid = r["id"]
    cat = c.get("/api/skills/catalog").json()
    assert any(x["id"] == hid for x in cat["skills"])
    obj = c.get("/api/skills/object/" + hid).json()
    assert obj["id"] == hid and "body_b64" in obj and "envelope" in obj
    assert c.get("/api/skills/object/" + "0" * 64).status_code == 404


def test_publish_endpoint():
    app, s = _fresh_app()
    c = TestClient(app)
    resp = c.post("/api/skills/publish",
                  json={"name": "P", "body": {"prompt": "hi"},
                        "capabilities": ["prompt.augment"]})
    assert resp.status_code == 200 and resp.json()["ok"]
    assert len(c.get("/api/skills/local").json()["skills"]) == 1


def test_promote_flow_gated():
    app, s = _fresh_app()
    c = TestClient(app)
    body = b'{"net":1}'
    env = st.sign_artifact(body, name="N", capabilities=["network.outbound"],
                           key_dir=tempfile.mkdtemp())
    s.ingest(body, env)
    hid = st.content_hash(body)
    pr = c.post("/api/skills/promote", json={"id": hid}).json()
    assert pr["ok"] is False and "network.outbound" in pr["verdict"]["needs_approval"]


def test_browse_bad_peer_graceful():
    app, s = _fresh_app()
    c = TestClient(app)
    r = c.post("/api/skills/browse", json={"base_url": "http://127.0.0.1:9/"})
    assert r.status_code == 200 and r.json()["ok"] is False


def test_inprocess_peer_roundtrip():
    # Stand up a "server" router app, fetch an object from it in-process over a real
    # httpx ASGI transport, then feed it to the CLIENT service -- proves serve+fetch.
    import asyncio, httpx
    server_app, server = _fresh_app()
    server.publish(b'{"prompt":"shared"}', name="Shared",
                   capabilities=["prompt.augment"], author="A")
    hid = server.local_catalog()[0]["id"]
    client = svc.SkillService(base_dir=tempfile.mkdtemp(), key_dir=tempfile.mkdtemp(),
                              policy=g.default_policy(require_trusted_author=False))
    async def _pull():
        transport = httpx.ASGITransport(app=server_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://peer") as hc:
            return (await hc.get("/api/skills/object/" + hid)).json()
    obj = asyncio.run(_pull())
    res = client.fetch_object(lambda h: obj, hid)
    assert res["ok"] and client.store.get(hid)["state"] == "quarantined"


def test_identity_and_trusted_endpoints():
    import tempfile as _tf
    app, s = _fresh_app()
    d = _tf.mkdtemp(); skill_api.set_key_dir(d)
    c = TestClient(app)
    ident = c.get("/api/skills/identity").json()
    assert ident["pubkey"] and ident["fingerprint"]
    peer = st.public_key_b64(key_dir=_tf.mkdtemp())
    assert c.post("/api/skills/trusted", json={"pubkey": peer, "label": "Peer"}).json()["ok"]
    assert any(k["pubkey"] == peer for k in c.get("/api/skills/trusted").json()["keys"])
    assert c.post("/api/skills/trusted/remove", json={"pubkey": peer}).json()["ok"]
    assert c.get("/api/skills/trusted").json()["keys"] == []
    skill_api.set_key_dir(None)


def test_promote_enforced_by_trusted_store():
    import tempfile as _tf
    d = _tf.mkdtemp(); skill_api.set_key_dir(d)
    app = FastAPI(); app.include_router(skill_api.skill_router)
    skill_api.set_config({"skill_share_enabled": True})
    skill_api._service_err = None
    skill_api._service = svc.SkillService(base_dir=_tf.mkdtemp(), key_dir=_tf.mkdtemp(),
        policy=g.default_policy(require_trusted_author=True))
    c = TestClient(app)
    peer_kd = _tf.mkdtemp()
    body = b'{"prompt":"hi"}'
    env = st.sign_artifact(body, name="P", capabilities=["prompt.augment"], key_dir=peer_kd)
    skill_api._service.ingest(body, env)
    hid = st.content_hash(body)
    assert c.post("/api/skills/promote", json={"id": hid}).json()["ok"] is False  # untrusted
    peer_pub = st.public_key_b64(key_dir=peer_kd)
    c.post("/api/skills/trusted", json={"pubkey": peer_pub, "label": "Peer"})
    assert c.post("/api/skills/promote", json={"id": hid}).json()["ok"] is True   # trusted
    skill_api.set_key_dir(None)


def test_export_import_endpoints():
    app, s = _fresh_app()
    c = TestClient(app)
    hid = s.publish(b'{"prompt":"hi"}', name="P", capabilities=["prompt.augment"], author="A")["id"]
    bundle = c.get("/api/skills/export/" + hid).json()
    assert bundle["schema"] == "oracleai.skill-bundle/1"
    app2, s2 = _fresh_app()
    c2 = TestClient(app2)
    r = c2.post("/api/skills/import", json={"bundle": bundle})
    assert r.json()["ok"] and s2.store.get(hid)["state"] == "quarantined"


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
