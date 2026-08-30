#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_downloads_allowlist.py -- what may be written into downloads, and by whom.

WHAT WAS WRONG

`re.sub(r'[^\w\-.]', '_', name)` was doing duty as a file-type check. It is not
one: it strips path separators and says nothing about what the file IS. So
`[SAVE_FILE: pwn.bat|@echo off ...]` from a model, or one POST to
/api/downloads/save, put an executable batch file in the person's own downloads
folder -- the folder the app then tells them to go and open.

main.py's `_bb_resolve_gate_path` docstring already named this route as half of
a past read-and-execute chain ("composed with api_save_to_downloads, whose
filename scrub permits .py: write a file, then name it"). The read-and-execute
half was closed in v2.15. This closes the write half.

AND THERE WERE THREE WRITERS

save_to_downloads() (the [SAVE_FILE:] tag and the MCP save_file tool) and
POST /api/downloads/save each carried their own scrub-and-write. Same job,
different rules: the HTTP route had no backup-before-overwrite, so the retry
loop that the tag path survives would silently destroy prior versions through
the route. The route now delegates, so there is one implementation and this
file can test it once.

WHERE THE LINE IS

Blocked: formats whose only purpose is execution -- .bat, .exe, .ps1, .vbs,
.lnk, .reg and friends. Allowed: source, markup, config and data -- including
.py and .js, which ARE executable on a Windows box with an interpreter
registered. A code editor that cannot save its own source files is not a code
editor. What stops a MODEL from writing one is the Beginner/Advanced/Expert
ladder, not the extension.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import sage_engine as se

_fails = []


def ok(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n            -> " + str(detail)) if detail and not cond else ""))
    if not cond:
        _fails.append(name)


print("\n=== 1. Executables and script hosts are refused ===")
BLOCKED = ["pwn.bat", "pwn.cmd", "pwn.exe", "pwn.com", "pwn.scr", "pwn.pif",
           "pwn.lnk", "pwn.msi", "pwn.hta", "pwn.cpl", "pwn.msc", "pwn.reg",
           "pwn.vbs", "pwn.vbe", "pwn.wsf", "pwn.wsh", "pwn.jse", "pwn.ps1",
           "pwn.psm1", "pwn.jar", "pwn.inf", "pwn.sct", "pwn.url"]
for name in BLOCKED:
    ok("%s is refused" % name, se.check_save_filename(name) is not None)

print("\n=== 2. Case and trailing dots do not get around it ===")
ok("PWN.BAT is refused", se.check_save_filename("PWN.BAT") is not None,
   "extension matching must be case-insensitive")
ok("pwn.bat. is refused", se.check_save_filename("pwn.bat.") is not None,
   "Windows discards trailing dots when opening, so 'pwn.bat.' opens as 'pwn.bat'")
ok("pwn.BaT.. is refused", se.check_save_filename("pwn.BaT..") is not None)
ok("a double extension is judged on the LAST one",
   se.check_save_filename("notes.bat.txt") is None
   and se.check_save_filename("notes.txt.bat") is not None,
   "the extension that matters is the one Windows will use")

print("\n=== 3. Source files a code editor must be able to save ===")
ALLOWED = ["main.py", "app.js", "app.mjs", "index.html", "styles.css",
           "data.json", "notes.md", "log.txt", "rows.csv", "conf.yaml",
           "q.sql", "run.sh", "main.c", "main.rs", "Main.java", "app.ts",
           "notebook.ipynb", "diagram.svg"]
for name in ALLOWED:
    ok("%s is allowed" % name, se.check_save_filename(name) is None,
       se.check_save_filename(name))
ok("an extensionless name is allowed (Makefile, LICENSE)",
   se.check_save_filename("Makefile") is None
   and se.check_save_filename("LICENSE") is None,
   "nothing without an extension is run by a double click on Windows")
ok("a dotfile is allowed", se.check_save_filename(".gitignore") is None)

print("\n=== 4. Nonsense names ===")
for bad in ("", ".", "..", "   ", "..."):
    ok("%r is refused" % bad, se.check_save_filename(bad) is not None)
ok("an unknown extension is refused (allowlist, not denylist)",
   se.check_save_filename("x.wat") is not None,
   "a new dangerous format must not be allowed by default")

print("\n=== 5. Windows reserved device names ===")
for name in ("CON", "con.txt", "PRN.md", "aux.py", "NUL", "COM1.txt", "lpt9.js"):
    ok("%s is refused" % name, se.check_save_filename(name) is not None,
       "Windows opens these as devices, not files")
ok("...but a name merely CONTAINING one is fine",
   se.check_save_filename("connection.py") is None
   and se.check_save_filename("console.js") is None,
   "reserved matching is on the whole stem, not a substring")

print("\n=== 6. The executor actually enforces it ===")
_tmp = tempfile.mkdtemp(prefix="dlallow_")
_real_dl = se.DOWNLOADS_DIR
try:
    import pathlib
    se.DOWNLOADS_DIR = pathlib.Path(_tmp)

    r = se.save_to_downloads("pwn.bat", "@echo off\ndel /q C:\\*")
    ok("save_to_downloads refuses a .bat", r.get("success") is False, r)
    ok("...and nothing was written",
       not os.path.exists(os.path.join(_tmp, "pwn.bat")),
       "a refusal that still writes the file is not a refusal")
    ok("...and the error names the extension", ".bat" in str(r.get("error", "")))

    r2 = se.save_to_downloads("hello.py", "print('hi')")
    ok("save_to_downloads accepts a .py", r2.get("success") is True, r2)
    ok("...and the file is on disk",
       os.path.exists(os.path.join(_tmp, "hello.py")))
    ok("...with the content intact",
       open(os.path.join(_tmp, "hello.py"), encoding="utf-8").read()
       == "print('hi')")

    # The scrub turns separators into underscores; the check must run on the
    # name that LANDS, not the one that was asked for.
    r3 = se.save_to_downloads("../../pwn.bat", "x")
    ok("a traversal-flavoured .bat is still refused", r3.get("success") is False,
       "the extension check runs on the sanitized name")
finally:
    se.DOWNLOADS_DIR = _real_dl

print("\n=== 7. One writer, not three ===")
_main = open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()
_route = _main[_main.index('@app.post("/api/downloads/save")'):]
_route = _route[:_route.index("@app.", 10)]
ok("the HTTP route delegates to the executor",
   "sage_engine.save_to_downloads(" in _route,
   "a second copy of the write is a second set of rules")
ok("...and does not write the file itself",
   "write_text" not in _route,
   "there should be exactly one place that writes into downloads")
ok("...and surfaces the executor's refusal as a 400",
   "HTTPException(400" in _route)

print("")
if _fails:
    print("%d CHECK(S) FAILED" % len(_fails))
    for f in _fails:
        print("   - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED - downloads save allowlist")
sys.exit(0)
