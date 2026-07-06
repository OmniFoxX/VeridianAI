#!/usr/bin/env python3
"""
test_expression_engine.py -- hardened conformance suite for expression_engine.

WHY THIS EXISTS
---------------
The Build Battle that produced expression_engine.py crowned a winner whose
tests never exercised: function calls, multi-argument calls, the ** operator,
boolean and/or, or trailing-token validation. Every one was broken in the
"winning" script (sqrt(9) raised "Undefined identifier", 2 ** 3 returned 2,
a and b raised "Undefined identifier: and", 2 3 silently returned 2). This
suite locks down those paths plus the core ones, so a future battle cannot
crown a winner that regresses them.

USAGE (gate a Build Battle on the exit code):
    python test_expression_engine.py      # exit 0 = all pass, 1 = any failure

Covers parse() evaluation, lint() validation, and the parse->eval_ast replay.
"""
import math
import sys

import expression_engine as ee

_passed = 0
_failed = 0
_failures = []


def _ok(label):
    global _passed
    _passed += 1


def _bad(label, detail):
    global _failed
    _failed += 1
    _failures.append(f"{label}  ::  {detail}")


def _eq(a, b, tol=1e-9):
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    try:
        return abs(a - b) <= tol
    except TypeError:
        return a == b


def val(expr, want, **kw):
    """parse(expr)['result'] must equal want."""
    label = f"parse {expr!r}"
    try:
        got = ee.parse(expr, **kw)["result"]
    except Exception as e:
        return _bad(label, f"raised {type(e).__name__}: {e}")
    _ok(label) if _eq(got, want) else _bad(label, f"got {got!r} want {want!r}")


def typ(expr, want_type, **kw):
    label = f"type {expr!r}"
    try:
        got = ee.parse(expr, **kw)["type"]
    except Exception as e:
        return _bad(label, f"raised {type(e).__name__}: {e}")
    _ok(label) if got == want_type else _bad(label, f"got type {got!r} want {want_type!r}")


def err(expr, exc=Exception, **kw):
    """parse(expr) must raise exc."""
    label = f"parse {expr!r} -> {exc.__name__}"
    try:
        got = ee.parse(expr, **kw)["result"]
    except exc:
        return _ok(label)
    except Exception as e:
        return _bad(label, f"raised {type(e).__name__}: {e} (wanted {exc.__name__})")
    _bad(label, f"no error; got {got!r}")


def lint_valid(expr, want_valid, **kw):
    label = f"lint {expr!r} valid=={want_valid}"
    try:
        res = ee.lint(expr, **kw)
    except Exception as e:
        return _bad(label, f"raised {type(e).__name__}: {e}")
    _ok(label) if res["valid"] is want_valid else _bad(label, f"got {res}")


def rt(expr, **kw):
    """parse(mode='both') and eval_ast(ast) must agree."""
    label = f"roundtrip {expr!r}"
    try:
        r = ee.parse(expr, mode="both", **kw)
        v2 = ee.eval_ast(r["ast"])["result"]
    except Exception as e:
        return _bad(label, f"raised {type(e).__name__}: {e}")
    _ok(label) if _eq(r["result"], v2) else _bad(label, f"parse={r['result']!r} eval_ast={v2!r}")


# ----- arithmetic & precedence -----
val("2 + 2 * 3", 8); val("(2 + 2) * 3", 12); val("2 - 3 - 4", -5)
val("10 / 4", 2.5); val("10 % 3", 1); val("-5 + 3", -2); val("-(-5)", 5)

# ----- exponent (was: silently dropped) -----
val("2 ** 3", 8); val("2 ** 10", 1024); val("2 ** 3 ** 2", 512)
val("(2 ** 3) ** 2", 64); val("-2 ** 2", -4); val("2 ** -1", 0.5)

# ----- comparisons -----
val("5 > 3", True); val("3 > 5", False); val("5 >= 5", True)
val("4 <= 3", False); val("2 == 2", True); val("2 != 3", True)

# ----- boolean and/or/not (was: entirely broken) -----
val("5 > 3 and 1 < 2", True); val("5 < 3 and 1 < 2", False)
val("1 > 2 or 3 > 1", True); val("1 > 2 or 3 < 1", False)
val("not (1 > 2)", True); val("not (1 < 2)", False)
val("5 > 3 and 2 > 1 or 1 > 9", True); val("1 < 2 and 3 < 4 and 5 < 6", True)

# ----- constants -----
val("PI", math.pi); val("E", math.e); val("TAU", 2 * math.pi); val("PI * 2", 2 * math.pi)

# ----- variables / assignment / multi-statement -----
val("x + 1", 6, variables={"x": 5}); val("a = 5", 5)
val("a = 5; a * 2", 10); val("a = 2; b = 3; a * b + 1", 7)

# ----- functions (was: all broken) -----
val("sqrt(9)", 3.0); val("sqrt(2)", math.sqrt(2)); val("abs(-7)", 7)
val("floor(3.7)", 3); val("ceil(3.2)", 4); val("round(3.14159, 2)", 3.14)
val("min(3, 7)", 3); val("max(3, 7, 2)", 7); val("clamp(15, 0, 10)", 10)
val("clamp(-3, 0, 10)", 0); val("pow(2, 5)", 32); val("factorial(5)", 120)
val("log(E)", 1.0); val("log10(100)", 2.0); val("sin(0)", 0.0); val("cos(0)", 1.0)
val("max(1, 2) + min(3, 4)", 5); val("sqrt(sqrt(16))", 2.0)  # nested calls

# ----- result type tagging -----
typ("2 + 2", "int"); typ("2.0 + 1", "float"); typ("5 > 3", "bool"); typ("sqrt(9)", "float")

# ----- error handling (several were silent before) -----
err("2 3", ee.ParseError)          # trailing tokens (was: returned 2)
err("5 +", ee.ParseError)          # incomplete
err("(2 + 3", ee.ParseError)       # unbalanced
err("x + 1", ee.ParseError)        # undefined variable
err("foo(3)", ee.ParseError)       # unknown function
err("1.2.3", ee.ParseError)        # malformed number
err("PI = 5", ee.ParseError)       # reassign constant
err("1 / 0", ZeroDivisionError)    # division by zero

# ----- lint() validation -----
lint_valid("2 + 3 * 4", True); lint_valid("(1 + 2) * 3", True)
lint_valid("1 +", False); lint_valid("(1 + 2", False)
lint_valid("x + 1", True, variables={"x": 1}); lint_valid("x + 1", False)

# ----- parse -> eval_ast replay round-trip -----
rt("2 + 2 * 3"); rt("sqrt(16) + PI"); rt("max(3, 7, 2)"); rt("factorial(5)")
rt("2 ** 3 ** 2"); rt("5 > 3 and 1 < 2"); rt("1 > 2 or 3 > 1"); rt("not (1 > 2)")
rt("a = 5; a * 2"); rt("clamp(15, 0, 10)"); rt("sin(0) + cos(0)")

print("=" * 64)
print(f"expression_engine conformance: {_passed} passed, {_failed} failed")
if _failures:
    print("FAILURES:")
    for fl in _failures:
        print("  -", fl)
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
