#!/usr/bin/env python3
"""Regression test for #69 BUG#1.

BUG#1: the CRAIID periodic jobs (fatigue/ops/llama) were mis-indented INTO the
`except` block of sage_daemon._periodic_worker, so they only ran when an
earlier job raised - i.e. never, during healthy operation. That silently
disabled CRAIID fatigue detection end-to-end.

This test parses sage_daemon.py with the `ast` module and asserts that every
periodic job is CALLED inside a `try` body of `_periodic_worker`, and that no
job is called ONLY inside an `except` handler (the BUG#1 signature). It needs
no running daemon - pure static structure - so it can gate commits/CI.

Run: python test_craiid_jobs_wiring.py   (exit 0 = PASS, 1 = FAIL)
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

SAGE_DAEMON = Path(__file__).resolve().parent / "sage_daemon.py"

# Every job the periodic worker is responsible for running each cadence.
EXPECTED_JOBS = {
    "_job_consolidate_procedural",
    "_job_chain_digest",
    "_job_anomaly_check",
    "_job_llama_progress",
    "_job_ops_snapshot",
    "_job_fatigue_check",
}


def _called_names(node: ast.AST) -> set[str]:
    """Bare-name function calls reachable under `node` (e.g. foo(), not a.b())."""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            out.add(n.func.id)
    return out


def main() -> int:
    tree = ast.parse(SAGE_DAEMON.read_text(encoding="utf-8"), filename=str(SAGE_DAEMON))

    worker = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_periodic_worker"),
        None,
    )
    if worker is None:
        print("FAIL: _periodic_worker() not found in sage_daemon.py")
        return 1

    in_try_body: set[str] = set()
    in_except_only: set[str] = set()
    for t in (n for n in ast.walk(worker) if isinstance(n, ast.Try)):
        body_calls: set[str] = set()
        for stmt in t.body:
            body_calls |= _called_names(stmt)
        handler_calls: set[str] = set()
        for h in t.handlers:
            for stmt in h.body:
                handler_calls |= _called_names(stmt)
        in_try_body |= (body_calls & EXPECTED_JOBS)
        in_except_only |= ((handler_calls - body_calls) & EXPECTED_JOBS)

    fails = []
    missing = EXPECTED_JOBS - in_try_body
    if missing:
        fails.append(f"jobs NOT called in any try-body: {sorted(missing)}")
    if in_except_only:
        fails.append(
            f"jobs called ONLY inside an except handler (BUG#1 signature): "
            f"{sorted(in_except_only)}"
        )

    if fails:
        print("CRAIID jobs-in-try test: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print(f"CRAIID jobs-in-try test: PASS (all {len(EXPECTED_JOBS)} jobs run in the try body)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
