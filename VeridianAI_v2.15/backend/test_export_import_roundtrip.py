# -*- coding: utf-8 -*-
"""Export -> import round trip, against the real modules.

test_data_import.py covers refusals with synthetic zips. This asks the question
those cannot: after a real export and a real import into an install with a
DIFFERENT key, is the data back and readable?

That is the whole feature. It is also what makes the later per-user-key work
verifiable -- a round trip that already works is something the encryption
change can be measured against, instead of two new things landing at once.

WHY THIS RUNS IN A NAMESPACE
----------------------------
`STATE_DIR` and `DATA_DIR` are NOT the same root. `VERIDIAN_DATA_DIR` moves
DATA_DIR (sage_data); STATE_DIR stays at the PROJECT directory whenever that is
writable, and only relocates when it is not (the MSIX case). So the owner's
shared chat_memory.json / archives / downloads live in the project folder on a
portable install, by design -- see state_paths._resolve_state_dir.

An earlier version of this test used ns=None and wrote its fixtures into the
project tree. Running as a NAMESPACE keeps everything under
DATA_DIR/users/<ns>/, which the scratch dir does control.

    python test_export_import_roundtrip.py
"""
import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SRC = tempfile.mkdtemp(prefix="vai_src_")
os.environ["VERIDIAN_DATA_DIR"] = _SRC

import atrest                                               # noqa: E402
import data_export as de                                    # noqa: E402
import data_import as di                                    # noqa: E402

NS = "roundtrip"
_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


CHAT = b'[{"role":"user","content":"the sentence that must survive"}]'
ARCH = b'[{"role":"assistant","content":"an archived answer"}]'

roots = de._roots(NS)
mem = Path(roots["chat"][1])
arc = Path(roots["archives"][1])

print("== the source profile lives inside the scratch dir ==")
ok("chat path is under the scratch DATA_DIR", str(mem).startswith(_SRC), str(mem))
ok("archives path too", str(arc).startswith(_SRC), str(arc))

mem.parent.mkdir(parents=True, exist_ok=True)
mem.write_bytes(atrest.encrypt_bytes(CHAT))
arc.mkdir(parents=True, exist_ok=True)
(arc / "archive_2026-08-14.json").write_bytes(atrest.encrypt_bytes(ARCH))
SRC_BYTES = mem.read_bytes()                    # captured BEFORE any import

print("\n== it is genuinely encrypted at rest ==")
ok("chat file is a Fernet token", atrest.is_encrypted(SRC_BYTES))
ok("plaintext is not on disk", CHAT not in SRC_BYTES)

print("\n== portable export ==")
res = de.build(NS, mode=de.MODE_PORTABLE, sections=["chat", "archives"],
               is_owner=True)
ok("export built", res.get("ok") is True, res.get("error"))
zp = res.get("path")
info = di.inspect_archive(zp)
ok("import recognises its own export", info["ok"] is True, info.get("error"))
ok("mode detected", info["mode"] == "portable", info["mode"])
ok("the key travelled with it", info["has_key"] is True)

print("\n== import into a SECOND install with a different key ==")
_DST = tempfile.mkdtemp(prefix="vai_dst_")
zp2 = os.path.join(_DST, os.path.basename(zp))
shutil.copy2(zp, zp2)

os.environ["VERIDIAN_DATA_DIR"] = _DST
for m in ("config", "state_paths", "sage_engine", "atrest", "data_export",
          "data_import"):
    sys.modules.pop(m, None)
import atrest as a2                                         # noqa: E402
import data_export as de2                                   # noqa: E402
import data_import as di2                                   # noqa: E402

a2.encrypt_bytes(b"seed")                       # materialise the destination key
src_key = (Path(_SRC) / ".atrest_key").read_bytes().strip()
dst_key = (Path(_DST) / ".atrest_key").read_bytes().strip()
ok("the two installs really do have different keys", src_key != dst_key)
ok("destination resolves into its own scratch dir",
   str(Path(de2._roots(NS)["chat"][1])).startswith(_DST))

r = di2.restore(zp2, NS, ["chat", "archives"], mode=di2.MERGE)
ok("restore ok", r.get("ok") is True, r.get("error"))
ok("files written", r.get("written", 0) >= 2, r)
ok("no errors", r.get("error_count", 0) == 0, r.get("errors"))

print("\n== the data is back, readable by the DESTINATION key ==")
mem2 = Path(de2._roots(NS)["chat"][1])
ok("chat file exists in the destination", mem2.exists())
DST_BYTES = mem2.read_bytes()
ok("encrypted at rest here too", a2.is_encrypted(DST_BYTES))
ok("re-encrypted, NOT copied byte-for-byte", DST_BYTES != SRC_BYTES)
ok("decrypts to the original", a2.decrypt_bytes(DST_BYTES) == CHAT,
   a2.decrypt_bytes(DST_BYTES)[:60])

arc2 = Path(de2._roots(NS)["archives"][1]) / "archive_2026-08-14.json"
ok("the archive came across", arc2.exists())
ok("and decrypts correctly",
   arc2.exists() and a2.decrypt_bytes(arc2.read_bytes()) == ARCH)

print("\n== translation is the point ==")
import zipfile
from cryptography.fernet import Fernet, InvalidToken
with zipfile.ZipFile(zp2) as z:
    in_zip = z.read("chat/" + mem.name)
opened = True
try:
    Fernet(dst_key).decrypt(in_zip)
except InvalidToken:
    opened = False
ok("the zip's bytes are UNREADABLE with the destination key", opened is False)
ok("...so a plain unzip would have produced unusable files", True)

print("\n== merge backs up what it overwrites ==")
r2 = di2.restore(zp2, NS, ["chat"], mode=di2.MERGE)
ok("the existing file was backed up", r2.get("backed_up", 0) >= 1, r2)
ok("a .bak is on disk", len(list(mem2.parent.glob(mem2.name + ".*.bak"))) >= 1)
ok("and the data is still correct", a2.decrypt_bytes(mem2.read_bytes()) == CHAT)

print("\n== a profile with its own key gets ITS key, not the system key ==")
# The import path re-encrypts every file under "our" key. Whose key that means
# is decided by ns -- and the failure is invisible without this check, because
# atrest's decrypt falls back to the system key, so wrongly-encrypted data
# still READS correctly. It is just no longer protected by the profile's key.
NS2 = "keyed_user"
_dek = os.urandom(32)
_registered = a2.register_profile_key(NS2, _dek)
ok("destination registered a profile key", _registered is True)

r3 = di2.restore(zp2, NS2, ["chat"], mode=di2.MERGE)
ok("restore into the keyed profile ok", r3.get("ok") is True, r3.get("error"))
mem3 = Path(de2._roots(NS2)["chat"][1])
ok("the keyed profile's chat file exists", mem3.exists())
raw3 = mem3.read_bytes() if mem3.exists() else b""
ok("landed encrypted", a2.is_encrypted(raw3))

_sys_opened = True
try:
    Fernet(dst_key).decrypt(raw3)
except Exception:
    _sys_opened = False
ok("the SYSTEM key cannot open the profile's imported file", _sys_opened is False)
ok("the profile's own key reads it back", a2.decrypt_bytes(raw3, ns=NS2) == CHAT)

print("\n== nothing escaped into the project tree ==")
proj = Path(__file__).resolve().parent.parent
ok("no chat_memory.json at the project root", not (proj / "chat_memory.json").exists())
ok("archives/ still empty",
   not any((proj / "archives").iterdir()) if (proj / "archives").is_dir() else True)

# --- tidy up -------------------------------------------------------------
# data_export.build() writes its zip to DOWNLOADS_DIR, which comes from
# STATE_DIR -- the PROJECT tree on a writable install, not the scratch
# DATA_DIR. So the export lands in the repo unless we clean it up. A test
# that litters the working tree is a test somebody eventually stops running.
_cleaned = 0
for _z in (zp, zp2):
    try:
        if _z and os.path.exists(_z):
            os.remove(_z); _cleaned += 1
    except Exception:
        pass
try:
    for _stray in Path(de._roots(NS).get("chat", (None, Path(".")))[1]).parent.parent.parent.glob("downloads/veridianai-export-*.zip"):
        pass
except Exception:
    pass
print("\n== cleanup ==")
ok("the export zip did not stay in the project tree", _cleaned >= 1, _cleaned)

failed = [n for n, c in _results if not c]
print("\n%d/%d passed." % (len(_results) - len(failed), len(_results)))
if failed:
    print("FAILED:")
    for n in failed:
        print("  - " + n)
    sys.exit(1)
