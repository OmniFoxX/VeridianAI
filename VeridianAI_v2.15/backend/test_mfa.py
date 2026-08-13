"""Gate tests for mfa.py: TOTP math + enrollment flow, recovery codes,
challenge tokens, reset. FIDO2 is exercised only to its graceful-absence
seam (real key touches need a human thumb).

Run directly:  python backend/test_mfa.py
Hermetic: config is stubbed, the store lives in a temp dir, and atrest is
disabled so the store is plaintext JSON we can also sanity-inspect.
"""
import base64
import os
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="vai_mfa_test_")
_cfg = types.ModuleType("config")
_cfg.DATA_DIR = _TMP
_cfg.BACKEND_DIR = HERE
_cfg.get = lambda key, default=None: default
sys.modules["config"] = _cfg
sys.modules["atrest"] = None  # force plaintext store (ImportError path)

import mfa  # noqa: E402
mfa._store_path = lambda: os.path.join(_TMP, ".mfa.json")

CHECKS = 0
FAILS = 0


def check(cond, label):
    global CHECKS, FAILS
    CHECKS += 1
    if cond:
        print("  ok  %s" % label)
    else:
        FAILS += 1
        print("FAIL  %s" % label)


print("== TOTP math (RFC 6238 SHA-1 test vector, truncated to 6 digits) ==")
rfc_secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
check(mfa._hotp(rfc_secret, 1) == "287082", "T=59s counter=1 -> 287082")
check(mfa._hotp(rfc_secret, 0x23523EC) == "081804", "T=1111111109 -> 081804")
check(mfa._hotp(rfc_secret, 0x273EF07) == "005924", "T=1234567890 -> 005924")
check(mfa._hotp(rfc_secret, 0x3F940AA) == "279037", "T=2000000000 -> 279037")

print("== enrollment flow ==")
u = "todd"
st = mfa.status(u)
check(not st["totp_enabled"] and not st["fido2_keys"], "fresh account: nothing enrolled")
check(mfa.enabled_methods(u) == [], "no methods -> no MFA challenge at login")
beg = mfa.totp_begin(u)
check(len(beg["secret"]) == 32 and beg["otpauth"].startswith("otpauth://totp/"),
      "begin returns secret + otpauth URI")
check(not mfa.status(u)["totp_enabled"], "PENDING enrollment is not yet trusted")
bad = mfa.totp_confirm(u, "000000")
check(not bad["success"], "wrong confirm code rejected")
now_counter = int(time.time()) // mfa.TOTP_STEP
good_code = mfa._hotp(beg["secret"], now_counter)
res = mfa.totp_confirm(u, good_code)
check(res["success"], "correct confirm code enables TOTP")
check(res["recovery_codes"] and len(res["recovery_codes"]) == mfa.RECOVERY_COUNT,
      "first enrollment mints %d recovery codes (shown once)" % mfa.RECOVERY_COUNT)
codes = res["recovery_codes"]
check(all(len(c) == 11 and "-" in c for c in codes), "codes are xxxxx-xxxxx shaped")
check(mfa.status(u)["totp_enabled"], "status reflects enabled")
check("totp" in mfa.enabled_methods(u) and "recovery" in mfa.enabled_methods(u),
      "enabled methods include totp + recovery")

print("== verification + replay protection ==")
next_code = mfa._hotp(beg["secret"], now_counter + 1)
check(mfa.verify_totp(u, next_code), "valid code verifies (window +1)")
check(not mfa.verify_totp(u, next_code), "SAME code replayed -> rejected")
check(not mfa.verify_totp(u, "123456") or True, "junk code path doesn't crash")
check(not mfa.verify_totp(u, good_code), "older counter than last used -> rejected")

print("== recovery codes: single use, format-forgiving, constant-time-ish ==")
c0 = codes[0]
check(mfa.verify_recovery(u, c0.upper().replace("-", " ")), "case/format-insensitive match")
check(not mfa.verify_recovery(u, c0), "recovery code is SINGLE use")
check(mfa.status(u)["recovery_remaining"] == mfa.RECOVERY_COUNT - 1, "count decremented")
check(mfa.verify_recovery(u, "2. " + codes[1]), "pasted WITH display numbering ('2. ...')")
check(mfa.verify_recovery(u, " 10)  " + codes[2].upper()), "numbering variant ('10) ...') + case")
check(not mfa.verify_recovery(u, "3. " + codes[1]), "numbered paste of an already-used code still dead")
check(not mfa.verify_recovery(u, "aaaaa-aaaaa"), "unknown code rejected")
reg = mfa.regenerate_recovery(u)
check(reg["success"] and len(reg["recovery_codes"]) == mfa.RECOVERY_COUNT, "regenerate -> fresh 10")
check(not mfa.verify_recovery(u, codes[3]), "old (UNUSED) codes dead after regenerate")
check(mfa.verify_recovery(u, reg["recovery_codes"][0]), "new code works")

print("== login challenge tokens ==")
user_dict = {"username": u, "ns": "todd_abcd1234", "is_owner": True}
tok = mfa.begin_challenge(user_dict, must_change=True, ttl=1234)
rec = mfa.peek_challenge(tok)
check(rec and rec["username"] == u and rec["must_change"] and rec["ttl"] == 1234,
      "peek returns the record without burning it")
check(mfa.peek_challenge(tok) is not None, "peek is repeatable")
rec2 = mfa.consume_challenge(tok)
check(rec2 and rec2["user"]["ns"] == "todd_abcd1234", "consume returns full user dict")
check(mfa.consume_challenge(tok) is None, "token is one-shot")
check(mfa.peek_challenge("nonsense") is None, "unknown token -> None")

print("== disable / reset ==")
mfa.totp_disable(u)
check(not mfa.status(u)["totp_enabled"], "disable works")
check(mfa.status(u)["recovery_remaining"] == 0,
      "recovery codes cleared when the LAST method is removed")
# re-enroll for the reset test
b2 = mfa.totp_begin(u)
mfa.totp_confirm(u, mfa._hotp(b2["secret"], int(time.time()) // mfa.TOTP_STEP))
check(mfa.status(u)["totp_enabled"], "re-enrollment works after disable")
r = mfa.reset_user(u)
check(r["success"] and r["had_mfa"], "owner reset clears the account's MFA")
check(mfa.enabled_methods(u) == [], "after reset: password-only again")

print("== FIDO2 ==")
avail = mfa.fido2_available()
check(isinstance(avail, bool), "fido2_available returns bool (here: %s)" % avail)
if not avail:
    rr = mfa.fido2_register(u, "test key")
    check(not rr["success"] and "install" in rr["error"], "register: clean 'not installed' error")
    ra = mfa.fido2_authenticate(u)
    check(not ra["success"], "authenticate: clean failure without the lib")
else:
    # SOFTWARE AUTHENTICATOR: exercises the REAL fido2_register /
    # fido2_authenticate ceremonies (register_begin/complete,
    # authenticate_begin/complete, storage round-trip, sign-count bump) with
    # only the hardware layer (_get_client) faked. This is the regression
    # gate for the 2026-07-19 'Add key fails instantly' bug -- a python-fido2
    # API change breaks THIS test, not just Todd's YubiKey.
    import hashlib as _h

    class SoftKey:
        """Minimal ES256 authenticator, attestation format 'none'."""

        def __init__(self):
            from cryptography.hazmat.primitives.asymmetric import ec
            self._sk = ec.generate_private_key(ec.SECP256R1())
            self.cred_id = os.urandom(32)
            self.counter = 0
            self.touches = 0

        def make_credential(self, options):
            from fido2.cose import ES256
            from fido2.webauthn import (
                AttestedCredentialData, AuthenticatorData, AttestationObject,
                CollectedClientData, RegistrationResponse,
                AuthenticatorAttestationResponse)
            self.touches += 1
            cd = CollectedClientData.create(
                CollectedClientData.TYPE.CREATE, options.challenge, mfa.ORIGIN)
            pub = ES256.from_cryptography_key(self._sk.public_key())
            acd = AttestedCredentialData.create(b"\x00" * 16, self.cred_id, pub)
            ad = AuthenticatorData.create(
                _h.sha256(options.rp.id.encode()).digest(),
                AuthenticatorData.FLAG.UP | AuthenticatorData.FLAG.AT,
                self.counter, bytes(acd))
            ao = AttestationObject.create("none", ad, {})
            return RegistrationResponse(
                raw_id=self.cred_id,
                response=AuthenticatorAttestationResponse(
                    client_data=cd, attestation_object=ao))

        def get_assertion(self, options):
            from cryptography.hazmat.primitives.asymmetric import ec as _ec
            from cryptography.hazmat.primitives import hashes as _hs
            from fido2.webauthn import (
                AuthenticatorData, CollectedClientData, AuthenticationResponse,
                AuthenticatorAssertionResponse)
            self.touches += 1
            cd = CollectedClientData.create(
                CollectedClientData.TYPE.GET, options.challenge, mfa.ORIGIN)
            self.counter += 1
            ad = AuthenticatorData.create(
                _h.sha256(options.rp_id.encode()).digest(),
                AuthenticatorData.FLAG.UP, self.counter)
            sig = self._sk.sign(bytes(ad) + cd.hash, _ec.ECDSA(_hs.SHA256()))
            resp = AuthenticationResponse(
                raw_id=self.cred_id,
                response=AuthenticatorAssertionResponse(
                    client_data=cd, authenticator_data=ad, signature=sig))

            class _Sel:
                def get_response(self, index):
                    return resp
            return _Sel()

    softkey = SoftKey()
    mfa._get_client = lambda pin=None: softkey
    u2 = "yubi_tester"
    rr = mfa.fido2_register(u2, "soft key")
    check(rr["success"], "register ceremony completes (got: %s)" % rr.get("error"))
    check(rr.get("recovery_codes") and len(rr["recovery_codes"]) == mfa.RECOVERY_COUNT,
          "first MFA method mints recovery codes")
    check(softkey.touches == 1, "register touched the key exactly once")
    st2 = mfa.status(u2)
    check(len(st2["fido2_keys"]) == 1 and st2["fido2_keys"][0]["label"] == "soft key",
          "credential stored with label")
    check(mfa.enabled_methods(u2) == ["fido2", "recovery"], "methods: fido2 + recovery")
    ra = mfa.fido2_authenticate(u2)
    check(ra["success"], "authenticate ceremony verifies (got: %s)" % ra.get("error"))
    check(mfa.status(u2)["fido2_keys"] and
          mfa._load()["users"][u2]["fido2"][0]["sign_count"] == 1,
          "sign counter bumped after assertion")
    # tampered signature must NOT verify
    from cryptography.hazmat.primitives.asymmetric import ec as _ec_t
    wrong = _ec_t.generate_private_key(_ec_t.SECP256R1())
    softkey._sk = wrong
    bad = mfa.fido2_authenticate(u2)
    check(not bad["success"], "assertion from the WRONG key is rejected")
    check(mfa.fido2_remove(u2, "no-such-id")["success"] is False, "remove unknown id -> False")
    rm = mfa.fido2_remove(u2, st2["fido2_keys"][0]["id"])
    check(rm["success"], "remove real credential works")
    check(mfa.status(u2)["recovery_remaining"] == 0,
          "recovery codes cleared with last method")

print("\n%d checks, %d failures" % (CHECKS, FAILS))
sys.exit(1 if FAILS else 0)
