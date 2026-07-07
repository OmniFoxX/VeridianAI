# prompt_cache_manager.py
# OracleAI Prompt Cache Manager
# Analyzes, segments, scores, and optimizes prompts for maximum KV cache efficiency.

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, List, Optional


# ── Dataclasses ────────────────────────────────────────────────────────

@dataclass
class PromptSegment:
    name: str
    content: str
    stable: bool
    token_estimate: int
    hash: str


@dataclass
class CacheAnalysisResult:
    segments: List[PromptSegment]
    stable_tokens: int
    dynamic_tokens: int
    total_tokens: int
    cache_efficiency: float
    cache_busters: List[str]
    recommended_order: List[str]
    warnings: List[str]
    is_optimal: bool


# ── Internal helpers ───────────────────────────────────────────────────

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _token_estimate(content: str) -> int:
    return len(content.split())


# ── Cache-buster patterns ──────────────────────────────────────────────

_TIMESTAMP_RE = re.compile(
    r'''(?x)
        \d{4}-\d{2}-\d{2}
        (?:[ T]\d{2}:\d{2}(?::\d{2})?
            (?:Z|[+-]\d{2}:?\d{2})?
        )?
    '''
)

_EPOCH_RE = re.compile(r'\b\d{10,}\b')

_UUID_RE = re.compile(
    r'''(?x)
        \b[0-9a-fA-F]{8}
        (?:-[0-9a-fA-F]{4}){3}
        -[0-9a-fA-F]{12}\b
    '''
)

_HUMAN_DATE_RE = re.compile(
    r'''(?xi)
        (?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)
        .*?\s+\d{1,2}
        (?:\s+\d{4})?
    '''
)

_SESSION_NONCE_RE = re.compile(
    r'''(?xi)
        \b(?:session|nonce|token|id)
        [\s_:-]*
        [A-Za-z0-9]{8,}
    '''
)

_LONG_TOKEN_RE = re.compile(r'\b[A-Za-z0-9]{20,}\b')


# ── Public API: detect_cache_busters ──────────────────────────────────

def detect_cache_busters(content: str, segment_name: str = "unknown") -> List[str]:
    warnings: List[str] = []

    if _TIMESTAMP_RE.search(content):
        warnings.append(f"Segment '{segment_name}' contains an ISO 8601 timestamp.")

    if _EPOCH_RE.search(content):
        warnings.append(f"Segment '{segment_name}' contains a Unix epoch value.")

    if _UUID_RE.search(content):
        warnings.append(f"Segment '{segment_name}' contains a UUID.")

    if _HUMAN_DATE_RE.search(content):
        warnings.append(f"Segment '{segment_name}' contains a human-readable date.")

    if _SESSION_NONCE_RE.search(content):
        warnings.append(f"Segment '{segment_name}' contains a session/identifier token.")

    for m in _LONG_TOKEN_RE.finditer(content):
        warnings.append(
            f"Segment '{segment_name}' contains a long variable token "
            f"'{m.group()}' (possible counter or nonce)."
        )

    seen = set()
    unique_warnings = []
    for w in warnings:
        if w not in seen:
            unique_warnings.append(w)
            seen.add(w)

    return unique_warnings


# ── Public API: compute_cache_efficiency ──────────────────────────────

def compute_cache_efficiency(stable_tokens: int, total_tokens: int) -> float:
    if total_tokens == 0:
        return 0.0
    return stable_tokens / total_tokens


# ── Stability inference ────────────────────────────────────────────────

def _default_stability(name: str) -> bool:
    return name in ("system_prompt", "tool_definitions", "addendum")


# ── Public API: recommend_order ───────────────────────────────────────

def recommend_order(
    segments: Dict[str, str],
    stable_keys: Optional[List[str]] = None,
) -> List[str]:
    if stable_keys is not None:
        stable_set = set(stable_keys)
    else:
        stable_set = {name for name in segments if _default_stability(name)}

    canonical_order = [
        "system_prompt",
        "tool_definitions",
        "addendum",
        "history",
        "user_message",
    ]
    canonical_rank = {name: idx for idx, name in enumerate(canonical_order)}
    MAX_RANK = len(canonical_order) + 1

    def sort_key(name: str):
        is_stable = name in stable_set

        # user_message is always last, unless explicitly marked stable
        if name == "user_message" and not is_stable:
            return (3, 0, name)

        if is_stable:
            rank = canonical_rank.get(name, MAX_RANK)
            return (0, rank, name)
        else:
            rank = canonical_rank.get(name, MAX_RANK)
            return (1, rank, name)

    return sorted(segments.keys(), key=sort_key)


# ── Public API: analyze_prompt ────────────────────────────────────────

def analyze_prompt(
    segments: Dict[str, str],
    stable_keys: Optional[List[str]] = None,
    previous_hashes: Optional[Dict[str, str]] = None,
) -> CacheAnalysisResult:

    # Handle empty input
    if not segments:
        return CacheAnalysisResult(
            segments=[],
            stable_tokens=0,
            dynamic_tokens=0,
            total_tokens=0,
            cache_efficiency=0.0,
            cache_busters=[],
            recommended_order=[],
            warnings=[],
            is_optimal=True,
        )

    # 1. Determine stability — explicit stable_keys is authoritative, not additive
    if stable_keys is not None:
        stability_map = {name: (name in stable_keys) for name in segments}
    else:
        stability_map = {name: _default_stability(name) for name in segments}

    # 2. Build PromptSegment objects
    seg_objects: List[PromptSegment] = []
    for name, content in segments.items():
        seg_objects.append(PromptSegment(
            name=name,
            content=content,
            stable=stability_map[name],
            token_estimate=_token_estimate(content),
            hash=_sha256(content),
        ))

    # 3. Hash-mismatch detection for stable segments
    cache_busters: List[str] = []
    if previous_hashes:
        for seg in seg_objects:
            if seg.stable and seg.name in previous_hashes:
                if seg.hash != previous_hashes[seg.name]:
                    cache_busters.append(
                        f"Hash mismatch for stable segment '{seg.name}': content changed."
                    )

    # 4. Pattern-based cache-buster detection
    for seg in seg_objects:
        cache_busters.extend(
            detect_cache_busters(seg.content, segment_name=seg.name)
        )

    # 5. Token aggregates
    stable_tokens = sum(s.token_estimate for s in seg_objects if s.stable)
    dynamic_tokens = sum(s.token_estimate for s in seg_objects if not s.stable)
    total_tokens = stable_tokens + dynamic_tokens

    # 6. Cache efficiency
    cache_efficiency = compute_cache_efficiency(stable_tokens, total_tokens)

    # 7. Non-fatal warnings
    warnings: List[str] = []

    if stable_tokens > 0 and dynamic_tokens > stable_tokens * 2:
        warnings.append(
            "Dynamic content (history/user_message) dominates token count — "
            "consider trimming conversation history."
        )

    add_seg = next((s for s in seg_objects if s.name == "addendum"), None)
    sys_seg = next((s for s in seg_objects if s.name == "system_prompt"), None)

    if add_seg and sys_seg:
        ratio = add_seg.token_estimate / max(sys_seg.token_estimate, 1)
        if ratio > 5.0:
            warnings.append(
                f"Addendum ({add_seg.token_estimate} tokens) is much larger than "
                f"system prompt ({sys_seg.token_estimate} tokens) — "
                f"consider consolidating into the system prompt."
            )

    if sys_seg and not sys_seg.content.strip():
        warnings.append("System prompt is empty.")

    seen_hashes: Dict[str, str] = {}
    for seg in seg_objects:
        if seg.hash in seen_hashes:
            warnings.append(
                f"Duplicate content detected between '{seen_hashes[seg.hash]}' "
                f"and '{seg.name}'."
            )
        else:
            seen_hashes[seg.hash] = seg.name

    # 8. Build and return result
    submitted_order = list(segments.keys())
    rec_order = recommend_order(segments, stable_keys)
    is_optimal = submitted_order == rec_order

    return CacheAnalysisResult(
        segments=seg_objects,
        stable_tokens=stable_tokens,
        dynamic_tokens=dynamic_tokens,
        total_tokens=total_tokens,
        cache_efficiency=cache_efficiency,
        cache_busters=cache_busters,
        recommended_order=rec_order,
        warnings=warnings,
        is_optimal=is_optimal,
    )