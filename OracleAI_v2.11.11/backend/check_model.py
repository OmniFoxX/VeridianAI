import os
import torch
import sys

paths = [
    r"E:\OracleAI_V2.3\backend\mlm_training_data\sage_plm.pt",
    r"E:\OracleAI_v2.3\bundled_models\sage_plm.pt",
    r"E:\sage_data\models\sage_plm.pt"
]

for p in paths:
    print(f"Checking: {p}")
    if os.path.exists(p):
        print("  File exists")
        try:
            # Try to load
            data = torch.load(p, map_location='cpu')
            print(f"  Loaded successfully. Keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
        except Exception as e:
            print(f"  Failed to load: {e}")
    else:
        print("  File does not exist")
    print()