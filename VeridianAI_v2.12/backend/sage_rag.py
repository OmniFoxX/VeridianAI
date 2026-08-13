import json
import math
import os
from pathlib import Path
import requests
from typing import List, Dict, Any, Optional

# Module‑level cache for loaded vector indexes – avoids re‑reading the file on every query.
_index_cache: dict[str, Dict[str, List[float]]] = {}

def get_embedding(text: str,
                  ollama_url: str = "http://localhost:11434",
                  task: Optional[str] = None) -> List[float]:
    """Return a list of floats representing the embedding for *text*.
    Returns [] if no backend could serve.

    v2.13: was Ollama-only, so semantic search silently did nothing for anyone
    who had not installed Ollama AND pulled an embedding model -- which is the
    whole point the embed tier exists to solve. Now goes through
    backend/embeddings.py: local embed tier first (ships with the app), Ollama
    second (for users who prefer it). The ollama_url argument is retained for
    call compatibility and is no longer authoritative.
    """
    try:
        from embeddings import embed_one
        return embed_one(text, task=task)
    except Exception:
        return []


def cosine_similarity(vec_a: List[float],
                      vec_b: List[float]) -> float:
    """Cosine similarity in the range [0.0, 1.0].
    Returns 0.0 for empty inputs or length‑mismatch; never raises."""
    if len(vec_a) != len(vec_b):
        return 0.0
    if not vec_a:                     # both vectors are empty → defined as 0.0
        return 0.0

    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))

    if norm_a == 0 or norm_b == 0:
        return 0.0
    # Clamp tiny rounding errors that could push the value slightly outside [0,1]
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


def load_vector_index(index_path: str) -> Dict[str, List[float]]:
    """Load persisted index from *index_path*.
    Returns an empty dict if the file is absent, unreadable, or contains invalid data."""
    if not os.path.isfile(index_path):
        return {}

    try:
        with open(index_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}

        # v2.13 SOURCE GUARD. Cosine similarity between vectors produced by
        # DIFFERENT embedding models is meaningless -- the spaces are unrelated,
        # so the scores are noise that looks like data. Indexes are now tagged
        # with the engine that built them; an index from another engine is
        # discarded rather than silently mixed. Untagged (pre-v2.13) indexes are
        # also discarded: they were built against Ollama and we cannot know
        # which model, so they are not safely comparable.
        stored_src = raw.pop("__embed_source__", None)
        try:
            # index_tag, not active_source: the source alone cannot tell one
            # llama-served embedding model from another, and a model swap with
            # matching dimensions would otherwise reuse the old vectors.
            from embeddings import index_tag
            current_src = index_tag()
        except Exception:
            current_src = None
        if current_src and stored_src != current_src:
            print(f"[sage_rag] vector index was built by {stored_src or 'an untagged/legacy'} "
                  f"backend, current is {current_src} -- discarding it. "
                  f"Vectors from different embedding models are not comparable; "
                  f"the index will rebuild as documents are searched.", flush=True)
            return {}

        cleaned: Dict[str, List[float]] = {}
        for k, v in raw.items():
            if isinstance(v, list) and all(isinstance(x, (int, float)) for x in v):
                cleaned[k] = [float(x) for x in v]
        return cleaned
    except Exception:
        return {}


def store_vector(index_path: str,
                 filename: str,
                 vector: List[float]) -> None:
    """Append or update *filename* → *vector* in the index at *index_path*.
       Write is atomic: data written to a temporary file then renamed."""
    tmp_path = None
    try:
        # Load existing index (ignore errors so we never raise)
        existing = load_vector_index(index_path)

        # Update / add entry – ensure vector is stored as floats
        existing[filename] = [float(x) for x in vector]

        # Tag the index with the engine that produced these vectors, so a later
        # backend change is detected instead of silently poisoning similarity.
        try:
            from embeddings import index_tag
            src = index_tag()
            if src:
                existing["__embed_source__"] = src
        except Exception:
            pass

        # Ensure the target directory exists
        dest_dir = os.path.dirname(os.path.abspath(index_path)) or "."
        os.makedirs(dest_dir, exist_ok=True)

        tmp_path = os.path.join(dest_dir, f"{os.path.basename(index_path)}.tmp")

        with open(tmp_path, "w", encoding="utf-8") as tf:
            json.dump(existing, tf, ensure_ascii=False, separators=(",", ":"))

        # Atomic replace (works on POSIX and Windows)
        os.replace(tmp_path, index_path)

    except Exception:
        # Fail silently per spec; clean up any stray temporary file
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def normalize_scores(results: List[Dict[str, Any]],
                     raw_max: float) -> List[Dict[str, Any]]:
    """Return a **new** list where each dict's 'score' field is divided by *raw_max*.
    Unchanged if raw_max == 0."""
    if raw_max == 0:
        return results.copy()

    out = []
    for entry in results:
        new_entry = dict(entry)                     # shallow copy
        try:
            new_entry["score"] = float(entry["score"]) / raw_max
        except (KeyError, TypeError):
            # If 'score' is missing or not numeric we leave it untouched.
            pass
        out.append(new_entry)
    return out


def is_feature_enabled(feature_name: str) -> bool:
    """Placeholder – in a real system this would read configuration.
       For the purpose of this module we assume the feature is enabled."""
    return True


def semantic_search(query: str,
                    archives: List[str],
                    index_path: str,
                    ollama_url: str = "http://localhost:11434") -> List[Dict[str, Any]]:
    """Vector-based semantic search.
    Returns up to three dicts (each containing 'filename', 'score' and
    'search_type':'semantic'). Empty list if the feature is disabled,
    the index cannot be loaded, or no matches pass the threshold."""
    # Feature gating — import the real flag from sage_engine if available,
    # fall back to enabled so the module stays usable in isolation/testing.
    try:
        from sage_engine import is_feature_enabled as _is_feature_enabled
    except Exception:
        _is_feature_enabled = lambda _: True

    if not _is_feature_enabled("semantic_search"):
        return []

    # Load (or cache) the vector index – lazy loading per spec.
    global _index_cache
    if index_path not in _index_cache:
        _index_cache[index_path] = load_vector_index(index_path)
    index_dict = _index_cache[index_path]

    # Get embedding for the query; abort early on failure.
    # The query side of the asymmetric pair -- see embeddings.TASK_QUERY.
    from embeddings import TASK_QUERY as _TQ
    query_vec = get_embedding(query, ollama_url, task=_TQ)
    if not query_vec:
        return []   # No usable vector → no results.

    # Lazy-index any archive that is missing from the index.
    missing = set(archives) - set(index_dict.keys())
    for fname in missing:
        try:
            raw = Path(fname).read_bytes()
            # Decrypt via atrest — same pattern as keyword_search
            import atrest
            messages = atrest.load_json_auto(raw)
            if not isinstance(messages, list):
                continue
            # Concatenate message content for embedding
            text = " ".join(
                m.get("content", "") for m in messages
                if isinstance(m, dict) and m.get("content")
            )
            if not text.strip():
                continue
        except Exception:
            continue
        # The document side of the pair.
        from embeddings import TASK_DOCUMENT as _TD
        vec = get_embedding(text, ollama_url, task=_TD)
        if vec:
            index_dict[fname] = vec
            store_vector(index_path, fname, vec)

    # Compute similarities, keep those >= 0.3, then normalise.
    raw_results: List[Dict[str, Any]] = []
    for fname, vec in index_dict.items():
        score = cosine_similarity(query_vec, vec)
        if score >= 0.3:
            raw_results.append({"filename": fname, "score": float(score)})

    if not raw_results:
        return []

    max_raw_score = max(r["score"] for r in raw_results)   # >0 because of threshold
    normalized = normalize_scores(raw_results, raw_max=max_raw_score)

    # Sort descending and keep top‑3.
    normalized.sort(key=lambda d: d["score"], reverse=True)
    top_three = [{"search_type": "semantic", **r} for r in normalized[:3]]
    return top_three
