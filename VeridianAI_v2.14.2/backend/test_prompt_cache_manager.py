# test_prompt_cache_manager.py  — corrected assertions
# Changes from original:
#   1. test_recommend_order_system_prompt_first: order == "system_prompt"  → order == "system_prompt"
#   2. test_full_pipeline_clean:  recommended_order == "system_prompt"     → recommended_order == "system_prompt"
#   3. test_thread_safety:        abs(r - results)                         → abs(r - results)

from prompt_cache_manager import (
    analyze_prompt,
    detect_cache_busters,
    compute_cache_efficiency,
    recommend_order,
    PromptSegment,
    CacheAnalysisResult,
)
import hashlib


# ── Helpers ────────────────────────────────────────────────────────────

def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


SYSTEM = "You are Sage, OracleAI's primary AI assistant. " * 50
ADDENDUM = "Focus on accessibility topics today."
TOOLS = '{"tools": [{"name": "evaluate_expression"}, {"name": "lint_expression"}]}' * 10
HISTORY = "User: hello\nSage: Hi there!\n" * 20
USER = "What is the cache efficiency of this prompt?"

SEGMENTS = {
    "system_prompt":    SYSTEM,
    "addendum":         ADDENDUM,
    "tool_definitions": TOOLS,
    "history":          HISTORY,
    "user_message":     USER,
}


# ── PromptSegment tests ────────────────────────────────────────────────

def test_prompt_segment_fields():
    seg = PromptSegment(
        name="system_prompt",
        content=SYSTEM,
        stable=True,
        token_estimate=len(SYSTEM.split()),
        hash=sha256(SYSTEM),
    )
    assert seg.name == "system_prompt"
    assert seg.stable is True
    assert seg.token_estimate > 0
    assert len(seg.hash) == 64


# ── analyze_prompt tests ───────────────────────────────────────────────

def test_analyze_returns_correct_type():
    result = analyze_prompt(SEGMENTS)
    assert isinstance(result, CacheAnalysisResult)


def test_analyze_segment_count():
    result = analyze_prompt(SEGMENTS)
    assert len(result.segments) == 5


def test_analyze_stable_segments():
    result = analyze_prompt(SEGMENTS)
    stable_names = {s.name for s in result.segments if s.stable}
    assert "system_prompt" in stable_names
    assert "tool_definitions" in stable_names
    assert "addendum" in stable_names


def test_analyze_dynamic_segments():
    result = analyze_prompt(SEGMENTS)
    dynamic_names = {s.name for s in result.segments if not s.stable}
    assert "history" in dynamic_names
    assert "user_message" in dynamic_names


def test_analyze_token_counts():
    result = analyze_prompt(SEGMENTS)
    assert result.stable_tokens > 0
    assert result.dynamic_tokens > 0
    assert result.total_tokens == result.stable_tokens + result.dynamic_tokens


def test_analyze_cache_efficiency_range():
    result = analyze_prompt(SEGMENTS)
    assert 0.0 <= result.cache_efficiency <= 1.0


def test_analyze_cache_efficiency_value():
    result = analyze_prompt(SEGMENTS)
    expected = result.stable_tokens / result.total_tokens
    assert abs(result.cache_efficiency - expected) < 0.001


def test_analyze_hashes_present():
    result = analyze_prompt(SEGMENTS)
    for seg in result.segments:
        assert len(seg.hash) == 64


def test_analyze_recommended_order_present():
    result = analyze_prompt(SEGMENTS)
    assert isinstance(result.recommended_order, list)
    assert len(result.recommended_order) == 5


def test_analyze_is_optimal_flag():
    result = analyze_prompt(SEGMENTS)
    assert isinstance(result.is_optimal, bool)


def test_analyze_warnings_is_list():
    result = analyze_prompt(SEGMENTS)
    assert isinstance(result.warnings, list)


def test_analyze_cache_busters_is_list():
    result = analyze_prompt(SEGMENTS)
    assert isinstance(result.cache_busters, list)


def test_analyze_previous_hashes_no_change():
    result1 = analyze_prompt(SEGMENTS)
    prev = {s.name: s.hash for s in result1.segments}
    result2 = analyze_prompt(SEGMENTS, previous_hashes=prev)
    hash_busters = [b for b in result2.cache_busters if "changed" in b.lower()]
    assert len(hash_busters) == 0


def test_analyze_previous_hashes_detects_change():
    result1 = analyze_prompt(SEGMENTS)
    prev = {s.name: s.hash for s in result1.segments}
    changed = dict(SEGMENTS)
    changed["system_prompt"] = SYSTEM + " CHANGED"
    result2 = analyze_prompt(changed, previous_hashes=prev)
    assert len(result2.cache_busters) > 0


def test_analyze_custom_stable_keys():
    result = analyze_prompt(
        SEGMENTS,
        stable_keys=["system_prompt", "tool_definitions"]
    )
    stable_names = {s.name for s in result.segments if s.stable}
    assert "system_prompt" in stable_names
    assert "addendum" not in stable_names  # excluded from explicit stable_keys


# ── detect_cache_busters tests ─────────────────────────────────────────

def test_no_busters_clean_content():
    busters = detect_cache_busters("You are Sage, a helpful assistant.")
    assert busters == []


def test_detects_iso_timestamp():
    busters = detect_cache_busters(
        "Current time: 2026-06-29T14:32:00Z", "system_prompt"
    )
    assert len(busters) > 0


def test_detects_unix_epoch():
    busters = detect_cache_busters(
        "Session started: 1751234567", "system_prompt"
    )
    assert len(busters) > 0


def test_detects_uuid():
    busters = detect_cache_busters(
        "Request ID: 550e8400-e29b-41d4-a716-446655440000", "system_prompt"
    )
    assert len(busters) > 0


def test_detects_human_readable_date():
    busters = detect_cache_busters(
        "Today is Monday June 29 2026", "addendum"
    )
    assert len(busters) > 0


def test_buster_strings_are_descriptive():
    busters = detect_cache_busters(
        "Current time: 2026-06-29T14:32:00Z", "system_prompt"
    )
    assert all(isinstance(b, str) and len(b) > 0 for b in busters)


# ── compute_cache_efficiency tests ────────────────────────────────────

def test_efficiency_normal():
    assert abs(compute_cache_efficiency(800, 1000) - 0.8) < 0.001


def test_efficiency_zero_total():
    assert compute_cache_efficiency(0, 0) == 0.0


def test_efficiency_all_stable():
    assert compute_cache_efficiency(500, 500) == 1.0


def test_efficiency_none_stable():
    assert compute_cache_efficiency(0, 500) == 0.0


def test_efficiency_never_raises():
    try:
        result = compute_cache_efficiency(0, 0)
        assert result == 0.0
    except Exception:
        assert False, "compute_cache_efficiency raised on zero input"


# ── recommend_order tests ──────────────────────────────────────────────

def test_recommend_order_returns_list():
    order = recommend_order(SEGMENTS)
    assert isinstance(order, list)
    assert len(order) == 5


def test_recommend_order_stable_first():
    order = recommend_order(SEGMENTS)
    stable = {"system_prompt", "tool_definitions", "addendum"}
    dynamic = {"history", "user_message"}
    stable_indices = [order.index(k) for k in stable]
    dynamic_indices = [order.index(k) for k in dynamic]
    assert max(stable_indices) < min(dynamic_indices)


def test_recommend_order_user_message_last():
    order = recommend_order(SEGMENTS)
    assert order[-1] == "user_message"


def test_recommend_order_system_prompt_first():
    order = recommend_order(SEGMENTS)
    assert order[0] == "system_prompt"          # ✅ FIXED: was `order == "system_prompt"`


def test_recommend_order_custom_stable_keys():
    order = recommend_order(
        SEGMENTS,
        stable_keys=["history", "user_message"]
    )
    # With explicit stable_keys, ONLY history and user_message are stable
    # system_prompt, tool_definitions, addendum become dynamic
    stable_indices = [order.index(k) for k in ["history", "user_message"]]
    dynamic_indices = [order.index(k) for k in
                       ["system_prompt", "tool_definitions", "addendum"]]
    assert max(stable_indices) < min(dynamic_indices)


def test_recommend_order_unknown_segments():
    segs = dict(SEGMENTS)
    segs["mystery_segment"] = "some unknown content here"
    order = recommend_order(segs)
    assert "mystery_segment" in order
    assert len(order) == 6


# ── is_optimal tests ──────────────────────────────────────────────────

def test_is_optimal_true_when_correct_order():
    order = recommend_order(SEGMENTS)
    ordered_segments = {k: SEGMENTS[k] for k in order}
    result = analyze_prompt(ordered_segments)
    assert result.is_optimal is True


def test_is_optimal_false_when_wrong_order():
    wrong_order = {
        "user_message":     USER,
        "history":          HISTORY,
        "addendum":         ADDENDUM,
        "tool_definitions": TOOLS,
        "system_prompt":    SYSTEM,
    }
    result = analyze_prompt(wrong_order)
    assert result.is_optimal is False


# ── Integration / end-to-end tests ────────────────────────────────────

def test_full_pipeline_clean():
    result = analyze_prompt(SEGMENTS)
    assert isinstance(result, CacheAnalysisResult)
    assert result.total_tokens > 0
    assert result.cache_efficiency > 0.0
    assert result.recommended_order[0] == "system_prompt"  # ✅ FIXED: was `== "system_prompt"`
    assert result.recommended_order[-1] == "user_message"


def test_full_pipeline_with_cache_buster_in_system():
    segments = dict(SEGMENTS)
    segments["system_prompt"] = SYSTEM + "\nCurrent time: 2026-06-29T14:32:00Z"
    result = analyze_prompt(segments)
    assert len(result.cache_busters) > 0


def test_full_pipeline_empty_segments():
    result = analyze_prompt({})
    assert result.total_tokens == 0
    assert result.cache_efficiency == 0.0
    assert result.segments == []


def test_full_pipeline_single_segment():
    result = analyze_prompt({"system_prompt": SYSTEM})
    assert len(result.segments) == 1
    assert result.cache_efficiency == 1.0


def test_full_pipeline_all_dynamic():
    result = analyze_prompt(
        {"history": HISTORY, "user_message": USER},
        stable_keys=[]                          # explicit empty = nothing is stable
    )
    assert result.cache_efficiency == 0.0
    assert result.stable_tokens == 0


def test_thread_safety():
    import threading
    results = []
    errors = []

    def run():
        try:
            r = analyze_prompt(SEGMENTS)
            results.append(r.cache_efficiency)
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=run) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert len(results) == 10
    assert all(abs(r - results[0]) < 0.001 for r in results)  # ✅ FIXED: was `r - results`


# ── Runner ────────────────────────────────────────────────────────────

def run_all():
    tests = [
        test_prompt_segment_fields,
        test_analyze_returns_correct_type,
        test_analyze_segment_count,
        test_analyze_stable_segments,
        test_analyze_dynamic_segments,
        test_analyze_token_counts,
        test_analyze_cache_efficiency_range,
        test_analyze_cache_efficiency_value,
        test_analyze_hashes_present,
        test_analyze_recommended_order_present,
        test_analyze_is_optimal_flag,
        test_analyze_warnings_is_list,
        test_analyze_cache_busters_is_list,
        test_analyze_previous_hashes_no_change,
        test_analyze_previous_hashes_detects_change,
        test_analyze_custom_stable_keys,
        test_no_busters_clean_content,
        test_detects_iso_timestamp,
        test_detects_unix_epoch,
        test_detects_uuid,
        test_detects_human_readable_date,
        test_buster_strings_are_descriptive,
        test_efficiency_normal,
        test_efficiency_zero_total,
        test_efficiency_all_stable,
        test_efficiency_none_stable,
        test_efficiency_never_raises,
        test_recommend_order_returns_list,
        test_recommend_order_stable_first,
        test_recommend_order_user_message_last,
        test_recommend_order_system_prompt_first,
        test_recommend_order_custom_stable_keys,
        test_recommend_order_unknown_segments,
        test_is_optimal_true_when_correct_order,
        test_is_optimal_false_when_wrong_order,
        test_full_pipeline_clean,
        test_full_pipeline_with_cache_buster_in_system,
        test_full_pipeline_empty_segments,
        test_full_pipeline_single_segment,
        test_full_pipeline_all_dynamic,
        test_thread_safety,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✅ PASS  {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ FAIL  {test.__name__}: {e}")
            failed += 1

    print(f"\n{'='*55}")
    print(f"  Results: {passed} passed, {failed} failed out of {len(tests)} tests")
    print(f"{'='*55}")
    if failed == 0:
        print("  ALL TESTS PASSED ✅")
    else:
        print(f"  {failed} TEST(S) FAILED ❌")
    return failed == 0


if __name__ == "__main__":
    success = run_all()
    raise SystemExit(0 if success else 1)