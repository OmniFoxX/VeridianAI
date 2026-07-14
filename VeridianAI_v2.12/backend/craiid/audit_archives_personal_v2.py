## audit_archives_personal_v2.py

import os
import json
import re
import string
import sys
from collections import Counter, defaultdict
from math import log2

import torch
import torch.nn as nn

# ─────────────────────────────────────────────
# STOPWORDS (consistent with audit_archives_deep.py)
# ─────────────────────────────────────────────
STOPWORDS = {
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'for', 'from', 'has', 'he', 'use',
    'in', 'is', 'it', 'its', 'of', 'on', 'that', 'the', 'to', 'was', 'were', 'well', 'within',
    'will', 'with', 'i', 'you', 'we', 'they', 'this', 'that', 'these', 'those', 'run', 'inside',
    'am', 'pm', 'ok', 'okay', 'hi', 'hello', 'hey', 'yeah', 'yes', 'no', 'not', 'need', 'outside',
    'so', 'do', 'did', 'have', 'had', 'but', 'or', 'if', 'then', 'than', 'just', 'make', 'over',
    'my', 'your', 'our', 'their', 'me', 'him', 'her', 'us', 'them', 'what', 'check', 'let', 'up',
    'when', 'where', 'how', 'all', 'been', 'being', 'get', 'got', 'also', 'up', 'get', 'set',
    'out', 'about', 'would', 'could', 'should', 'can', 'may', 'might', 'now', 'new', 'old', 'down',
    'like', 'just', 'really', 'very', 'much', 'more', 'some', 'any', 'there', 'wouldnt', 'wont',
    'here', 'which', 'who', 'into', 'over', 'after', 'before', 'between', 'through', 'throughout',
}

# ─────────────────────────────────────────────
# PLM ARCHITECTURE (must match train_plm_daemon_v2.py)
# ─────────────────────────────────────────────
class MicroLanguageModel(nn.Module):
    def __init__(self, input_dim, vocab_size, hidden_dim=16):
        super().__init__()
        self.network = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, vocab_size)
        )

    def forward(self, x):
        return self.network(x)


# ─────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────
def find_archives_root(start_path=None):
    """Walk up directory tree looking for an 'archives' folder."""
    if start_path is None:
        start_path = os.getcwd()
    for _ in range(5):
        archives_path = os.path.join(start_path, 'archives')
        if os.path.isdir(archives_path):
            return archives_path
        start_path = os.path.dirname(start_path)
    return None


def tokenize(text):
    """Lowercase, strip punctuation, tokenize, remove stopwords and digits."""
    text = text.lower()
    text = text.translate(str.maketrans('', '', string.punctuation))
    tokens = re.findall(r'\b\w+\b', text)
    return [t for t in tokens if t not in STOPWORDS and not t.isdigit() and len(t) > 1]


def compute_entropy(counter):
    """Shannon entropy of a word frequency distribution."""
    total = sum(counter.values())
    if total == 0:
        return 0.0
    entropy = 0.0
    for count in counter.values():
        p = count / total
        if p > 0:
            entropy -= p * log2(p)
    return entropy


def compute_lexical_richness(counter):
    """Type-Token Ratio: unique words / total words. Higher = more varied vocabulary."""
    total = sum(counter.values())
    unique = len(counter)
    return unique / total if total > 0 else 0.0


def score_behavioral_consistency(turn_lengths):
    """
    Measures how consistent User's message lengths are over time.
    Low variance = consistent/predictable, high variance = exploratory.
    Returns a 0-1 score where 1.0 = perfectly consistent.
    """
    if len(turn_lengths) < 2:
        return 1.0
    mean = sum(turn_lengths) / len(turn_lengths)
    variance = sum((x - mean) ** 2 for x in turn_lengths) / len(turn_lengths)
    std_dev = variance ** 0.5
    # Normalize: cap at mean*2 for a 0-1 score
    consistency = max(0.0, 1.0 - (std_dev / (mean * 2))) if mean > 0 else 0.0
    return round(consistency, 4)


def detect_drift(archive_files, archives_dir, role='user', segment_count=3):
    """
    Split archives chronologically into segments and compare top vocabulary.
    Detects whether User's language/focus has shifted over time.
    Returns per-segment top words for comparison.
    """
    sorted_files = sorted(archive_files)
    segment_size = max(1, len(sorted_files) // segment_count)
    segments = []

    for i in range(segment_count):
        start = i * segment_size
        # Last segment gets any remainder
        end = start + segment_size if i < segment_count - 1 else len(sorted_files)
        segment_files = sorted_files[start:end]
        counter = Counter()

        for filename in segment_files:
            filepath = os.path.join(archives_dir, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, list):
                    for turn in data:
                        if isinstance(turn, dict) and turn.get('role') == role:
                            content = turn.get('content', '')
                            counter.update(tokenize(content))
            except Exception:
                continue

        segments.append({
            'files': segment_files,
            'top_words': dict(counter.most_common(15))
        })

    return segments


# ─────────────────────────────────────────────
# PLM CORRELATION
# ─────────────────────────────────────────────
def load_plm(model_path, device='cpu'):
    """Load trained PLM and return model + metadata."""
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"PLM not found: {model_path}")

    # nosemgrep -- weights_only=True blocks arbitrary-code execution during
    # unpickling; only checkpoint['model_state_dict'] (tensors) is consumed below.
    checkpoint = torch.load(model_path, map_location=device, weights_only=True)

    required_keys = {'model_state_dict', 'idx_to_label', 'feature_dim', 'hidden_dim'}
    missing = required_keys - checkpoint.keys()
    if missing:
        raise ValueError(f"PLM checkpoint missing keys: {missing}")

    idx_to_label = checkpoint['idx_to_label']
    vocab_size = len(idx_to_label)

    model = MicroLanguageModel(
        input_dim=checkpoint['feature_dim'],
        vocab_size=vocab_size,
        hidden_dim=checkpoint['hidden_dim']
    ).to(device)

    model.load_state_dict(checkpoint['model_state_dict'])
    model.eval()

    return model, checkpoint


def build_plm_feature_vector(user_counter, all_commands):
    """
    Derive a PLM-compatible feature vector from archive vocabulary patterns.

    Maps User's conversational behavioral signals to the 5 PLM features:
      [same_as_prev, consolidate_recent, run_digest_recent, verify_chain_recent, command_entropy]

    This is a behavioral approximation — not a live command sequence,
    but a profile-level signal derived from archive patterns.
    """
    total_tokens = sum(user_counter.values())
    if total_tokens == 0:
        return [0.0, 0.0, 0.0, 0.0, 0.0]

    # FIX: most_common returns list of tuples, extract the count from 
    top_word_count = user_counter.most_common(1)[0] [1] if user_counter else 0
    top_word_freq = top_word_count / total_tokens
    same_as_prev_proxy = round(min(top_word_freq * 2, 1.0), 4)

    # Command proxies unchanged
    consolidate_proxy = round(
        min(sum(user_counter.get(w, 0) for w in
            ['consolidate', 'save', 'store', 'keep', 'preserve', 'archive']) / max(total_tokens, 1), 1.0), 4
    )
    run_digest_proxy = round(
        min(sum(user_counter.get(w, 0) for w in
            ['digest', 'summarize', 'summary', 'recap', 'review', 'process']) / max(total_tokens, 1), 1.0), 4
    )
    verify_chain_proxy = round(
        min(sum(user_counter.get(w, 0) for w in
            ['verify', 'check', 'confirm', 'validate', 'test', 'ensure']) / max(total_tokens, 1), 1.0), 4
    )

    # Entropy: behavioral complexity of User's vocabulary
    entropy = compute_entropy(user_counter)
    max_entropy = log2(len(user_counter)) if len(user_counter) > 1 else 1.0
    entropy_normalized = round(entropy / max_entropy if max_entropy > 0 else 0.0, 4)

    return [
        same_as_prev_proxy,
        consolidate_proxy,
        run_digest_proxy,
        verify_chain_proxy,
        entropy_normalized
    ]


def run_plm_correlation(model, checkpoint, feature_vector, device='cpu'):
    """Run PLM inference against archive-derived feature vector."""
    idx_to_label = checkpoint['idx_to_label']
    features_tensor = torch.tensor([feature_vector], dtype=torch.float32).to(device)

    with torch.no_grad():
        logits = model(features_tensor)
        probabilities = torch.softmax(logits, dim=1)
        predicted_idx = torch.argmax(probabilities, dim=1).item()

    predicted_label = idx_to_label[predicted_idx]
    probs = probabilities.cpu().numpy().tolist()[0]
    prob_dict = {label: round(prob, 4) for label, prob in zip(idx_to_label, probs)}

    return predicted_label, prob_dict


# ─────────────────────────────────────────────
# PERSONALIZATION CANDIDATE EXTRACTION
# ─────────────────────────────────────────────
def extract_personalization_candidates(user_counter, assistant_counter, top_n=1000):
    """
    Identify User-specific vocabulary that CRAIID should learn.
    Candidates are words that appear frequently in User's messages
    but are rare or absent in Sage's responses — User's unique signal.
    """
    user_top = set(w for w, _ in user_counter.most_common(1000))
    assistant_top = set(w for w, _ in assistant_counter.most_common(1000))

    # Words User uses that Sage doesn't mirror back strongly
    user_unique = user_top - assistant_top
    user_shared = user_top & assistant_top

    candidates = {
        word: user_counter[word]
        for word in user_unique
        if user_counter[word] > 2  # Filter noise — must appear more than twice
    }

    # Sort by frequency
    candidates_sorted = dict(
        sorted(candidates.items(), key=lambda x: x, reverse=True)[:top_n]
    )

    return candidates_sorted, list(user_shared)[:top_n]


# ─────────────────────────────────────────────
# MAIN ANALYSIS
# ─────────────────────────────────────────────
def analyze_personal(archives_dir, plm_path, output_path, device='cpu', top_n=1000, show_stopwords: bool = False):
    """
    Full personal behavioral analysis:
    - User-voice isolation and vocabulary fingerprint
    - Behavioral consistency and drift detection
    - PLM feature vector derivation from archive patterns
    - PLM correlation: what does Sage's model predict about User's behavioral style?
    - Personalization candidates for CRAIID
    """
    archive_files = [f for f in os.listdir(archives_dir) if f.endswith('.json')]
    print(f"Found {len(archive_files)} archive files in {archives_dir}")

    user_counter = Counter()
    assistant_counter = Counter()
    user_turn_lengths = []
    all_user_messages = []
    session_count = 0
    skipped = 0

    # ── Pass 1: Extract all dialogue ──────────────────────────────
    for filename in sorted(archive_files):
        filepath = os.path.join(archives_dir, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if not isinstance(data, list):
                skipped += 1
                continue

            session_count += 1
            for turn in data:
                if not isinstance(turn, dict):
                    continue
                role = turn.get('role', '').strip().lower()
                content = turn.get('content', '').strip()
                if not content:
                    continue

                tokens = tokenize(content)
                if role == 'user':
                    user_counter.update(tokens)
                    user_turn_lengths.append(len(content.split()))
                    all_user_messages.append(content)
                elif role == 'assistant':
                    assistant_counter.update(tokens)

        except Exception as e:
            print(f"  Error reading {filename}: {e}")
            skipped += 1

    if not user_counter:
        raise ValueError("No user messages found in archives.")

    print(f"Processed {session_count} sessions ({skipped} skipped)\n")

    # ── Behavioral Metrics ─────────────────────────────────────────
    consistency_score = score_behavioral_consistency(user_turn_lengths)
    lexical_richness = compute_lexical_richness(user_counter)
    vocab_entropy = compute_entropy(user_counter)
    max_entropy = log2(len(user_counter)) if len(user_counter) > 1 else 1.0
    entropy_normalized = round(vocab_entropy / max_entropy, 4) if max_entropy > 0 else 0.0

    avg_turn_length = round(sum(user_turn_lengths) / len(user_turn_lengths), 2) if user_turn_lengths else 0.0

    # ── Drift Detection ────────────────────────────────────────────
    drift_segments = detect_drift(archive_files, archives_dir, role='user', segment_count=3)

    # ── Personalization Candidates ─────────────────────────────────
    personalization_candidates, shared_vocab = extract_personalization_candidates(
        user_counter, assistant_counter, top_n=top_n
    )

    # ── PLM Correlation ────────────────────────────────────────────
    plm_result = None
    plm_feature_vector = None

    if plm_path and os.path.exists(plm_path):
        print(f"Loading PLM from {plm_path}...")
        try:
            model, checkpoint = load_plm(plm_path, device)
            plm_feature_vector = build_plm_feature_vector(user_counter, list(checkpoint['idx_to_label']))
            predicted_command, prob_dict = run_plm_correlation(model, checkpoint, plm_feature_vector, device)
            plm_result = {
                'feature_vector': plm_feature_vector,
                'feature_labels': [
                    'same_as_prev_proxy',
                    'consolidate_proxy',
                    'run_digest_proxy',
                    'verify_chain_proxy',
                    'entropy_normalized'
                ],
                'predicted_command': predicted_command,
                'probabilities': dict(sorted(prob_dict.items(), key=lambda x: x, reverse=True))
            }
            print(f"PLM correlation complete. Predicted behavioral command: {predicted_command}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  WARNING: PLM correlation failed: {e}")
            plm_result = {'error': str(e)}
    else:
        print("  WARNING: PLM path not provided or not found — skipping PLM correlation.")
        plm_result = {'error': 'PLM not available'}

    # ── Assemble Full Report ───────────────────────────────────────
    report = {
        'meta': {
            'archives_dir': archives_dir,
            'plm_path': plm_path,
            'sessions_processed': session_count,
            'sessions_skipped': skipped,
            'total_user_tokens': sum(user_counter.values()),
            'total_assistant_tokens': sum(assistant_counter.values()),
            'unique_user_vocab': len(user_counter),
            'unique_assistant_vocab': len(assistant_counter)
        },
        'user_behavioral_profile': {
            'avg_turn_length_words': avg_turn_length,
            'behavioral_consistency_score': consistency_score,
            'lexical_richness_ttr': round(lexical_richness, 4),
            'vocabulary_entropy_normalized': entropy_normalized,
            'top_words': dict(user_counter.most_common(top_n))
        },
        'drift_analysis': {
            'description': (
                'Archives split into 3 chronological segments. '
                'Compare top_words across segments to detect focus/language drift over time.'
            ),
            'segments': [
                {
                    'segment': i + 1,
                    'files': s['files'],
                    'top_words': s['top_words']
                }
                for i, s in enumerate(drift_segments)
            ]
        },
        'personalization_candidates': {
            'description': (
                'Words User uses frequently that Sage does not mirror back — '
                'User unique signal for CRAIID personalization layer.'
            ),
            'user_unique_vocabulary': personalization_candidates,
            'shared_vocabulary_sample': shared_vocab
        },
        'plm_correlation': {
            'description': (
                'PLM feature vector derived from archive behavioral patterns. '
                'Reflects User conversational style mapped to daemon command tendencies. '
                'Predicted command represents the behavioral archetype CRAIID should expect.'
            ),
            'result': plm_result
        }
    }

    # ── Save Report ────────────────────────────────────────────────
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2)

    print(f"\nPersonal audit report saved to {output_path}")

    # ── Console Summary ────────────────────────────────────────────
    print("\n" + "=" * 1000)
    print("USER BEHAVIORAL PROFILE SUMMARY")
    print("=" * 1000)
    print(f"  Sessions analyzed:          {session_count}")
    print(f"  Total user tokens:          {sum(user_counter.values())}")
    print(f"  Unique user vocabulary:     {len(user_counter)}")
    print(f"  Avg turn length (words):    {avg_turn_length}")
    print(f"  Behavioral consistency:     {consistency_score}")
    print(f"  Lexical richness (TTR):     {round(lexical_richness, 4)}")
    print(f"  Vocabulary entropy:         {entropy_normalized}")
    print()
    print(f"  Top 1000 User words:")
    for word, count in user_counter.most_common(1000):
        print(f"    {word:<25} {count}")
    print()
    if plm_result and 'predicted_command' in plm_result:
        print(f"  PLM Behavioral Prediction:  {plm_result['predicted_command']}")
        print(f"  Probability Distribution:")
        for cmd, prob in plm_result['probabilities'].items():
            print(f"    {cmd:<25} {prob:.4f}")
        print("=" * 1000)

    # ── Stopword candidate harvester ──────────────────────────────
    if show_stopwords:
        print("\n─── Bottom 200 Stopword Candidates ───────────────")
        bottom_200 = user_counter.most_common()[:-201:-1]  # reverse slice = least frequent
        for i, (term, count) in enumerate(bottom_200, 1):
            print(f"  {i:>3}. {term} ({count})")
        print("──────────────────────────────────────────────────")

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Personal archive audit with PLM correlation for CRAIID baseline"
    )
    parser.add_argument(
        "--archives",
        default=None,
        help="Path to archives directory (auto-detected if omitted)"
    )
    parser.add_argument(
        "--plm",
        default=r"E:\OracleAI_v2.3\backend\mlm_training_data\sage_plm.pt",
        help="Path to trained PLM .pt file"
    )
    parser.add_argument(
        "--output",
        default=r"E:\OracleAI_v2.3\backend\craiid\audit_personal_report.json",
        help="Path to save JSON report"
    )
    parser.add_argument(
        "--device",
        choices=["cpu", "cuda"],
        default="cpu",
        help="Compute device for PLM inference (default: cpu)"
    )
    parser.add_argument(
        "--top",
        type=int,
        default=1000,
        help="Top N words to include in analysis (default: 1000)"
    )
    parser.add_argument(
        "--stopwords",
        action="store_true",
        help="Print bottom 200 stopword candidates after audit"
    )

    args = parser.parse_args()

    # Resolve archives directory
    archives_dir = args.archives
    if archives_dir is None:
        archives_dir = find_archives_root()
    if archives_dir is None:
        archives_dir = r"E:\OracleAI_v2.3\archives"
    if not os.path.isdir(archives_dir):
        print(f"ERROR: Could not find archives directory: {archives_dir}")
        sys.exit(1)

    try:
        analyze_personal(
            archives_dir=archives_dir,
            plm_path=args.plm,
            output_path=args.output,
            device=args.device,
            top_n=args.top,
            show_stopwords=args.stopwords
        )
    except Exception as e:
        print(f"Audit failed: {str(e)}")
        raise


if __name__ == '__main__':
    main()