## daemon_feature_engineer_v2.py

import csv
import os
from collections import deque
import math

def engineer_features_v2(input_path, output_path, window_size=10):
    """
    Engineer features WITHOUT time leakage - pure command sequence patterns.
    Focuses on relative patterns in daemon command history for true behavioral prediction.
    Args:
        input_path: Path to daemon_calls.csv (0.0,0.0,0.0,0.0,1.0,command)
        output_path: Path to save engineered features CSV
        window_size: Size of sliding window for context features (recommended: 10-20)
    """
    # Read and extract just the command column
    commands = []
    with open(input_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 6:
                commands.append(row[5].strip())

    if not commands:
        raise ValueError("No valid commands found")

    total_lines = len(commands)
    print(f"Loaded {total_lines} commands from {input_path}")

    # Prepare sliding window for context (NO time_norm - removes positional leakage)
    window = deque(maxlen=window_size)
    features_list = []
    labels_list = []

    for i, cmd in enumerate(commands):
        # Feature 1: Same as previous command? (1.0 if yes, else 0.0)
        same_as_prev = 1.0 if i > 0 and cmd == commands[i-1] else 0.0
    
        # Feature 2-4: Recent command frequencies (consolidate, run_digest, verify_chain)
        consolidate_recent = window.count('consolidate_now') / window_size if window else 0.0
        run_digest_recent = window.count('run_digest_now') / window_size if window else 0.0
        verify_chain_recent = window.count('verify_chain') / window_size if window else 0.0
    
        # Feature 5: Command entropy in window (measures unpredictability/pattern complexity)
        # Higher entropy = more exploratory/random behavior (potential anomaly precursor)
        if window:
            freq = {}
            for c in window:
                freq[c] = freq.get(c, 0) + 1  # Fixed: was `f   req[c]`
            entropy = 0.0
            for count in freq.values():
                p = count / len(window)
                if p > 0:
                    entropy -= p * math.log2(p)
            max_entropy = math.log2(window_size) if window_size > 1 else 1
            command_entropy = entropy / max_entropy if max_entropy > 0 else 0.0
        else:
            command_entropy = 0.0
    
        # Current feature vector (ALL RELATIVE - NO ABSOLUTE TIME)
        feature_vec = [
            same_as_prev,
            consolidate_recent,
            run_digest_recent,
            verify_chain_recent,
            command_entropy
        ]
    
        features_list.append(feature_vec)
        labels_list.append(cmd)
    
        # Update window AFTER processing current command
        window.append(cmd)

    # Write output CSV
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        for features, label in zip(features_list, labels_list):
            writer.writerow(features + [label])

    print(f"Saved engineered features (v2 - NO time leakage) to {output_path}")
    print(f"Features: [same_as_prev, consolidate_recent, run_digest_recent, verify_chain_recent, command_entropy]")
    print(f"Labels: {sorted(set(labels_list))}")
    print(f"Window size: {window_size}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Engineer features v2 (relative only) from daemon command sequence")
    parser.add_argument("--input", required=True, help="Path to daemon_calls.csv")
    parser.add_argument("--output", required=True, help="Path to save engineered features CSV")
    parser.add_argument("--window", type=int, default=10, help="Sliding window size for context features")
    args = parser.parse_args()
    engineer_features_v2(args.input, args.output, args.window)