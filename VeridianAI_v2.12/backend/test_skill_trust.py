"""Self-running unit tests for skill_trust (Aether skill-share Layer 1).

Run:  python test_skill_trust.py     (no pytest needed)
Covers: sign/verify roundtrip, the trust gate (authentic vs authorized), body
tampering, capability-escalation tampering, public-key-swap forgery, forward-
compatible local annotations, identity persistence, fingerprint format, and
exception-safety on garbage input.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skill_trust as st


def _d():
    return tempfile.mkdtemp()


def test_roundtrip_and_trust():
    d = _d()
    body = b'{"prompt":"summarize neutrally","binds":"browser"}'
    env = st.sign_artifact(body, name="Neutral Summarizer", version="1.0",
                           capabilities=["network.outbound"], author="sage-A", key_dir=d)
    r = st.verify_artifact(env, body)
    assert r["ok"] and r["reason"] == "valid", r
    assert r["id"] == st.content_hash(body)
    pub = env["payload"]["author_pub"]
    assert st.verify_artifact(env, body, trusted_pubkeys=[pub])["trusted"] is True
    assert st.verify_artifact(env, body, trusted_pubkeys=["nope"])["trusted"] is False
    u = st.verify_artifact(env, body, trusted_pubkeys=[])
    assert u["ok"] is True and u["trusted"] is False  # authentic but unauthorized


def test_tamper_body():
    env = st.sign_artifact(b"hello", name="x", key_dir=_d())
    r = st.verify_artifact(env, b"hello-tampered")
    assert not r["ok"] and r["reason"] == "body hash mismatch", r


def test_tamper_capability_escalation():
    env = st.sign_artifact(b"hello", name="x", capabilities=["read"], key_dir=_d())
    env["payload"]["capabilities"] = ["read", "network.outbound", "code.exec"]
    r = st.verify_artifact(env, b"hello")
    assert not r["ok"] and r["reason"] == "bad signature", r


def test_pubkey_swap_forgery():
    env = st.sign_artifact(b"hello", name="x", key_dir=_d())
    env["payload"]["author_pub"] = st.public_key_b64(key_dir=_d())
    assert not st.verify_artifact(env, b"hello")["ok"]


def test_local_annotation_does_not_break_sig():
    env = st.sign_artifact(b"hello", name="x", key_dir=_d())
    env["quarantined"] = True
    env["fetched_at"] = 123
    assert st.verify_artifact(env, b"hello")["ok"] is True


def test_identity_persists():
    d = _d()
    assert st.public_key_b64(key_dir=d) == st.public_key_b64(key_dir=d)


def test_fingerprint_format():
    fp = st.fingerprint(st.public_key_b64(key_dir=_d()))
    assert len(fp.replace(" ", "")) == 16 and fp == fp.upper()


def test_garbage_inputs_safe():
    assert st.verify_artifact(None, b"x")["ok"] is False
    assert st.verify_artifact({}, b"x")["ok"] is False
    assert st.verify_artifact({"payload": 5, "sig": ""}, b"x")["ok"] is False


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
