"""Self-running unit tests for skill_store (Aether skill-share Layer 2).

Run:  python test_skill_store.py     (no pytest needed; needs skill_trust.py alongside)
Covers: verified put/get roundtrip, rejection of body-mismatched envelopes,
content dedupe, promotion-state preservation on re-put, on-disk tamper detection,
state filtering, size cap, reversible removal, cross-instance persistence, stats.
"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skill_trust as st
import skill_store as ss


def _store():
    return ss.SkillStore(base_dir=tempfile.mkdtemp())


def _signed(body=b'{"k":"v"}', caps=None, name="Demo"):
    env = st.sign_artifact(body, name=name, version="1.0",
                           capabilities=caps or ["read"], author="sage-A",
                           key_dir=tempfile.mkdtemp())
    return body, env


def test_put_get_roundtrip():
    s = _store(); body, env = _signed()
    r = s.put(body, env)
    assert r["ok"], r
    hid = r["id"]
    assert s.get_body(hid) == body
    assert s.get_envelope(hid)["payload"]["id"] == hid
    row = s.get(hid)
    assert row["state"] == ss.STATE_QUARANTINED and row["verified"] == 1
    assert json.loads(row["capabilities"]) == ["read"]
    assert s.verify(hid)["ok"] is True


def test_put_rejects_tampered():
    s = _store(); body, env = _signed()
    r = s.put(b"different-body", env)
    assert not r["ok"] and "verify failed" in r["reason"], r
    assert s.get(env["payload"]["id"]) is None


def test_dedupe():
    s = _store(); body, env = _signed()
    a = s.put(body, env); b = s.put(body, env)
    assert a["ok"] and b["ok"] and a["id"] == b["id"]
    assert b["deduped"] is True
    assert len(s.list()) == 1


def test_state_preserve_on_reput():
    s = _store(); body, env = _signed()
    hid = s.put(body, env)["id"]
    assert s.promote(hid) is True
    assert s.get(hid)["state"] == ss.STATE_PROMOTED
    s.put(body, env)
    assert s.get(hid)["state"] == ss.STATE_PROMOTED


def test_verify_detects_disk_tamper():
    s = _store(); body, env = _signed()
    hid = s.put(body, env)["id"]
    s._body_path(hid).write_bytes(b"corrupted-on-disk")
    assert not s.verify(hid)["ok"]


def test_list_filters_by_state():
    s = _store()
    h1 = s.put(*_signed(body=b"one", name="One"))["id"]
    s.put(*_signed(body=b"two", name="Two"))
    s.promote(h1)
    prom = s.list(state=ss.STATE_PROMOTED)
    assert len(prom) == 1 and prom[0]["id"] == h1
    assert len(s.list(state=ss.STATE_QUARANTINED)) == 1


def test_size_cap():
    s = _store(); body, env = _signed(body=b"x" * 100)
    r = s.put(body, env, max_body_bytes=10)
    assert not r["ok"] and "size cap" in r["reason"]
    assert s.get(env["payload"]["id"]) is None


def test_remove_reversible():
    s = _store(); body, env = _signed()
    hid = s.put(body, env)["id"]
    assert s.remove(hid) is True
    assert s.get(hid) is None
    assert not s._body_path(hid).exists()
    assert list((s.base / "removed").glob(hid + "*")), "body must survive in removed/"


def test_persists_across_instances():
    base = tempfile.mkdtemp()
    s1 = ss.SkillStore(base_dir=base); body, env = _signed()
    hid = s1.put(body, env)["id"]
    s2 = ss.SkillStore(base_dir=base)
    assert s2.get_body(hid) == body and s2.get(hid) is not None


def test_stats():
    s = _store()
    for w in (b"a", b"bb", b"ccc"):
        s.put(*_signed(body=w, name=w.decode()))
    out = s.stats()
    assert out["count"] == 3
    assert out["by_state"].get(ss.STATE_QUARANTINED) == 3
    assert out["total_bytes"] == 6


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
