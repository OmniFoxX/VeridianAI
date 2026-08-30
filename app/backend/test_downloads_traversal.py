#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_downloads_traversal.py -- can anything escape the downloads folder?

WHY THIS EXISTS

CodeQL alerts 194-199, `py/path-injection`, HIGH, six of them: every
filesystem use of `path` inside `sage_engine.save_to_downloads` -- resolve,
exists, copy2, write_text, stat. They appeared the moment Phase 2a pointed
`POST /api/downloads/save` at that function, which gave the analysis a clean
data path from an HTTP request body to a file write. The alerts are correct
about the data flow. The question is whether the guards hold.

"I read the code and it looked fine" is not an answer to a HIGH path-injection
alert, and dismissing six of them on that basis is how a real one eventually
gets waved through. So this attacks the function instead: every traversal shape
worth trying goes in, and the assertion is that the file lands inside the
downloads folder or does not land at all.

THE THREE GUARDS, so a future reader knows what is being defended:

  1. `re.sub(r'[^\\w\\-.]', '_', ...)` -- keeps word characters, hyphen and dot.
     Every separator, drive colon and wildcard becomes '_'. Nothing that could
     traverse survives, EXCEPT '..', because dot is allowed.
  2. `check_save_filename` refuses '.', '..' and reserved Windows device names,
     and enforces the extension allowlist on the SANITIZED name.
  3. `path.resolve().relative_to(_dl.resolve())` -- an explicit containment
     check, and it runs BEFORE any use of the path.

Guard 3 is the backstop. Guards 1 and 2 are why it never has to fire.

    python test_downloads_traversal.py
"""
import io
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_fails = []


def ok(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n            -> " + str(detail)) if detail and not cond else ""))
    if not cond:
        _fails.append(name)


import sage_engine as se                                   # noqa: E402

# RUN AGAINST A THROWAWAY NAMESPACE, NOT THE REAL DOWNLOADS FOLDER.
#
# The first version used ns=_NS and sprayed 32 files with names like
# "C:\\Windows\\System32\\pwn.txt" into the actual downloads directory --
# and when the guards were disabled to CONTROL the test, two of them
# landed OUTSIDE it, in the data folder itself. The test was right and the
# blast radius was wrong. A namespace gives the same code path its own
# directory, so the worst a broken guard can do is escape into a tree this
# file owns and removes.
_NS = "ztraversalprobe"
_DL = se.downloads_dir_for(_NS).resolve()
_OUTSIDE = _DL.parent

# A canary written OUTSIDE the downloads folder. If any payload overwrites it,
# containment failed and we will know exactly which one.
_CANARY = _OUTSIDE / "traversal_canary.txt"
try:
    io.open(str(_CANARY), "w", encoding="utf-8").write("UNTOUCHED")
    _canary_ok = True
except OSError as e:
    _canary_ok = False
    print("  note: could not place the canary (%s); the containment assertion"
          " below still runs on resolved paths" % e)


PAYLOADS = [
    "../pwn.txt",
    "../../pwn.txt",
    "../../../../../../../../etc/passwd",
    "..\\..\\pwn.txt",
    "....//pwn.txt",
    "..%2f..%2fpwn.txt",
    "..%252f..%252fpwn.txt",
    "%2e%2e%2fpwn.txt",
    "..;/pwn.txt",
    "./../pwn.txt",
    "/etc/passwd",
    "/absolute/pwn.txt",
    "C:\\Windows\\System32\\pwn.txt",
    "\\\\server\\share\\pwn.txt",
    "\\\\?\\C:\\pwn.txt",
    "..",
    ".",
    "...",
    "....",
    "..\x00/pwn.txt",
    "pwn.txt\x00.png",
    "sub/dir/pwn.txt",
    "sub\\dir\\pwn.txt",
    "~/pwn.txt",
    "$HOME/pwn.txt",
    "%APPDATA%\\pwn.txt",
    "con.txt",
    "PRN.md",
    "aux",
    "lpt1.json",
    "nul.txt",
    "com9.md",
    "pwn.txt.",
    "pwn.txt ",
    "pwn.bat",
    "pwn.BAT",
    "pwn.bat.",
    "pwn.ps1",
    "pwn.txt.bat",
    "a" * 300 + ".txt",
    "\u202epwn.txt",           # right-to-left override
    "pwn\u200b.txt",           # zero-width space
]

print("=== 1. Nothing lands outside the downloads folder ===")
_escaped = []
_written = []
for payload in PAYLOADS:
    try:
        res = se.save_to_downloads(payload, "PAYLOAD-BODY", ns=_NS)
    except Exception as e:
        _escaped.append("%r raised %s: %s" % (payload, type(e).__name__, e))
        continue
    if not res.get("success"):
        continue
    # It was accepted. Where did it actually go?
    name = res.get("filename") or ""
    try:
        landed = (se.downloads_dir_for(_NS) / name).resolve()
        landed.relative_to(_DL)
        _written.append(name)
    except (ValueError, OSError):
        _escaped.append("%r -> %s" % (payload, res.get("path") or name))

ok("no payload wrote outside the downloads folder",
   not _escaped, _escaped[:4])
ok("...and none of them raised out of the function",
   all("raised" not in e for e in _escaped),
   "save_to_downloads returns a dict; an exception is a different contract")
ok("the canary outside the folder is untouched",
   (not _canary_ok)
   or io.open(str(_CANARY), encoding="utf-8").read() == "UNTOUCHED")

print("\n=== 2. The dangerous ones were refused, not merely relocated ===")
for bad in ("pwn.bat", "pwn.BAT", "pwn.bat.", "pwn.ps1", "pwn.txt.bat"):
    r = se.save_to_downloads(bad, "x", ns=_NS)
    ok("%r is refused" % bad, r.get("success") is False, r)
for dev in ("con.txt", "PRN.md", "aux", "lpt1.json", "nul.txt", "com9.md"):
    r = se.save_to_downloads(dev, "x", ns=_NS)
    ok("reserved device name %r is refused" % dev,
       r.get("success") is False, r)
for empty in ("..", ".", "   "):
    r = se.save_to_downloads(empty, "x", ns=_NS)
    ok("%r is refused" % empty, r.get("success") is False, r)

print("\n=== 3. The guards are where the comments say they are ===")
_src = io.open(os.path.join(_HERE, "sage_engine.py"), encoding="utf-8").read()
_fn = _src.split("def save_to_downloads")[1].split("\ndef ")[0]
ok("separators are stripped before the join",
   _fn.index("re.sub") < _fn.index("_dl / safe_name"),
   "sanitising after building the path would be decoration")
ok("the name is vetted before the join",
   _fn.index("check_save_filename") < _fn.index("_dl / safe_name"))
ok("containment is checked before any USE of the path",
   _fn.index("relative_to") < _fn.index("write_text"),
   "guard 3 is the backstop; a backstop that runs after the write is not one")
ok("the sanitised name is what gets joined, not the original",
   "_dl / safe_name" in _fn and "_dl / filename" not in _fn,
   "using the validation's VERDICT but not its RESULT is the classic version "
   "of this mistake")

print("\n=== 4. A legitimate save still works ===")
_good = se.save_to_downloads("traversal_test_ok.md", "# fine", ns=_NS)
ok("an ordinary filename saves", _good.get("success") is True, _good)
ok("...inside the downloads folder",
   (se.downloads_dir_for(_NS) / "traversal_test_ok.md").exists())

# Tidy up the whole probe namespace. Best effort -- nothing above depends
# on deletion succeeding, but leaving files called
# "C:\Windows\System32\pwn.txt" in somebody's data folder is its own
# small unkindness.
import shutil as _shutil                                   # noqa: E402
try:
    os.unlink(str(_CANARY))
except OSError:
    pass
try:
    _root = se.user_data_dir(_NS)
    if _root:
        _shutil.rmtree(str(_root), ignore_errors=True)
except Exception:
    pass

print("")
if _fails:
    print("%d CHECK(S) FAILED" % len(_fails))
    for f in _fails:
        print("   - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED - downloads traversal (%d payloads)" % len(PAYLOADS))
sys.exit(0)
