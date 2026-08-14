# -*- coding: utf-8 -*-
"""profile_keys.py + session DEK storage.

keywrap.py is tested pure; this covers the layer that knows what a profile is:
which profiles get a key at all, where the recovery key lives, and that a data
key never ends up somewhere it could be logged.

    python test_profile_keys.py
"""
import os
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="vai_pk_")
os.environ["VERIDIAN_DATA_DIR"] = _TMP

import keywrap                                              # noqa: E402
import profile_keys as pk                                   # noqa: E402
import session as sess                                      # noqa: E402

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


def raises(exc, fn, *a, **k):
    try:
        fn(*a, **k); return False
    except exc:
        return True
    except Exception:
        return False


print("== the OWNER has no profile key, by design ==")
ok("keywrap_path(None) is None", pk.keywrap_path(None) is None)
ok("has_profile_key(None) is False", pk.has_profile_key(None) is False)
ok("create_for_profile(None, ...) returns None, does not mint one",
   pk.create_for_profile(None, "pw") is None)
ok("unlock(None, ...) is None", pk.unlock(None, "pw") is None)
ok("destroy_for_profile(None) is False", pk.destroy_for_profile(None) is False)
ok("profile_key_info(None) is None", pk.profile_key_info(None) is None)

print("\n== a namespaced profile does get one ==")
p = pk.keywrap_path("alice")
ok("path is under sage_data/users/alice",
   p and "users" in str(p) and "alice" in str(p), str(p))
ok("path is inside the scratch dir", str(p).startswith(_TMP), str(p))
ok("none exists yet", pk.has_profile_key("alice") is False)

dek = pk.create_for_profile("alice", "alice-password", recovery=True)
ok("create returns a DEK", isinstance(dek, bytes) and len(dek) == 32)
ok("has_profile_key is now True", pk.has_profile_key("alice") is True)
ok("creating twice is refused",
   raises(keywrap.KeywrapError, pk.create_for_profile, "alice", "x"))

print("\n== unlock paths ==")
ok("password opens it", pk.unlock("alice", "alice-password") == dek)
ok("wrong password raises BadKey",
   raises(keywrap.BadKey, pk.unlock, "alice", "nope"))
ok("recovery opens it (it was enabled)", pk.unlock_with_recovery("alice") == dek)
ok("unlock on a profile with no key returns None",
   pk.unlock("nosuchprofile", "x") is None)

print("\n== the machine recovery key ==")
rk1 = pk.machine_recovery_key()
rk2 = pk.machine_recovery_key()
ok("stable across calls", rk1 == rk2)
ok("it is encrypted at rest, not lying in the open", True)
import atrest
raw = (open(os.path.join(_TMP, pk.RECOVERY_KEY_NAME), "rb").read())
ok("the file is a Fernet token", atrest.is_encrypted(raw))
ok("the key is NOT in the file in the clear", rk1 not in raw)

print("\n== recovery is opt-out at creation ==")
dek_b = pk.create_for_profile("bob", "bob-password", recovery=False)
ok("bob has a key", pk.has_profile_key("bob"))
ok("bob's password works", pk.unlock("bob", "bob-password") == dek_b)
ok("recovery CANNOT open bob",
   raises(keywrap.BadKey, pk.unlock_with_recovery, "bob"))
ok("info reports no recovery",
   pk.profile_key_info("bob")["recovery_enabled"] is False)
ok("...and alice's does", pk.profile_key_info("alice")["recovery_enabled"] is True)

print("\n== THE ASYMMETRY, at profile level ==")
ok("disable_recovery needs no key", pk.disable_recovery("alice") is True)
ok("recovery no longer opens alice",
   raises(keywrap.BadKey, pk.unlock_with_recovery, "alice"))
ok("alice's password still does", pk.unlock("alice", "alice-password") == dek)
ok("re-enabling REQUIRES an open DEK",
   pk.enable_recovery("alice", pk.unlock("alice", "alice-password")) is True)
ok("...and now recovery works again", pk.unlock_with_recovery("alice") == dek)

print("\n== granting recovery to bob needs BOB's password ==")
ok("nobody can enable it without the DEK",
   pk.enable_recovery("bob", pk.unlock("bob", "bob-password")) is True)
ok("which required bob's password to obtain", True)

print("\n== password change keeps the DEK ==")
ok("rewrap succeeds", pk.rewrap_password("bob", dek_b, "bob-new-password"))
ok("new password opens it", pk.unlock("bob", "bob-new-password") == dek_b)
ok("old password does not", raises(keywrap.BadKey, pk.unlock, "bob", "bob-password"))
ok("the DEK is unchanged -- no data re-encryption",
   pk.unlock("bob", "bob-new-password") == dek_b)

print("\n== API token wraps ==")
ok("add a wrap", pk.add_token_wrap("bob", dek_b, "ora_t1", "raw-token-value"))
ok("the token opens the DEK",
   pk.unlock_with_token("bob", "ora_t1", "raw-token-value") == dek_b)
ok("a wrong token does not",
   raises(keywrap.BadKey, pk.unlock_with_token, "bob", "ora_t1", "wrong"))
ok("dropping it works", pk.drop_token_wrap("bob", "ora_t1") is True)
ok("...and it stops opening",
   raises(keywrap.BadKey, pk.unlock_with_token, "bob", "ora_t1", "raw-token-value"))

print("\n== destroy ==")
ok("destroy returns True", pk.destroy_for_profile("bob") is True)
ok("bob's data is now unopenable by anyone",
   pk.unlock("bob", "bob-new-password") is None)
ok("destroying again is False", pk.destroy_for_profile("bob") is False)

print("\n== session DEK storage: never inside the session record ==")
user = {"username": "alice", "ns": "alice", "is_owner": False}
tok = sess.create_session(user)
ok("session created", bool(tok))
ok("no DEK yet", sess.get_session_dek(tok) is None)
ok("attach one", sess.set_session_dek(tok, dek) is True)
ok("retrievable by name", sess.get_session_dek(tok) == dek)

rec = sess.get_session(tok)
ok("get_session does NOT expose the key",
   all(v != dek for v in rec.values()), list(rec.keys()))
import json
ok("the record is safe to serialise / log",
   dek.hex() not in json.dumps(rec, default=str))

ok("an unknown token cannot be given a key",
   sess.set_session_dek("not-a-real-token", dek) is False)
ok("logout drops the key",
   (sess.destroy_session(tok), sess.get_session_dek(tok))[1] is None)

tok2 = sess.create_session(user)
sess.set_session_dek(tok2, dek)
sess.destroy_user_sessions("alice")
ok("destroy_user_sessions drops keys too", sess.get_session_dek(tok2) is None)

failed = [n for n, c in _results if not c]
print("\n%d/%d passed." % (len(_results) - len(failed), len(_results)))
if failed:
    print("FAILED:")
    for n in failed:
        print("  - " + n)
    sys.exit(1)
