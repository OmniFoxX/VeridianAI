"""
test_save_verify_path.py -- v2.13.4 root-cause gate (save/verify paths)
=======================================================================

Run: python test_save_verify_path.py

The multi-day false-negative: save_to_downloads writes ONLY into
DOWNLOADS_DIR; verify_written_file anchored relative paths to project
root, so "verify BugSquashNote.txt" checked E:\\<root>\\BugSquashNote.txt
and failed while the file sat in downloads\\. Covers:
  * downloads-fallback resolution (basename, sanitized basename,
    sanitized FULL path -- the E__..._downloads_x.txt flattening)
  * backup-before-overwrite (.bak created, prior content preserved,
    pruned to 3)
  * honest failure when the file truly doesn't exist anywhere

Uses a temp downloads dir INSIDE the project root (containment rule
requires it); fully cleaned up afterward.
"""

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sage_engine as se  # noqa: E402

PASS = 0
FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f"FAIL  {label}  {extra}")


_orig_dl = se.DOWNLOADS_DIR
_root = Path(__file__).parent.parent
_tmp = _root / "downloads" / "_test_v2134_tmp"
_tmp.mkdir(parents=True, exist_ok=True)
se.DOWNLOADS_DIR = _tmp

try:
    print("== root cause: bare-filename verify resolves to downloads ==")
    r = se.save_to_downloads("PathNote.txt", "bug squash note v1")
    check("save ok", r.get("success"), str(r))
    check("save reports absolute path",
          r.get("path", "").endswith("PathNote.txt"))
    v = se.verify_written_file("PathNote.txt")
    check("verify OK via downloads fallback", v.startswith("[VERIFY OK]"),
          v[:120])
    check("resolution is visible in result",
          "auto-resolved to downloads" in v, v[:160])

    print("== root cause: wrong project-root path also recovers ==")
    wrong = str(_root / "PathNote.txt")   # the exact multi-day mistake
    v = se.verify_written_file(wrong)
    check("verify OK from wrong-root path",
          v.startswith("[VERIFY OK]"), v[:120])

    print("== flattened-filename case (the nudge divergence) ==")
    flat_input = str(_root / "downloads" / "squash_note.txt")
    r2 = se.save_to_downloads(flat_input, "nudged content")
    check("full-path save flattens but succeeds", r2.get("success"))
    check("filename was sanitizer-flattened",
          "_" in r2.get("filename", "") and
          r2["filename"].endswith("squash_note.txt"), str(r2))
    v2 = se.verify_written_file(flat_input)
    check("verify of original full path finds flattened file",
          v2.startswith("[VERIFY OK]"), v2[:160])

    print("== honest failure when truly missing ==")
    v3 = se.verify_written_file("never_saved_anywhere.txt")
    check("still fails for missing files",
          v3.startswith("[VERIFY FAILED]"), v3[:120])
    check("failure notes downloads was checked",
          "also checked downloads/" in v3, v3[:160])

    print("== backup-before-overwrite ==")
    r3 = se.save_to_downloads("PathNote.txt", "rewritten v2")
    check("overwrite ok", r3.get("success"))
    check("backup reported", bool(r3.get("backup")), str(r3))
    baks = list(_tmp.glob("PathNote.txt.*.bak"))
    check("backup file exists", len(baks) == 1, str(baks))
    if baks:
        check("backup preserves v1 content",
              baks[0].read_text(encoding="utf-8")
              == "bug squash note v1")
    check("current file is v2",
          (_tmp / "PathNote.txt").read_text(encoding="utf-8")
          == "rewritten v2")
    for i in range(4):
        time.sleep(1.05)   # distinct timestamps
        se.save_to_downloads("PathNote.txt", f"rewrite {i + 3}")
    baks = list(_tmp.glob("PathNote.txt.*.bak"))
    # Environment probe: some sandboxed mounts block unlink (EPERM), in
    # which case prune's per-file OSError guard correctly no-ops. Only
    # assert the count where deletion is actually possible.
    _probe = _tmp / "_unlink_probe.tmp"
    _probe.write_text("x")
    try:
        _probe.unlink()
        _can_delete = True
    except OSError:
        _can_delete = False
    if _can_delete:
        check("backups pruned to 3", len(baks) <= 3, str(len(baks)))
    else:
        check("backups pruned to 3 (SKIPPED: mount blocks delete; "
              "prune guard no-opped as designed)", True)
    check("first save (no prior file) had no backup",
          not r.get("backup"))
finally:
    se.DOWNLOADS_DIR = _orig_dl
    shutil.rmtree(_tmp, ignore_errors=True)

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
