# -*- coding: utf-8 -*-
"""The key lifecycle as the app drives it: create, login, MFA hop, password
change, delete, burn.

keywrap and profile_keys are tested pure. This covers the ORDERING and the
carry-across, which is where lifecycle bugs live -- a key that is re-wrapped
after the password changes, or one that never crosses the second-factor hop,
both look fine in isolation and fail in use.

    python test_key_lifecycle.py
"""
import os
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="vai_life_")
os.environ["VERIDIAN_DATA_DIR"] = _TMP

import keywrap                                              # noqa: E402
import profile_keys as pk                                   # noqa: E402
import session as sess                                      # noqa: E402
import mfa                                                  # noqa: E402

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


print("== profile creation ==")
DEK = pk.create_for_profile("alice", "pw-one", recovery=True)
ok("alice has a key", pk.has_profile_key("alice"))
ok("the owner still does not", pk.has_profile_key(None) is False)

print("\n== login: the key reaches the session ==")
user = {"username": "alice", "ns": "alice", "is_owner": False}
tok = sess.create_session(user)
# mirrors main._attach_profile_key
d = pk.unlock("alice", "pw-one")
ok("unlocked at login", d == DEK)
ok("attached to the session", sess.set_session_dek(tok, d) is True)
ok("readable by token", sess.get_session_dek(tok) == DEK)

print("\n== logout takes the key with it ==")
sess.destroy_session(tok)
ok("key gone after logout", sess.get_session_dek(tok) is None)

print("\n== the MFA hop: password at step 1, session at step 2 ==")
ch = mfa.begin_challenge(user, must_change=False, ttl=3600)
ok("challenge created", bool(ch))
d1 = pk.unlock("alice", "pw-one")          # step 1 has the password
ok("key parked against the challenge", mfa.stash_challenge_dek(ch, d1) is True)

rec = mfa.consume_challenge(ch)            # step 2 burns the challenge
ok("challenge consumed", rec is not None and rec["user"]["ns"] == "alice")
import json
ok("the returned record does NOT carry the key",
   DEK.hex() not in json.dumps(rec, default=str))

tok2 = sess.create_session(rec["user"])
carried = mfa.take_challenge_dek(ch)
ok("the key survived the hop", carried == DEK)
ok("attached to the real session", sess.set_session_dek(tok2, carried) is True)
ok("an MFA user can read their data", sess.get_session_dek(tok2) == DEK)
ok("the challenge key is one-shot", mfa.take_challenge_dek(ch) is None)

print("\n== orphaned challenge keys do not linger ==")
ch2 = mfa.begin_challenge(user, ttl=3600)
mfa.stash_challenge_dek(ch2, DEK)
mfa.consume_challenge(ch2)                 # consumed, key never collected
ok("the orphan is present before a sweep", mfa._PENDING_DEKS.get(ch2) == DEK)
mfa.begin_challenge(user, ttl=3600)        # any new challenge sweeps
ok("swept once another challenge starts", mfa._PENDING_DEKS.get(ch2) is None)

print("\n== password change: unwrap BEFORE, re-wrap AFTER ==")
old_dek = pk.unlock("alice", "pw-one")
ok("opened with the current password", old_dek == DEK)
ok("re-wrapped under the new one", pk.rewrap_password("alice", old_dek, "pw-two"))
ok("new password works", pk.unlock("alice", "pw-two") == DEK)
ok("old password does not", raises(keywrap.BadKey, pk.unlock, "alice", "pw-one"))
ok("the DEK never changed, so no data was re-encrypted",
   pk.unlock("alice", "pw-two") == DEK)

print("\n== the ordering that prevents a lockout ==")
# If set_password succeeded and the re-wrap then failed, the profile would be
# unopenable. main.py unwraps FIRST and refuses the whole change if it cannot.
ok("a wrong current password fails BEFORE anything is written",
   raises(keywrap.BadKey, pk.unlock, "alice", "not-the-password"))
ok("...and the profile is untouched", pk.unlock("alice", "pw-two") == DEK)

print("\n== recovery survives a password change ==")
ok("recovery still opens it", pk.unlock_with_recovery("alice") == DEK)

print("\n== deleting a profile destroys its key ==")
pk.create_for_profile("bob", "bob-pw", recovery=False)
ok("bob has a key", pk.has_profile_key("bob"))
ok("destroy returns True", pk.destroy_for_profile("bob") is True)
ok("bob's data is unreadable by anyone now", pk.unlock("bob", "bob-pw") is None)
ok("alice is unaffected", pk.unlock("alice", "pw-two") == DEK)

print("\n== burn destroys the key too ==")
ok("alice's key exists", pk.has_profile_key("alice"))
ok("burn destroys it", pk.destroy_for_profile("alice") is True)
ok("and it is gone", pk.has_profile_key("alice") is False)
ok("even recovery cannot open it now",
   pk.unlock_with_recovery("alice") is None)

print("\n== single-user installs are untouched ==")
ok("owner create is a no-op", pk.create_for_profile(None, "pw") is None)
ok("owner unlock is a no-op", pk.unlock(None, "pw") is None)
ok("owner destroy is a no-op", pk.destroy_for_profile(None) is False)
otok = sess.create_session({"username": "owner", "ns": None, "is_owner": True})
ok("an owner session simply has no key", sess.get_session_dek(otok) is None)
ok("...which is a normal state, not an error", sess.has_session_dek(otok) is False)

failed = [n for n, c in _results if not c]
print("\n%d/%d passed." % (len(_results) - len(failed), len(_results)))
if failed:
    print("FAILED:")
    for n in failed:
        print("  - " + n)
    sys.exit(1)
