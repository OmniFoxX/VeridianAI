#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_export_containment.py -- a profile's export contains only that profile.

THE BUG THIS EXISTS FOR (found 2026-08-13, live in v2.14.2)
-----------------------------------------------------------
data_export._roots(ns) resolved chat, archives and evidence per-profile and
then fell through to SHARED paths for everything else. So a non-owner pressing
"export my data" received, in plain text:

    the owner's memory chain, the owner's procedural memory, the owner's
    uploads, config.json -- and, because exports are written into the shared
    downloads folder, any previous PORTABLE export nested inside, which
    carries KEY/fernet.key: the app-wide key protecting every profile.

One button, complete compromise of at-rest encryption for everyone. It passed
review because the first three sections ARE correctly namespaced, so the code
reads right until you follow the fourth.

The rule now is one line -- a profile exports what is inside its own directory
-- and it is enforced twice: by construction, and by a path check that a later
edit cannot get around by adding a section.

    python test_export_containment.py
"""
import os
import shutil
import sys
import tempfile
import warnings
import zipfile

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="vai_exportc_")
os.environ["VERIDIAN_DATA_DIR"] = _TMP

from pathlib import Path                                     # noqa: E402
import atrest                                                # noqa: E402
import data_export as de                                     # noqa: E402
import profile_keys as pk                                    # noqa: E402
import sage_engine                                           # noqa: E402
from config import MEMORY_DIR, PROCEDURAL_DIR, UPLOAD_DIR     # noqa: E402

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


atrest.encrypt_bytes(b"seed")
NS = "alice"
DEK = pk.create_for_profile(NS, "alice-pw", recovery=True)
atrest.register_profile_key(NS, DEK)

AROOT = Path(sage_engine.user_data_dir(NS))
(AROOT / "uploads").mkdir(parents=True, exist_ok=True)
(AROOT / "chat_memory.json").write_bytes(atrest.encrypt_bytes(b'["alice"]', ns=NS))
(AROOT / "uploads" / "alice_note.txt").write_bytes(b"alice's own upload")

OWNER_SECRETS = {
    Path(MEMORY_DIR) / "memory_chain.log": b"OWNER CHAIN ENTRY",
    Path(PROCEDURAL_DIR) / "procedural.json": b'{"owner": "private procedure"}',
    Path(UPLOAD_DIR) / "owner_scan.txt": b"OWNER LAB RESULT",
}
for p, blob in OWNER_SECRETS.items():
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(blob)

print("=== 1. The owner takes a portable export (it carries the app-wide key) ===")
own_portable = de.build(None, "portable", ["chat"], True)
ok("owner portable export built", own_portable.get("ok") is True, own_portable)
with zipfile.ZipFile(own_portable["path"]) as z:
    ok("it really does contain the key", "KEY/fernet.key" in z.namelist())

print("\n=== 2. A profile's sections are all inside its own directory ===")
roots = de._roots(NS)
outside = {k: str(v[1]) for k, v in roots.items()
           if not de._within(AROOT, v[1])}
ok("no section escapes the profile directory", not outside, outside)
ok("the shared sections are absent entirely",
   not ({"memory_chain", "config", "docs", "snapshots"} & set(roots)),
   sorted(roots))
ok("but she still gets her own uploads/downloads/procedural",
   {"uploads", "downloads", "procedural"} <= set(roots), sorted(roots))

print("\n=== 3. Her export carries none of the owner's material ===")
al = de.build(NS, "readable", None, False)
ok("her export built", al.get("ok") is True, al)
with zipfile.ZipFile(al["path"]) as z:
    names = z.namelist()
    blob = b"".join(z.read(n) for n in names if not n.endswith("/"))
for secret in (b"OWNER CHAIN ENTRY", b"private procedure", b"OWNER LAB RESULT"):
    ok("absent from her export: %s" % secret.decode(), secret not in blob)
ok("no nested export zip", not [n for n in names
                                if n.startswith("downloads/") and n.endswith(".zip")],
   names)
ok("no config.json", not [n for n in names if n.startswith("config/")], names)
ok("her own data IS there", any("alice_note" in n for n in names), names)

print("\n=== 4. The owner is not impoverished by the fix ===")
own = de.build(None, "readable", None, True)
with zipfile.ZipFile(own["path"]) as z:
    onames = z.namelist()
ok("the owner still exports the memory chain",
   any("memory_chain" in n for n in onames))
ok("and settings", any(n.startswith("config/") for n in onames))
ok("but no longer nests previous exports inside",
   not [n for n in onames if n.startswith("downloads/") and n.endswith(".zip")],
   [n for n in onames if n.startswith("downloads/")])

print("\n=== 5. Inventory tells the same story as the export ===")
inv_sections = {s["key"] for s in de.inventory(NS)["sections"]}
ok("inventory offers her exactly the contained sections",
   inv_sections == set(roots), (sorted(inv_sections), sorted(roots)))
ok("inventory never advertises the memory chain to a profile",
   "memory_chain" not in inv_sections)

print("\n=== 6. The containment check itself is not a no-op ===")
ok("_within accepts a real child", de._within(AROOT, AROOT / "uploads") is True)
ok("_within rejects a sibling", de._within(AROOT, Path(MEMORY_DIR)) is False)
ok("_within rejects a traversal",
   de._within(AROOT, AROOT / ".." / "bob") is False)

shutil.rmtree(_TMP, ignore_errors=True)

_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
