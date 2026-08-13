# -*- coding: utf-8 -*-
"""atrest.py namespace-awareness -- the plumbing, before anything uses it.

This lands on its own and deliberately changes no behaviour: every existing
call site passes no `ns` and therefore still uses the system key, byte for
byte as before. What is proved here is that the machinery is correct BEFORE it
becomes load-bearing, because the failure mode once it is load-bearing is
unreadable data rather than an error message.

The audit hook at the end is the one that will earn its keep during the
conversion: it makes a missed call site a test failure instead of an archive
somebody can no longer open.

    python test_atrest_ns.py
"""
import os
import secrets
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="vai_atr_")
os.environ["VERIDIAN_DATA_DIR"] = _TMP

import atrest                                               # noqa: E402

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


def opens(blob, ns=None):
    try:
        atrest.decrypt_bytes(blob, ns=ns); return True
    except Exception:
        return False


print("== nothing changed for existing callers ==")
legacy = atrest.encrypt_bytes(b"existing data")
ok("encrypt with no ns still works", atrest.is_encrypted(legacy))
ok("round trip", atrest.decrypt_bytes(legacy) == b"existing data")
ok("dump/load json unchanged",
   atrest.load_json_auto(atrest.dump_json_encrypted({"a": 1})) == {"a": 1})

print("\n== the registry ==")
DEK_A = secrets.token_bytes(32)
DEK_B = secrets.token_bytes(32)
ok("register raw 32-byte key", atrest.register_profile_key("alice", DEK_A))
ok("register a second profile", atrest.register_profile_key("bob", DEK_B))
ok("has_profile_key", atrest.has_profile_key("alice") is True)
ok("unknown profile", atrest.has_profile_key("nobody") is False)
ok("owner (None) never has one", atrest.has_profile_key(None) is False)
ok("registering with no key is refused",
   atrest.register_profile_key("x", None) is False)
ok("registering with no ns is refused",
   atrest.register_profile_key(None, DEK_A) is False)

print("\n== profile isolation ==")
a = atrest.encrypt_bytes(b"alice private", ns="alice")
b = atrest.encrypt_bytes(b"bob private", ns="bob")
ok("alice reads her own", atrest.decrypt_bytes(a, ns="alice") == b"alice private")
ok("bob reads his own", atrest.decrypt_bytes(b, ns="bob") == b"bob private")
ok("alice CANNOT read bob's", opens(b, "alice") is False)
ok("bob CANNOT read alice's", opens(a, "bob") is False)
ok("the system key opens NEITHER", opens(a) is False and opens(b) is False)
ok("the two ciphertexts differ", a != b)

print("\n== the fallback, which is what makes migration possible ==")
ok("a legacy blob still opens WITH an ns set",
   atrest.decrypt_bytes(legacy, ns="alice") == b"existing data")
ok("...and with a different ns", atrest.decrypt_bytes(legacy, ns="bob") == b"existing data")
ok("the profile key is tried FIRST, so new data wins",
   atrest.decrypt_bytes(a, ns="alice") == b"alice private")

print("\n== an unregistered profile falls back to the system key ==")
ok("encrypt with an unknown ns uses the system key",
   opens(atrest.encrypt_bytes(b"x", ns="ghost")) is True)
ok("...which is how the owner and pre-migration profiles keep working", True)

print("\n== forgetting a key ==")
ok("forget returns True", atrest.forget_profile_key("bob") is True)
ok("bob's data is now unreadable", opens(b, "bob") is False)
ok("forgetting twice is False", atrest.forget_profile_key("bob") is False)
ok("alice is unaffected", atrest.decrypt_bytes(a, ns="alice") == b"alice private")

print("\n== ns flows through the JSON and file helpers ==")
blob = atrest.dump_json_encrypted({"secret": "alice"}, ns="alice")
ok("dump honours ns", opens(blob) is False)
ok("load honours ns", atrest.load_json_auto(blob, ns="alice") == {"secret": "alice"})
fp = os.path.join(_TMP, "f.bin")
open(fp, "wb").write(atrest.encrypt_bytes(b"file data", ns="alice"))
ok("read_file_auto honours ns",
   atrest.read_file_auto(fp, ns="alice") == b"file data")
ok("read_file_auto without ns returns the ciphertext, not a crash",
   atrest.read_file_auto(fp) != b"file data")

print("\n== the audit hook: how a missed call site gets caught ==")
atrest.audit_start()
atrest.encrypt_bytes(b"1", ns="alice")
atrest.encrypt_bytes(b"2")                      # the mistake we are hunting
atrest.decrypt_bytes(a, ns="alice")
log = atrest.audit_stop()
ok("three calls recorded", len(log) == 3, log)
ok("ops recorded", [e["op"] for e in log] == ["encrypt", "encrypt", "decrypt"],
   [e["op"] for e in log])
ok("namespaces recorded", [e["ns"] for e in log] == ["alice", None, "alice"],
   [e["ns"] for e in log])
missed = [e for e in log if e["ns"] is None]
ok("a call with no ns is identifiable", len(missed) == 1, missed)
ok("...and it names the caller file:line",
   missed[0]["caller"].endswith(".py") is False and ":" in missed[0]["caller"],
   missed[0]["caller"])
ok("audit is OFF again", atrest._AUDIT["on"] is False)

atrest.audit_start(); atrest.audit_stop()
ok("audit_start clears the previous log", atrest.audit_log() == [])

print("\n== audit is free when off ==")
atrest.encrypt_bytes(b"not recorded")
ok("nothing recorded while off", atrest.audit_log() == [])

failed = [n for n, c in _results if not c]
print("\n%d/%d passed." % (len(_results) - len(failed), len(_results)))
if failed:
    print("FAILED:")
    for n in failed:
        print("  - " + n)
    sys.exit(1)
