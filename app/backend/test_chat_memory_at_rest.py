#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_chat_memory_at_rest.py -- the live conversation is encrypted on disk.

THE GAP THIS EXISTS FOR (found 2026-08-13, live through v2.15.0)
----------------------------------------------------------------
save_chat_memory wrote plain JSON:

    _memory_file(ns).write_text(json.dumps(history, indent=2), ...)

_memory_file(ns) serves the owner AND every profile, so every live conversation
sat readable on disk. Archives in the very same folder were encrypted, which is
what made it invisible: the saved history was protected while the conversation
being had was not.

It surfaced through the export. A portable export copies files verbatim, so
chat_memory.json travelled out in the clear no matter what passphrase was set --
the passphrase wrapped the key correctly, and the file it protected had never
been encrypted. Todd found it by testing the export and disbelieving the
explanation, which was the right instinct.

No migration pass: atrest.load_json_auto reads an encrypted blob OR legacy
plaintext, so old files keep opening and are re-written encrypted on next save.

    python test_chat_memory_at_rest.py
"""
import json
import os
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

_TMP = tempfile.mkdtemp(prefix="vai_chatrest_")
os.environ["VERIDIAN_DATA_DIR"] = _TMP

from pathlib import Path                # noqa: E402
import atrest                           # noqa: E402
import profile_keys as pk               # noqa: E402
import sage_engine                      # noqa: E402

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


FERNET = b"gAAAAA"
CONVO = [{"role": "user", "content": "my private medical question"},
         {"role": "assistant", "content": "a private answer"}]
NEEDLE = b"my private medical question"

atrest.encrypt_bytes(b"seed")
NS = "alice"
DEK = pk.create_for_profile(NS, "alice-pw", recovery=True)
atrest.register_profile_key(NS, DEK)


print("=== 1. The owner's conversation is encrypted on disk ===")
sage_engine.save_chat_memory(CONVO)
owner_file = sage_engine._memory_file(None)
raw = Path(owner_file).read_bytes()
ok("the file is a Fernet blob", raw.lstrip()[:6] == FERNET, raw[:24])
ok("the plaintext is NOT on disk", NEEDLE not in raw)
ok("it round-trips", sage_engine.load_chat_memory() == CONVO,
   sage_engine.load_chat_memory())


print("\n=== 2. A profile's conversation uses THAT profile's key ===")
sage_engine.save_chat_memory(CONVO, ns=NS)
pf = Path(sage_engine._memory_file(NS))
praw = pf.read_bytes()
ok("the profile's file is encrypted", praw.lstrip()[:6] == FERNET, praw[:24])
ok("the plaintext is NOT on disk", NEEDLE not in praw)
ok("it round-trips with the profile's key",
   sage_engine.load_chat_memory(ns=NS) == CONVO)
ok("the profile's own key opens it",
   atrest.decrypt_with_profile_key(praw, NS) is not None)
try:
    atrest.decrypt_with_system_key(praw)
    ok("the SYSTEM key does NOT open a profile's conversation", False,
       "system key decrypted it -- profile isolation is not real")
except Exception:
    ok("the SYSTEM key does NOT open a profile's conversation", True)


print("\n=== 3. A pre-v2.15 PLAINTEXT file still loads (no migration pass) ===")
legacy = [{"role": "user", "content": "written before v2.15"}]
pf.write_text(json.dumps(legacy, indent=2), encoding="utf-8")
ok("the legacy file is plaintext to begin with",
   pf.read_bytes().lstrip()[:6] != FERNET)
ok("it still loads", sage_engine.load_chat_memory(ns=NS) == legacy,
   sage_engine.load_chat_memory(ns=NS))

print("\n=== 4. ...and the next save upgrades it in place ===")
sage_engine.save_chat_memory(legacy, ns=NS)
ok("now encrypted", pf.read_bytes().lstrip()[:6] == FERNET)
ok("content survived the upgrade", sage_engine.load_chat_memory(ns=NS) == legacy)
ok("the old plaintext is gone from the file",
   b"written before v2.15" not in pf.read_bytes())


print("\n=== 5. Degenerate files do not throw ===")
pf.write_bytes(b"")
ok("empty file -> []", sage_engine.load_chat_memory(ns=NS) == [])
pf.write_text("{ not valid json", encoding="utf-8")
ok("corrupt file -> [] rather than a crash",
   sage_engine.load_chat_memory(ns=NS) == [])


print("\n=== 6. CRAIID can still read the owner's conversation ===")
# craiid_author reads chat_memory.json directly; if it could not handle the
# encrypted form, the whole CRAIID pipeline would silently see an empty thread.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "craiid"))
import craiid_author                                   # noqa: E402
enc = atrest.dump_json_encrypted(CONVO)
ok("_decode_chat_memory reads the ENCRYPTED form",
   craiid_author._decode_chat_memory(enc) == CONVO)
ok("_decode_chat_memory still reads legacy PLAINTEXT",
   craiid_author._decode_chat_memory(json.dumps(legacy).encode("utf-8")) == legacy)


print("\n=== 7. The export consequence, which is how this was found ===")
# A portable export copies files verbatim. That is correct behaviour -- it is
# only safe because what it copies is now ciphertext.
sage_engine.save_chat_memory(CONVO, ns=NS)
ok("a verbatim copy of the file carries no plaintext",
   NEEDLE not in pf.read_bytes())

_p = sum(1 for _, c in _results if c)
_f = len(_results) - _p
print("\n%d/%d passed." % (_p, len(_results)))
sys.exit(1 if _f else 0)
