"""
test_archive_titles.py -- regression gate for custom archive titles (v2.12.9)
=============================================================================

Run:  python test_archive_titles.py    (no pytest dependency; same style as
                                        test_customs.py / test_access_window.py)

Feature (field request, Todd): forked conversations share their opening, so
the archive browser's first-sentence previews are identical across forks.
An OPTIONAL custom_title now lives in an atrest-encrypted sidecar
(sage_engine._TITLES_FILE) in the archive folder -- NEVER in the filename,
NEVER inside the archive files themselves (CRAIID compression validation,
context_fatigue_detector, and keyword/semantic search all depend on archives
staying plain lists).

Covers:
  * archive_conversation returns filename + suggested_title
  * get_archives surfaces custom_title (None by default; legacy fields intact)
  * set_archive_title: set, clear (-> fallback), traversal/bogus/missing
  * fork detection: shared opening + divergence -> suggestion drawn from the
    first DIVERGENT user message; unrelated chats -> no suggestion
  * delete_archive removes the sidecar entry with the file
  * the sidecar is invisible to get_archives and to every existing
    glob("archive_*.json") / glob("*.json") reader
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import sage_engine as se  # noqa: E402

# Redirect the archive folder to a temp dir so the gate touches nothing real.
_tmp = Path(tempfile.mkdtemp(prefix="archive_titles_test_"))
se._archive_folder = lambda ns=None: _tmp
se.save_chat_memory = lambda h, ns=None: None

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


BASE = [
    {"role": "user", "content": "Hello Toga, let's plan the garden."},
    {"role": "assistant", "content": "Great, what are we growing?"},
    {"role": "user", "content": "Tomatoes and peppers to start."},
    {"role": "assistant", "content": "Noted. Beds or containers?"},
]

print("archive_conversation")
r1 = se.archive_conversation(list(BASE))
check("base archive saves", r1.get("success") is True, r1)
check("no fork suggestion on a first archive", r1.get("suggested_title") is None, r1)
check("filename in response", (r1.get("filename") or "").startswith("archive_"), r1)
fn1 = r1["filename"]

print("get_archives metadata")
lst = se.get_archives()
check("listing works", len(lst) == 1 and lst[0]["filename"] == fn1)
check("custom_title None by default", lst[0]["custom_title"] is None)
check("legacy fields intact", "preview" in lst[0] and "message_count" in lst[0])

print("set_archive_title")
r = se.set_archive_title(fn1, "  Garden planning - main thread  ")
check("set (trimmed)", r.get("success") and
      r["custom_title"] == "Garden planning - main thread", r)
check("title in listing", se.get_archives()[0]["custom_title"] ==
      "Garden planning - main thread")
r = se.set_archive_title(fn1, "")
check("clear -> fallback state", r.get("success") and r["custom_title"] is None, r)
check("cleared in listing", se.get_archives()[0]["custom_title"] is None)
check("overlong title capped", se.set_archive_title(fn1, "x" * 500)["custom_title"]
      == "x" * se._MAX_TITLE_LEN)
se.set_archive_title(fn1, "")
check("traversal rejected", se.set_archive_title("../../evil.json", "x")["success"] is False)
check("non-archive name rejected", se.set_archive_title("notes.txt", "x")["success"] is False)
check("missing file rejected",
      se.set_archive_title("archive_29990101_000000.json", "x")["success"] is False)

print("fork detection")
time.sleep(1.1)  # distinct timestamped filename
fork = list(BASE) + [
    {"role": "system", "content": "=== SESSION BOUNDARY ...", "session_boundary": True},
    {"role": "user", "content": "Actually, scrap peppers. Can we do a greenhouse instead? It changes everything."},
    {"role": "assistant", "content": "A greenhouse opens up year-round options."},
]
r2 = se.archive_conversation(fork)
check("fork archive saves", r2.get("success") is True, r2)
sug = r2.get("suggested_title")
check("fork -> suggestion offered", bool(sug), r2)
check("suggestion from DIVERGENT user msg, not the shared opening",
      bool(sug) and "scrap peppers" in sug and "Hello Toga" not in sug, repr(sug))
check("suggestion title-sized", bool(sug) and len(sug) <= 60, repr(sug))

time.sleep(1.1)
other = [
    {"role": "user", "content": "Totally different topic: help me with taxes."},
    {"role": "assistant", "content": "Sure."},
    {"role": "user", "content": "Where do I start?"},
    {"role": "assistant", "content": "Gather your documents."},
]
r3 = se.archive_conversation(other)
check("unrelated chat -> no suggestion", r3.get("suggested_title") is None, r3)

print("delete + sidecar invisibility")
se.set_archive_title(fn1, "doomed title")
rd = se.delete_archive(fn1)
check("delete works", rd.get("success") is True, rd)
check("title entry removed with the file", fn1 not in se._load_titles())
names = [a["filename"] for a in se.get_archives()]
check("sidecar never listed as an archive", se._TITLES_FILE not in names)
check("sidecar invisible to glob('archive_*.json')",
      se._TITLES_FILE not in [p.name for p in _tmp.glob("archive_*.json")])
check("sidecar invisible to glob('*.json') (CRAIID readers)",
      se._TITLES_FILE not in [p.name for p in _tmp.glob("*.json")])

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
shutil.rmtree(_tmp, ignore_errors=True)
sys.exit(1 if FAIL else 0)
