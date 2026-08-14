## audit_archives_deep.py

import os
import json
import re
import string
from collections import Counter, defaultdict

# Basic English stopwords (expand as needed)
STOPWORDS = {
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'for', 'from', 'has', 'he',
    'in', 'is', 'it', 'its', 'of', 'on', 'that', 'the', 'to', 'was', 'were',
    'will', 'with', 'i', 'you', 'we', 'they', 'this', 'that', 'these', 'those',
    'am', 'pm', 'ok', 'okay', 'hi', 'hello', 'hey', 'yeah', 'yes', 'no', 'not',
    'so', 'do', 'did', 'have', 'had', 'but', 'or', 'if', 'then', 'than', 'just',
    'my', 'your', 'our', 'their', 'me', 'him', 'her', 'us', 'them', 'what',
    'when', 'where', 'how', 'all', 'been', 'being', 'get', 'got', 'also', 'up',
    'out', 'about', 'would', 'could', 'should', 'can', 'may', 'might', 'now'
}

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
    """Lowercase, strip punctuation, tokenize, remove stopwords."""
    text = text.lower()
    text = text.translate(str.maketrans('', '', string.punctuation))
    tokens = re.findall(r'\b\w+\b', text)
    return [t for t in tokens if t not in STOPWORDS and not t.isdigit() and len(t) > 1]

def analyze_archives(archives_dir, top_n=600):
    """
    Deep vocabulary analysis of archive JSON files.
    Breaks down word frequency per role (user/assistant/system)
    for CRAIID logic framework building.
    """
    archive_files = [f for f in os.listdir(archives_dir) if f.endswith('.json')]
    print(f"Analyzing {len(archive_files)} archive files...\n")

    # Per-role word tracking
    role_word_counts = defaultdict(Counter)
    global_counter = Counter()
    role_message_counts = defaultdict(int)
    skipped = 0

    for filename in sorted(archive_files):
        filepath = os.path.join(archives_dir, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if not isinstance(data, list):
                print(f"  Warning: {filename} is not a list — skipping")
                skipped += 1
                continue

            for turn in data:
                if not isinstance(turn, dict):
                    continue
                role = turn.get('role', 'unknown').strip().lower()
                content = turn.get('content', '').strip()
                if not content:
                    continue

                tokens = tokenize(content)
                role_word_counts[role].update(tokens)
                global_counter.update(tokens)
                role_message_counts[role] += 1

        except Exception as e:
            print(f"  Error reading {filename}: {e}")
            skipped += 1

    if skipped:
        print(f"  ({skipped} files skipped due to errors)\n")

    # --- Global Summary ---
    print("=" * 72)
    print(f"GLOBAL TOP {top_n} MEANINGFUL WORDS (all roles combined)")
    print("=" * 72)
    for word, count in global_counter.most_common(top_n):
        print(f"  {word:<25} {count}")

    # --- Per-Role Breakdown ---
    print()
    for role in sorted(role_word_counts.keys()):
        counter = role_word_counts[role]
        msg_count = role_message_counts[role]
        print("=" * 100)
        print(f"ROLE: '{role}' — {msg_count} messages, {sum(counter.values())} meaningful tokens")
        print(f"TOP {top_n} WORDS:")
        print("=" * 100)
        for word, count in counter.most_common(top_n):
            print(f"  {word:<25} {count}")
        print()

    # --- Vocabulary Overlap (useful for CRAIID pattern building) ---
    roles = list(role_word_counts.keys())
    if len(roles) >= 2:
        print("=" * 69)
        print("VOCABULARY OVERLAP BETWEEN ROLES")
        print("=" * 69)
        for i in range(len(roles)):
            for j in range(i + 1, len(roles)):
                r1, r2 = roles[i], roles[j]
                vocab1 = set(role_word_counts[r1].keys())
                vocab2 = set(role_word_counts[r2].keys())
                overlap = vocab1 & vocab2
                print(f"  '{r1}' ∩ '{r2}': {len(overlap)} shared words")
        print()

    return role_word_counts, global_counter

def main():
    archives_dir = find_archives_root()
    if archives_dir is None:
        archives_dir = r"E:\OracleAI_v2.2\archives"
        if not os.path.isdir(archives_dir):
            print(f"ERROR: Could not find archives directory. Tried default: {archives_dir}")
            import sys
            sys.exit(1)

    print(f"Archives directory: {archives_dir}\n")
    analyze_archives(archives_dir, top_n=600)

if __name__ == '__main__':
    main()