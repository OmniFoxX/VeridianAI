# context_fatigue_detector.py
import argparse
import json
import os
import re
import sys
import math
from collections import Counter
from pathlib import Path  

def _import_atrest():
    """Locate atrest.py whether this module runs from backend\\ or backend\\craiid\\."""
    try:
        import atrest
        return atrest
    except ImportError:
        here = Path(__file__).resolve().parent
        for cand in (here, here.parent):
            if (cand / "atrest.py").exists() and str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
        import atrest
        return atrest


def load_archives(archives_dir: Path):
    """Load all archive JSON files from the given directory.

    v2.11.12 fix (2026-07-02): archives are Fernet-encrypted at rest
    (atrest.py) and this loader was doing a plain json.load, so EVERY
    archive failed with 'Expecting value: line 1 column 1' and the
    fatigue detector + CRAIID ops snapshot ran on zero archives.
    atrest.load_json_auto handles encrypted AND legacy plaintext files,
    so nothing is lost either way. If atrest itself is unavailable
    (standalone use outside the project), fall back to plaintext parse."""
    try:
        _atrest = _import_atrest()
    except Exception:
        _atrest = None
    archives = []
    for file_path in sorted(archives_dir.glob("archive_*.json")):
        try:
            blob = file_path.read_bytes()
            if _atrest is not None:
                data = _atrest.load_json_auto(blob)
            else:
                data = json.loads(blob.decode("utf-8"))
            # Expect each archive to have a "messages" list or similar; adapt as needed.
            if isinstance(data, dict) and "messages" in data:
                archives.append(data["messages"])
            elif isinstance(data, list):
                archives.append(data)
            else:
                # Fallback: treat the whole file as a single message container
                archives.append([data])
        except Exception as e:
            print(f"Warning: Could not load {file_path}: {e}", file=sys.stderr)
    return archives

def extract_user_texts(messages):
    """Extract plain text from user messages."""
    texts = []
    for msg in messages:
        # Assuming each message is a dict with "role" and "content"
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                # Sometimes content is a list of parts; join strings
                texts.append(" ".join(part if isinstance(part, str) else str(part) for part in content))
    return texts

def extract_assistant_texts(messages):
    """Extract plain text from ASSISTANT messages.

    v2.12.17: the detector previously analyzed USER texts only, so
    assistant-output degradation was invisible to it (and to CRAIID).
    Incident 2026-07-23: after a long build-battle session with a
    stopped/resent generation, Toga's reply came out token-FUSED --
    the last quarter was one unbroken string, no spaces or punctuation.
    Nothing flagged it. This extractor + compute_whitespace_collapse()
    close that gap."""
    texts = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                texts.append(" ".join(part if isinstance(part, str) else str(part) for part in content))
    return texts


def compute_whitespace_collapse(texts, window_turns,
                                min_len=200,
                                ratio_threshold=0.05,
                                run_threshold=300):
    """Detect token-fusion / whitespace collapse in recent assistant output.

    Normal English prose runs ~0.15-0.18 whitespace-to-character ratio.
    Fused output (dropped space tokens) collapses toward 0.0, and the
    longest unbroken character run explodes. Either signal on a
    sufficiently long message (>= min_len chars, so code hashes or URLs
    in short replies do not false-positive) marks collapse.

    Returns (collapsed: bool, details: dict)."""
    window = texts[-window_turns:] if len(texts) >= window_turns else texts
    worst_ratio, worst_run, flagged = 1.0, 0, []
    for i, t in enumerate(window):
        if not isinstance(t, str) or len(t) < min_len:
            continue
        ws = sum(1 for ch in t if ch.isspace())
        ratio = ws / len(t)
        longest = max((len(run) for run in t.split()), default=0)
        worst_ratio = min(worst_ratio, ratio)
        worst_run = max(worst_run, longest)
        if ratio < ratio_threshold or longest > run_threshold:
            flagged.append({
                "index_in_window": i,
                "length": len(t),
                "whitespace_ratio": round(ratio, 4),
                "longest_unbroken_run": longest,
                "preview": t[:80],
            })
    details = {
        "messages_flagged": len(flagged),
        "worst_whitespace_ratio": round(worst_ratio, 4),
        "longest_unbroken_run": worst_run,
        "flagged": flagged,
    }
    return (len(flagged) > 0), details


def compute_metrics(texts, window_turns):
    """Compute fatigue metrics over the last `window_turns` user messages."""
    if not texts:
        return 0.0, 0.0, 0.0  # token_ratio, repetition_ratio, entropy
    # Take the last `window_turns` messages (or fewer if not enough)
    window = texts[-window_turns:] if len(texts) >= window_turns else texts
    # Join texts for tokenization
    joined = " ".join(window)
    # Simple tokenization: split on whitespace and punctuation
    tokens = re.findall(r"\b\w+\b", joined.lower())
    token_count = len(tokens)
    # Estimate max tokens: we can use a config or approximate from data; here we use a high constant.
    # For fatigue we just need a ratio; we can use a moving average of token count.
    # We'll compute token ratio as token_count / (window_turns * avg_tokens_per_turn)
    # For simplicity, use a fixed max of 500 tokens per turn (adjustable).
    max_expected = window_turns * 500
    token_ratio = min(token_count / max_expected, 1.0) if max_expected > 0 else 0.0

    # Repetition ratio: proportion of tokens that are repeats (1 - unique/total)
    if token_count > 0:
        unique_ratio = len(set(tokens)) / token_count
        repetition_ratio = 1.0 - unique_ratio
    else:
        repetition_ratio = 0.0

    # Entropy: Shannon entropy of token distribution (normalized 0-1)
    if token_count > 0:
        freq = Counter(tokens)
        probs = [count / token_count for count in freq.values()]
        entropy = -sum(p * (p and math.log(p, 2)) for p in probs)  # log base 2
        # Max entropy for distinct tokens = log2(num_unique)
        max_entropy = math.log(len(freq), 2) if len(freq) > 1 else 1.0
        entropy_norm = entropy / max_entropy if max_entropy > 0 else 0.0
    else:
        entropy_norm = 0.0

    return token_ratio, repetition_ratio, entropy_norm


def main():
    parser = argparse.ArgumentParser(
        description="Detect context fatigue from recent user messages in archive files."
    )
    parser.add_argument(
        "--archives-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "archives",
        help="Directory containing archive JSON files (default: E:\\OracleAI_v2.2\\archives)",
    )
    parser.add_argument(
        "--window-turns",
        type=int,
        default=5,
        help="Number of recent user turns to consider for fatigue calculation",
    )
    parser.add_argument(
        "--token-threshold",
        type=float,
        default=0.7,
        help="Token ratio threshold (0-1) above which fatigue is signaled",
    )
    parser.add_argument(
        "--repetition-threshold",
        type=float,
        default=0.6,
        help="Repetition ratio threshold (0-1) above which fatigue is signaled",
    )
    parser.add_argument(
        "--entropy-threshold",
        type=float,
        default=0.4,
        help="Normalized entropy threshold (0-1) below which fatigue is signaled (low entropy = repetitive)",
    )
    parser.add_argument(
        "--whitespace-ratio-threshold",
        type=float,
        default=0.05,
        help="Assistant-output whitespace ratio below which token-fusion/"
             "whitespace collapse is signaled (normal prose is ~0.15+)",
    )
    parser.add_argument(
        "--unbroken-run-threshold",
        type=int,
        default=300,
        help="Longest unbroken (no-whitespace) character run in assistant "
             "output above which collapse is signaled",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "sage_data" / "downloads" / "coordinator_signal.json",
        help="File to write the fatigue signal JSON (default: Downloads\\coordinator_signal.json)",
    )
    args = parser.parse_args()

    archives_dir = args.archives_dir
    if not archives_dir.is_dir():
        print(f"Error: Archives directory not found: {archives_dir}", file=sys.stderr)
        sys.exit(1)

    archives = load_archives(archives_dir)
    # Flatten messages preserving order across files (simple concatenation)
    all_messages = []
    for msg_list in archives:
        if isinstance(msg_list, list):
            all_messages.extend(msg_list)
    user_texts = extract_user_texts(all_messages)

    token_ratio, repetition_ratio, entropy_norm = compute_metrics(user_texts, args.window_turns)

    # v2.12.17: assistant-side output-degradation check (token fusion).
    assistant_texts = extract_assistant_texts(all_messages)
    ws_collapsed, ws_details = compute_whitespace_collapse(
        assistant_texts, args.window_turns,
        ratio_threshold=args.whitespace_ratio_threshold,
        run_threshold=args.unbroken_run_threshold)

    fatigue_signaled = False
    reasons = []
    if token_ratio > args.token_threshold:
        fatigue_signaled = True
        reasons.append(f"token_ratio {token_ratio:.2f} > threshold {args.token_threshold}")
    if repetition_ratio > args.repetition_threshold:
        fatigue_signaled = True
        reasons.append(f"repetition_ratio {repetition_ratio:.2f} > threshold {args.repetition_threshold}")
    if entropy_norm < args.entropy_threshold:
        fatigue_signaled = True
        reasons.append(f"entropy_norm {entropy_norm:.2f} < threshold {args.entropy_threshold}")
    if ws_collapsed:
        fatigue_signaled = True
        reasons.append(
            f"assistant whitespace collapse: {ws_details['messages_flagged']} "
            f"message(s), worst ratio {ws_details['worst_whitespace_ratio']}, "
            f"longest run {ws_details['longest_unbroken_run']} chars")

    signal = {
        "fatigue_detected": fatigue_signaled,
        "metrics": {
            "token_ratio": round(token_ratio, 3),
            "repetition_ratio": round(repetition_ratio, 3),
            "entropy_normalized": round(entropy_norm, 3),
        },
        "assistant_whitespace_collapse": ws_details,
        "window_turns": args.window_turns,
        "user_messages_considered": len(user_texts),
        "total_user_messages": len([m for m in all_messages if isinstance(m, dict) and m.get("role") == "user"]),
        "reasons": reasons,
        "timestamp": __import__("datetime").datetime.now().isoformat(),
    }

    # Ensure output directory exists
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(signal, f, indent=2)

    print(json.dumps(signal, indent=2))
    if fatigue_signaled:
        print("Fatigue detected. Signal written to:", args.output)
    else:
        print("No fatigue detected.")

if __name__ == "__main__":
    main()