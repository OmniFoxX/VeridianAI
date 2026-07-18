"""
test_intent_scope.py -- SUB-TASK 1 gate: scope classification + persistence
===========================================================================

Run: python test_intent_scope.py
Deliberately separate from test_action_intent.py (sub-task 2) so a
regression attributes to one layer without a second cold-load cycle.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import intent_scope as isc  # noqa: E402

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


print("== phrasing -> scope ==")
CASES = [
    # observe-only phrasings
    ("just note it for next time", isc.OBSERVE),
    ("review your work and note where you could do better going forward",
     isc.OBSERVE),
    ("keep that in mind for future reference", isc.OBSERVE),
    ("don't apply this yet, file that away", isc.OBSERVE),
    ("observe only: prefer shorter variable names", isc.OBSERVE),
    # apply-immediately phrasings
    ("go ahead and apply that", isc.APPLY),
    ("from now on use tabs", isc.APPLY),
    ("always do a syntax check after saving", isc.APPLY),
    ("apply it now please", isc.APPLY),
    ("effective now, save to the projects folder", isc.APPLY),
    # apply-with-confirmation phrasings
    ("ask me first before changing style", isc.CONFIRM),
    ("check with me before applying improvements", isc.CONFIRM),
    ("run it by me before you apply anything", isc.CONFIRM),
    ("you can improve things but get my approval", isc.CONFIRM),
]
for text, want in CASES:
    got, phrase = isc.classify_scope(text)
    check(f"{want:>23} <- {text[:44]!r}", got == want,
          f"got {got} (phrase={phrase})")

print("== precedence: confirmation beats apply ==")
got, phrase = isc.classify_scope(
    "go ahead and improve things, but ask me first")
check("confirm wins over apply", got == isc.CONFIRM, f"{got}/{phrase}")

print("== ambiguity defaults to observe-only (safer direction) ==")
for text in ("review your work, note where you could do better",
             "code quality matters",
             "be excellent",
             ""):
    got, phrase = isc.classify_scope(text)
    check(f"default observe <- {text[:40]!r}",
          got == isc.OBSERVE and (phrase is None or text != ""),
          f"got {got}")
got, phrase = isc.classify_scope("review your work, note improvements")
check("default carries matched=None for inspectability",
      phrase is None or isinstance(phrase, str))

print("== fallback text (model paraphrased scope away) ==")
got, phrase = isc.classify_scope(
    "code_review|check quality of output",           # paraphrase, no scope
    "please remember to review, but just note it for next time")
check("scope recovered from user text", got == isc.OBSERVE
      and phrase is not None, f"{got}/{phrase}")

print("== never raises ==")
check("None input", isc.classify_scope(None) == (isc.OBSERVE, None))
check("bytes-ish input", isc.classify_scope(12345)[0] == isc.OBSERVE)

print("== persistence shape (metadata round-trip) ==")
# Simulates what main.py stores and what scoped_instruction_names reads
# back on a later turn -- persisted, not re-inferred (spec requirement).
kb = {"successful": {
    "code_review": {"value": "note improvements",
                    "metadata": {"scope": isc.OBSERVE,
                                 "scope_matched": "for next time"}},
    "tabs_rule": {"value": "use tabs",
                  "metadata": {"scope": isc.APPLY}},
    "style_conf": {"value": "style changes",
                   "metadata": {"scope": isc.CONFIRM}},
    "task:abc:fib": {"value": "steps...", "metadata": {"source":
                     "auto_task_done"}},
}, "unsuccessful": {}}
names = isc.scoped_instruction_names(kb)
check("observe+confirm retrievable later", names == ["code_review",
      "style_conf"], str(names))
check("apply-immediately not cited as restraint", "tabs_rule" not in names)
check("legacy/no-scope entries not cited", "task:abc:fib" not in names)

print("== gloss rendering ==")
check("observe gloss forbids current task",
      "do NOT apply" in isc.scope_gloss(isc.OBSERVE))
check("confirm gloss demands go-ahead",
      "WAIT" in isc.scope_gloss(isc.CONFIRM))
check("unknown scope falls back safe",
      "do NOT apply" in isc.scope_gloss("bogus"))

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
