"""
Gate test for RAG-001: sage_rag.py
All 10 checks must pass. Any failure = entry does not advance.
"""
import math
import json
import tempfile
import os
import sage_rag


# ── 1. cosine_similarity: identical vectors = 1.0 ──────────────────────
def test_cosine_identical():
    assert abs(sage_rag.cosine_similarity([1, 0, 0], [1, 0, 0]) - 1.0) < 1e-6

# ── 2. cosine_similarity: orthogonal vectors = 0.0 ─────────────────────
def test_cosine_orthogonal():
    assert abs(sage_rag.cosine_similarity([1, 0, 0], [0, 1, 0])) < 1e-6

# ── 3. cosine_similarity: empty inputs = 0.0, no raise ─────────────────
def test_cosine_empty():
    assert sage_rag.cosine_similarity([], []) == 0.0

# ── 4. cosine_similarity: mismatched lengths = 0.0, no raise ───────────
def test_cosine_mismatch():
    assert sage_rag.cosine_similarity([1, 2], [1, 2, 3]) == 0.0

# ── 5. cosine_similarity: known angle ──────────────────────────────────
def test_cosine_known():
    # [1,1] vs [1,0] = 45 degrees = cos(45) ≈ 0.7071
    result = sage_rag.cosine_similarity([1, 1], [1, 0])
    assert abs(result - math.cos(math.pi / 4)) < 1e-4

# ── 6. store_vector + load_vector_index round-trip ─────────────────────
def test_vector_roundtrip():
    with tempfile.TemporaryDirectory() as tmpdir:
        index_path = os.path.join(tmpdir, "vector_index.json")
        vec = [0.1, 0.2, 0.3, 0.4, 0.5]
        sage_rag.store_vector(index_path, "archive_001.json", vec)
        loaded = sage_rag.load_vector_index(index_path)
        assert "archive_001.json" in loaded
        assert loaded["archive_001.json"] == vec

# ── 7. load_vector_index: missing file returns {} ──────────────────────
def test_load_missing():
    result = sage_rag.load_vector_index("/nonexistent/path/index.json")
    assert result == {}

# ── 8. store_vector: two entries, both persist ─────────────────────────
def test_store_multiple():
    with tempfile.TemporaryDirectory() as tmpdir:
        index_path = os.path.join(tmpdir, "vector_index.json")
        sage_rag.store_vector(index_path, "a.json", [1.0, 0.0])
        sage_rag.store_vector(index_path, "b.json", [0.0, 1.0])
        loaded = sage_rag.load_vector_index(index_path)
        assert len(loaded) == 2
        assert loaded["a.json"] == [1.0, 0.0]
        assert loaded["b.json"] == [0.0, 1.0]

# ── 9. normalize_scores: values capped at 1.0 ──────────────────────────
def test_normalize():
    results = [
        {"filename": "a.json", "score": 8},
        {"filename": "b.json", "score": 5},
        {"filename": "c.json", "score": 10},
    ]
    normalized = sage_rag.normalize_scores(results, raw_max=10)
    assert all(r["score"] <= 1.0 for r in normalized)
    scores = {r["filename"]: r["score"] for r in normalized}
    assert abs(scores["c.json"] - 1.0) < 1e-6
    assert abs(scores["a.json"] - 0.8) < 1e-6
    assert abs(scores["b.json"] - 0.5) < 1e-6

# ── 10. normalize_scores: zero raw_max returns unchanged ───────────────
def test_normalize_zero():
    results = [{"filename": "a.json", "score": 5}]
    out = sage_rag.normalize_scores(results, raw_max=0)
    assert out["score"] == 5


# ── Runner ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    tests = [
        test_cosine_identical,
        test_cosine_orthogonal,
        test_cosine_empty,
        test_cosine_mismatch,
        test_cosine_known,
        test_vector_roundtrip,
        test_load_missing,
        test_store_multiple,
        test_normalize,
        test_normalize_zero,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed}/{passed+failed} passed")
    if failed:
        raise SystemExit(1)