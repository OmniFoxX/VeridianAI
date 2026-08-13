## audit_archives_lite.py

import os
import json
import collections
import re
import sys

def find_archives_root(start_path=None):
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
        archives_dir = r"E:\OracleAI_v2.2\archives"
        if not os.path.isdir(archives_dir):
            print(f"ERROR: Could not find archives directory. Tried default: {archives_dir}")
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