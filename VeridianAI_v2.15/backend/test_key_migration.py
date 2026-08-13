#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_key_migration.py -- the first-unlock conversion, including its refusals.

The interesting assertions here are the negative ones. A migration that
converts files is easy to write and easy to believe; what makes it safe to run
against somebody's only copy of their data is that it declines to touch
anything it does not fully understand, and that a failure leaves the original
byte-for-byte intact.

    python test_key_migration.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="vai_keymig_")
os.environ["VERIDIAN_DATA_DIR"] = _TMP

from pathlib import Path                                     # noqa: E402
from cryptography.fernet import Fernet                        # noqa: E402

import atrest                                                # noqa: E402
import key_migration as km                                   # noqa: E402
import profile_keys as pk                                    # noqa: E402
import sage_engine                                           # noqa: E402

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


NS = "alice"
atrest.encrypt_bytes(b"seed")                    # materialise the system key
SYS_KEY = (Path(_TMP) / ".atrest_key").read_bytes().strip()


def sys_opens(blob):
    try:
        Fernet(SYS_KEY).decrypt(blob)
        return True
    except Exception:
        return False


def her_opens(blob):
    try:
        atrest.decrypt_with_profile_key(blob, NS)
        return True
    except Exception:
        return False


DEK = pk.create_for_profile(NS, "alice-pw", recovery=True)
ROOT = Path(sage_engine.user_data_dir(NS))
ROOT.mkdir(parents=True, exist_ok=True)
(ROOT / "archives").mkdir(exist_ok=True)

# --- the fixture: one of each kind of file -------------------------------
CHAT = b'[{"role": "user", "content": "my medical history"}]'
ARCH = b'{"messages": ["an archived conversation"]}'
PLAIN = b"a note nobody ever encrypted"
FOREIGN = Fernet(Fernet.generate_key()).encrypt(b"encrypted with a third key")

(ROOT / "chat_memory.json").write_bytes(atrest.encrypt_bytes(CHAT))
(ROOT / "archives" / "archive_2026-08-01.json").write_bytes(atrest.encrypt_bytes(ARCH))
(ROOT / "notes.txt").write_bytes(PLAIN)
(ROOT / "foreign.dat").write_bytes(FOREIGN)

# Outside the profile: the owner's own store, which must never be touched.
OWNER_FILE = Path(_TMP) / "chat_memory.json"
OWNER_FILE.write_bytes(atrest.encrypt_bytes(b"the owner's conversation"))
OWNER_BEFORE = OWNER_FILE.read_bytes()

print("=== 1. It refuses to run without the key ===")
r = km.run(NS)
ok("declines when the profile is locked", r.get("ok") is False, r)
ok("and says why", "not unlocked" in str(r.get("error", "")), r)
ok("nothing was converted", r.get("converted", 0) == 0)
ok("the files are untouched",
   (ROOT / "chat_memory.json").read_bytes() != b"" and
   sys_opens((ROOT / "chat_memory.json").read_bytes()))

print("\n=== 2. plan() writes nothing ===")
atrest.register_profile_key(NS, DEK)
p = km.plan(NS)
ok("plan sees the two convertible files", p.get("converted") == 2, p)
ok("plan counts the plaintext one", p.get("plaintext") == 1, p)
ok("plan counts the unopenable one", p.get("unreadable") == 1, p)
ok("still under the system key afterwards",
   sys_opens((ROOT / "chat_memory.json").read_bytes()))
ok("plan left no sentinel", km.is_done(NS) is False)

print("\n=== 3. run() converts, and the result is HERS ===")
r = km.run(NS)
ok("ok", r.get("ok") is True, r)
ok("converted 2", r.get("converted") == 2, r)
chat_after = (ROOT / "chat_memory.json").read_bytes()
arch_after = (ROOT / "archives" / "archive_2026-08-01.json").read_bytes()
ok("chat opens with her key", her_opens(chat_after))
ok("chat does NOT open with the system key", sys_opens(chat_after) is False)
ok("archive opens with her key", her_opens(arch_after))
ok("archive does NOT open with the system key", sys_opens(arch_after) is False)
ok("chat content is unchanged", atrest.decrypt_with_profile_key(chat_after, NS) == CHAT)
ok("archive content is unchanged",
   atrest.decrypt_with_profile_key(arch_after, NS) == ARCH)

print("\n=== 4. What it declined to touch ===")
ok("the plaintext file is still exactly as it was",
   (ROOT / "notes.txt").read_bytes() == PLAIN)
ok("and was counted, not hidden", r.get("plaintext") == 1, r)
ok("the third-key file is byte-identical",
   (ROOT / "foreign.dat").read_bytes() == FOREIGN)
ok("and was counted as unreadable", r.get("unreadable") == 1, r)
ok("the keywrap was not re-keyed",
   (ROOT / ".keywrap.json").exists() and
   json.loads((ROOT / ".keywrap.json").read_text(encoding="utf-8")) is not None)

print("\n=== 5. Scope: nothing outside the profile directory ===")
ok("the owner's store is untouched", OWNER_FILE.read_bytes() == OWNER_BEFORE)
ok("and still opens with the system key", sys_opens(OWNER_FILE.read_bytes()))

print("\n=== 6. It leaves no temporary files behind ===")
strays = [str(f) for f in ROOT.rglob("*" + km._TMP_SUFFIX)]
ok("no .keymig.tmp anywhere", not strays, strays)

print("\n=== 7. Idempotent: a second run converts nothing ===")
ok("marked done", km.is_done(NS) is True)
r2 = km.run(NS)
ok("second run converts 0", r2.get("converted") == 0, r2)
ok("and recognises them as already hers", r2.get("already") == 2, r2)
ok("content survived the second pass",
   atrest.decrypt_with_profile_key((ROOT / "chat_memory.json").read_bytes(), NS) == CHAT)

print("\n=== 8. A failed write leaves the original intact ===")
NS2 = "bob"
DEK2 = pk.create_for_profile(NS2, "bob-pw", recovery=True)
atrest.register_profile_key(NS2, DEK2)
ROOT2 = Path(sage_engine.user_data_dir(NS2))
ROOT2.mkdir(parents=True, exist_ok=True)
BOB = b"bob's only copy of something important"
(ROOT2 / "chat_memory.json").write_bytes(atrest.encrypt_bytes(BOB))
BEFORE = (ROOT2 / "chat_memory.json").read_bytes()

_real_encrypt = atrest.encrypt_bytes
atrest.encrypt_bytes = lambda data, ns=None: b"gAAAAAnot-a-real-token"
try:
    r3 = km.run(NS2)
finally:
    atrest.encrypt_bytes = _real_encrypt

ok("the run reports failure", r3.get("ok") is False, r3)
ok("counted as failed", r3.get("failed") == 1, r3)
ok("THE ORIGINAL IS BYTE-IDENTICAL",
   (ROOT2 / "chat_memory.json").read_bytes() == BEFORE)
ok("and still readable", atrest.decrypt_with_system_key(BEFORE) == BOB)
ok("no sentinel, so it will try again", km.is_done(NS2) is False)
ok("no temp file left", not list(ROOT2.rglob("*" + km._TMP_SUFFIX)))

print("\n=== 9. And then it succeeds on the retry ===")
r4 = km.run(NS2)
ok("retry converts it", r4.get("converted") == 1, r4)
ok("now under bob's key",
   atrest.decrypt_with_profile_key((ROOT2 / "chat_memory.json").read_bytes(), NS2) == BOB)
ok("marked done", km.is_done(NS2) is True)

print("\n=== 10. The real login hook, not a re-implementation ===")
print("(importing main -- the banner below is the app's)")
print("-" * 70)
import main                                                  # noqa: E402
print("-" * 70)
NS3 = "carol"
DEK3 = pk.create_for_profile(NS3, "carol-pw", recovery=True)
atrest.register_profile_key(NS3, DEK3)
ROOT3 = Path(sage_engine.user_data_dir(NS3))
ROOT3.mkdir(parents=True, exist_ok=True)
CAROL = b"carol's conversation"
(ROOT3 / "chat_memory.json").write_bytes(atrest.encrypt_bytes(CAROL))

m1 = main._migrate_profile_key_once(None, NS3)
ok("the login hook reports what it did", bool(m1) and m1.get("converted") == 1, m1)
ok("it produced a line a person can read",
   isinstance(m1.get("summary"), str) and "converted" in m1["summary"], m1)
ok("carol's file is now under her key",
   atrest.decrypt_with_profile_key((ROOT3 / "chat_memory.json").read_bytes(), NS3) == CAROL)
m2 = main._migrate_profile_key_once(None, NS3)
ok("a second login says nothing", m2 is None, m2)
ok("the owner is never migrated", main._migrate_profile_key_once(None, None) is None)

shutil.rmtree(_TMP, ignore_errors=True)

_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
