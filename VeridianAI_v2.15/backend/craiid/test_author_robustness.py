#!/usr/bin/env python3
"""Robustness regression for the CRAIID Author (#69 follow-up).

Every new OracleAI install starts with little or no history, so the Author's
context reconstruction MUST work in all data scenarios - empty, minimal,
malformed - without dying, stalling, or raising. This test drives
prepare_warm_instance() across those scenarios (with the source paths pointed
at throwaway temp dirs) and asserts it always returns a valid result dict and
never raises.

Run: python test_author_robustness.py   (exit 0 = PASS, 1 = FAIL)
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import craiid_author as A  # noqa: E402


def _point_paths_at(d: Path) -> None:
    """Redirect the Author's module-level source paths at a temp sandbox."""
    A._CHAT_MEMORY_FILE = d / "chat_memory.json"
    A._ARCHIVES_DIR = d / "archives"
    A._VLTS_DIR = d / "vlts_archives"
    A._RECONSTRUCTS_DIR = d / "reconstructs"


def _scenario(name, build):
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _point_paths_at(d)
        try:
            build(d)
        except Exception as e:  # test-setup failure, not an Author failure
            return name, False, f"setup raised: {e}"
        try:
            res = A.prepare_warm_instance({"trigger": "test", "fatigue_score": 0.9})
        except BaseException as e:  # the Author must NEVER raise (incl. stalls->KbInt)
            return name, False, f"prepare_warm_instance RAISED {type(e).__name__}: {e}"

        if not (isinstance(res, dict) and res.get("status") in ("ok", "partial", "error")):
            return name, False, f"bad result: {res!r}"
        # A non-raising run must still produce a written reconstruction doc.
        op = res.get("output_path")
        if not op or not Path(op).exists():
            return name, False, f"no reconstruction written (status={res.get('status')})"
        total = res.get("summary", {}).get("total_entries")
        return name, True, f"status={res['status']} entries={total} err={res.get('error')}"


def _b_empty(d):                       # fresh install: nothing exists
    pass


def _b_single(d):                      # one chat turn, no archives
    (d / "chat_memory.json").write_text(
        json.dumps([{"role": "user", "content": "first ever message"}]),
        encoding="utf-8")


def _b_dict_messages(d):               # {"messages":[...]} shape, a few turns
    (d / "chat_memory.json").write_text(
        json.dumps({"messages": [{"role": "user", "content": f"m{i}"} for i in range(3)]}),
        encoding="utf-8")


def _b_malformed_chat(d):              # corrupt chat_memory.json
    (d / "chat_memory.json").write_text("{ this is not valid json", encoding="utf-8")


def _b_malformed_archive(d):           # valid chat + a junk archive file
    (d / "chat_memory.json").write_text(json.dumps([{"role": "user", "content": "hi"}]), encoding="utf-8")
    (d / "archives").mkdir(parents=True, exist_ok=True)
    (d / "archives" / "archives_0001.json").write_text("<<not json>>", encoding="utf-8")


def _b_wrong_structure(d):             # chat_memory is a bare int
    (d / "chat_memory.json").write_text("42", encoding="utf-8")


def _b_normal(d):                      # healthy-ish data
    (d / "chat_memory.json").write_text(
        json.dumps([{"role": "user", "content": f"turn {i}"} for i in range(8)]),
        encoding="utf-8")
    (d / "archives").mkdir(parents=True, exist_ok=True)
    (d / "archives" / "archives_0001.json").write_text(
        json.dumps([{"insight": "x", "score": 0.5}]), encoding="utf-8")


SCENARIOS = [
    ("fresh_install_empty",   _b_empty),
    ("single_chat_entry",     _b_single),
    ("dict_messages_shape",   _b_dict_messages),
    ("malformed_chat_json",   _b_malformed_chat),
    ("malformed_archive",     _b_malformed_archive),
    ("wrong_structure_int",   _b_wrong_structure),
    ("normal_data",           _b_normal),
]


def main() -> int:
    fails = []
    for name, build in SCENARIOS:
        n, ok, detail = _scenario(name, build)
        print(f"  [{'PASS' if ok else 'FAIL'}] {n:22s} {detail}")
        if not ok:
            fails.append(n)
    if fails:
        print(f"Author robustness: FAIL ({len(fails)} scenario(s): {fails})")
        return 1
    print(f"Author robustness: ALL {len(SCENARIOS)} SCENARIOS PASS (no raise/stall on any data shape)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
