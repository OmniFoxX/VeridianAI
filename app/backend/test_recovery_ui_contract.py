# -*- coding: utf-8 -*-
"""The recovery UI's contract with the backend.

The wording IS the safety feature here, so it is asserted rather than trusted:
a later tidy-up that softens "permanently unreadable" into "will be reset"
would remove the only warning somebody gets before destroying a year of work,
and nothing else in the system would notice.

Also checks the UI sends what the endpoints expect -- a mismatch would silently
create every profile as recoverable, which fails SAFE but means the sovereign
option quietly does not exist.

    python test_recovery_ui_contract.py
"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_FE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")

def _joined(src):
    """Source with adjacent string literals joined.

    Both languages build these messages by concatenation, so a phrase the user
    reads as one sentence is several literals in the file. Searching the raw
    source for the assembled text fails on the line break -- which looked like
    missing warnings on the first run of this file, and was not.

    Python:  "one "\n"two"      -> "one two"
    JS:      'one ' +\n'two'    -> 'one two'
    """
    import re as _re
    out = _re.sub(r'"\s*\n\s*"', "", src)            # python implicit concat
    out = _re.sub(r"'\s*\+\s*\n\s*'", "", out)      # js explicit concat
    out = _re.sub(r'"\s*\+\s*\n\s*"', "", out)      # js, double-quoted
    return out


_AUTH_RAW = io.open(os.path.join(_FE, "js", "auth.js"), encoding="utf-8").read()
_MAIN_RAW = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "main.py"), encoding="utf-8").read()
AUTH_JS = _joined(_AUTH_RAW)
MAIN_PY = _joined(_MAIN_RAW)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


print("== the choice is offered, and both outcomes are stated ==")
ok("a recovery radio group exists", 'name="ua-recovery"' in AUTH_JS)
ok("recoverable is the default", 'id="ua-rec-yes" value="1" checked' in AUTH_JS)
ok("the sovereign option exists", 'id="ua-rec-no"' in AUTH_JS)
ok("option 1 says the owner can read it too",
   "you are able to read this profile" in AUTH_JS)
ok("option 2 says nobody can read it",
   "not you, not this app" in AUTH_JS)
ok("option 2 states the loss, in the same breath",
   "the data is gone permanently and no one can get it back" in AUTH_JS)
ok("...and it is emphasised, not buried",
   "<strong>If the password is lost" in AUTH_JS)

print("\n== the UI sends what the endpoint reads ==")
ok("UI sends owner_recovery", "owner_recovery: ownerRecovery" in AUTH_JS)
ok("endpoint reads owner_recovery", 'payload.get("owner_recovery", True)' in MAIN_PY)
ok("endpoint defaults to TRUE (the safe one)",
   'payload.get("owner_recovery", True)' in MAIN_PY)
ok("UI also defaults to TRUE if the control is missing",
   "var ownerRecovery = !(recNo && recNo.checked)" in AUTH_JS)

print("\n== the destructive reset is two-stage, like Burn ==")
ok("stage 1 is a read-and-confirm", "I understand, continue" in AUTH_JS)
ok("stage 2 requires typing the exact string",
   'typed !== "DISCARD DATA"' in AUTH_JS)
ok("cancelling says nothing changed",
   AUTH_JS.count("nothing was changed") >= 2)
ok("the backend requires the same string",
   'payload.get("confirm") != "DISCARD DATA"' in MAIN_PY)
ok("the UI passes it through", 'send("DISCARD DATA")' in AUTH_JS)

print("\n== the warning text itself ==")
for phrase, why in [
        ("PERMANENTLY UNREADABLE", "states the outcome in plain capitals"),
        ("conversations, archives, research, learned procedures",
         "names what is lost rather than saying 'data'"),
        ("There is no undo", "rules out an undo explicitly"),
        ("no support path that recovers it", "rules out asking for help later"),
        ("might still be remembered", "suggests trying the password first"),
        ("that was the point of the setting",
         "reminds them this was a deliberate choice, not a fault")]:
    ok("warning %s" % why, phrase in MAIN_PY, phrase)

print("\n== the recoverable path says something different ==")
ok("recovered outcome reports data intact",
   "their data was recovered and is intact" in MAIN_PY.lower())
ok("the two outcomes are distinguishable by the caller",
   '"outcome": outcome' in MAIN_PY and '"recovered"' in MAIN_PY
   and '"discarded"' in MAIN_PY)

print("\n== a profile the owner cannot recover is visible as such ==")
ok("a badge is rendered", "password-only" in AUTH_JS)
ok("the badge explains itself on hover",
   "You cannot " in AUTH_JS and "recover it" in AUTH_JS)
ok("it is driven by the KEY, not a stored setting",
   "/recovery" in AUTH_JS and "d.recovery_enabled" in AUTH_JS)

print("\n== granting recovery is refused to anyone but the user ==")
ok("endpoint refuses a non-self grant",
   "only this profile can grant recovery" in MAIN_PY)
ok("...and says why", "requires the data key" in MAIN_PY)
ok("dropping is available to either party",
   "profile.recovery_disabled" in MAIN_PY)

print("\n== house conventions ==")
ok("uses oracleConfirm, not native confirm()",
   "oracleConfirm(" in AUTH_JS and "window.confirm(" not in AUTH_JS)
ok("uses oraclePrompt with a native fallback",
   "window.oraclePrompt" in AUTH_JS and "window.prompt(" in AUTH_JS)
ok("my additions are pure ASCII",
   all(ord(c) < 128 for c in
       _AUTH_RAW[_AUTH_RAW.index("// Owner-initiated password reset."):
                 _AUTH_RAW.index("  async function uaDelete(")]))

failed = [n for n, c in _results if not c]
print("\n%d/%d passed." % (len(_results) - len(failed), len(_results)))
if failed:
    print("FAILED:")
    for n in failed:
        print("  - " + n)
    sys.exit(1)
