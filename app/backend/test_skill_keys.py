"""Self-running unit tests for skill_keys (Aether skill-share Layer 5).

Run:  python test_skill_keys.py     (needs skill_trust.py alongside)
Covers: add/list/remove, idempotent re-add with relabel, invalid-key rejection,
remove-missing, stable self identity, persistence, and tolerance of legacy
bare-string entries.
"""
import os, sys, base64, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skill_trust as st
import skill_keys as kk


def test_add_list_remove():
    d = tempfile.mkdtemp()
    pub = st.public_key_b64(key_dir=tempfile.mkdtemp())
    assert kk.list_keys(d) == []
    r = kk.add_key(pub, label="Family A", key_dir=d)
    assert r["ok"] and r["fingerprint"] == st.fingerprint(pub)
    keys = kk.list_keys(d)
    assert len(keys) == 1 and keys[0]["pubkey"] == pub and keys[0]["label"] == "Family A"
    assert kk.is_trusted(pub, d) and kk.trusted_pubkeys(d) == [pub]
    assert kk.remove_key(pub, key_dir=d)["ok"] and kk.list_keys(d) == []


def test_dedupe_and_relabel():
    d = tempfile.mkdtemp()
    pub = st.public_key_b64(key_dir=tempfile.mkdtemp())
    kk.add_key(pub, "first", key_dir=d)
    r = kk.add_key(pub, "second", key_dir=d)
    assert r["deduped"] is True
    assert kk.list_keys(d)[0]["label"] == "second" and len(kk.list_keys(d)) == 1


def test_invalid_key_rejected():
    d = tempfile.mkdtemp()
    assert kk.add_key("not base64!!", key_dir=d)["ok"] is False
    assert kk.add_key("", key_dir=d)["ok"] is False
    assert kk.add_key(base64.b64encode(b"short").decode(), key_dir=d)["ok"] is False


def test_remove_missing():
    assert kk.remove_key("whatever", key_dir=tempfile.mkdtemp())["ok"] is False


def test_self_identity_stable():
    d = tempfile.mkdtemp()
    a = kk.self_identity(key_dir=d)
    assert a["pubkey"] and a["fingerprint"] == st.fingerprint(a["pubkey"])
    assert kk.self_identity(key_dir=d)["pubkey"] == a["pubkey"]


def test_persist_across_calls():
    d = tempfile.mkdtemp()
    pub = st.public_key_b64(key_dir=tempfile.mkdtemp())
    kk.add_key(pub, "X", key_dir=d)
    assert kk.is_trusted(pub, d)


def test_legacy_string_entries_tolerated():
    import json
    from pathlib import Path
    d = tempfile.mkdtemp()
    pub = st.public_key_b64(key_dir=tempfile.mkdtemp())
    (Path(d) / ".trusted_skill_keys.json").write_text(json.dumps([pub]))
    assert kk.is_trusted(pub, d) and kk.list_keys(d)[0]["pubkey"] == pub


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
