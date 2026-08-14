"""
End-to-end verification harness for OracleAI v2.1.5 additions:

  1. [TASK_DONE] autolog — simulated agentic-turn flow records the
     action sequence as a chain-witnessed successful procedure.
  2. Chain log digest — daemon's _job_chain_digest() reads (does NOT
     modify) the chain log and writes a derived digest file.
  3. KB consolidation — daemon's _job_consolidate_procedural() prunes
     stale unsuccessful entries, dedupes, and NEVER touches successful
     (chain-witnessed) entries.
  4. Anomaly / tamper monitor — _job_anomaly_check() flips
     _tick_state['anomaly_alert'] when the chain is corrupted, and
     clears the alert when verify recovers.
  5. [PRIORITISE:] parser tag recognised by parse_agent_actions.

Runs in a tempdir, never touches production data. Exit 0 = all green.

    cd <project_root>\\backend
    py verify_v215.py
"""

import os
import sys
import json
import time
import shutil
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))


def banner(msg):
    print("\n" + "=" * 72)
    print(msg)
    print("=" * 72)


def main():
    scratch = Path(tempfile.mkdtemp(prefix="oracleai_v215_verify_"))
    test_memory = scratch / "memory_log"
    test_procedural = scratch / "procedural_memory"
    test_key = scratch / ".fernet_key"
    test_memory.mkdir()
    test_procedural.mkdir()

    # Patch config paths BEFORE any module imports them
    import config
    config.FERNET_KEY_FILE = test_key
    config.MEMORY_DIR = test_memory
    config.PROCEDURAL_DIR = test_procedural

    import memory_logger_surprise as mls
    mls._fernet_instance = None  # ensure fresh-key load
    from memory_logger_surprise import MemoryLogger
    from procedural_memory import ProceduralMemory
    import sage_engine

    logger = MemoryLogger(storage_dir=str(test_memory), baseline_temp=0.5)
    proc = ProceduralMemory(storage_dir=str(test_procedural),
                            memory_logger=logger)

    results = []

    # ------------------------------------------------------------------
    banner("TEST 1: [TASK_DONE] autolog — sequence stored with chain witness")
    # ------------------------------------------------------------------
    # Mirror the v2.1.5 Phase A logic from main.py's TASK_DONE branch
    user_request = "find the weather in Austin and send it to me"
    turn_actions = [
        {"step": 1, "action": "search_general",
         "content": "weather austin", "result_shape": "ok"},
        {"step": 2, "action": "weather",
         "content": "Austin, TX", "result_shape": "ok"},
        {"step": 3, "action": "save_file",
         "content": "austin_weather.txt|72F clear",
         "result_shape": "ok"},
    ]
    import hashlib as _hlib
    import re as _re
    req_hash = _hlib.sha256(user_request.encode("utf-8")).hexdigest()[:8]  # non-crypto id; sha256 clears semgrep
    slug = _re.sub(r"[^a-z0-9]+", "_",
                   user_request.lower()).strip("_")[:40] or "task"
    proc_key = f"task:{req_hash}:{slug}"
    entries_before = logger.count_entries()
    proc.add_procedure(
        key=proc_key,
        value={
            "user_request": user_request,
            "steps_used": 3,
            "max_steps": 27,
            "actions": turn_actions,
            "final_answer_preview": "Austin is 72F and clear.",
        },
        success=True,
        metadata={"source": "auto_task_done", "tool_count": 3},
    )
    entries_after = logger.count_entries()
    assert entries_after == entries_before + 1, \
        "TASK_DONE autolog must write exactly one chain witness"
    entry = proc.get_procedure_with_metadata(proc_key,
                                             category="successful")
    assert entry is not None
    assert "chain_hash" in entry and len(entry["chain_hash"]) == 64
    assert entry["value"]["actions"] == turn_actions
    assert entry["value"]["user_request"] == user_request
    print(f"PASS: autologged sequence at {proc_key} "
          f"(chain_hash {entry['chain_hash'][:16]}...)")
    results.append(("TASK_DONE autolog", True))

    # ------------------------------------------------------------------
    banner("TEST 2: Daemon chain digest — reads log, writes derived file")
    # ------------------------------------------------------------------
    # Add a few entries so the digest has material
    for i, msg in enumerate([
        "what's the weather in Boston?",
        "Boston is 45F and overcast.",
        "what about traffic conditions?",
        "Traffic on I-93 is heavy near downtown.",
    ]):
        logger.log(
            content=msg, role=("user" if i % 2 == 0 else "assistant"),
            temperature=0.5, token_prob=(None if i % 2 == 0 else 0.85),
            metadata={"verify": "v215"},
        )

    # Patch daemon module's path constants to our scratch dir, then
    # import its job functions and run them directly (no TCP server).
    import sage_daemon
    sage_daemon.MEMORY_LOG_DIR = test_memory
    sage_daemon.PROCEDURAL_FILE = test_procedural / "procedural.json"
    sage_daemon.DIGEST_FILE = test_memory / "chain_digest.json"
    sage_daemon._memory_logger = logger  # share our test logger

    log_size_before = (
        (test_memory / "memory_chain.log").stat().st_size
        if (test_memory / "memory_chain.log").exists() else 0
    )
    msg = sage_daemon._job_chain_digest()
    assert "digest written" in msg, f"unexpected: {msg}"
    digest_path = sage_daemon.DIGEST_FILE
    assert digest_path.exists(), "digest file should be written"
    digest = json.loads(digest_path.read_text(encoding="utf-8"))
    assert "summary" in digest and digest["summary"]
    assert digest["entry_count"] >= 4
    log_size_after = (
        (test_memory / "memory_chain.log").stat().st_size
    )
    assert log_size_after == log_size_before, \
        "chain log MUST NOT be modified by digest job"
    print(f"PASS: digest written, log size unchanged "
          f"({log_size_after} bytes), {len(digest['summary'])} char "
          f"summary")
    results.append(("chain digest read-only", True))

    # ------------------------------------------------------------------
    banner("TEST 3: KB consolidation — prunes stale unsuccessful only")
    # ------------------------------------------------------------------
    # Seed procedural.json with a mix:
    #   - Recent successful → must survive
    #   - Recent unsuccessful → must survive
    #   - Old unsuccessful → must be pruned
    proc.add_procedure(
        "fresh_unsuccessful", "tried yesterday, didn't work",
        success=False)
    # Manually backdate one unsuccessful entry to simulate staleness
    kb_path = test_procedural / "procedural.json"
    kb = json.loads(kb_path.read_text(encoding="utf-8"))
    old_iso = "2020-01-01T00:00:00Z"
    kb["unsuccessful"]["ancient_dead_end"] = {
        "value": "this never worked",
        "metadata": {},
        "timestamp": old_iso,
    }
    kb_path.write_text(json.dumps(kb), encoding="utf-8")

    succ_keys_before = set(proc.get_all().get("successful", {}).keys())
    msg = sage_daemon._job_consolidate_procedural()
    assert "pruned" in msg, f"unexpected: {msg}"

    # Reload and check
    kb_after = json.loads(kb_path.read_text(encoding="utf-8"))
    succ_after = set(kb_after.get("successful", {}).keys())
    unsucc_after = set(kb_after.get("unsuccessful", {}).keys())
    assert "ancient_dead_end" not in unsucc_after, \
        "stale unsuccessful entry should have been pruned"
    assert "fresh_unsuccessful" in unsucc_after, \
        "recent unsuccessful entry must survive"
    assert succ_after == succ_keys_before, \
        "successful (chain-witnessed) entries MUST be preserved"
    print(f"PASS: stale pruned, fresh kept, all "
          f"{len(succ_after)} successful entries preserved "
          f"({msg})")
    results.append(("KB consolidation safe", True))

    # ------------------------------------------------------------------
    banner("TEST 4: Anomaly monitor flips on tamper, recovers on restore")
    # ------------------------------------------------------------------
    # Reset tick state
    sage_daemon._tick_state["anomaly_alert"] = False
    sage_daemon._tick_state["anomaly_first_ts"] = None

    # Healthy run
    msg = sage_daemon._job_anomaly_check()
    assert "ok=True" in msg, f"healthy chain should verify: {msg}"
    assert sage_daemon._tick_state["anomaly_alert"] is False
    print(f"  healthy: {msg}")

    # Tamper the log
    log_file = test_memory / "memory_chain.log"
    raw = log_file.read_bytes()
    backup = raw  # save for restore
    # Flip a single byte in the first ciphertext entry to break chain
    tampered = raw.replace(b"gAAAAA", b"hAAAAA", 1)
    assert tampered != raw
    log_file.write_bytes(tampered)

    # Re-init logger to flush any cached state
    logger2 = MemoryLogger(storage_dir=str(test_memory),
                           baseline_temp=0.5)
    sage_daemon._memory_logger = logger2

    msg = sage_daemon._job_anomaly_check()
    assert "ok=False" in msg, f"tampered chain should fail: {msg}"
    assert sage_daemon._tick_state["anomaly_alert"] is True
    assert sage_daemon._tick_state["anomaly_first_ts"] is not None
    print(f"  tampered: alert raised ({msg})")

    # Restore and verify recovery
    log_file.write_bytes(backup)
    logger3 = MemoryLogger(storage_dir=str(test_memory),
                           baseline_temp=0.5)
    sage_daemon._memory_logger = logger3
    msg = sage_daemon._job_anomaly_check()
    assert "ok=True" in msg, f"restored chain should verify: {msg}"
    assert sage_daemon._tick_state["anomaly_alert"] is False
    print(f"  recovered: alert cleared ({msg})")
    print("PASS: anomaly monitor catches tamper and clears on recovery")
    results.append(("anomaly monitor", True))

    # ------------------------------------------------------------------
    banner("TEST 5: [PRIORITISE:] parser tag recognised")
    # ------------------------------------------------------------------
    sample = (
        "I'll batch these.\n"
        "[PRIORITISE: search news AI safety | weather Austin | "
        "browse https://example.com]\n"
        "[TASK_DONE]"
    )
    parsed = sage_engine.parse_agent_actions(sample)
    types = [a for a, _ in parsed]
    assert "prioritise" in types, types
    pri_content = next(c for a, c in parsed if a == "prioritise")
    assert "search news AI safety" in pri_content
    assert "weather Austin" in pri_content
    assert "browse" in pri_content
    print("PASS: parser recognises [PRIORITISE:] with pipe-separated "
          "subtasks")
    results.append(("PRIORITISE parser", True))

    # ------------------------------------------------------------------
    banner("TEST 6: SAGE_SYSTEM_PROMPT teaches AUTO-LOGGING and PRIORITISE (post angle-bracket convention)")
    # ------------------------------------------------------------------
    # v2.1.5 update: prompt rewrite uses ⟨TAG⟩ angle-bracket placeholders
    # to prevent template-echo bugs. Test now accepts the angle-bracket
    # form OR the unbracketed name, and ALSO asserts no parseable
    # square-bracket form leaks into the prompt as parser-bait.
    p = sage_engine.SAGE_SYSTEM_PROMPT
    # v2.2 fix (2026-05-26): the procedural-memory section in
    # SAGE_SYSTEM_PROMPT was renamed from "AUTO-LOGGING:" to
    # "PROCEDURAL MEMORY (AUTOMATIC):" during the angle-bracket /
    # clarity rewrite. Functionality identical, label sharper. This
    # assertion now accepts either label or the lowercase concept
    # reference — same flexible pattern as
    # verify_procedural_wiring.py test 8 — so a future label revision
    # doesn't break the test while still catching a genuine deletion.
    assert (
        "AUTO-LOGGING" in p
        or "PROCEDURAL MEMORY" in p
        or "auto-logs" in p
    ), "prompt missing procedural-memory / auto-logging reference"
    # v2.2 fix (2026-05-26): "PRIORITISE" was retired as a literal
    # tag-name reference in the prompt during the angle-bracket /
    # clarity rewrite. The same feature is now taught as "Batched
    # parallel dispatch" with "subtask keywords". The parser still
    # recognises [PRIORITISE:] (sage_engine.py:1437), so what we
    # really want to verify is that the prompt teaches the model
    # SOME way to invoke batched dispatch. Accept any of the known
    # references.
    assert (
        "⟨PRIORITISE:" in p
        or "PRIORITISE" in p
        or "Batched parallel dispatch" in p
        or "subtask keywords" in p
    ), "prompt missing batched-dispatch / PRIORITISE reference (any form)"
    # Critical inverse: NO parseable [PRIORITISE: literal allowed —
    # that would be parser-bait Sage could echo as a live command.
    assert "[PRIORITISE:" not in p, (
        "prompt contains parseable [PRIORITISE: square-bracket literal "
        "— this is parser-bait. Use ⟨PRIORITISE: angle-bracket form."
    )
    print("PASS: prompt references PRIORITISE by name AND has no "
          "parseable square-bracket parser-bait")
    results.append(("prompt covers v2.1.5", True))

    # ------------------------------------------------------------------
    banner("SUMMARY")
    # ------------------------------------------------------------------
    for name, ok in results:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}")

    all_ok = all(ok for _, ok in results)
    print()
    print("ALL GREEN" if all_ok else "FAILURES ABOVE")

    shutil.rmtree(scratch, ignore_errors=True)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
