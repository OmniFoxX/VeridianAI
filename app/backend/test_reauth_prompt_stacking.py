#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_reauth_prompt_stacking.py -- the unlock prompt is visible, and one
unlock authorises one action.

WHAT TODD HIT (2026-08-23)

    "the Password required for this action popup actually stays behind the
     profile creation submenu ... I clicked it 3 times and made three new
     requests for new accounts on accident"

TWO SEPARATE DEFECTS, and only fixing both makes it safe.

1. VISIBILITY. reauth.js drew its overlay at z-index 10000. The profile
   overlays in auth.js are 99999, 100000, 100002 and 100003. So the prompt
   raised BY the profile submenu rendered BEHIND the profile submenu. From
   where the person sits the Create button simply did nothing, so they press
   it again.

2. REPLAY. requireUnlock already refused to stack two prompts -- a second
   caller joined the first one's promise. But the fetch interceptor then
   replayed EVERY joined request on that single unlock. Three clicks, one
   password, three accounts.

   This is the one that had to be fixed on its own terms. Fix only the
   z-index and the bug goes quiet without going away: any future dialog that
   sits above 100050, any slow status call, any double-click on a trackpad,
   and one authorisation again authorises N actions. A person types their
   password to approve the thing they are looking at, once.

WHY THE RACE RE-TEST IS LOAD-BEARING

unlockInternal awaits status() before it claims the prompt. Two clicks a few
milliseconds apart can both pass an entry test taken before that await, and
then both open a prompt and both replay. The claim is only atomic if the test
happens after the await with no await between it and the assignment.

    python test_reauth_prompt_stacking.py
"""
import io
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_FRONTEND = os.path.join(os.path.dirname(_HERE), "frontend")

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


def _read(*parts):
    return io.open(os.path.join(*parts), encoding="utf-8").read()


REAUTH = _read(_FRONTEND, "js", "reauth.js")
AUTH = _read(_FRONTEND, "js", "auth.js")
CHAT = _read(_FRONTEND, "js", "chat.js")
INDEX = _read(_FRONTEND, "index.html")
CSS = _read(_FRONTEND, "css", "styles.css")


def _code_only(src):
    """Line comments and block comments out. Every check below is about what
    the code does; this project has now had five assertions go red or green on
    a COMMENT, including one in the test written to stop exactly that."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(ln.split("//", 1)[0] for ln in src.splitlines())


R_CODE = _code_only(REAUTH)


# =============================================================================
print("=== 1. The layer ladder: dialogs < unlock prompt < tooltip ===")
# =============================================================================
# Both rungs were broken, for the same reason and with the same symptom: an
# element painted into the DOM, correct in every respect a DOM check can see,
# and covered on screen by something that happened to be numbered higher.
#
#   * the unlock prompt at 10000, under profile overlays at 100002/100003
#   * the shared a11y tooltip at 100000, under those same overlays
#
# Collected from the source rather than listed, so a NEW dialog numbered above
# either of them fails here rather than being found by a person clicking a
# button that looks dead.
_mine = [int(m) for m in re.findall(r"z-index:\s*(\d+)", R_CODE)]
ok("reauth.js sets a z-index", bool(_mine), R_CODE.count("z-index"))
_prompt_z = max(_mine) if _mine else 0

_tipm = re.search(r"\.a11y-tip\s*\{[^}]*?z-index:\s*(\d+)", CSS, re.S)
ok("the shared tooltip's z-index was found", _tipm is not None,
   "a11y-tooltip.js appends ONE tip to document.body; if this rule moved, "
   "the check below cannot see it")
_tip_z = int(_tipm.group(1)) if _tipm else 0

# Everything else that paints an overlay. The tooltip's own rule is excluded
# by value -- it is the top rung, not a competitor for it.
_others = []
for _label, _src in (("auth.js", AUTH), ("chat.js", CHAT),
                     ("index.html", INDEX), ("styles.css", CSS)):
    for _m in re.finditer(r"z-index:\s*(\d+)", _code_only(_src)):
        _v = int(_m.group(1))
        if _v not in (_tip_z,):
            _others.append((_label, _v))
_top = max(_others, key=lambda t: t[1]) if _others else ("?", 0)
print("        dialogs reach %d (%s)  |  unlock prompt %d  |  tooltip %d"
      % (_top[1], _top[0], _prompt_z, _tip_z))

ok("the unlock prompt is above the highest dialog",
   _prompt_z > _top[1],
   "the prompt is at %d and %s reaches %d -- a gate that renders behind the "
   "thing it is gating reads as a dead button" % (_prompt_z, _top[0], _top[1]))
ok("...with headroom, not by one",
   _prompt_z - _top[1] >= 20,
   "only %d above %s(%d): the next dialog added lands on top of the prompt "
   "and this returns silently" % (_prompt_z - _top[1], _top[0], _top[1]))
ok("the tooltip is above the unlock prompt, and so above everything",
   _tip_z > _prompt_z,
   "tooltip %d vs prompt %d. A tip that cannot be SEEN satisfies none of "
   "WCAG 1.4.13 for a sighted user, while passing every DOM-level check -- "
   "the element is there and its text is right." % (_tip_z, _prompt_z))
ok("...with headroom too", _tip_z - _prompt_z >= 20,
   "%d above the prompt" % (_tip_z - _prompt_z))
ok("the old, buried prompt value is gone",
   "z-index:10000;" not in R_CODE,
   "10000 is below all four profile overlays -- this exact number is the bug")
ok("...and the old, buried tooltip value with it",
   _tip_z != 100000,
   "100000 sits under the profile overlays at 100002 and 100003")
ok("the ladder is written down where the top rung is defined",
   "LAYER LADDER" in CSS.upper(),
   "three files pick these numbers independently; without the ladder stated "
   "somewhere, the fourth one guesses")


# =============================================================================
print("\n=== 2. One unlock replays exactly one request ===")
# =============================================================================
ok("unlockInternal reports whether it joined an existing prompt",
   "joined: true" in R_CODE and "joined: false" in R_CODE,
   "the interceptor cannot tell an authorised retry from a piggybacked one "
   "without being told")
ok("the interceptor refuses to replay a joined request",
   re.search(r"if\s*\(\s*!r\.ok\s*\|\|\s*r\.joined\s*\)\s*return res", R_CODE)
   is not None,
   "this is the line that turns three clicks back into one account")
ok("...and it asks unlockInternal, not the public boolean",
   "unlockInternal(msg)" in R_CODE,
   "window.requireUnlock returns only ok, so the interceptor would be blind "
   "to joining and would replay everything again")
ok("the public entry point still returns a plain boolean",
   re.search(r"window\.requireUnlock\s*=.*?\)\.ok;", R_CODE, re.S) is not None,
   "chat.js and the palette call this expecting true/false")


# =============================================================================
print("\n=== 3. Claiming the prompt is atomic across the status() await ===")
# =============================================================================
_body = R_CODE.split("async function unlockInternal")[1].split(
    "window.requireUnlock")[0] if "async function unlockInternal" in R_CODE else ""
ok("unlockInternal was found", bool(_body))
ok("_open is tested BEFORE the status call", _body.lstrip().find("if (_open)")
   < _body.find("await status()"),
   "the cheap path: a prompt already up, joined without a round-trip")
ok("...and AGAIN after it, before the prompt is claimed",
   _body.count("if (_open)") >= 2,
   "await status() yields; two clicks can both pass a single entry test and "
   "both go on to open a prompt and replay")

# The claim must be the very NEXT statement after that guard, or the window
# reopens. Checked structurally, not by eye.
#
# The first version of this searched the span between the guard and the claim
# for "await" and went red on correct code: the guard is a one-line
# `if (_open) return { ok: await _open, ... };`, so its own body sat in the
# span. That await is on the JOINING path -- if it runs, the function has
# already returned and never reaches the claim. Searching a region for a
# keyword answers "does this text appear", not "does this run"; the question
# here is what executes next, so ask that instead.
_tail = _body.rsplit("if (_open)", 1)[1] if _body.count("if (_open)") >= 2 else ""
_after_guard = _tail.split(";", 1)[1] if ";" in _tail else ""
ok("...and the claim is the statement immediately after that guard",
   _after_guard.lstrip().startswith("_open = prompt("),
   "next statement is %r -- anything that yields here lets a second click "
   "through and both callers open a prompt"
   % _after_guard.strip()[:120])


# =============================================================================
print("\n=== 4. Nothing here loosened the gate ===")
# =============================================================================
# Todd's standing rule for the day: no change gets to open a security or HIPAA
# gap. Both fixes above are meant to fail CLOSED -- the worst case is an action
# the person must click once more, never one that happens twice or one that
# happens unauthorised.
ok("a refused unlock still blocks the retry",
   "!r.ok" in R_CODE,
   "cancelling the prompt must return the 401, not the replayed request")
ok("the interceptor still skips /api/reauth itself",
   '"/api/reauth"' in R_CODE,
   "or a refusal from the unlock endpoint prompts for an unlock, forever")
ok("...and still refuses non-replayable bodies",
   'typeof body !== "string"' in R_CODE,
   "re-sending a consumed stream would send something other than what the "
   "person authorised")
ok("...and still only acts on needs_reauth",
   "needs_reauth" in R_CODE)
ok("the server remains the real boundary",
   "the real boundary is the" in REAUTH,
   "this file is a courtesy to the person at the keyboard; if that claim "
   "stops being written down, someone will start trusting it")


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
