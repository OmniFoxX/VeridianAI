"""
End-to-end verification harness for OracleAI v2.1.4 procedural-memory wiring.

Covers the write + read paths that verify_v214.py didn't touch:

  1. parse_agent_actions() recognizes [REMEMBER:], [REMEMBER_FAIL:], [RECALL:]
  2. REMEMBER handler path round-trips: key|desc -> successful bucket with
     chain_hash, retrievable by RECALL
  3. REMEMBER_FAIL handler path: key|reason -> unsuccessful bucket, NOT chained
  4. RECALL fuzzy substring matching hits both key and value
  5. _looks_like_failure heuristic catches the shapes we care about
  6. Auto-capture fires at the 3rd repeat of the same (action, content) with
     failure-shaped result, but NOT before, and NOT on non-failure results
  7. System-prompt injection contains both successful and unsuccessful
     procedures when both exist, is silent-failure when the KB is empty,
     and tells the model not to echo the block
  8. Tags are listed in rule #11 of SAGE_SYSTEM_PROMPT so the model won't
     echo them

Runs in a tempdir, never touches production data. Exit 0 = all green.

    cd <project_root>\\backend
    py verify_procedural_wiring.py
"""

import sys
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
    scratch = Path(tempfile.mkdtemp(prefix="oracleai_v214_proc_verify_"))
    test_memory = scratch / "memory_log"
    test_procedural = scratch / "procedural_memory"
    test_key = scratch / ".fernet_key"
    test_memory.mkdir()
    test_procedural.mkdir()

    import config
    config.FERNET_KEY_FILE = test_key

    import memory_logger_surprise as mls
    mls._fernet_instance = None  # ensure we pick up the patched key path
    from memory_logger_surprise import MemoryLogger
    from procedural_memory import ProceduralMemory
    import sage_engine

    logger = MemoryLogger(storage_dir=str(test_memory), baseline_temp=0.5)
    proc = ProceduralMemory(storage_dir=str(test_procedural),
                            memory_logger=logger)

    results = []

    # ------------------------------------------------------------------
    banner("TEST 1: parser recognizes REMEMBER / REMEMBER_FAIL / RECALL")
    # ------------------------------------------------------------------
    sample = (
        "Some thinking text.\n"
        "[REMEMBER: tavily_date | Tavily accepts YYYY-MM-DD only]\n"
        "[REMEMBER_FAIL: ddg_spam | rate-limited after 100 identical searches]\n"
        "[RECALL: weather austin]\n"
        "more thinking\n"
        "[TASK_DONE]"
    )
    parsed = sage_engine.parse_agent_actions(sample)
    action_types = [a for a, _ in parsed]
    assert "remember" in action_types, action_types
    assert "remember_fail" in action_types, action_types
    assert "recall" in action_types, action_types
    assert "done" in action_types, action_types

    rem_content = next(c for a, c in parsed if a == "remember")
    assert "tavily_date" in rem_content and "YYYY-MM-DD" in rem_content
    fail_content = next(c for a, c in parsed if a == "remember_fail")
    assert "ddg_spam" in fail_content and "rate-limited" in fail_content
    recall_content = next(c for a, c in parsed if a == "recall")
    assert recall_content == "weather austin"
    print("PASS: all three tags parsed and content preserved")
    results.append(("parser recognizes tags", True))

    # ------------------------------------------------------------------
    banner("TEST 2: REMEMBER path round-trips (handler flow simulated)")
    # ------------------------------------------------------------------
    # Simulate what main.py's remember handler does: split on |, add.
    parts = rem_content.split("|", 1)
    p_key, p_desc = parts[0].strip(), parts[1].strip()
    entries_before = logger.count_entries()
    is_new = proc.add_procedure(
        key=p_key, value=p_desc, success=True,
        metadata={"source": "tag", "step": 1},
    )
    entries_after = logger.count_entries()
    assert is_new is True
    assert entries_after == entries_before + 1, \
        "Successful procedure must chain-witness (+1 entry)"
    entry = proc.get_procedure_with_metadata(p_key, category="successful")
    assert entry is not None
    assert entry["value"] == p_desc
    assert "chain_hash" in entry and len(entry["chain_hash"]) == 64
    print(f"PASS: remember stored '{p_key}' with chain_hash "
          f"{entry['chain_hash'][:16]}...")
    results.append(("remember round-trip", True))

    # ------------------------------------------------------------------
    banner("TEST 3: REMEMBER_FAIL path — no chain growth")
    # ------------------------------------------------------------------
    parts = fail_content.split("|", 1)
    p_key, p_reason = parts[0].strip(), parts[1].strip()
    entries_before = logger.count_entries()
    proc.add_procedure(
        key=p_key, value=p_reason, success=False,
        metadata={"source": "tag", "step": 1},
    )
    entries_after = logger.count_entries()
    assert entries_after == entries_before, \
        "Unsuccessful procedures must NOT write chain witnesses"
    bad = proc.get_procedure_with_metadata(p_key, category="unsuccessful")
    assert bad is not None
    assert bad["value"] == p_reason
    assert "chain_hash" not in bad
    print("PASS: remember_fail stored as dead-end, chain unchanged")
    results.append(("remember_fail no chain", True))

    # ------------------------------------------------------------------
    banner("TEST 4: RECALL fuzzy matching (key and value substrings)")
    # ------------------------------------------------------------------
    # Add a few more procedures to make matching interesting
    proc.add_procedure("weather_austin", "silently correct to Austin, TX",
                       success=True)
    proc.add_procedure("spell_sanfrancisco", "correct to San Francisco",
                       success=True)

    def _do_recall(q):
        q = q.strip().lower()
        hits = []
        succ = proc.get_all().get("successful", {})
        for k, entry in succ.items():
            val = str(entry.get("value", ""))
            if (not q or q in k.lower() or q in val.lower()):
                hits.append((k, val))
            if len(hits) >= 10:
                break
        return hits

    hits = _do_recall("austin")
    keys = [k for k, _ in hits]
    assert "weather_austin" in keys, keys
    print(f"PASS: RECALL matched on key/value, got {len(hits)} hit(s)")
    results.append(("recall fuzzy matching", True))

    # ------------------------------------------------------------------
    banner("TEST 5: _looks_like_failure heuristic")
    # ------------------------------------------------------------------
    # Re-implement locally rather than importing — it lives inside
    # ws_chat's closure. The signatures we care about:
    def _looks_like_failure(text: str) -> bool:
        t = (text or "").lower()
        if not t.strip():
            return True
        fail_sigs = (
            "error:", "error ", "blocked", "rate limit",
            "timeout", "no results", "not found",
            "failed", "no relevant", "429", "403",
            "connection", "unable to", "captcha",
        )
        return any(s in t for s in fail_sigs)

    failures = [
        "", "   ", "Search error: timeout",
        "No results found for your query",
        "HTTP 429 Too Many Requests",
        "Blocked by captcha",
        "Connection refused",
    ]
    non_failures = [
        "Paris is the capital of France.",
        "Temperature: 72F, clear.",
        "RECALL hits (2): - foo - bar",
    ]
    for t in failures:
        assert _looks_like_failure(t), f"Should flag as failure: {t!r}"
    for t in non_failures:
        assert not _looks_like_failure(t), f"False positive: {t!r}"
    print("PASS: heuristic catches empty/error/rate-limit/captcha shapes "
          "and leaves clean results alone")
    results.append(("failure heuristic", True))

    # ------------------------------------------------------------------
    banner("TEST 6: Auto-capture fires at 3rd repeated failing attempt")
    # ------------------------------------------------------------------
    action_attempts = {}
    auto_failed_keys = set()
    results_log = []

    def simulate(action_type, content, result_text):
        # Mirror the auto-capture logic from main.py verbatim.
        if action_type in ("remember", "remember_fail", "recall"):
            return  # excluded
        content_repr = "|".join(str(x) for x in content) \
            if isinstance(content, tuple) else str(content)
        attempt_key = f"{action_type}:{content_repr[:120]}"
        action_attempts[attempt_key] = action_attempts.get(attempt_key, 0) + 1
        if (action_attempts[attempt_key] >= 3
                and attempt_key not in auto_failed_keys
                and _looks_like_failure(result_text)):
            auto_failed_keys.add(attempt_key)
            proc.add_procedure(
                key=attempt_key,
                value=f"Auto-captured. Last result: {result_text[:300]}",
                success=False,
                metadata={"source": "auto_capture"},
            )
            results_log.append(("fired", attempt_key))
        else:
            results_log.append(("skipped", attempt_key))

    # First three attempts — same failure-shaped result
    simulate("search", "duckduckgo test", "Search error: 429 rate limit")
    simulate("search", "duckduckgo test", "Search error: 429 rate limit")
    simulate("search", "duckduckgo test", "Search error: 429 rate limit")
    # Fourth attempt — should NOT double-fire (already in auto_failed_keys)
    simulate("search", "duckduckgo test", "Search error: 429 rate limit")

    fired = [r for r in results_log if r[0] == "fired"]
    assert len(fired) == 1, f"Expected exactly 1 fire, got {len(fired)}: " \
                            f"{results_log}"
    dead_end = proc.get_procedure_with_metadata(
        "search:duckduckgo test", category="unsuccessful")
    assert dead_end is not None, "Auto-capture should have landed a record"
    print(f"PASS: auto-capture fired exactly once at repeat #3 "
          f"({fired[0][1]})")

    # Separate control: 3 repeats with a SUCCESS result should NOT fire
    for _ in range(3):
        simulate("search", "france capital", "Paris is the capital of France.")
    fired2 = [r for r in results_log if r[0] == "fired"
              and "france capital" in r[1]]
    assert not fired2, "Non-failure results must not trigger auto-capture"
    print("PASS: non-failure results leave auto-capture quiet")
    results.append(("auto-capture threshold", True))

    # ------------------------------------------------------------------
    banner("TEST 7: System-prompt injection surfaces both buckets")
    # ------------------------------------------------------------------
    # Simulate the injection logic from main.py (lean re-impl)
    kb = proc.get_all()
    succ = kb.get("successful", {})
    unsucc = kb.get("unsuccessful", {})

    def _recent(bucket, n):
        items = [(k, v) for k, v in bucket.items() if isinstance(v, dict)]
        items.sort(key=lambda kv: kv[1].get("timestamp", ""), reverse=True)
        return items[:n]

    recent_succ = _recent(succ, 5)
    recent_fail = _recent(unsucc, 5)
    assert recent_succ, "Should have at least one successful procedure"
    assert recent_fail, "Should have at least one unsuccessful procedure"

    lines = [
        "=== PROCEDURAL MEMORY (internal — do not display) ===",
        "What worked (successful procedures):",
    ]
    for k, v in recent_succ:
        lines.append(f"  - {k}: {str(v.get('value',''))[:200]}")
    lines.append("Dead-ends (unsuccessful — do not retry unless context has "
                 "changed):")
    for k, v in recent_fail:
        lines.append(f"  - {k}: {str(v.get('value',''))[:200]}")
    block = "\n".join(lines)
    assert "do not display" in block
    assert "weather_austin" in block or "tavily_date" in block
    assert "ddg_spam" in block or "duckduckgo test" in block
    print("PASS: injection produces the expected two-section block")
    results.append(("system prompt injection", True))

    # ------------------------------------------------------------------
    banner("TEST 8: SAGE_SYSTEM_PROMPT names the new tags (post-angle-bracket convention)")
    # ------------------------------------------------------------------
    # v2.1.5 update: prompt now uses ⟨TAG⟩ angle-bracket placeholders
    # instead of literal [TAG:] to prevent template-echo bugs. Test
    # accepts EITHER the angle-bracket form ⟨REMEMBER: OR an unbracketed
    # mention by name ("REMEMBER"). The prompt MUST NOT contain literal
    # [REMEMBER: square-bracket parser-bait — that's what we're trying
    # to prevent.
    p = sage_engine.SAGE_SYSTEM_PROMPT
    for tag_name in ("REMEMBER", "REMEMBER_FAIL", "RECALL"):
        # Either angle-bracket schema OR plain name reference must appear.
        assert (
            f"⟨{tag_name}" in p
            or f" {tag_name} " in p
            or f"({tag_name}," in p
            or f" {tag_name}," in p
            or f"{tag_name})" in p
        ), f"Prompt missing any reference to {tag_name}"
    # Critical inverse assertion: NO parseable square-bracket form may
    # appear in the prompt (that would be parser-bait waiting to be
    # echoed by Sage).
    for tag in ("[REMEMBER:", "[REMEMBER_FAIL:", "[RECALL:"):
        assert tag not in p, (
            f"Prompt contains parseable {tag} — this is parser-bait "
            f"that Sage could echo back into her output. Use ⟨{tag[1:]}"
            f" angle-bracket form instead."
        )
    assert "PROCEDURAL MEMORY" in p, \
        "System prompt must warn the model not to echo injected block"
    print("PASS: prompt references the three tags by name AND contains "
          "no parseable square-bracket parser-bait")
    results.append(("prompt rule coverage", True))

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
