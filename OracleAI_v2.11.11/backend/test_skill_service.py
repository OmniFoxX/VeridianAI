"""Self-running unit tests for skill_service (Aether skill-share Layer 4).

Run:  python test_skill_service.py    (no pytest; needs skill_trust/store/gate alongside)
Covers (against a stub transport): publish + catalog, serve-only-promoted,
fetch roundtrip lands quarantined with a verdict, hash-mismatch rejection,
browse local-state annotation, capability-gated promotion, and the trusted-author
requirement on promotion.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skill_trust as st
import skill_gate as g
import skill_service as svc


def _server():
    return svc.SkillService(base_dir=tempfile.mkdtemp(), key_dir=tempfile.mkdtemp())


def _client(**pol):
    return svc.SkillService(base_dir=tempfile.mkdtemp(), key_dir=tempfile.mkdtemp(),
                            policy=g.default_policy(**pol))


def test_publish_and_catalog():
    s = _server()
    r = s.publish(b'{"x":1}', name="N", version="1.0",
                  capabilities=["prompt.augment"], author="A")
    assert r["ok"]
    cat = s.local_catalog()
    assert len(cat) == 1 and cat[0]["id"] == r["id"] and cat[0]["name"] == "N"
    sh = s.get_shareable(r["id"])
    assert sh and sh["envelope"]["payload"]["id"] == r["id"]


def test_get_shareable_only_promoted():
    s = _server()
    body = b'{"y":2}'
    env = st.sign_artifact(body, name="Q", capabilities=["prompt.augment"],
                           key_dir=tempfile.mkdtemp())
    s.ingest(body, env)  # lands quarantined, not shareable
    assert s.get_shareable(st.content_hash(body)) is None
    assert s.get_shareable("0" * 64) is None


def test_fetch_roundtrip_quarantines():
    server, client = _server(), _client(require_trusted_author=False)
    r = server.publish(b'{"hooks":{"append_footer":"x"}}', name="F",
                       capabilities=["hook.append_footer"], author="A")
    hid = r["id"]
    fr = client.fetch_object(lambda h: server.get_shareable(h), hid)
    assert fr["ok"] and fr["id"] == hid
    assert client.store.get(hid)["state"] == "quarantined"
    assert fr["verdict"]["recommendation"] == "eligible"


def test_fetch_hash_mismatch_rejected():
    server, client = _server(), _client(require_trusted_author=False)
    r = server.publish(b'{"a":1}', name="A1", capabilities=["prompt.augment"], author="A")
    bad = server.get_shareable(r["id"])
    fr = client.fetch_object(lambda h: bad, "0" * 64)  # requested != returned
    assert not fr["ok"] and "mismatch" in fr["reason"]
    assert client.store.get(r["id"]) is None


def test_browse_annotates_local_state():
    server, client = _server(), _client(require_trusted_author=False)
    r = server.publish(b'{"prompt":"hi"}', name="P", capabilities=["prompt.augment"], author="A")
    items = client.browse(lambda: server.local_catalog())["items"]
    assert items[0]["have"] is False
    client.fetch_object(lambda h: server.get_shareable(h), r["id"])
    items2 = client.browse(lambda: server.local_catalog())["items"]
    assert items2[0]["have"] is True and items2[0]["local_state"] == "quarantined"


def test_promote_gated_on_capability():
    server, client = _server(), _client(require_trusted_author=False)
    r = server.publish(b'{"net":1}', name="Net", capabilities=["network.outbound"], author="A")
    hid = r["id"]
    client.fetch_object(lambda h: server.get_shareable(h), hid)
    ok, verdict = client.promote(hid)
    assert ok is False and "network.outbound" in verdict["needs_approval"]
    client.policy = g.default_policy(require_trusted_author=False,
                                     allowed_caps=["network.outbound"])
    ok2, _ = client.promote(hid)
    assert ok2 is True and client.store.get(hid)["state"] == "promoted"


def test_promote_requires_trusted_author():
    server = _server()
    server_pub = st.public_key_b64(key_dir=server.key_dir)
    client = _client(require_trusted_author=True)
    r = server.publish(b'{"prompt":"hi"}', name="P", capabilities=["prompt.augment"], author="A")
    hid = r["id"]
    client.fetch_object(lambda h: server.get_shareable(h), hid, trusted_pubkeys=[server_pub])
    assert client.promote(hid, trusted_pubkeys=[])[0] is False
    assert client.promote(hid, trusted_pubkeys=[server_pub])[0] is True


def test_bundle_roundtrip():
    src = _server()
    hid = src.publish(b'{"prompt":"hi"}', name="P", capabilities=["prompt.augment"], author="A")["id"]
    bundle = src.export_bundle(hid)
    assert bundle and bundle["schema"] == "oracleai.skill-bundle/1"
    dst = _client(require_trusted_author=False)
    res = dst.import_bundle(bundle)
    assert res["ok"] and dst.store.get(hid)["state"] == "quarantined"
    assert dst.store.verify(hid)["ok"]


def test_bundle_tamper_body_rejected():
    import base64 as _b64
    src = _server()
    hid = src.publish(b'{"x":1}', name="X", capabilities=["prompt.augment"], author="A")["id"]
    bundle = src.export_bundle(hid)
    bundle["body_b64"] = _b64.b64encode(b"tampered").decode()
    dst = _client(require_trusted_author=False)
    assert dst.import_bundle(bundle)["ok"] is False


def test_bundle_malformed_rejected():
    dst = _client(require_trusted_author=False)
    assert dst.import_bundle(None)["ok"] is False
    assert dst.import_bundle({"schema": "wrong"})["ok"] is False
    assert dst.import_bundle({"schema": "oracleai.skill-bundle/1"})["ok"] is False


def test_export_missing_is_none():
    assert _server().export_bundle("0" * 64) is None


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
