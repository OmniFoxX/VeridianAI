# -*- coding: utf-8 -*-
"""data_import.py -- reading an export back in.

An archive from elsewhere is hostile input. Most of these tests are about
refusing things, because that is where the risk lives: zip-slip, credential
smuggling, zip bombs, and splicing a foreign hash chain into ours.

    python test_data_import.py
"""
import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_tmp = tempfile.mkdtemp(prefix="vai_import_")
os.environ["VERIDIAN_DATA_DIR"] = _tmp

import data_import as di                                    # noqa: E402

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


def mkzip(entries, name="a.zip", manifest=True):
    p = os.path.join(_tmp, name)
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        if manifest:
            z.writestr("MANIFEST.txt",
                       "VeridianAI data export\nMode    : readable\n"
                       "Created : 2026-08-14 10:00:00\n")
        for n, b in entries.items():
            z.writestr(n, b)
    return p


print("== entry-name safety: the refusals that matter ==")
CASES = [
    ("archives/ok.json", True, "ordinary"),
    ("archives/sub/deep.json", True, "nested"),
    ("../escape.json", False, "parent traversal"),
    ("archives/../../escape.json", False, "embedded traversal"),
    ("/etc/passwd", False, "absolute posix"),
    ("C:/Windows/evil.dll", False, "drive letter"),
    ("archives/./x.json", False, "dot segment"),
    ("chat/.api_keystore.json", False, "credential smuggling"),
    ("chat/.atrest_key", False, "key smuggling"),
    ("chat/.keywrap.json", False, "keywrap smuggling"),
    ("archives//x.json", False, "empty segment"),
    ("", False, "empty name"),
]
for nm, want, why in CASES:
    got, _ = di._entry_is_safe(nm)
    ok("%-28s %s (%s)" % (repr(nm)[:28], "allow" if want else "REFUSE", why),
       got == want, got)

print("\n== backslash paths are normalised, not trusted ==")
ok("windows traversal refused", di._entry_is_safe(r"archives\..\..\x")[0] is False)
ok("windows separator allowed when clean",
   di._entry_is_safe(r"archives\ok.json")[0] is True)

print("\n== _resolve_within is the belt to that brace ==")
root = Path(_tmp) / "dest"
root.mkdir(exist_ok=True)
ok("normal path resolves inside", di._resolve_within(root, "a/b.json") is not None)
ok("traversal resolves to None", di._resolve_within(root, "../../etc/x") is None)
ok("absolute resolves to None", di._resolve_within(root, "/etc/passwd") is None)

print("\n== inspection happens before anything is written ==")
z1 = mkzip({"archives/a.json": b'{"x":1}', "chat/c.json": b'{"y":2}'})
info = di.inspect_archive(z1)
ok("archive recognised", info["ok"] is True, info.get("error"))
ok("mode read from the manifest", info["mode"] == "readable", info["mode"])
ok("sections listed", sorted(s["key"] for s in info["sections"]) == ["archives", "chat"],
   [s["key"] for s in info["sections"]])
ok("file count right", info["file_count"] == 2, info["file_count"])
ok("no key present", info["has_key"] is False)

print("\n== unsafe entries are reported, not silently dropped ==")
z2 = mkzip({"archives/good.json": b"{}", "../evil.json": b"x",
            "chat/.api_keystore.json": b'{"tokens":[]}'}, "b.zip")
i2 = di.inspect_archive(z2)
ok("only the safe entry counts", i2["file_count"] == 1, i2["file_count"])
ok("two entries refused", len(i2["skipped"]) == 2, i2["skipped"])
ok("the refusal is surfaced to the user",
   any("refused for safety" in w for w in i2["warnings"]), i2["warnings"])
reasons = {s["reason"] for s in i2["skipped"]}
ok("keystore refused as 'never imported'", "never imported" in reasons, reasons)

print("\n== not-an-export inputs ==")
notzip = os.path.join(_tmp, "plain.txt")
open(notzip, "wb").write(b"hello")
ok("non-zip refused", di.inspect_archive(notzip)["ok"] is False)
ok("missing file refused", di.inspect_archive(os.path.join(_tmp, "nope.zip"))["ok"] is False)
z3 = mkzip({"stuff/x.json": b"{}"}, "c.zip", manifest=False)
i3 = di.inspect_archive(z3)
ok("no manifest -> still inspectable", i3["ok"] is True)
ok("...but warned about", any("No MANIFEST" in w for w in i3["warnings"]), i3["warnings"])

print("\n== zip bomb defences ==")
big = os.path.join(_tmp, "bomb.zip")
with zipfile.ZipFile(big, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("MANIFEST.txt", "VeridianAI data export\n")
    z.writestr("archives/big.bin", b"\0" * (5 * 1024 * 1024))
ib = di.inspect_archive(big)
ok("a high ratio is refused", ib["ok"] is False and "ratio" in ib.get("error", ""),
   ib.get("error"))
ok("limits are constants, not magic numbers",
   di.MAX_TOTAL_BYTES > 0 and di.MAX_RATIO > 0)

print("\n== the memory chain is never merged ==")
z4 = mkzip({"memory_chain/memory_chain.log": b"gAAAAAfake",
            "archives/a.json": b"{}"}, "d.zip")
i4 = di.inspect_archive(z4)
mc = next(s for s in i4["sections"] if s["key"] == "memory_chain")
ok("flagged unmergeable", mc["mergeable"] is False)
ok("and explained, not just flagged", "sequence" in mc.get("note", ""), mc.get("note"))
ok("archives stays mergeable",
   next(s for s in i4["sections"] if s["key"] == "archives")["mergeable"] is True)

print("\n== dry run writes nothing ==")
before = sorted(os.listdir(_tmp))
r = di.restore(z1, None, ["archives", "chat"], dry_run=True)
ok("dry run reports ok", r["ok"] is True)
ok("dry run counts what WOULD be written", r["written"] == 2, r["written"])
ok("dry run says so plainly", "preview" in r["note"])
ok("nothing appeared on disk", sorted(os.listdir(_tmp)) == before)

print("\n== a portable archive without its key is caught ==")
p = os.path.join(_tmp, "port.zip")
with zipfile.ZipFile(p, "w") as z:
    z.writestr("MANIFEST.txt", "VeridianAI data export\nMode    : portable\n")
    z.writestr("archives/a.json", b"gAAAAAsomethingencrypted")
ip = di.inspect_archive(p)
ok("warned that it cannot be decrypted",
   any("carries no key" in w for w in ip["warnings"]), ip["warnings"])

print("\n== the never-import list covers credentials AND keys ==")
for n in (".api_keystore.json", ".atrest_key", ".keywrap.json", "fernet.key",
          ".backend_mode"):
    ok("refused: %s" % n, di._entry_is_safe("chat/" + n)[0] is False)

failed = [n for n, c in _results if not c]
print("\n%d/%d passed." % (len(_results) - len(failed), len(_results)))
if failed:
    print("FAILED:")
    for n in failed:
        print("  - " + n)
    sys.exit(1)
