import csv
from collections import Counter

path = r"E:\OracleAI_v2.3\backend\mlm_training_data\daemon_calls.csv"

commands = []
with open(path, 'r', encoding='utf-8') as f:
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