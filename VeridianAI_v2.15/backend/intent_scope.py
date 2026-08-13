"""
intent_scope.py -- Intent-fidelity guardrails, Part 1 (v2.13.2)
================================================================

Two deterministic, dependency-free classifiers (2026-07-17 fibonacci
scope-creep incident):

  1. classify_scope(text)  -- SUB-TASK 1
     Assigns an explicit scope to a standing/persistent instruction:
       observe_only            never apply to the task in progress
       apply_immediately       apply to current and future matching tasks
       apply_with_confirmation flag intent + wait for user go-ahead
     DEFAULT IS observe_only -- the safer failure direction. Phrase
     tables are editable constants, checked in precedence order
     (confirmation > immediate > observe) because confirmation phrases
     often embed "apply" ("ask before applying").

  2. classify_action(...)  -- SUB-TASK 2
     Labels a tool action `requested` or `opportunistic` (with a
     human-readable reason) using ONLY traceable turn evidence: the
     user's request text, this turn's save/verify history, and stored
     scope metadata. Rules are conservative and explainable; the
     deferred plan/execute diffing is the eventual precise version.
     Policy: read-only tools default to requested (normal agentic
     decomposition); EFFECTFUL tools (save_file, code, generate_image,
     remember) need linkage to the request or they're opportunistic.

Everything here is pure logic -- no I/O, no imports beyond stdlib --
so both suites run without a backend.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional, Tuple

__version__ = "2.13.2"

OBSERVE = "observe_only"
APPLY = "apply_immediately"
CONFIRM = "apply_with_confirmation"

# ---------------------------------------------------------------------------
# Sub-task 1: scope classification
# ---------------------------------------------------------------------------
# Checked FIRST (highest precedence).
_CONFIRM_PHRASES = (
    "ask me first", "ask before", "ask first", "check with me",
    "confirm before", "confirm with me", "run it by me", "get my ok",
    "get my okay", "get my approval", "with my permission",
    "clear it with me", "let me approve", "before you apply",
    "before applying",
)
# Negated-application phrases: checked BEFORE the apply pass, because
# "don't apply this yet" contains the substring "apply this" and would
# otherwise false-positive as apply-immediately.
_NEGATION_OBSERVE = (
    "don't apply", "do not apply", "don't change", "do not change",
    "don't do it yet", "do not act", "don't act", "no action",
    "hold off",
)
# Checked second.
_APPLY_PHRASES = (
    "go ahead", "apply it", "apply that", "apply this", "apply now",
    "do it now", "do that now", "starting now", "right away",
    "immediately", "from now on", "always do", "every time",
    "make it so", "put it into effect", "effective now",
)
# Checked third; anything unmatched also lands here (default).
_OBSERVE_PHRASES = (
    "just note", "note it", "note that", "note this", "for next time",
    "next time", "for the future", "for future", "future reference",
    "going forward", "keep in mind", "keep that in mind",
    "don't apply", "do not apply", "don't change", "do not change",
    "no action needed", "just remember", "just log", "observe only",
    "for later", "file that away",
)


def classify_scope(text: str,
                   fallback_text: str = "") -> Tuple[str, Optional[str]]:
    """Classify a standing instruction's scope.

    Returns (scope, matched_phrase). matched_phrase is None when the
    default applied -- store it so the assignment is inspectable later
    instead of re-inferred fresh each time (Todd's sub-task 1 spec).
    Tries `text` first, then `fallback_text` (e.g. the user's raw turn
    text when the model's REMEMBER description paraphrased it away).
    Never raises.
    """
    try:
        for blob in (text or "", fallback_text or ""):
            low = " ".join(str(blob).lower().split())
            if not low:
                continue
            for p in _CONFIRM_PHRASES:
                if p in low:
                    return CONFIRM, p
            for p in _NEGATION_OBSERVE:
                if p in low:
                    return OBSERVE, p
            for p in _APPLY_PHRASES:
                if p in low:
                    return APPLY, p
            for p in _OBSERVE_PHRASES:
                if p in low:
                    return OBSERVE, p
        return OBSERVE, None          # ambiguous -> safer direction
    except Exception:
        return OBSERVE, None


_SCOPE_GLOSS = {
    OBSERVE: ("observe-only — do NOT apply this to the task in "
              "progress; it is a note for future work"),
    APPLY: "apply-immediately — applies to the current and future tasks",
    CONFIRM: ("apply-with-confirmation — flag your intent and WAIT for "
              "the user's go-ahead BEFORE acting on it"),
}


def scope_gloss(scope: str) -> str:
    """Prompt-injection rendering for a stored scope."""
    return _SCOPE_GLOSS.get(scope, _SCOPE_GLOSS[OBSERVE])


# ---------------------------------------------------------------------------
# Sub-task 2: action classification
# ---------------------------------------------------------------------------
# Effectful tools need request linkage; read-only tools are normal
# agentic decomposition.
_EFFECTFUL = {"save_file", "code", "generate_image", "remember",
              "remember_fail"}

# keyword -> tool linkage evidence in the user's request text
_TOOL_KEYWORDS = {
    "save_file": ("save", "write", "create", "make a file", "file",
                  "script", "note", "document"),
    "code": ("run", "execute", "code", "compute", "calculate", "script",
             "program", "test"),
    "generate_image": ("image", "picture", "draw", "generate", "art",
                       "illustration", "photo"),
    "remember": ("remember", "note", "keep in mind", "log", "memorize",
                 "don't forget"),
    "remember_fail": ("remember", "note", "log"),
    "verify_file": ("verify", "check", "confirm", "make sure"),
}

# Retry content-similarity floor: a retry after failure that rewrites
# the file beyond this drift is doing MORE than fixing the failure.
DRIFT_RATIO_FLOOR = 0.6

# ── Literal-spec conformance (Todd's mid-run note, 2026-07-17) ─────
# An opportunistic change isn't only "extra beyond the request" -- it
# can CONTRADICT the request. "Print the first 11 numbers" literally
# asked for printing; a rewrite to return-a-list deletes the requested
# behavior even if it's better style. Deterministic detectors: if the
# user's text contains the term AND the prior content had the feature
# AND the new content dropped it, that's a spec regression -- flagged
# regardless of how small the diff is. Editable table.
_SPEC_TERMS = {
    "print":     ("print(",),
    "return":    ("return ",),
    "docstring": ('"""', "'''"),
    "comment":   ("#",),
    "type hint": ("->",),
    "main guard": ("__main__",),
}


def _spec_regressions(user_text: str, old_content: str,
                      new_content: str) -> List[str]:
    """Literal request terms whose corresponding content feature existed
    in the prior version but was removed in the new one. Never raises."""
    out = []
    try:
        low = " ".join((user_text or "").lower().split())
        old = str(old_content or "")
        new = str(new_content or "")
        for term, feats in _SPEC_TERMS.items():
            if term not in low:
                continue
            had = any(f in old for f in feats)
            has = any(f in new for f in feats)
            if had and not has:
                out.append(term)
    except Exception:
        pass
    return out


def _mentions(user_text: str, words) -> Optional[str]:
    low = " ".join((user_text or "").lower().split())
    for w in words:
        if w in low:
            return w
    return None


def _fname_linked(user_text: str, fname: str) -> bool:
    """The filename (or its stem words) appear in the request."""
    low = (user_text or "").lower()
    f = (fname or "").lower()
    if f and f in low:
        return True
    stem = re.split(r"[.\\/]", f)[0]
    words = [w for w in re.split(r"[_\-\s]+", stem) if len(w) > 3]
    return bool(words) and all(w in low for w in words)


def classify_action(tool: str,
                    target: str,
                    user_text: str,
                    turn_saves: Optional[Dict[str, List[Dict]]] = None,
                    results_acc: Optional[Dict[str, Any]] = None,
                    scoped_instructions: Optional[List[str]] = None,
                    new_content: str = "") -> Tuple[str, str]:
    """Label one tool action.

    Returns (label, reason): label in {"requested", "opportunistic"}.
    Deterministic rules, most specific first. Never raises.

    tool/target: the action (target = filename, query, path...).
    turn_saves:  {fname: [{"content": str, "ok": bool}, ...]} this turn.
    results_acc: the turn's tool_results_acc (for verify failures).
    scoped_instructions: names of stored observe-only/confirm standing
                 instructions, cited as candidate causes when an
                 effectful action has no request linkage.
    new_content: for save_file, the content about to be written (drift
                 check on retries).
    """
    try:
        turn_saves = turn_saves or {}
        results_acc = results_acc or {}
        cause_hint = ""
        if scoped_instructions:
            cause_hint = (" Possible cause: standing instruction "
                          + " / ".join(f"'{s}'"
                                       for s in scoped_instructions[:3])
                          + " (scoped observe-only/confirm).")

        # ── save_file: the fibonacci rules ─────────────────────────
        if tool == "save_file":
            fname = target or ""
            history = turn_saves.get(fname, [])
            if history:
                prior = history[-1]
                prior_ok = bool(prior.get("ok"))
                verify_failed = _verify_failed_for(fname, results_acc)
                if prior_ok and not verify_failed:
                    return ("opportunistic",
                            f"re-save of '{fname}' after a successful, "
                            f"unchallenged save this turn -- redo beyond "
                            f"the request.{cause_hint}")
                # retry after save error or verify failure = recovery...
                prior_content = str(prior.get("content", ""))
                ratio = difflib.SequenceMatcher(
                    None, prior_content,
                    str(new_content or "")).ratio()
                # ...unless it deletes literally-requested behavior --
                # checked FIRST and independent of drift size: a small
                # edit that removes the asked-for print() is worse than
                # a big one that keeps it.
                regress = _spec_regressions(user_text, prior_content,
                                            new_content)
                if regress:
                    return ("opportunistic",
                            f"retry of '{fname}' removed behavior the "
                            f"request literally asked for: "
                            + ", ".join(f"'{r}'" for r in regress)
                            + f" (similarity {ratio:.2f})."
                            + cause_hint)
                if ratio < DRIFT_RATIO_FLOOR:
                    return ("opportunistic",
                            f"retry of '{fname}' rewrote content "
                            f"substantially (similarity "
                            f"{ratio:.2f} < {DRIFT_RATIO_FLOOR}) -- "
                            f"beyond fixing the failure.{cause_hint}")
                return ("requested",
                        f"retry of '{fname}' after failure "
                        f"(recovery; similarity {ratio:.2f})")
            # User explicitly named THIS file -> always requested.
            if _fname_linked(user_text, fname):
                return ("requested", "filename named in the user's request")
            # v2.13.4 proliferation rule (BugSquashNote cascade): a NEW,
            # un-named filename appearing after a failure this turn is
            # abandon-and-restart, not the requested save — even though
            # generic words like 'save'/'file' appear in the request.
            # Distinct signal from content-drift-under-same-name.
            failed_file = _turn_failure(turn_saves, results_acc)
            if failed_file and turn_saves:
                return ("opportunistic",
                        f"new filename '{fname}' appeared mid-retry-"
                        f"cascade (after failure on '{failed_file}') -- "
                        f"abandon-and-restart instead of repairing the "
                        f"original.{cause_hint}")
            if _mentions(user_text, _TOOL_KEYWORDS["save_file"]):
                return ("requested", "save linked to the user's request")
            return ("opportunistic",
                    f"save of '{fname}' has no linkage to the current "
                    f"request.{cause_hint}")

        # ── verify_file: protocol-implied after any save ───────────
        if tool == "verify_file":
            if turn_saves or _mentions(user_text,
                                       _TOOL_KEYWORDS["verify_file"]):
                return ("requested",
                        "verification protocol after save (or asked)")
            return ("requested", "read-only verification")

        # ── other effectful tools: need linkage ────────────────────
        if tool in _EFFECTFUL:
            hit = _mentions(user_text, _TOOL_KEYWORDS.get(tool, ()))
            if hit:
                return ("requested", f"user request mentions '{hit}'")
            return ("opportunistic",
                    f"{tool} has no linkage to the current "
                    f"request.{cause_hint}")

        # ── read-only tools: normal decomposition ──────────────────
        return ("requested", "task decomposition (read-only tool)")
    except Exception as e:
        # Fail toward the quiet label -- classification must never
        # break dispatch.
        return ("requested", f"classifier error ({type(e).__name__})")


def _turn_failure(turn_saves: Dict[str, List[Dict]],
                  results_acc: Dict[str, Any]) -> Optional[str]:
    """Name of a file with a failed save or failed verify this turn,
    else None. Powers the proliferation rule. Never raises."""
    try:
        for f, hist in (turn_saves or {}).items():
            if any(not h.get("ok") for h in (hist or [])):
                return f
            if _verify_failed_for(f, results_acc or {}):
                return f
    except Exception:
        pass
    return None


def _verify_failed_for(fname: str, results_acc: Dict[str, Any]) -> bool:
    """True if a verify_file result this turn references fname and looks
    failed. Mirrors main.py's fail-signal heuristic, scoped to verify."""
    base = (fname or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if not base:
        return False
    fail_sigs = ("not found", "missing", "does not exist", "failed",
                 "error", "no such file", "syntax")
    for k, v in results_acc.items():
        if not str(k).startswith("verify_file:"):
            continue
        if base not in (str(k) + str(v)).lower():
            continue
        if any(s in str(v).lower() for s in fail_sigs):
            return True
    return False


def scoped_instruction_names(kb: Dict[str, Any]) -> List[str]:
    """Extract names of stored standing instructions whose scope is
    observe-only or apply-with-confirmation, for causal citation.
    kb = procedural.get_all(). Never raises."""
    out = []
    try:
        for k, v in (kb.get("successful") or {}).items():
            if not isinstance(v, dict):
                continue
            sc = (v.get("metadata") or {}).get("scope")
            if sc in (OBSERVE, CONFIRM):
                out.append(k)
    except Exception:
        pass
    return sorted(out)
