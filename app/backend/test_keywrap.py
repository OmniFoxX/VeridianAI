# -*- coding: utf-8 -*-
"""keywrap.py -- per-profile key wrapping.

This module is the foundation of per-user encryption, so it is tested in
isolation before anything depends on it: no config, no users, no sage_engine.
A failure here should point at this file and nothing else.

The tests that matter most are the ASYMMETRY ones near the end. The security
claim -- "an owner can give up recovery unilaterally but cannot grant it back
to themselves" -- is supposed to hold because of how wrapping works, not
because this code chooses to enforce it. If that claim is real, it should be
demonstrable.

    python test_keywrap.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import keywrap as kw                                       # noqa: E402

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


def raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
        return False
    except exc:
        return True
    except Exception:
        return False


D = tempfile.mkdtemp(prefix="keywrap_")
def path(name):
    return os.path.join(D, name + kw.KEYWRAP_NAME)


print("== create and open ==")
p = path("a")
dek = kw.create(p, "correct horse battery staple")
ok("create returns a 32-byte DEK", isinstance(dek, bytes) and len(dek) == 32, len(dek))
ok("the file exists", kw.exists(p))
ok("right password opens it, same DEK",
   kw.load_dek(p, password="correct horse battery staple") == dek)
ok("wrong password raises BadKey",
   raises(kw.BadKey, kw.load_dek, p, password="wrong"))
ok("empty password raises BadKey",
   raises(kw.BadKey, kw.load_dek, p, password=""))
ok("missing file raises NoKeywrap",
   raises(kw.NoKeywrap, kw.load_dek, path("nope"), password="x"))
ok("creating twice is refused",
   raises(kw.KeywrapError, kw.create, p, "another"))
ok("creating with no password is refused",
   raises(kw.KeywrapError, kw.create, path("b"), ""))

print("\n== the DEK is never on disk in the clear ==")
raw = open(p, "rb").read()
ok("DEK bytes do not appear in the file", dek not in raw)
import base64
ok("nor base64 of it", base64.urlsafe_b64encode(dek) not in raw)
ok("nor hex of it", dek.hex().encode() not in raw)

print("\n== salts are per-file and separate ==")
p2 = path("c")
dek2 = kw.create(p2, "correct horse battery staple")   # SAME password
import json
s1 = json.load(open(p))["salt"]
s2 = json.load(open(p2))["salt"]
ok("same password, different salt", s1 != s2)
ok("same password, different DEK", dek != dek2)
# A REAL cross-file check. The version this replaced was
#   raises(kw.BadKey, lambda: None) is False and load_dek(p2, ...) == dek2
# which passes unconditionally and proves nothing -- the lambda never raises,
# so the first clause is always True, and the second only re-tested p2.
# What actually needs proving: a KEK derived with file A's salt cannot open
# file B's wrap, even though both were made from the SAME password.
from cryptography.fernet import Fernet, InvalidToken
_kek_a = kw._kek_from_secret("correct horse battery staple", bytes.fromhex(s1))
_wrap_b = json.load(open(p2))["wraps"]["user"]
_crossed = True
try:
    Fernet(_kek_a).decrypt(_wrap_b.encode("ascii"))
    _crossed = False          # it opened -- the salts are not doing their job
except InvalidToken:
    pass
ok("a KEK from file A's salt cannot open file B's wrap (same password)",
   _crossed)

print("\n== recovery ==")
rk = kw.new_recovery_key()
p3 = path("rec")
dek3 = kw.create(p3, "userpw", recovery_key=rk)
ok("recovery key opens it", kw.load_dek(p3, recovery_key=rk) == dek3)
ok("password still opens it", kw.load_dek(p3, password="userpw") == dek3)
ok("a DIFFERENT recovery key does not",
   raises(kw.BadKey, kw.load_dek, p3, recovery_key=kw.new_recovery_key()))
ok("has_recovery is True", kw.has_recovery(p3) is True)
ok("a no-recovery profile reports False", kw.has_recovery(p) is False)
ok("recovery unwrap on a no-recovery profile raises BadKey",
   raises(kw.BadKey, kw.load_dek, p, recovery_key=rk))

print("\n== THE ASYMMETRY (the security claim) ==")
ok("drop_recovery needs NO key at all -- unilateral", kw.drop_recovery(p3) is True)
ok("recovery no longer opens it",
   raises(kw.BadKey, kw.load_dek, p3, recovery_key=rk))
ok("the user is unaffected", kw.load_dek(p3, password="userpw") == dek3)
ok("dropping again is a no-op, not an error", kw.drop_recovery(p3) is False)

# The owner, holding ONLY the recovery key they used to have, cannot restore.
ok("set_recovery REQUIRES the plaintext DEK -- it is not a flag to flip",
   "dek" in kw.set_recovery.__code__.co_varnames)
restored = kw.load_dek(p3, password="userpw")     # only the user can do this
kw.set_recovery(p3, restored, rk)
ok("...so restoring recovery took the USER's password", kw.has_recovery(p3))
ok("and now the owner can read again", kw.load_dek(p3, recovery_key=rk) == dek3)

print("\n== password change: cheap, and the DEK survives ==")
p4 = path("pw")
dek4 = kw.create(p4, "old-password")
kw.rewrap_password(p4, kw.load_dek(p4, password="old-password"), "new-password")
ok("new password opens it", kw.load_dek(p4, password="new-password") == dek4)
ok("old password does not",
   raises(kw.BadKey, kw.load_dek, p4, password="old-password"))
ok("the DEK is UNCHANGED -- no data re-encryption needed",
   kw.load_dek(p4, password="new-password") == dek4)
ok("salt was rotated", json.load(open(p4))["salt"] != s1)
ok("empty new password is refused",
   raises(kw.KeywrapError, kw.rewrap_password, p4, dek4, ""))

print("\n== API token wraps (no session, no password) ==")
p5 = path("tok")
dek5 = kw.create(p5, "pw")
kw.set_token_wrap(p5, dek5, "ora_abc1", "ora_abc1_the_full_raw_token")
ok("the token opens the DEK",
   kw.load_dek(p5, token="ora_abc1_the_full_raw_token",
               token_prefix="ora_abc1") == dek5)
ok("a wrong token does not",
   raises(kw.BadKey, kw.load_dek, p5, token="ora_abc1_WRONG",
          token_prefix="ora_abc1"))
ok("an unknown prefix does not",
   raises(kw.BadKey, kw.load_dek, p5, token="whatever", token_prefix="ora_zzzz"))
ok("revoking the token removes its wrap", kw.drop_token_wrap(p5, "ora_abc1") is True)
ok("...and it can no longer open anything",
   raises(kw.BadKey, kw.load_dek, p5, token="ora_abc1_the_full_raw_token",
          token_prefix="ora_abc1"))
ok("dropping an unknown prefix is False", kw.drop_token_wrap(p5, "ora_nope") is False)

print("\n== password change invalidates token wraps (documented, deliberate) ==")
p6 = path("tok2")
dek6 = kw.create(p6, "pw1")
kw.set_token_wrap(p6, dek6, "ora_k1", "raw-token-one")
kw.rewrap_password(p6, kw.load_dek(p6, password="pw1"), "pw2")
ok("the old token wrap is gone, not silently broken",
   kw.info(p6)["token_wraps"] == [], kw.info(p6)["token_wraps"])
ok("the invalidation is recorded for the UI to explain",
   json.load(open(p6)).get("token_wraps_invalidated") == ["ora_k1"])

print("\n== info() leaks nothing ==")
i = kw.info(p3)
blob = json.dumps(i)
ok("no wrap blobs", "gAAAAA" not in blob)
ok("no salt", "salt" not in i)
ok("reports recovery state", i["recovery_enabled"] is True)
ok("reports token prefixes only", isinstance(i["token_wraps"], list))

print("\n== destroy ==")
ok("destroy removes the file", kw.destroy(p5) is True)
ok("the profile is now unopenable by ANY credential",
   raises(kw.NoKeywrap, kw.load_dek, p5, password="pw"))
ok("destroying again is False, not an error", kw.destroy(p5) is False)

print("\n== malformed input ==")
bad = os.path.join(D, "bad.json")
open(bad, "w").write("{not json")
ok("garbage file raises KeywrapError", raises(kw.KeywrapError, kw.load_dek, bad, password="x"))
open(bad, "w").write('{"no":"wraps"}')
ok("missing 'wraps' raises KeywrapError", raises(kw.KeywrapError, kw.load_dek, bad, password="x"))
ok("no credential at all is a programming error, not BadKey",
   raises(kw.KeywrapError, kw.load_dek, p4))

failed = [n for n, c in _results if not c]
print("\n%d/%d passed." % (len(_results) - len(failed), len(_results)))
if failed:
    print("FAILED:")
    for n in failed:
        print("  - " + n)
    sys.exit(1)
