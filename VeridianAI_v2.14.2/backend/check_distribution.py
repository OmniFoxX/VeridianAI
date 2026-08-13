"""check_distribution.py -- dev utility: command distribution in daemon_calls.csv.

v2.12.2: was hardcoded to E:\\OracleAI_v2.3 (Todd-specific leftover).
Self-locates via __file__ like dependency_checker.py.
Optionally pass an explicit path:  python check_distribution.py <path-to.csv>
"""
import csv
import sys
from collections import Counter
from pathlib import Path

path = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    Path(__file__).resolve().parent / "mlm_training_data" / "daemon_calls.csv")

if not path.exists():
    sys.exit(f"Not found: {path}")

commands = []
with open(path, "r", encoding="utf-8") as f:
    reader = csv.reader(f)
    for row in reader:
        if len(row) >= 6:
            commands.append(row[5].strip())

counts = Counter(commands)
total = len(commands)
print(f"Total commands: {total}")
print()
for cmd, count in counts.most_common():
    pct = 100 * count / total
    print(f"  {cmd}: {count} ({pct:.9f}%)")
