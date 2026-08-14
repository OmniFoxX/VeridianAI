# -*- coding: utf-8 -*-
"""Archive search must not cross profiles -- and every atrest call in
sage_engine must carry the namespace it belongs to.

THE LEAK THIS EXISTS FOR (found 2026-08-14, fixed same day)

`keyword_search(query, archives)` read every file from the module-level
ARCHIVE_FOLDER -- the OWNER'S folder -- while the `archives` list handed to it
came from `get_archives(ns)`, somebody else's. Two silent failures:

  * archive filenames are timestamps, so a collision between profiles was
    plausible -- and then a non-owner searching their own archives was served
    the OWNER'S content, scored and returned as if it were theirs
  * with no collision the read simply failed, `continue` swallowed it, and that
    profile's search quietly returned nothing

Reproduced with a deliberate collision: alice searching for a word appearing
only in the owner's archive received the owner's note. The first test below is
that reproduction, kept as a regression.

    python test_archive_isolation.py
"""
import os
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="vai_iso_")
os.environ["VERIDIAN_DATA_DIR"] = _TMP

import atrest                                               # noqa: E402
import sage_engine as se                                    # noqa: E402
from pathlib import Path                                    # noqa: E402

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


NAME = "archive_2026-08-14T10-00-00.json"          # the SAME name in both
OWNER_TEXT = "OWNER SECRET medical record confidential"
ALICE_TEXT = "alice notes about gardening"

owner_dir = Path(se._archive_folder(None))
alice_dir = Path(se._archive_folder("alice"))
bob_dir = Path(se._archive_folder("bob"))
for d in (owner_dir, alice_dir, bob_dir):
    d.mkdir(parents=True, exist_ok=True)

(owner_dir / NAME).write_bytes(
    atrest.dump_json_encrypted([{"role": "user", "content": OWNER_TEXT}]))
(alice_dir / NAME).write_bytes(
    atrest.dump_json_encrypted([{"role": "user", "content": ALICE_TEXT}],
                               ns="alice"))
(bob_dir / "archive_2026-08-14T11-00-00.json").write_bytes(
    atrest.dump_json_encrypted([{"role": "user", "content": "bob confidential plans"}],
                               ns="bob"))

print("== the collision that made the leak reachable ==")
ok("owner and alice have an identically-named archive",
   (owner_dir / NAME).exists() and (alice_dir / NAME).exists())
ok("they are in different folders", str(owner_dir) != str(alice_dir))

print("\n== REGRESSION: alice must never see the owner's content ==")
alice_archives = se.get_archives("alice")
hits = se.keyword_search("confidential", alice_archives, ns="alice")
leaked = []
for h in hits:
    body = atrest.load_json_auto((alice_dir / h["filename"]).read_bytes(), ns="alice")
    leaked.append(body[0]["content"])
ok("alice's search returns no owner content",
   all(OWNER_TEXT not in t for t in leaked), leaked)
ok("in fact alice matches nothing for that word", hits == [], hits)

print("\n== ...and alice's OWN search still works ==")
mine = se.keyword_search("gardening", se.get_archives("alice"), ns="alice")
ok("alice finds her own archive", len(mine) == 1, mine)
ok("it is her file", mine and mine[0]["filename"] == NAME)

print("\n== the owner still finds theirs ==")
owner_hits = se.keyword_search("confidential", se.get_archives(None), ns=None)
ok("owner finds the owner archive", len(owner_hits) == 1, owner_hits)

print("\n== bob is isolated from both ==")
bob_hits = se.keyword_search("confidential", se.get_archives("bob"), ns="bob")
ok("bob finds only his own", len(bob_hits) == 1, bob_hits)
ok("bob does not see the owner's",
   all(h["filename"] != NAME for h in bob_hits))
ok("alice does not see bob's",
   all("11-00-00" not in h["filename"] for h in se.get_archives("alice")))

print("\n== search_all_archives passes ns to both searchers ==")
import inspect
src_all = inspect.getsource(se.search_all_archives)
ok("keyword_search gets ns", "keyword_search(query, archives, ns=ns)" in src_all)
ok("semantic_search gets ns", "semantic_search(query, archives, ns=ns)" in src_all)
ok("keyword_search accepts it", "ns" in inspect.signature(se.keyword_search).parameters)
ok("semantic_search accepts it", "ns" in inspect.signature(se.semantic_search).parameters)

print("\n== the vector index is per-profile too ==")
sem_src = inspect.getsource(se.semantic_search)
ok("index path is derived from user_data_dir(ns)", "user_data_dir(ns)" in sem_src)
ok("...not a single shared file", 'DATA_DIR / "vector_index.json"' in sem_src
   and "base / \"vector_index.json\"" in sem_src)

print("\n== AUDIT: no per-profile path calls atrest without a namespace ==")
atrest.audit_start()
se.get_archives("alice")
se.keyword_search("gardening", se.get_archives("alice"), ns="alice")
se.load_archive(NAME, "alice")
se._load_titles("alice")
log = atrest.audit_stop()
missed = [e for e in log if e["ns"] is None]
ok("every call carried a namespace", missed == [],
   ["%s @ %s" % (e["op"], e["caller"]) for e in missed])
ok("the audit actually observed calls", len(log) > 0, len(log))

print("\n== ...and the owner's own path is legitimately ns=None ==")
atrest.audit_start()
se.get_archives(None)
log2 = atrest.audit_stop()
ok("owner calls use the system key", all(e["ns"] is None for e in log2), log2)

# --- tidy up -------------------------------------------------------------
# The OWNER's archive folder is STATE_DIR/archives, and STATE_DIR is the
# PROJECT tree whenever it is writable -- VERIDIAN_DATA_DIR does not move it.
# So the owner-side fixture lands in the repo unless it is removed. Same trap
# as the export round-trip test; see state_paths._resolve_state_dir.
try:
    (owner_dir / NAME).unlink()
except Exception:
    pass
print("\n== cleanup ==")
ok("the owner fixture did not stay in the project tree",
   not (owner_dir / NAME).exists())

failed = [n for n, c in _results if not c]
print("\n%d/%d passed." % (len(_results) - len(failed), len(_results)))
if failed:
    print("FAILED:")
    for n in failed:
        print("  - " + n)
    sys.exit(1)
