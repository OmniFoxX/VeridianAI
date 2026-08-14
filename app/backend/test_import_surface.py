#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_import_surface.py -- import is reachable, and it is two steps.

data_import.py had 72 passing tests and no endpoint and no button, so a person
could take their data out and had no way to put it back. A library nobody can
call is not a feature; it is a plan.

Restoring is also the only operation on this surface that can overwrite
someone's history, so it does not happen as a side effect of choosing a file:
the archive is staged and inspected first, and nothing is written until a
second, explicit call. These tests hold that shape in place.

    python test_import_surface.py
"""
import os
import shutil
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="vai_impsurf_")
os.environ["VERIDIAN_DATA_DIR"] = _TMP

from pathlib import Path                                     # noqa: E402
import atrest                                                # noqa: E402
import data_export as de                                     # noqa: E402
import data_import as di                                     # noqa: E402
import keywrap                                               # noqa: E402
import profile_keys as pk                                    # noqa: E402
import sage_engine                                           # noqa: E402

print("(importing main -- the app banner follows)")
print("-" * 70)
import main                                                  # noqa: E402
from fastapi.testclient import TestClient                     # noqa: E402
print("-" * 70)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


atrest.encrypt_bytes(b"seed")
SECRET = b'[{"role": "user", "content": "the only copy of this"}]'

# NOTE, and it cost a stray file in the install tree to learn:
# VERIDIAN_DATA_DIR moves DATA_DIR, but the OWNER's chat, archives and evidence
# ledger follow STATE_DIR, which is PROJECT_DIR whenever PROJECT_DIR is
# writable. So writing to the owner store from a test writes into the real
# checkout. Everything here that would WRITE therefore targets a profile
# namespace (which does live under DATA_DIR); the owner path is exercised with
# dry runs, which write nothing anywhere.
MIGRANT = "migrant"
MDEK = pk.create_for_profile(MIGRANT, "migrant-pw", recovery=True)
atrest.register_profile_key(MIGRANT, MDEK)
MROOT = Path(sage_engine.user_data_dir(MIGRANT))
MROOT.mkdir(parents=True, exist_ok=True)
MEM = MROOT / "chat_memory.json"
MEM.write_bytes(atrest.encrypt_bytes(SECRET, ns=MIGRANT))

# The loopback guard is real and doing its job; TestClient reports its host as
# "testclient". Present as loopback rather than weakening the guard for tests.
client = TestClient(main.app, client=("127.0.0.1", 50000))


def stage(path):
    with open(path, "rb") as fh:
        return client.post("/api/import/inspect",
                           files={"file": (Path(path).name, fh.read(),
                                           "application/zip")}).json()


print("\n=== 1. Export, wipe, put it back ===")
exp = de.build(MIGRANT, "portable", ["chat"], False, export_key=MDEK)
ok("portable export built", exp.get("ok") is True, exp)
MEM.unlink()
info = stage(exp["path"])
ok("inspect succeeds", info.get("ok") is True, info.get("error"))
ok("it names what is inside", [s["key"] for s in info.get("sections", [])] == ["chat"],
   info.get("sections"))
ok("INSPECT ALONE WRITES NOTHING", not MEM.exists())
ok("and hands back a staging id", bool(info.get("stage_id")))

dry = client.post("/api/import", json={"stage_id": info["stage_id"],
                                       "sections": ["chat"], "dry_run": True}).json()
ok("a dry run reports without writing", dry.get("ok") is True and not MEM.exists(), dry)

# The write itself goes through data_import directly, into the profile's own
# directory. The endpoint's own write path is covered by the staging-cleanup
# assertions below; what is being checked here is that the round trip restores
# the actual bytes.
done = di.restore(exp["path"], MIGRANT, ["chat"], mode=di.MERGE)
ok("restore succeeds", done.get("ok") is True, done.get("error"))
ok("the data is genuinely back",
   MEM.exists() and atrest.decrypt_bytes(MEM.read_bytes(), ns=MIGRANT) == SECRET)

print("\n=== 2. The staged copy does not linger ===")
# An abandoned upload is a second copy of someone's data in a directory they
# never chose, so it is removed once it has been used.
# A real (non-dry) restore through the endpoint, so the staging cleanup is
# exercised -- dry runs deliberately leave the archive staged for the commit.
#
# It uses the PROCEDURAL section rather than chat: with no session the endpoint
# resolves to the owner, and the owner's chat lives under STATE_DIR, which is
# this checkout. Procedural memory follows DATA_DIR, so the write lands in the
# temporary directory where a test's writes belong.
(Path(_TMP) / "procedural_memory").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / "procedural_memory" / "procedural.json").write_bytes(
    atrest.encrypt_bytes(b'{"learned": "something"}'))
_p_exp = de.build(None, "portable", ["procedural"], True)
_p_info = stage(_p_exp["path"])
_real = client.post("/api/import", json={"stage_id": _p_info["stage_id"],
                                         "sections": ["procedural"],
                                         "dry_run": False}).json()
ok("the endpoint's write path succeeds", _real.get("ok") is True, _real.get("error"))
ok("re-using the id fails",
   client.post("/api/import", json={"stage_id": _p_info["stage_id"]}).json().get("ok") is False)
def _is_staged(sid):
    return (Path(_TMP) / ".import_staging" / (sid + ".zip")).exists()


ok("the committed archive's staged copy is gone", not _is_staged(_p_info["stage_id"]))
# And the one that was only DRY RUN is deliberately still there: a dry run is
# how someone reads the report before deciding, so throwing the upload away at
# that point would make them choose the file again to say yes.
ok("a dry-run archive is kept for the commit", _is_staged(info["stage_id"]))

print("\n=== 3. Refusals ===")
ok("an unknown staging id is refused",
   client.post("/api/import", json={"stage_id": "nope"}).json().get("ok") is False)
ok("a traversal-shaped id is refused",
   client.post("/api/import", json={"stage_id": "../../etc/passwd"}).json().get("ok") is False)
notzip = client.post("/api/import/inspect",
                     files={"file": ("x.zip", b"not a zip", "application/zip")}).json()
ok("a file that is not an archive is refused", notzip.get("ok") is False)
_before = len(list((Path(_TMP) / ".import_staging").glob("*.zip")))
client.post("/api/import/inspect",
            files={"file": ("y.zip", b"also not a zip", "application/zip")})
ok("and it is not left staged",
   len(list((Path(_TMP) / ".import_staging").glob("*.zip"))) == _before)
ok("an unknown mode is refused",
   client.post("/api/import", json={"stage_id": "x", "mode": "obliterate"}
               ).json().get("ok") is False)

print("\n=== 4. A profile can take a portable export of its own ===")
NS = "alice"
DEK = pk.create_for_profile(NS, "alice-pw", recovery=True)
atrest.register_profile_key(NS, DEK)
AROOT = Path(sage_engine.user_data_dir(NS))
AROOT.mkdir(parents=True, exist_ok=True)
ASECRET = b'[{"role": "user", "content": "alice private"}]'
(AROOT / "chat_memory.json").write_bytes(atrest.encrypt_bytes(ASECRET, ns=NS))

refused = de.build(NS, "portable", ["chat"], False)
ok("refused while her key is not in hand", refused.get("ok") is False, refused)
ok("and the refusal explains why, not just 'no'",
   "not unlocked" in str(refused.get("error", "")), refused.get("error"))

hers = de.build(NS, "portable", ["chat"], False, export_key=DEK)
ok("allowed with her key", hers.get("ok") is True, hers.get("error"))
import zipfile                                                # noqa: E402
with zipfile.ZipFile(hers["path"]) as z:
    key_in_zip = z.read("KEY/fernet.key").strip()
ok("the key that travels is HERS",
   key_in_zip == atrest.fernet_key_bytes(DEK))
ok("...and is NOT the app-wide key",
   key_in_zip != Path(atrest._key_path()).read_bytes().strip())

print("\n=== 5. The optional passphrase ===")
prot = de.build(NS, "portable", ["chat"], False, export_key=DEK,
                passphrase="a passphrase she chose")
ok("built", prot.get("ok") is True, prot.get("error"))
with zipfile.ZipFile(prot["path"]) as z:
    names = z.namelist()
ok("the key itself is NOT in the archive", "KEY/fernet.key" not in names, names)
ok("a wrap is", di.KEY_WRAPPED in names, names)
ok("the result says it is protected", prot.get("protected") is True)

pinfo = stage(prot["path"])
ok("inspect warns before anything is chosen",
   pinfo.get("needs_passphrase") is True and pinfo.get("key_style") == "passphrase",
   pinfo)
none_given = client.post("/api/import", json={"stage_id": pinfo["stage_id"],
                                              "sections": ["chat"]}).json()
ok("restore without it is refused", none_given.get("ok") is False)
ok("and asks for the passphrase specifically",
   none_given.get("needs_passphrase") is True, none_given)
wrong = client.post("/api/import", json={"stage_id": pinfo["stage_id"],
                                         "sections": ["chat"],
                                         "passphrase": "not it"}).json()
ok("a wrong passphrase says so rather than 'corrupt archive'",
   wrong.get("ok") is False and wrong.get("needs_passphrase") is True, wrong)
ok("nothing was changed by either attempt",
   atrest.decrypt_bytes(MEM.read_bytes(), ns=MIGRANT) == SECRET)
right = client.post("/api/import", json={"stage_id": pinfo["stage_id"],
                                         "sections": ["chat"],
                                         "passphrase": "a passphrase she chose",
                                         "dry_run": True}).json()
ok("the right passphrase opens it", right.get("ok") is True, right.get("error"))
ok("...and it really did read the archive, not just accept the word",
   right.get("written", 0) >= 1, right)

print("\n=== 6. The wrap is a real one ===")
doc = keywrap.wrap_key_with_password(b"x" * 32, "pw")
ok("the key is not sitting in the document",
   ("78" * 32) not in str(doc) and "xxxx" not in str(doc))
ok("it round-trips", keywrap.unwrap_key_with_password(doc, "pw") == b"x" * 32)
_bad = False
try:
    keywrap.unwrap_key_with_password(doc, "wrong")
except keywrap.BadKey:
    _bad = True
ok("a wrong passphrase raises BadKey, not a silent None", _bad)
_mal = False
try:
    keywrap.unwrap_key_with_password({"junk": 1}, "pw")
except keywrap.KeywrapError:
    _mal = True
ok("a malformed document is distinguishable from a wrong passphrase", _mal)

# The install tree is not a scratch directory. Assert it, rather than trusting
# that every path above resolved where it was expected to.
_proj = Path(__file__).resolve().parent.parent
ok("nothing was written into the project tree",
   not (_proj / "chat_memory.json").exists() and
   not list(_proj.glob("chat_memory.json.*.bak")),
   [str(p) for p in _proj.glob("chat_memory.json*")])

shutil.rmtree(_TMP, ignore_errors=True)

_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
