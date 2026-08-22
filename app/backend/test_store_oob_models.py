#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_store_oob_models.py -- a fresh Store install finds a model to run.

WHAT WAS WRONG

store_launch.py picked the primary (Toga) model with a single default:

    sage_file = os.environ.get("SAGE_MODEL_FILE",
                               "all_hands_openhands_lm_7b_v0_1_Q6_K_L.gguf")

That file is not bundled. On a fresh Store install -- no SAGE_MODEL_FILE, no
user copy in sage_data/models -- _resolve_model found nothing, returned "", and
tier_launcher skipped the tier. The app opened with an EMPTY PRIMARY MODEL SLOT.

That is the first thing a Store reviewer sees, and there is nothing on screen
to explain it. The daemon and embed slots were already ordered candidate lists
and resolved fine, which is why this stayed invisible: two of three tiers came
up, so the app looked like it was working.

THE SAME BUG, THE OTHER WAY ROUND, IS ALREADY IN THE HISTORY

start.bat records it at line ~125:

    the MSIX found bundled_models\\qwen2.5_coder_1.5b_instruct.gguf and the
    portable looked for ..._base.gguf, found nothing, skipped the tier

The portable launcher was given a candidate list then. The Store launcher was
not -- so the fix landed in the path used by people who already have the app,
and missed the one used by the reviewer deciding whether to publish it.

WHAT THIS ASSERTS

Not "the list contains the right string" -- that is the assertion that would
have passed while the bug shipped, because the list WAS what somebody intended.
It resolves each tier the way the launcher does, against the files that are
actually in bundled_models, and requires the two chat tiers to land on
something real.

    python test_store_oob_models.py
"""
import ast
import io
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("\n          -> " + str(detail)) if detail and not cond else ""))


_SL_PATH = os.path.join(_ROOT, "store_launch.py")
SL = io.open(_SL_PATH, encoding="utf-8").read() if os.path.exists(_SL_PATH) else ""
ok("store_launch.py is present", bool(SL), _SL_PATH)

_BUNDLED = os.path.join(_ROOT, "bundled_models")
_shipped = set()
if os.path.isdir(_BUNDLED):
    _shipped = {f for f in os.listdir(_BUNDLED) if f.lower().endswith(".gguf")}
ok("bundled_models contains at least one model", bool(_shipped),
   "%s -> %r" % (_BUNDLED, sorted(_shipped)))


# =============================================================================
print("\n=== Each tier's candidate list, read off the AST ===")
# =============================================================================
# Read from the source rather than importing: build_env() touches the data
# directory and the environment, and this only needs the literals.
def _candidates(var):
    """The list literal assigned to `var`, ignoring the env-var branch.

    Written to survive the shape `x = [env] if env else [a, b, c]`, which is
    how all three slots are written -- the interesting half is the else.
    """
    for n in ast.walk(ast.parse(SL)):
        if not isinstance(n, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == var for t in n.targets):
            continue
        v = n.value
        if isinstance(v, ast.IfExp):
            v = v.orelse
        if isinstance(v, ast.List):
            return [e.value for e in v.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if isinstance(v, ast.Call):      # os.environ.get(name, "default")
            return [a.value for a in v.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)
                    and a.value.endswith(".gguf")]
    return []


TIERS = (("sage_file", "primary (Toga)", True),
         ("daemon_file", "daemon", True),
         ("embed_file", "embed", False))

for _var, _label, _required in TIERS:
    _cands = _candidates(_var)
    ok("%s has candidates" % _label, bool(_cands), _var)

    # THE ACTUAL QUESTION: does any candidate exist in what ships?
    _hit = next((c for c in _cands if c in _shipped), None)
    if _required:
        ok("...and a fresh install resolves the %s tier" % _label,
           _hit is not None,
           "none of %r is in bundled_models (%r).\n"
           "          On a machine with no user-supplied model this tier is "
           "SKIPPED and its slot comes up empty -- which for the primary tier "
           "is the first thing a Store reviewer sees." % (_cands, sorted(_shipped)))
    else:
        # The embed tier is allowed to be absent: tier_launcher says so, and
        # semantic search falls back to lexical. Reported, never failed.
        print("  ----  %s tier resolves: %s (optional -- lexical fallback)"
              % (_label, _hit or "NOTHING"))

    if _hit:
        print("        %s -> %s" % (_label, _hit))


# =============================================================================
print("\n=== The list shape, so the next slot cannot regress to one name ===")
# =============================================================================
ok("every chat tier is an ordered LIST, not a lone default",
   len(_candidates("sage_file")) > 1 and len(_candidates("daemon_file")) > 1,
   "a single default cannot degrade: if that one file is absent the tier is "
   "skipped, and 'skipped' looks identical to 'broken' from the UI")
ok("the user's own choice still wins",
   'os.environ.get("SAGE_MODEL_FILE"' in SL,
   "SAGE_MODEL_FILE must still override everything, or somebody who picked a "
   "model gets overruled by a bundled one")
ok("the bundled instruct model is reachable from the primary tier",
   "qwen2.5_coder_1.5b_instruct.gguf" in _candidates("sage_file"),
   "this is the model that is actually in the package")


_failed = [n for n, c in _results if not c]
print("\n  %d/%d passed." % (len(_results) - len(_failed), len(_results)))
if _failed:
    print("  FAILED:")
    for n in _failed:
        print("    - " + n)
sys.exit(1 if _failed else 0)
