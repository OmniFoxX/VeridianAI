#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_aiq_nudge_scope.py -- a nudge reaches its sender's session, and no other.

THE SCENARIO (Todd, 2026-08-23)

    "person A sends a QNudge but logs off and leaves before it fires, then
     person B logs on and types a prompt and clicks send, a rogue QNudge fires
     off first and the model gets person A's nudge but thinks it must act on
     it per the belief it was an instruction from person B"

Every part of that was possible. Nudges were signed files in ONE shared
directory, sage_data/nudges, with no owning profile recorded anywhere. The
consumer globbed the directory between agentic steps and injected whatever
verified. And the injected text read:

    "[VERIFIED USER NUDGE -- mid-run directive from Todd, HMAC-checked]"

-- hardcoded, for every profile. So a nudge from anyone was announced to the
model as the owner's instruction. The HMAC proved the file had not been
forged; it said nothing about whose turn it belonged to, because there was
nothing in it to say.

WHY THE NAMESPACE IS INSIDE THE SIGNATURE

Putting it in the filename, or on an unsigned line, would mean a rename
re-aims somebody else's directive at your session. The HMAC exists so that a
file on disk cannot make Toga do something a person did not ask for; signing
the timestamp already stops replay, and signing the namespace stops re-aiming.

WHY EXPIRY AS WELL AS SCOPING

Scoping stops the wrong DELIVERY. It does not stop a forgotten nudge sitting
in a shared directory until its author returns -- possibly days later, to a
run it was never meant for. A nudge steers the run happening now.

    python test_aiq_nudge_scope.py
"""
import ast
import hashlib
import hmac
import io
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


from aiq_nudge import AIQNudge                                  # noqa: E402

MAIN = io.open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()
AP = io.open(os.path.join(_HERE, "access_policy.py"), encoding="utf-8").read()

_TMP = Path(tempfile.mkdtemp(prefix="vai_nudge_"))
N = AIQNudge(_TMP / "k.key", _TMP / "w")
WATCH = _TMP / "w"

try:
    # =========================================================================
    print("=== 1. The leak itself ===")
    # =========================================================================
    N.send("delete every file you find", ns="alice")
    ok("another profile does NOT receive it",
       N.read_pending(ns="bob") == [],
       "this is Todd's scenario, and it is the whole reason for this file")
    ok("the OWNER does not receive it either",
       N.read_pending(ns=None) == [],
       "the owner is a namespace like any other here, not a wildcard")
    _mine = [e["content"] for e in N.read_pending(ns="alice")]
    ok("the sender DOES receive it", _mine == ["delete every file you find"],
       _mine)
    ok("...exactly once", N.read_pending(ns="alice") == [],
       "one-shot directives, not idempotent state")

    # =========================================================================
    print("\n=== 2. It cannot be re-aimed ===")
    # =========================================================================
    _p = N.send("do the wrong thing", ns="alice")
    _p.rename(_p.with_name("nudge_9999999999999.txt"))
    ok("renaming the file does not re-target it",
       N.read_pending(ns="bob") == [],
       "the namespace is inside the signature, not in the filename")
    N.read_pending(ns="alice")          # consume it back out of the way

    _p2 = N.send("tampered", ns="alice")
    _lines = _p2.read_text(encoding="utf-8").split("\n")
    _lines[2] = "bob"                   # edit the namespace line in place
    _p2.write_text("\n".join(_lines), encoding="utf-8")
    ok("editing the namespace breaks the signature",
       N.read_pending(ns="bob") == [])
    ok("...and the file is quarantined, not silently dropped",
       any(f.name.count(".rejected_") for f in WATCH.glob("*")),
       [f.name for f in WATCH.glob("*")])
    for _f in WATCH.glob("*.rejected_*"):
        _f.unlink()

    # =========================================================================
    print("\n=== 3. A forgotten nudge ages out ===")
    # =========================================================================
    _old_ts = time.strftime("%Y-%m-%dT%H:%M:%S",
                            time.localtime(time.time() - 3600))
    (WATCH / "nudge_old.txt").write_text(
        N.sign("stale directive", ns="alice", timestamp=_old_ts),
        encoding="utf-8")
    ok("an hour-old nudge is not delivered",
       N.read_pending(ns="alice", max_age_sec=300) == [])
    ok("...and does not linger in a shared directory",
       not (WATCH / "nudge_old.txt").exists())

    _fresh = N.send("still relevant", ns="alice")
    ok("a fresh one still arrives",
       len(N.read_pending(ns="alice", max_age_sec=300)) == 1)

    # =========================================================================
    print("\n=== 4. The pre-namespace format is refused, not guessed at ===")
    # =========================================================================
    _ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    _body = "pre-2.16.1 nudge"
    _sig = hmac.new(N._key, ("%s\n%s" % (_ts, _body)).encode("utf-8"),
                    hashlib.sha256).hexdigest()
    (WATCH / "nudge_legacy.txt").write_text(
        "%s\n%s\n%s" % (_sig, _ts, _body), encoding="utf-8")
    ok("a legacy 3-line nudge reaches nobody",
       N.read_pending(ns=None) == [] and N.read_pending(ns="alice") == [],
       "it carries no owning profile, so there is no honest way to deliver it")
    ok("...and is discarded rather than quarantined",
       not (WATCH / "nudge_legacy.txt").exists()
       and not any(WATCH.glob("*legacy*")),
       "it is a format this build cannot attribute, not evidence of tampering")

    # =========================================================================
    print("\n=== 5. Logout takes the pending nudges with it ===")
    # =========================================================================
    # Sent in a tight loop ON PURPOSE. The filename was nudge_<ms>.txt,
    # so two sends inside one millisecond produced the same path and the
    # second overwrote the first -- across profiles. It surfaced as the
    # flush count below being wrong, on a machine whose clock happened to
    # be coarse enough to collide, which is a terrible way to find out.
    _pre = len(list(WATCH.glob("nudge_*.txt")))
    for _i in range(8):
        N.send("burst-%d" % _i, ns="carol")
    ok("eight nudges sent back to back are eight FILES",
       len(list(WATCH.glob("nudge_*.txt"))) - _pre == 8,
       "same-millisecond sends must not share a filename")
    ok("...and all eight are still readable",
       len(N.read_pending(ns="carol")) == 8)
    N.flush(ns="carol")

    N.send("a1", ns="alice")
    N.send("a2", ns="alice")
    N.send("b1", ns="bob")
    ok("flush removes exactly the signing-out profile's nudges",
       N.flush(ns="alice") == 2)
    ok("...and leaves everyone else's alone",
       [e["content"] for e in N.read_pending(ns="bob")] == ["b1"])
    ok("there is no flush-all", "def flush_all" not in
       io.open(os.path.join(_HERE, "aiq_nudge.py"), encoding="utf-8").read(),
       "one signing-out user must not be able to clear everybody's directives")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)


# =============================================================================
print("\n=== 6. Wired into the app, not just available ===")
# =============================================================================
# Both of the checks below were written GLOBALLY the first time and both went
# red against correct code -- the same two traps this project keeps re-walking
# into, in the very file written to catch them.
#
#   "_owner_gate(request) ... not in MAIN" -- true of the nudge endpoint, and
#   false of main.py, because six OTHER endpoints are legitimately owner-only.
#   A whole-file search cannot answer a question about one function.
#
#   MAIN.split("aiq_nudge.read_pending(")[1] -- the first occurrence is inside
#   the COMMENT explaining what read_pending quarantines. The assertion read
#   prose and reported the consumer unscoped.
#
# So: comments stripped, and the endpoint located by AST rather than guessed at
# by line offset, which drifts the moment anything above it is edited.
def _code_only(src):
    out = []
    for line in src.splitlines():
        out.append("" if line.lstrip().startswith("#") else line.split("#", 1)[0])
    return "\n".join(out)


MAIN_CODE = _code_only(MAIN)


def _func_src(name):
    for n in ast.walk(ast.parse(MAIN)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return ast.get_source_segment(MAIN, n) or ""
    return ""


_SEND = _func_src("api_aiq_nudge")
ok("the send endpoint was found by name", bool(_SEND),
   "if this is empty every check under it passes without looking at anything")
ok("the send endpoint is no longer owner-only",
   "_owner_gate(" not in _code_only(_SEND),
   "this is the call that stopped every non-owner steering their own session")
ok("...and the removal is explained where it happened, not just done",
   "_owner_gate" in _SEND,
   "the reason it stopped being owner-only belongs next to the code, or the "
   "next person restores the gate as a safety improvement")
ok("...and it checks the revocable policy instead",
   "_ap.nudge_allowed(_uname)" in _code_only(_SEND))
ok("...and it stays localhost-only",
   "_is_local_client(request)" in _code_only(_SEND),
   "opening this up per profile must not also open it up to the network")
ok("the sender's namespace is signed into the nudge",
   "aiq_nudge.send(message, ns=_nudge_ns)" in _code_only(_SEND))

# Every consumer, not just the first -- one unscoped read is the whole bug, and
# a second one added later would hide behind a check that stops at [1].
_reads = MAIN_CODE.split("aiq_nudge.read_pending(")[1:]
ok("the consumer call site was found in code", len(_reads) >= 1,
   "found %d -- the earlier version of this counted a comment as a call site"
   % len(_reads))
ok("EVERY consumer reads only this turn's namespace",
   bool(_reads) and all("ns=_ws_ns," in r[:400] for r in _reads),
   "unscoped, it injected whatever the last person left behind")
ok("logout flushes before the session is destroyed",
   MAIN.index("aiq_nudge.flush(ns=") <
   MAIN.index("_session.destroy_session(request.cookies.get(_AUTH_COOKIE))"),
   "afterwards there is no session left to ask which namespace to flush")
ok("the directive no longer claims to come from Todd",
   "directive from Todd" not in MAIN,
   "it said that for every profile, telling the model something untrue about "
   "who it was taking instruction from")

ok("nudge_allowed defaults to ON",
   '"nudge_allowed": True,' in AP,
   "an ability every profile has and the owner switches OFF -- not a grant")
ok("...and is validated as a boolean",
   'nudge_allowed must be a boolean' in AP)
ok("...with a fail-OPEN accessor, like its siblings",
   'get_policy(username).get("nudge_allowed", True)' in AP,
   "a transient policy-store error must not silently take away a live control")

_AUTH = io.open(os.path.join(os.path.dirname(_HERE), "frontend", "js",
                             "auth.js"), encoding="utf-8").read()
ok("Access Controls can revoke it", 'id="ac-nudge"' in _AUTH)
ok("...defaulting to allowed when the key is absent",
   'a.nudge_allowed !== false' in _AUTH,
   "=== true would show existing profiles as revoked the moment this ships")
ok("...and saves it", "nudge_allowed: !!el(\"ac-nudge\").checked," in _AUTH)


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
