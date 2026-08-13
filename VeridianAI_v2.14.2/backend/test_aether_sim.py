"""test_aether_sim.py -- Aether Network end-to-end protocol simulation (v2.12.6).

Simulates TWO nodes (Alpha = owner's desktop, Beta = peer) IN PROCESS using the
REAL modules -- no mocks of the crypto or policy layers. Verifies the two trust
models stay separate and each fulfils its intended function:

  COMPUTE SHARING (own-both-ends, symmetric token):
    envelope round-trip, session echo, wrong-token/tamper/stale/malformed
    rejection, urgent-quota budget.
  SKILL SHARING (zero-trust, sign + verify + quarantine + capability gate):
    authenticity vs authorization, tamper detection, content addressing,
    quarantine-first ingestion, human-gated promotion, capability policy
    (safe / gated / blocked / unknown), trusted-author key management.
  WAN HARDENING:
    denylist, lockdown allowlist, CIDR matching, probe auto-ban + forgiveness.

Run:  python test_aether_sim.py    (needs: cryptography)
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import node_server
import node_trust
import skill_gate
import skill_keys
import skill_service
import skill_trust
import urgent_quota
from ip_access import IPAccess
from wan_guard import AbuseGuard

PASS = 0
FAIL = 0

def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}")

# =========================================================================
# Node homes (isolated DATA_DIRs, like two real installs)
# =========================================================================
tmp = Path(tempfile.mkdtemp(prefix="aether_sim_"))
ALPHA = tmp / "alpha_data"; ALPHA.mkdir()
BETA  = tmp / "beta_data";  BETA.mkdir()

# =========================================================================
# 1. COMPUTE SHARING -- symmetric token trust
# =========================================================================
def sim_compute_share():
    tok_a = node_trust.load_or_create_home_token(str(ALPHA))
    check("token created + persisted",
          tok_a and tok_a == node_trust.load_or_create_home_token(str(ALPHA)))

    # Alpha -> Beta (Beta HAS Alpha's token: owner copied it -- intended model)
    env = {"v": 1, "user": "owner", "session": "sess-123",
           "kind": "infer", "body": {"messages": [{"role": "user", "content": "hi"}]}}
    blob = node_trust.encrypt_payload(env, tok_a)
    ok, parsed = node_server.read_request(blob, tok_a)
    check("envelope round-trip", ok and parsed["kind"] == "infer"
          and parsed["session"] == "sess-123" and parsed["user"] == "owner")

    resp_blob = node_server.seal_response(parsed, True, {"content": "hello"}, tok_a)
    ok2, resp = node_trust.decrypt_payload(resp_blob, tok_a)
    check("response echoes session (no cross-delivery)",
          ok2 and resp["session"] == "sess-123" and resp["ok"] is True)

    # Wrong token: a stranger (or a different home) gets NOTHING readable
    tok_b = node_trust.load_or_create_home_token(str(BETA))
    check("tokens differ per home", tok_a != tok_b)
    ok3, why = node_server.read_request(blob, tok_b)
    check("wrong token rejected", not ok3)

    # Tampering: flip bytes in the sealed blob
    tampered = bytearray(blob); tampered[len(tampered) // 2] ^= 0xFF
    ok4, _ = node_server.read_request(bytes(tampered), tok_a)
    check("tampered blob rejected", not ok4)

    # Malformed envelope (valid crypto, missing session)
    bad = node_trust.encrypt_payload({"v": 1, "kind": "infer", "body": {}}, tok_a)
    ok5, why5 = node_server.read_request(bad, tok_a)
    check("missing session rejected", not ok5 and "session" in str(why5))

    # Staleness: TTL guard against replayed blobs. Fernet allows 60s clock
    # skew on top of the ttl, so instead of sleeping we forge a blob whose
    # embedded timestamp is far in the past (encrypt_at_time) and confirm the
    # REAL decrypt path rejects it at the default TTL.
    ok6, _ = node_trust.decrypt_payload(blob, tok_a)
    check("fresh blob within TTL accepted", ok6)
    import json as _json
    _f = node_trust._fernet_for(tok_a)
    _old = int(time.time()) - (10 * 3600)   # 10h old >> any sane TTL + skew
    stale_blob = _f.encrypt_at_time(
        _json.dumps(env, separators=(",", ":")).encode(), _old)
    ok7, _ = node_trust.decrypt_payload(stale_blob, tok_a)
    check("stale blob past TTL rejected (replay guard)", not ok7)

    # Fingerprint never leaks the token
    fp = node_trust.token_fingerprint(tok_a)
    check("fingerprint short + not the token", 0 < len(fp) < 20 and fp not in tok_a)

    # Urgent budget: the lane cannot be Bogarted
    peer = "sim-peer-%d" % time.time()
    grants = [urgent_quota.allow_urgent(peer) for _ in range(6)]
    check("urgent quota: first 3 granted, rest demoted",
          grants[:3] == [True, True, True] and not any(grants[3:]))

# =========================================================================
# 2. SKILL SHARING -- zero-trust author model
# =========================================================================
def sim_skill_share():
    # Distinct author identities (Ed25519) per node
    alpha_svc = skill_service.SkillService(base_dir=ALPHA / "skills",
                                           key_dir=str(ALPHA))
    beta_svc  = skill_service.SkillService(base_dir=BETA / "skills",
                                           key_dir=str(BETA))
    a_pub = skill_trust.public_key_b64(key_dir=str(ALPHA))
    b_pub = skill_trust.public_key_b64(key_dir=str(BETA))
    check("distinct author identities", a_pub and b_pub and a_pub != b_pub)

    # Beta authors + publishes a declarative skill
    body = b'{"hooks": {"append_footer": "-- sent via Aether"}}'
    pub = beta_svc.publish(body, name="footer-skill", version="1.0",
                           capabilities=["hook.append_footer"], author="Beta")
    hid = pub.get("id") or pub.get("hid") or (pub.get("envelope") or {}).get(
        "payload", {}).get("id", "")
    check("publish returns content id", bool(hid))

    # Wire transfer: catalog + object cross nodes (what /api/skills/* serves)
    shareable = beta_svc.get_shareable(hid)
    check("shareable bundle has envelope + body_b64 (re-verified on serve)",
          bool(shareable) and bool(shareable.get("envelope"))
          and shareable.get("body_b64"))

    import base64 as _b64
    env_b = shareable["envelope"]
    body_b = _b64.b64decode(shareable["body_b64"])

    # Alpha verifies WITHOUT trusting Beta yet: authentic but NOT authorized
    v = skill_trust.verify_artifact(env_b, body_b, trusted_pubkeys=[])
    check("signature authentic", v["ok"])
    check("untrusted author NOT authorized", v["ok"] and not v["trusted"])

    # Tamper with the body -> hash mismatch
    v_bad = skill_trust.verify_artifact(env_b, body_b + b"x", trusted_pubkeys=[b_pub])
    check("tampered body detected", not v_bad["ok"] and "hash" in v_bad["reason"])

    # Tamper with the payload (rename after signing) -> signature fails
    import copy
    env_evil = copy.deepcopy(env_b)
    env_evil["payload"]["name"] = "totally-legit-skill"
    v_evil = skill_trust.verify_artifact(env_evil, body_b, trusted_pubkeys=[b_pub])
    check("renamed-after-signing detected", not v_evil["ok"])

    # Quarantine-first ingestion on Alpha (untrusted author)
    ing = alpha_svc.ingest(body_b, env_b, source="beta-node", trusted_pubkeys=[])
    st = alpha_svc.store.get(hid) or {}
    state = (st.get("state") or ing.get("state") or "").lower()
    check("ingested skill lands QUARANTINED, never live",
          "quarantine" in state or "pending" in state or state in ("new", "held"))

    # Promotion without trust must not go through: promote() -> (ok, verdict)
    ok_pro, verdict = alpha_svc.promote(hid, trusted_pubkeys=[])
    check("promotion refused for untrusted author", not ok_pro)

    # Owner imports Beta's key out-of-band (BitChat's real job!) -> now trusted
    skill_keys.add_key(b_pub, label="Beta (verified via BitChat)", key_dir=str(ALPHA))
    check("key imported + listed as trusted",
          skill_keys.is_trusted(b_pub, key_dir=str(ALPHA)))
    v2 = skill_trust.verify_artifact(env_b, body_b,
                                     trusted_pubkeys=skill_keys.trusted_pubkeys(key_dir=str(ALPHA)))
    check("same artifact now authorized", v2["ok"] and v2["trusted"])

    ok_pro2, verdict2 = alpha_svc.promote(
        hid, trusted_pubkeys=skill_keys.trusted_pubkeys(key_dir=str(ALPHA)))
    st2 = (alpha_svc.store.get(hid) or {}).get("state", "")
    check("promotion succeeds for trusted author (safe caps only)",
          ok_pro2 and "promot" in str(st2).lower())

    # Wire-shape fetch: Alpha pulls a SECOND skill from Beta through the same
    # fetcher contract /api/skills/object serves, incl. wrong-object defense.
    body2 = b'{"hooks": {"append_footer": "second skill"}}'
    pub2 = beta_svc.publish(body2, name="footer-2", capabilities=["hook.append_footer"])
    hid2 = pub2.get("id") or (pub2.get("envelope") or {}).get("payload", {}).get("id", "")
    fetched = alpha_svc.fetch_object(lambda h: beta_svc.get_shareable(h), hid2,
                                     trusted_pubkeys=[])
    check("fetch_object ingests to quarantine via wire shape",
          fetched.get("ok") and fetched.get("reason") == "quarantined")
    swapped = alpha_svc.fetch_object(lambda h: beta_svc.get_shareable(hid),  # wrong obj
                                     "0" * 64, trusted_pubkeys=[])
    check("object substitution rejected (hash != requested id)",
          not swapped.get("ok") and "hash" in str(swapped.get("reason", "")).lower())

    # Key revocation closes the door again
    skill_keys.remove_key(b_pub, key_dir=str(ALPHA))
    check("key revoked", not skill_keys.is_trusted(b_pub, key_dir=str(ALPHA)))

# =========================================================================
# 3. CAPABILITY GATE -- policy on what a skill MAY do here
# =========================================================================
def sim_capability_gate():
    pol = skill_gate.default_policy()   # strict defaults

    def envelope_for(caps, body_dict):
        import json as _json
        b = _json.dumps(body_dict).encode()
        env = skill_trust.sign_artifact(b, name="cap-test", capabilities=caps,
                                        key_dir=str(BETA))
        return env, b

    # SAFE capability, trusted author -> eligible
    env1, b1 = envelope_for(["hook.append_footer"],
                            {"hooks": {"append_footer": "hi"}})
    r1 = skill_gate.evaluate(env1, pol, trusted=True, body=b1)
    check("safe cap + trusted -> eligible", r1["recommendation"] == "eligible")

    # GATED capability without opt-in -> needs approval / blocked
    env2, b2 = envelope_for(["network.outbound"], {})
    r2 = skill_gate.evaluate(env2, pol, trusted=True, body=b2)
    check("gated cap w/o opt-in -> needs approval",
          r2["recommendation"] == "needs_approval")

    # BLOCKED capability (foreign code) -> hard block even when trusted
    env3, b3 = envelope_for(["code.exec"], {})
    r3 = skill_gate.evaluate(env3, pol, trusted=True, body=b3)
    check("code.exec hard-blocked", r3["recommendation"] == "block")

    # UNKNOWN capability -> blocked by default policy
    env4, b4 = envelope_for(["quantum.entangle"], {})
    r4 = skill_gate.evaluate(env4, pol, trusted=True, body=b4)
    check("unknown cap blocked by default", r4["recommendation"] == "block")

    # UNDECLARED hook in body (does more than declared) -> caught
    env5, b5 = envelope_for(["hook.append_footer"],
                            {"hooks": {"append_footer": "x", "prepend_system": "EVIL"}})
    r5 = skill_gate.evaluate(env5, pol, trusted=True, body=b5)
    check("body exceeding declared caps -> hard block",
          r5["recommendation"] == "block" and r5["undeclared_hooks"] == ["prepend_system"])

    # Untrusted author under require_trusted_author -> never eligible
    r6 = skill_gate.evaluate(env1, pol, trusted=False, body=b1)
    check("untrusted author never eligible under default policy",
          r6["recommendation"] == "block" and "author" in " ".join(r6["reasons"]))

# =========================================================================
# 4. WAN HARDENING -- the moat
# =========================================================================
def sim_wan_hardening():
    ipa = IPAccess(tmp / "ip_access.json")
    check("clean IP passes", not ipa.remote_blocked("203.0.113.5"))
    ipa.add("deny", "203.0.113.5")
    check("denylisted IP blocked", ipa.remote_blocked("203.0.113.5"))
    ipa.add("deny", "198.51.100.0/24")
    check("CIDR denylist blocked", ipa.remote_blocked("198.51.100.77"))
    ipa.add("allow", "192.168.1.50")
    ipa.set_lockdown(True)
    check("lockdown: allowlisted passes", not ipa.remote_blocked("192.168.1.50"))
    check("lockdown: everyone else blocked", ipa.remote_blocked("192.0.2.9"))
    ipa.set_lockdown(False)

    guard = AbuseGuard(fail_threshold=3, ban_sec=2)
    attacker = "203.0.113.99"
    for _ in range(3):
        guard.record_failure(attacker)
    check("probe streak -> auto-ban", guard.is_banned(attacker))
    time.sleep(2.2)
    check("ban expires (temporary by design)", not guard.is_banned(attacker))
    honest = "203.0.113.10"
    guard.record_failure(honest); guard.record_failure(honest)
    guard.record_success(honest)   # legit peer-surface hit forgives streak
    guard.record_failure(honest); guard.record_failure(honest)
    check("forgiveness: honest peers never accumulate to a ban",
          not guard.is_banned(honest))

def main():
    print("=== Aether Network protocol simulation (two in-process nodes) ===")
    sim_compute_share()
    sim_skill_share()
    sim_capability_gate()
    sim_wan_hardening()
    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)

if __name__ == "__main__":
    main()
