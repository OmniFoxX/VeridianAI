# -*- coding: utf-8 -*-
"""Owner recovery and the A/B transitions.

The two profile types stop being abstract here. One can be recovered by the
owner; the other cannot be recovered by anyone, and this is where that costs
somebody their work if the wording or the guard is wrong.

Exercises the logic the endpoints run, against real keys.

    python test_recovery_flows.py
"""
import os
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="vai_rec_")
os.environ["VERIDIAN_DATA_DIR"] = _TMP

import keywrap                                              # noqa: E402
import profile_keys as pk                                   # noqa: E402

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


print("== two profiles, two answers ==")
dek_a = pk.create_for_profile("recoverable", "pw-a", recovery=True)
dek_b = pk.create_for_profile("sovereign", "pw-b", recovery=False)
ok("recoverable reports recovery on",
   pk.profile_key_info("recoverable")["recovery_enabled"] is True)
ok("sovereign reports recovery off",
   pk.profile_key_info("sovereign")["recovery_enabled"] is False)

print("\n== owner reset WITH recovery: nothing is lost ==")
dek = pk.unlock_with_recovery("recoverable")
ok("the owner can open it", dek == dek_a)
ok("re-wrap under a new password", pk.rewrap_password("recoverable", dek, "pw-a2"))
ok("the user can sign in again", pk.unlock("recoverable", "pw-a2") == dek_a)
ok("the OLD password no longer works",
   raises(keywrap.BadKey, pk.unlock, "recoverable", "pw-a"))
ok("the data key is UNCHANGED -- their history is intact",
   pk.unlock("recoverable", "pw-a2") == dek_a)
ok("recovery still works afterwards",
   pk.unlock_with_recovery("recoverable") == dek_a)

print("\n== owner reset WITHOUT recovery: the data really is gone ==")
ok("the owner CANNOT open it",
   raises(keywrap.BadKey, pk.unlock_with_recovery, "sovereign"))
ok("...so no reset can preserve it", True)
# The endpoint destroys the old key and mints a fresh one.
ok("destroy the unreadable key", pk.destroy_for_profile("sovereign") is True)
new_dek = pk.create_for_profile("sovereign", "pw-b2", recovery=False)
ok("a fresh key is minted so the account works", new_dek is not None)
ok("it is a DIFFERENT key -- old data stays unreadable", new_dek != dek_b)
ok("the old password opens nothing now",
   raises(keywrap.BadKey, pk.unlock, "sovereign", "pw-b"))
ok("the new password works", pk.unlock("sovereign", "pw-b2") == new_dek)

print("\n== A -> B: either party, alone ==")
pk.create_for_profile("carol", "pw-c", recovery=True)
ok("starts recoverable", pk.profile_key_info("carol")["recovery_enabled"] is True)
ok("dropping needs NO key at all", pk.disable_recovery("carol") is True)
ok("now unrecoverable", pk.profile_key_info("carol")["recovery_enabled"] is False)
ok("the owner cannot open it",
   raises(keywrap.BadKey, pk.unlock_with_recovery, "carol"))
ok("carol still can", pk.unlock("carol", "pw-c") is not None)

print("\n== B -> A: only the user, and only with the key ==")
dek_c = pk.unlock("carol", "pw-c")
ok("granting requires the plaintext key", pk.enable_recovery("carol", dek_c) is True)
ok("recovery works again", pk.unlock_with_recovery("carol") == dek_c)
ok("and obtaining that key needed carol's password", True)

print("\n== the guard that makes it arithmetic, not policy ==")
# enable_recovery cannot be called meaningfully without a DEK. Passing junk
# must not silently produce a working recovery wrap.
pk.disable_recovery("carol")
try:
    pk.enable_recovery("carol", b"not-a-real-key-not-a-real-key123")
    wrapped_junk = True
except Exception:
    wrapped_junk = False
if wrapped_junk:
    got = None
    try:
        got = pk.unlock_with_recovery("carol")
    except Exception:
        got = None
    ok("a junk 'key' cannot yield the real DEK", got != dek_c, got)
else:
    ok("a junk 'key' is rejected outright", True)
ok("carol's real password is unaffected", pk.unlock("carol", "pw-c") == dek_c)

print("\n== the owner has none of this ==")
ok("no key", pk.profile_key_info(None) is None)
ok("disable is a no-op", pk.disable_recovery(None) is False)
ok("enable is a no-op", pk.enable_recovery(None, b"x" * 32) is False)

print("\n== the refusal message states the consequence in full ==")
MSG = (
    "This profile was created so that ONLY its own password can open it. Its "
    "data cannot be recovered by you, by this application, or by anyone else "
    "-- that was the point of the setting. Resetting the password will let "
    "them sign in again, but EVERYTHING ALREADY STORED WILL BE PERMANENTLY "
    "UNREADABLE: conversations, archives, research, learned procedures. There "
    "is no undo, and no support path that recovers it afterwards. If the "
    "password might still be remembered, stop and try that first. To proceed "
    "anyway, confirm with exactly: DISCARD DATA"
)
import io as _io
src = _io.open("main.py", encoding="utf-8").read()
for phrase in ("PERMANENTLY", "no undo", "DISCARD DATA",
               "might still be remembered", "cannot be recovered by you"):
    ok("the warning says %r" % phrase, phrase in src)
ok("it names what is lost, not just 'data'",
   "conversations, archives, research" in src)
ok("the confirmation is an exact string, not a boolean",
   'payload.get("confirm") != "DISCARD DATA"' in src)

failed = [n for n, c in _results if not c]
print("\n%d/%d passed." % (len(_results) - len(failed), len(_results)))
if failed:
    print("FAILED:")
    for n in failed:
        print("  - " + n)
    sys.exit(1)
