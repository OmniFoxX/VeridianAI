## audit_archives_lite.py

import os
import json
import collections
import re
import sys

def find_archives_root(start_path=None):
    # v2.15.2: ask the app before searching. User content moved to sage_data on
    # 2026-08-13; walking up from here finds the install directory's leftover
    # (empty) archives folder, which is why this reported an empty corpus
    # instead of an error. craiid_paths keeps the walk-up as its last resort so
    # a standalone run from a terminal still works.
    try:
        import sys as _sys, os as _os
        _here = _os.path.dirname(_os.path.abspath(__file__))
        if _here not in _sys.path:
            _sys.path.insert(0, _here)
        from craiid_paths import archives_dir as _archives_dir
        found = _archives_dir(start_path)
        return str(found) if found else None
    except Exception:
        pass

    # Fallback: the original search, unchanged.
    if start_path is None:
        start_path = os.getcwd()
    for _ in range(5):
        archives_path = os.path.join(start_path, 'archives')
        if os.path.isdir(archives_path):
            return archives_path
        start_path = os.path.dirname(start_path)
    return None

def main():
    archives_dir = find_archives_root()
    if archives_dir is None:
        # v2.15: see audit_archives_deep.py -- the hardcoded developer-drive
        # fallback is gone; the message now describes what was actually tried.
        print("ERROR: Could not find the archives directory. Looked relative "
              "to this file and in the working directory.")
        sys.exit(1)
    print(f"Archives directory: {archives_dir}")
    
    archive_files = [f for f in os.listdir(archives_dir) if f.endswith('.json')]
    print(f"Found {len(archive_files)} archive files")
    
    all_messages = []
    total_chars = 0
    for filename in archive_files:
        filepath = os.path.join(archives_dir, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                for turn in data:
                    if isinstance(turn, dict):
                        role = turn.get('role', '')
                        content = turn.get('content', '')
                        if content:
                            all_messages.append(content)
                            total_chars += len(content)
            else:
                print(f"Warning: {filename} does not contain a list")
        except Exception as e:
            print(f"Error reading {filename}: {e}")
    
    print(f"Total messages: {len(all_messages)}")
    print(f"Total characters: {total_chars}")
    
    words = []
    for msg in all_messages:
        tokens = re.findall(r'\b\w+\b', msg.lower())
        words.extend(tokens)
    
    counter = collections.Counter(words)
    top100 = counter.most_common(100)
    print("Top 100 words:")
    for word, count in top100:
        print(f"{word}: {count}")

if __name__ == '__main__':
    main()