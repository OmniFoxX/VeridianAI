# journalist_ops_detector_v3.3

import json
import re
import math
import sys
import os
from collections import deque, Counter
from pathlib import Path
from typing import List, Set, Tuple, Dict

TOKEN_PATTERN = re.compile(r"\b\w+\b")


def load_ops_lexicon(json_path: str | Path, top_n: int = 850) -> Set[str]:
    """Load and return the top `top_n` ops terms from audit_personal_report.json."""
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    vocab: dict = data.get("user_behavioral_profile", {}).get("top_words", {})
    # Sort by frequency descending
    sorted_terms = sorted(vocab.items(), key=lambda kv: kv[1], reverse=True)
    return {term.lower() for term, _ in sorted_terms[:top_n]}


def tokenize(text: str) -> List[str]:
    """Return a list of lowercased word tokens."""
    return TOKEN_PATTERN.findall(text.lower())


def ops_score(tokens: List[str], lexicon: Set[str]) -> float:
    """Fraction of tokens that belong to the ops lexicon."""
    if not tokens:
        return 0.0
    return sum(1 for t in tokens if t in lexicon) / len(tokens)


def _population_stdev(values: List[float]) -> float:
    """Population standard deviation (divide by N)."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((x - mean) ** 2 for x in values) / n)


class JournalistOpsDetector:
    """
    Ops-mode detector combining burst detection, dynamic z-score,
    and session-level Counter for hit frequency analysis.
    """

    def __init__(
        self,
        lexicon: Set[str],
        burst_window: int = 3,
        burst_k: int = 3,
        zscore_window: int = 20,
        zscore_k: float = 1.5,
    ):
        self.lexicon = lexicon
        self.burst_window = burst_window
        self.burst_k = burst_k
        self.zscore_k = zscore_k
        self.recent_turns: deque[Set[str]] = deque(maxlen=burst_window)
        self.recent_scores: deque[float] = deque(maxlen=zscore_window)
        self.session_hits: Counter = Counter()

    def update(self, turn_text: str) -> Tuple[bool, float, Dict]:
        """
        Process a single turn.
        Returns (is_ops_mode, score, details_dict).
        """
        tokens = tokenize(turn_text)
        score = ops_score(tokens, self.lexicon)

        # Per-turn matched terms
        turn_matches = [t for t in tokens if t in self.lexicon]

        # Session Counter update
        self.session_hits.update(turn_matches)

        # Burst detection: union of token sets in the window intersected with lexicon
        self.recent_turns.append(set(tokens))
        burst_flag = False
        if len(self.recent_turns) == self.burst_window:
            union_set: Set[str] = set().union(*self.recent_turns)
            ops_in_window = union_set & self.lexicon
            burst_flag = len(ops_in_window) >= self.burst_k

        # Dynamic z-score detection (population stdev)
        self.recent_scores.append(score)
        zscore_flag = False
        if len(self.recent_scores) >= 2:
            mean = sum(self.recent_scores) / len(self.recent_scores)
            std = _population_stdev(list(self.recent_scores))
            zscore_flag = (std > 0) and (score > mean + self.zscore_k * std)

        is_ops = burst_flag or zscore_flag

        details: Dict = {
            "score": score,
            "burst_flag": burst_flag,
            "zscore_flag": zscore_flag,
            "mean_score": sum(self.recent_scores) / len(self.recent_scores)
            if self.recent_scores
            else 0.0,
            "std_score": _population_stdev(list(self.recent_scores)),
            # Per-turn top hits – useful for coordinator signal payload
            "top_hits": Counter(turn_matches).most_common(24),
        }
        return is_ops, score, details

    def session_summary(self) -> Dict:
        """Return a summary of the full session's ops term activity."""
        total_hits = sum(self.session_hits.values())
        return {
            "total_ops_hits": total_hits,
            "unique_ops_terms": len(self.session_hits),
            "top_terms": self.session_hits.most_common(24),
            "hit_distribution": ("focused" if len(self.session_hits) <= 16 else "broad"),
        }


def main() -> None:
    """Interactive stdin loop for real-time ops-mode detection."""
    default_path = os.path.join(os.path.dirname(
        __file__), "audit_personal_report.json")
    audit_path = sys.argv if len(sys.argv) > 1 else default_path

    if not os.path.isfile(audit_path):
        print(f"Error: audit file not found at {audit_path}")
        sys.exit(1)

    lexicon = load_ops_lexicon(audit_path, top_n=850)
    print(f"Built ops lexicon with {len(lexicon)} terms:")
    print(", ".join(sorted(lexicon)[:42]), "...\n")

    # ── Write lexicon immediately after loading, not after the loop ──
    out_path = os.path.join(os.path.dirname(__file__), "ops_lexicon.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        for term in sorted(lexicon):
            f.write(term + "\n")
    print(f"Full lexicon saved to {out_path}\n")
    # ─────────────────────────────────────────────────────────────────

    detector = JournalistOpsDetector(lexicon)

    print("Enter turns (blank line to finish):")
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            break

        flag, score, det = detector.update(line)
        reasons = []
        if det["burst_flag"]:
            reasons.append("burst")
        if det["zscore_flag"]:
            reasons.append("z-score")
        trigger = f" ← [{' + '.join(reasons)}]" if flag else ""

        print(
            f"Score: {score:.4f} | Ops-mode? {flag}  "
            f"| burst={det['burst_flag']}, z={det['zscore_flag']}{trigger}"
        )
        if det["top_hits"]:
            hits_str = ", ".join(
                f"{term}×{count}" for term, count in det["top_hits"])
            print(f"  → turn hits: [{hits_str}]")

    # Session summary (still printed after loop for interactive use)
    summary = detector.session_summary()
    print("\n─── Session Summary ──────────────────────────────")
    print(f"  Total ops hits     : {summary['total_ops_hits']}")
    print(f"  Unique ops terms   : {summary['unique_ops_terms']}")
    print(f"  Hit distribution   : {summary['hit_distribution']}")
    top_str = ", ".join(f"{t}×{c}" for t, c in summary["top_terms"])
    print(f"  Top 24 terms       : {top_str}")
    print("──────────────────────────────────────────────────")

    # Save full lexicon for inspection
    out_path = os.path.join(os.path.dirname(__file__), "ops_lexicon.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        for term in sorted(lexicon):
            f.write(term + "\n")
    print(f"\nFull lexicon saved to {out_path}")

if __name__ == "__main__":
    main()
