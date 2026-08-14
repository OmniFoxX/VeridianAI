#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_profile_key_residency.py -- the profile key actually reaches at-rest,
and leaves when it should.

Everything downstream was built before this: ns= is threaded through the whole
codebase, atrest keeps a registry of profile keys, and the login path unwraps
the DEK into the session. But NOTHING called atrest.register_profile_key, so
the registry stayed empty in a live run and every ns= resolved to the system
key via the fallback. Reads and writes all worked; the per-profile encryption
simply was not happening. That is the failure this file exists to catch, and
the only way to catch it is to assert on the KEY MATERIAL, not on whether the
data round-trips.

It calls main._attach_profile_key directly rather than re-implementing it.
test_key_lifecycle.py mirrors that function ("# mirrors main._attach_profile_key")
and therefore could not have caught a missing registration -- the mirror would
have been just as wrong, and just as green.

    python test_profile_key_residency.py
"""
import os
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="vai_resid_")
os.environ["VERIDIAN_DATA_DIR"] = _TMP

import atrest                                               # noqa: E402
import profile_keys as pk                                   # noqa: E402
import session as sess                                      # noqa: E402
from cryptography.fernet import Fernet                       # noqa: E402

print("(importing main -- the banner below is the app's, not the test's)")
print("-" * 70)
import main                                                 # noqa: E402
print("-" * 70)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


def _system_key():
    from pathlib import Path
    return (Path(_TMP) / ".atrest_key").read_bytes().strip()


def _system_can_open(blob):
    try:
        Fernet(_system_key()).decrypt(blob)
        return True
    except Exception:
        return False


ALICE = {"username": "alice", "ns": "alice", "is_owner": False}
BOB = {"username": "bob", "ns": "bob", "is_owner": False}
OWNER = {"username": "todd", "ns": None, "is_owner": True}

DEK_A = pk.create_for_profile("alice", "alice-pw-1", recovery=True)
DEK_B = pk.create_for_profile("bob", "bob-pw-1", recovery=True)
atrest.encrypt_bytes(b"seed")            # materialise the system key on disk

print("\n=== 1. A wrong password registers nothing ===")
t_bad = sess.create_session(ALICE)
ok("attach refuses the wrong password",
   main._attach_profile_key(t_bad, "alice", "not-the-password") is False)
ok("and nothing was registered", atrest.has_profile_key("alice") is False)
sess.destroy_session(t_bad)

print("\n=== 2. Login registers the profile key with atrest ===")
ok("not registered before login", atrest.has_profile_key("alice") is False)
t1 = sess.create_session(ALICE)
ok("attach succeeds", main._attach_profile_key(t1, "alice", "alice-pw-1") is True)
ok("session holds the key", sess.get_session_dek(t1) == DEK_A)
ok("AT-REST holds the key", atrest.has_profile_key("alice") is True)

print("\n=== 3. ...and it is HER key, not the system key ===")
blob = atrest.encrypt_bytes(b"alice private note", ns="alice")
ok("the system key cannot open what she wrote", _system_can_open(blob) is False)
ok("her key reads it back",
   atrest.decrypt_bytes(blob, ns="alice") == b"alice private note")
# The control: the same call without ns is system-key work, and must stay so.
ok("ns-less writes still use the system key",
   _system_can_open(atrest.encrypt_bytes(b"system thing")) is True)

print("\n=== 4. The owner has no profile key, and never gets one ===")
t_own = sess.create_session(OWNER)
ok("attach is a no-op for the owner",
   main._attach_profile_key(t_own, None, "whatever") is False)
ok("no owner key registered", atrest.has_profile_key(None) is False)
sess.destroy_session(t_own)

print("\n=== 5. Two sessions: the key survives until the LAST one goes ===")
t2 = sess.create_session(ALICE)
main._attach_profile_key(t2, "alice", "alice-pw-1")
ok("two live sessions hold it", sess.ns_has_live_dek("alice") is True)
sess.destroy_session(t1)
ok("after one logout the key is STILL registered",
   atrest.has_profile_key("alice") is True)
ok("the other tab can still read",
   atrest.decrypt_bytes(blob, ns="alice") == b"alice private note")
sess.destroy_session(t2)
ok("after the last logout it is gone", atrest.has_profile_key("alice") is False)
ok("and no session claims to hold it", sess.ns_has_live_dek("alice") is False)

print("\n=== 6. A locked profile falls back, it does not crash ===")
# Her file is unreadable now -- correct, and it must say so rather than
# returning something wrong.
_opened = True
try:
    atrest.decrypt_bytes(blob, ns="alice")
except Exception:
    _opened = False
ok("her data is unreadable while she is logged out", _opened is False)

print("\n=== 7. Expiry releases the key too ===")
# This was a real leak: expiry dropped the session record and left the data
# key resident in _DEKS forever, so "the key dies with the session" was only
# true for logout.
t3 = sess.create_session(ALICE, ttl=-1)          # already expired
main._attach_profile_key(t3, "alice", "alice-pw-1")
ok("registered while attaching", atrest.has_profile_key("alice") is True)
ok("the expired session reads as gone", sess.get_session(t3) is None)
ok("the data key went with it", sess.get_session_dek(t3) is None)
ok("and at-rest dropped it", atrest.has_profile_key("alice") is False)

print("\n=== 8. destroy_user_sessions releases it ===")
t4 = sess.create_session(ALICE)
main._attach_profile_key(t4, "alice", "alice-pw-1")
ok("registered", atrest.has_profile_key("alice") is True)
sess.destroy_user_sessions("alice")
ok("password change / kick releases the key",
   atrest.has_profile_key("alice") is False)

print("\n=== 9. One profile's logout does not disturb another ===")
ta = sess.create_session(ALICE)
tb = sess.create_session(BOB)
main._attach_profile_key(ta, "alice", "alice-pw-1")
main._attach_profile_key(tb, "bob", "bob-pw-1")
ok("both registered",
   atrest.has_profile_key("alice") and atrest.has_profile_key("bob"))
b_blob = atrest.encrypt_bytes(b"bob note", ns="bob")
sess.destroy_session(ta)
ok("alice released", atrest.has_profile_key("alice") is False)
ok("bob untouched", atrest.has_profile_key("bob") is True)
ok("bob still reads his own", atrest.decrypt_bytes(b_blob, ns="bob") == b"bob note")
ok("bob's key is not alice's", DEK_A != DEK_B)
_cross = True
try:
    atrest.decrypt_bytes(b_blob, ns="alice")
except Exception:
    _cross = False
ok("and alice's namespace cannot open bob's file", _cross is False)
sess.destroy_session(tb)

print("\n=== 10. Signing back in makes it readable again ===")
t5 = sess.create_session(ALICE)
main._attach_profile_key(t5, "alice", "alice-pw-1")
ok("the file she wrote before is readable after a fresh login",
   atrest.decrypt_bytes(blob, ns="alice") == b"alice private note")
sess.destroy_session(t5)

import shutil                                               # noqa: E402
shutil.rmtree(_TMP, ignore_errors=True)

_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
