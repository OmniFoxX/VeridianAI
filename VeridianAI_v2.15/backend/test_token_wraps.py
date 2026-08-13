# -*- coding: utf-8 -*-
"""API tokens carrying their own way into a profile's data.

An API caller has no session and no password. Without a wrap, a bearer token
authenticates successfully and then cannot decrypt anything it is entitled to
read -- a failure that looks like corruption rather than a missing step. This
is the piece the design flagged as most likely to be discovered late.

    python test_token_wraps.py
"""
import os
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="vai_tw_")
os.environ["VERIDIAN_DATA_DIR"] = _TMP

import keywrap                                              # noqa: E402
import profile_keys as pk                                   # noqa: E402
import auth                                                 # noqa: E402
from pathlib import Path                                    # noqa: E402
auth.KEYSTORE_PATH = Path(_TMP) / ".api_keystore.json"
auth._save_keystore(auth._empty_keystore())

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


print("== a token can open the profile it belongs to ==")
DEK = pk.create_for_profile("alice", "alice-pw", recovery=True)
tok = auth.issue_token("alice", "editor", ["chat:*"])
ok("wrap added at issue time", pk.add_token_wrap("alice", DEK, tok[:8], tok))
ok("the token opens the data key",
   pk.unlock_with_token("alice", tok[:8], tok) == DEK)
ok("...which is the SAME key the password opens",
   pk.unlock("alice", "alice-pw") == DEK)

print("\n== and only that token ==")
ok("a wrong token does not",
   raises(keywrap.BadKey, pk.unlock_with_token, "alice", tok[:8], "ora_wrong"))
ok("an unknown prefix does not",
   raises(keywrap.BadKey, pk.unlock_with_token, "alice", "ora_zzzz", tok))

print("\n== cross-profile ==")
DEK_B = pk.create_for_profile("bob", "bob-pw", recovery=False)
tok_b = auth.issue_token("bob", "bob key", ["*"])
pk.add_token_wrap("bob", DEK_B, tok_b[:8], tok_b)
ok("bob's token opens bob's key",
   pk.unlock_with_token("bob", tok_b[:8], tok_b) == DEK_B)
ok("bob's token cannot open alice's profile",
   raises(keywrap.BadKey, pk.unlock_with_token, "alice", tok_b[:8], tok_b))
ok("alice's token cannot open bob's",
   raises(keywrap.BadKey, pk.unlock_with_token, "bob", tok[:8], tok))
ok("the two keys are different", DEK != DEK_B)

print("\n== revoking a key takes its access with it ==")
ok("wrap dropped", pk.drop_token_wrap("alice", tok[:8]) is True)
ok("the token can no longer open anything",
   raises(keywrap.BadKey, pk.unlock_with_token, "alice", tok[:8], tok))
ok("alice's password still works", pk.unlock("alice", "alice-pw") == DEK)

print("\n== rotation clears every wrap for that profile ==")
t1 = auth.issue_token("alice", "k1", ["*"])
t2 = auth.issue_token("alice", "k2", ["*"])
pk.add_token_wrap("alice", DEK, t1[:8], t1)
pk.add_token_wrap("alice", DEK, t2[:8], t2)
ok("two wraps present", len(pk.profile_key_info("alice")["token_wraps"]) == 2,
   pk.profile_key_info("alice")["token_wraps"])
dropped = pk.clear_token_wraps("alice")
ok("both cleared", sorted(dropped) == sorted([t1[:8], t2[:8]]), dropped)
ok("neither opens anything now",
   raises(keywrap.BadKey, pk.unlock_with_token, "alice", t1[:8], t1)
   and raises(keywrap.BadKey, pk.unlock_with_token, "alice", t2[:8], t2))
ok("clearing again returns nothing", pk.clear_token_wraps("alice") == [])

print("\n== a password change invalidates token wraps ==")
t3 = auth.issue_token("alice", "k3", ["*"])
pk.add_token_wrap("alice", DEK, t3[:8], t3)
pk.rewrap_password("alice", pk.unlock("alice", "alice-pw"), "alice-pw2")
ok("the wrap is gone, not silently broken",
   pk.profile_key_info("alice")["token_wraps"] == [],
   pk.profile_key_info("alice")["token_wraps"])
ok("the token no longer opens the profile",
   raises(keywrap.BadKey, pk.unlock_with_token, "alice", t3[:8], t3))
ok("the new password does", pk.unlock("alice", "alice-pw2") == DEK)

print("\n== the owner has no wraps to manage ==")
otok = auth.issue_token(None, "owner key", ["*"])
ok("add is a no-op", pk.add_token_wrap(None, b"x" * 32, otok[:8], otok) is False)
ok("drop is a no-op", pk.drop_token_wrap(None, otok[:8]) is False)
ok("clear is empty", pk.clear_token_wraps(None) == [])
ok("the token still verifies fine",
   (auth._verify_token(otok, auth._load_keystore()) or {}).get("owner_ns") is None)

print("\n== destroying the profile takes every wrap with it ==")
t4 = auth.issue_token("bob", "k4", ["*"])
pk.add_token_wrap("bob", DEK_B, t4[:8], t4)
ok("wrap present", pk.profile_key_info("bob")["token_wraps"] == [t4[:8], tok_b[:8]]
   or len(pk.profile_key_info("bob")["token_wraps"]) == 2)
pk.destroy_for_profile("bob")
ok("no key, so no wraps", pk.profile_key_info("bob") is None)
ok("the token cannot open anything",
   pk.unlock_with_token("bob", t4[:8], t4) is None)

print("\n== the wiring exists where it must ==")
import io as _io
MAIN = _io.open("main.py", encoding="utf-8").read()
AUTH = _io.open("auth.py", encoding="utf-8").read()
ok("keys/create adds a wrap", "_pk.add_token_wrap(ns, _dek, token[:8], token)" in MAIN)
ok("keys/revoke drops one", "_pk.drop_token_wrap(ns, prefix)" in MAIN)
ok("rotation clears then re-adds",
   "_pk.clear_token_wraps(ns)" in MAIN and MAIN.count("add_token_wrap") >= 2)
ok("require_scope unwraps from the token", "unlock_with_token" in AUTH)
ok("only the KEY is published, never the token",
   "request.state.api_dek" in AUTH and "request.state.api_token" not in AUTH)
ok("main exposes one accessor for both doors", "_request_dek" in MAIN)

failed = [n for n, c in _results if not c]
print("\n%d/%d passed." % (len(_results) - len(failed), len(_results)))
if failed:
    print("FAILED:")
    for n in failed:
        print("  - " + n)
    sys.exit(1)
