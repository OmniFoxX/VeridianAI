#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_procedural_at_rest.py -- the procedural store is ciphertext on disk.

WHAT WAS WRONG (found 2026-08-20)

sage_data/procedural_memory/procedural.json was plaintext JSON while every
neighbour in sage_data was Fernet-encrypted. It holds verbatim user text:

    user_request           up to 500 chars of the user's own message
    final_answer_preview   300 chars of the reply
    <dict key>             a slug of the request, e.g.
                           "task:276aa555:hello_sage_and_welcome_to_"

71 entries, 102 KB, zero ciphertext in the file.

WHY THE EXISTING GUARD DID NOT CATCH IT

test_atrest_call_sites.py requires every atrest call to be classified PROFILE
(ns=) or SYSTEM TIER. It is a good rule and it worked -- on the files it could
see. It audits CALL SITES. procedural_memory.py made no atrest calls at all,
so there was nothing to classify and the module was invisible to it.

A test that checks how existing calls are written cannot see a file that never
calls. Section 5 below closes that specific hole: modules known to persist user
content must go through atrest, so the next one added has to be classified
rather than merely forgotten.

Everything here works on a TEMPORARY store. It never touches real data.

    python test_procedural_at_rest.py
"""
import io
import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


import atrest                                              # noqa: E402
from procedural_memory import ProceduralMemory             # noqa: E402

_TMP = tempfile.mkdtemp(prefix="vai_proc_test_")

# Text chosen so a plaintext leak is unmistakable in a raw byte scan.
SECRET_KEY = "task:deadbeef:patient_reported_chest_pain_at"
SECRET_REQ = "Patient MRN 88231 reported chest pain at 0300, BP 150/95."
SECRET_ANS = "Recorded. Advised immediate escalation to on-call cardiology."


def _fresh(name="s1"):
    d = os.path.join(_TMP, name)
    os.makedirs(d, exist_ok=True)
    return d


def _store_path(d):
    return os.path.join(d, "procedural.json")


# =============================================================================
print("=== 1. What lands on disk is ciphertext ===")
# =============================================================================
_d = _fresh("write")
_pm = ProceduralMemory(storage_dir=_d)
_pm.add_procedure(SECRET_KEY,
                  {"user_request": SECRET_REQ,
                   "final_answer_preview": SECRET_ANS,
                   "steps_used": 2},
                  metadata={"source": "test"})

def _try_json(b):
    try:
        json.loads(b.decode("utf-8"))
        return True
    except Exception:
        return False


_raw = io.open(_store_path(_d), "rb").read()
ok("the file is recognised as our ciphertext", atrest.is_encrypted(_raw))
ok("it does not parse as plaintext JSON", not _try_json(_raw))

# The load-bearing assertion. Not "encryption was called" -- the actual bytes.
ok("the user's request text is ABSENT from the raw bytes",
   SECRET_REQ.encode() not in _raw,
   "this is the assertion the whole file exists for")
ok("the reply preview is ABSENT from the raw bytes",
   SECRET_ANS.encode() not in _raw)
ok("the SLUGGED KEY is absent too",
   SECRET_KEY.encode() not in _raw,
   "keys leak content as well -- 'task:<hash>:<first words of the request>'. "
   "Per-field encryption would have left these in the clear.")
ok("no fragment of the request survives either",
   b"chest pain" not in _raw and b"88231" not in _raw)
ok("the bucket names are not visible as plaintext structure",
   b'"successful"' not in _raw)


# =============================================================================
print("\n=== 2. It reads back, and a fresh process sees it ===")
# =============================================================================
_pm2 = ProceduralMemory(storage_dir=_d)      # re-reads from disk
_got = _pm2.get_procedure(SECRET_KEY)
ok("a new instance loads the encrypted store", _got is not None)
ok("the value round-trips exactly",
   _got == {"user_request": SECRET_REQ,
            "final_answer_preview": SECRET_ANS,
            "steps_used": 2}, _got)
ok("the key survives intact", SECRET_KEY in _pm2.list_procedures("successful"))
ok("verify_integrity accepts an ENCRYPTED store", _pm2.verify_integrity(),
   "a plain json.load here would choke on ciphertext and call every upgraded "
   "install corrupt")


# =============================================================================
print("\n=== 3. Legacy plaintext still loads (upgrades lose nothing) ===")
# =============================================================================
_d2 = _fresh("legacy")
_legacy = {"successful": {SECRET_KEY: {"value": {"user_request": SECRET_REQ},
                                       "metadata": {}, "timestamp": "t",
                                       "chain_hash": "h"}},
           "unsuccessful": {}}
io.open(_store_path(_d2), "w", encoding="utf-8").write(json.dumps(_legacy))
ok("the fixture really is plaintext",
   not atrest.is_encrypted(io.open(_store_path(_d2), "rb").read()))

_pm3 = ProceduralMemory(storage_dir=_d2)
ok("a legacy plaintext store still loads",
   _pm3.get_procedure(SECRET_KEY) == {"user_request": SECRET_REQ},
   "load_json_auto's plaintext fallback is what makes the upgrade lossless")

# ...and the next write converts it, with nothing lost.
_pm3.add_procedure("task:second:another_one", {"user_request": "second"})
_raw3 = io.open(_store_path(_d2), "rb").read()
ok("the next save converts the file to ciphertext", atrest.is_encrypted(_raw3))
ok("the pre-existing entry survived the conversion",
   ProceduralMemory(storage_dir=_d2).get_procedure(SECRET_KEY)
   == {"user_request": SECRET_REQ})
ok("and the old plaintext is no longer in the file",
   SECRET_REQ.encode() not in _raw3)


# =============================================================================
print("\n=== 4. An unreadable store is NEVER overwritten ===")
# =============================================================================
# The dangerous shape: _load could not decrypt -> returns {} -> _save writes {}
# over a healthy file and the user's data is gone. Encryption introduces this
# failure mode; plaintext could not really hit it.
_d3 = _fresh("unreadable")
io.open(_store_path(_d3), "wb").write(b"gAAAAABnot-a-real-token-at-all")
_before = io.open(_store_path(_d3), "rb").read()

_pm4 = ProceduralMemory(storage_dir=_d3)
ok("an unreadable store latches _load_failed",
   getattr(_pm4, "_load_failed", False) is True)
ok("...and presents as empty in memory rather than raising",
   _pm4.list_procedures("successful") == [])

_pm4.add_procedure("task:new:should_not_be_written", {"user_request": "x"})
_after = io.open(_store_path(_d3), "rb").read()
ok("the file on disk was NOT overwritten", _after == _before,
   "silently trading undecryptable data for an empty dict is the worst "
   "possible response to 'I cannot read this'")

_d4 = _fresh("missing")
_pm5 = ProceduralMemory(storage_dir=_d4)
ok("a genuinely ABSENT store does not latch (writes must work)",
   getattr(_pm5, "_load_failed", True) is False,
   "'no file yet' and 'file I cannot read' must not be conflated")
_pm5.add_procedure("task:first:hello", {"user_request": "hello"})
ok("...and a first write succeeds", os.path.exists(_store_path(_d4)))
ok("...encrypted", atrest.is_encrypted(io.open(_store_path(_d4), "rb").read()))


# =============================================================================
print("\n=== 5. The blind spot that hid this ===")
# =============================================================================
# test_atrest_call_sites.py audits how atrest calls are WRITTEN. It cannot see
# a module that never calls atrest -- which is exactly what procedural_memory
# was. Name the modules that persist user content and require each to reach
# atrest at all.
_MUST_ENCRYPT = {
    "procedural_memory.py": "user_request / final_answer_preview, verbatim",
}
for _mod, _why in sorted(_MUST_ENCRYPT.items()):
    _p = os.path.join(_HERE, _mod)
    if not os.path.exists(_p):
        ok("%s is present" % _mod, False)
        continue
    _src = io.open(_p, encoding="utf-8").read()
    ok("%s reaches atrest at all (%s)" % (_mod, _why),
       "import atrest" in _src,
       "a call-site audit cannot classify a module that makes no calls")
    ok("%s classifies its calls (SYSTEM TIER or ns=)" % _mod,
       "SYSTEM TIER" in _src or "ns=" in _src)

_psrc = io.open(os.path.join(_HERE, "procedural_memory.py"),
                encoding="utf-8").read()
ok("the writer encrypts", "dump_json_encrypted" in _psrc)
ok("the reader auto-detects", "load_json_auto" in _psrc)
ok("verify_integrity uses the same reader as _load",
   _psrc.count("load_json_auto") >= 2,
   "if verify_integrity kept a raw json.load it would report every encrypted "
   "store as corrupt")
ok("no raw json.dump of the knowledge base remains",
   "json.dump(self._knowledge_base" not in _psrc)


# =============================================================================
print("\n=== 6. The migration script ===")
# =============================================================================
try:
    import migrate_procedural_at_rest as _mig
    ok("migrate_procedural_at_rest imports", True)
    ok("it refuses to keep a plaintext copy, and says why",
       "No plaintext copy was kept" in io.open(
           os.path.join(_HERE, "migrate_procedural_at_rest.py"),
           encoding="utf-8").read())
    _msrc = io.open(os.path.join(_HERE, "migrate_procedural_at_rest.py"),
                    encoding="utf-8").read()
    # Ordering asserted on the AST, not on string positions. The first draft
    # compared _msrc.index("load_json_auto") against _msrc.index("os.replace")
    # and failed -- because "os.replace" appears in the module DOCSTRING,
    # above any code. Searching prose for the name of a thing tells you
    # nothing about where the thing happens.
    import ast as _ast
    _mt = _ast.parse(_msrc)
    _mfn = [n for n in _ast.walk(_mt)
            if isinstance(n, _ast.FunctionDef) and n.name == "migrate"]
    ok("migrate() is present to inspect", len(_mfn) == 1)
    if _mfn:
        def _call_lines(fn, name):
            out = []
            for c in _ast.walk(fn):
                if isinstance(c, _ast.Call):
                    f = c.func
                    if (isinstance(f, _ast.Attribute) and f.attr == name) or \
                            (isinstance(f, _ast.Name) and f.id == name):
                        out.append(c.lineno)
            return sorted(out)

        _verify = _call_lines(_mfn[0], "load_json_auto")
        _replace = _call_lines(_mfn[0], "replace")
        ok("the round trip is verified before anything is replaced",
           bool(_verify) and bool(_replace) and min(_verify) < min(_replace),
           "verify at %r, replace at %r" % (_verify, _replace))
        ok("and it is re-verified AFTER the replace, off disk",
           len(_verify) >= 2 and max(_verify) > min(_replace),
           "verify lines %r vs replace %r" % (_verify, _replace))
    ok("it is idempotent on an already-encrypted store",
       "already encrypted" in _msrc)
    ok("it re-reads from DISK after writing",
       "final = io.open" in _msrc,
       "verifying the in-memory blob proves nothing about what landed")
    ok("it has a dry-run", "--dry-run" in _msrc)
    ok("every abort path leaves the original alone",
       _msrc.count("left untouched") >= 4)
except Exception as _e:
    ok("migrate_procedural_at_rest imports", False,
       "%s: %s" % (type(_e).__name__, _e))


# =============================================================================
print("\n=== 7. The daemon touches the same file, and must agree ===")
# =============================================================================
# sage_daemon runs in its OWN process and rewrites this file wholesale during
# consolidation. Encrypting only the app's writer would have produced two
# failures, one silent and one destructive:
#   read  -- json.load on ciphertext raises, consolidation returns
#            "read failed" forever and quietly stops working
#   write -- _atomic_write_json puts the store back as PLAINTEXT, undoing the
#            at-rest protection on the first tick after migration
_dsrc = io.open(os.path.join(_HERE, "sage_daemon.py"), encoding="utf-8").read()
ok("sage_daemon reaches atrest", "import atrest" in _dsrc)
ok("it has a dedicated encrypted reader for the store",
   "_read_procedural_kb" in _dsrc)
ok("...and a dedicated encrypted writer", "_write_procedural_kb" in _dsrc)
ok("the generic plaintext writer no longer touches the procedural file",
   "_atomic_write_json(PROCEDURAL_FILE" not in _dsrc,
   "that call would have silently reverted the encryption")
ok("the generic plaintext writer still exists for pipeline state",
   "_atomic_write_json(DIGEST_FILE" in _dsrc,
   "digest and CRAIID task state are not user content; encrypting them was "
   "not in scope and should not happen by accident")
ok("no raw json.load of the procedural file remains",
   'open(PROCEDURAL_FILE, "r"' not in _dsrc)

_dt = __import__("ast").parse(_dsrc)
_djob = [n for n in _dt.body
         if isinstance(n, __import__("ast").FunctionDef)
         and n.name == "_job_consolidate_procedural"]
ok("_job_consolidate_procedural is present", len(_djob) == 1)
if _djob:
    _jsrc = _dsrc[_dsrc.index("def _job_consolidate_procedural"):]
    _jsrc = _jsrc[:_jsrc.index("\ndef ", 10)]
    ok("an unreadable store makes it return, not rewrite",
       "kb is None" in _jsrc and "left untouched" in _jsrc,
       "consolidation rewrites the WHOLE file -- proceeding on a misread "
       "would delete every entry it could not parse")
    ok("it writes through the encrypted writer",
       "_write_procedural_kb(kb)" in _jsrc)

# Both processes must agree on the namespace or they cannot read each other.
ok("app and daemon both classify the store SYSTEM TIER",
   "SYSTEM TIER" in _psrc and "SYSTEM TIER" in _dsrc,
   "a namespace disagreement between the two processes would look like "
   "random corruption")


shutil.rmtree(_TMP, ignore_errors=True)

_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
