#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_export_modes.py -- readable and portable exports are NOT the same file.

WHY THIS EXISTS (2026-08-13)
----------------------------
Todd took an export, unzipped it, found readable copies of his data, and
reasonably concluded the passphrase option had done nothing -- "apparently they
do the same thing, but shouldn't be".

They do not do the same thing. Both produce a .zip, because a zip is just a
container, but the CONTENTS are opposites:

    Readable   data_export.py:302   atrest.read_file_auto(f)  -> DECRYPTED plaintext
    Portable   data_export.py:308   f.read_bytes()            -> verbatim ciphertext

The difference is invisible from outside the file, which is exactly why it needs
a test rather than a paragraph. This asserts the property directly: take one
encrypted file, export it both ways, and look at what actually lands in the zip.

Also covers the passphrase, whose real job is narrower than the old UI claimed:
it wraps the KEY carried inside a portable export. It never encrypted the zip,
so unzipping was never going to prompt for it.

    python test_export_modes.py
"""
import os
import sys
import tempfile
import warnings
import zipfile

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

_TMP = tempfile.mkdtemp(prefix="vai_expmodes_")
os.environ["VERIDIAN_DATA_DIR"] = _TMP

from pathlib import Path                       # noqa: E402
import atrest                                  # noqa: E402
import data_export as de                       # noqa: E402
import profile_keys as pk                      # noqa: E402
import sage_engine                             # noqa: E402

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


# A Fernet token always begins "gAAAAA". That marker is how we tell ciphertext
# from plaintext without needing the key.
FERNET_MARK = b"gAAAAA"
SECRET = b"the quick brown fox jumped over the lazy dog"

atrest.encrypt_bytes(b"seed")
NS = "alice"
DEK = pk.create_for_profile(NS, "alice-pw", recovery=True)
atrest.register_profile_key(NS, DEK)

ROOT = Path(sage_engine.user_data_dir(NS))
ROOT.mkdir(parents=True, exist_ok=True)
chat = ROOT / "chat_memory.json"
chat.write_bytes(atrest.encrypt_bytes(SECRET, ns=NS))

ok("the file on disk really is encrypted to begin with",
   chat.read_bytes().lstrip()[:6] == FERNET_MARK, chat.read_bytes()[:24])


def build(mode, passphrase=None):
    return de.build(NS, mode, None, False, DEK, passphrase)


def entries(res):
    with zipfile.ZipFile(res["path"]) as z:
        return {n: z.read(n) for n in z.namelist() if not n.endswith("/")}


print("\n=== 1. A READABLE export decrypts ===")
r = build(de.MODE_READABLE)
ok("it built", r.get("ok") is True, r)
if r.get("ok"):
    body = b"".join(entries(r).values())
    ok("the plaintext IS present -- this zip is readable by anyone",
       SECRET in body)
    ok("no Fernet ciphertext remains", FERNET_MARK not in body)
    ok("it reports what it decrypted", r.get("decrypted", 0) >= 1, r.get("decrypted"))

print("\n=== 2. A PORTABLE export does NOT decrypt ===")
p = build(de.MODE_PORTABLE)
ok("it built", p.get("ok") is True, p)
if p.get("ok"):
    ents = entries(p)
    body = b"".join(v for k, v in ents.items() if not k.startswith("KEY/"))
    ok("the plaintext is ABSENT -- the opposite of the readable export",
       SECRET not in body)
    ok("the content is still Fernet ciphertext", FERNET_MARK in body)
    ok("nothing was decrypted", p.get("decrypted", 0) == 0, p.get("decrypted"))

print("\n=== 3. Portable WITHOUT a passphrase ships the bare key ===")
if p.get("ok"):
    names = set(entries(p))
    ok("KEY/fernet.key is present", "KEY/fernet.key" in names, sorted(names))
    ok("so the zip alone opens everything -- 'treat it like a password' is literal",
       "KEY/key.wrapped.json" not in names)
    ok("and it says so", any("like a password" in n for n in p.get("notes", [])),
       p.get("notes"))

print("\n=== 4. Portable WITH a passphrase wraps the key instead ===")
w = build(de.MODE_PORTABLE, "correct horse battery staple")
ok("it built", w.get("ok") is True, w)
if w.get("ok"):
    names = set(entries(w))
    ok("the bare key is GONE", "KEY/fernet.key" not in names, sorted(names))
    ok("a wrapped key is there instead", "KEY/key.wrapped.json" in names)
    ok("the result is flagged protected", w.get("protected") is True)
    blob = entries(w)["KEY/key.wrapped.json"]
    ok("the wrap does not contain the raw key",
       DEK not in blob and atrest.fernet_key_bytes(DEK) not in blob)
    import json as _json
    import keywrap as _kw
    doc = _json.loads(blob.decode("utf-8"))
    ok("the right passphrase opens the wrap",
       _kw.unwrap_key_with_password(doc, "correct horse battery staple")
       == atrest.fernet_key_bytes(DEK).strip())
    try:
        _kw.unwrap_key_with_password(doc, "wrong passphrase")
        ok("a wrong passphrase is refused", False, "it opened anyway")
    except Exception as e:
        ok("a wrong passphrase is refused", type(e).__name__ in ("BadKey", "KeywrapError"),
           type(e).__name__)

print("\n=== 5. The zip itself was never password-protected, in any mode ===")
# This is the expectation the old UI created and the code never met. Recording
# it as a fact rather than a surprise: the archive always opens; what a
# passphrase protects is the KEY inside it.
for label, res in (("readable", r), ("portable", p), ("portable+passphrase", w)):
    if res.get("ok"):
        try:
            with zipfile.ZipFile(res["path"]) as z:
                z.read(z.namelist()[0])
            ok("%s: the zip opens with no password (by design)" % label, True)
        except RuntimeError as e:
            ok("%s: the zip opens with no password (by design)" % label,
               False, "zip demanded a password: %s" % e)

print("\n=== 6. A passphrase on a READABLE export is inert ===")
# The backend ignores it; the frontend now warns before letting you get here.
ri = build(de.MODE_READABLE, "this cannot help you")
if ri.get("ok"):
    ok("not reported as protected", ri.get("protected") is False)
    body = b"".join(entries(ri).values())
    ok("and the contents are still plain text -- which is why the UI must warn",
       SECRET in body)

_p = sum(1 for _, c in _results if c)
_f = len(_results) - _p
print("\n%d/%d passed." % (_p, len(_results)))
sys.exit(1 if _f else 0)
