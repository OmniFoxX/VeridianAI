"""check_model.py -- dev utility: sanity-check the sage_plm.pt checkpoint.

v2.12.2: was hardcoded to E:\\OracleAI_v2.3 paths (Todd-specific leftovers).
Self-locates now, same pattern as dependency_checker.py: this file lives in
backend/, so candidates are derived from __file__ + config's DATA_DIR.
Optionally pass an explicit path:  python check_model.py <path-to.pt>
"""
import os
import sys
from pathlib import Path

import torch

BACKEND = Path(__file__).resolve().parent
PROJECT = BACKEND.parent

def _candidates():
    if len(sys.argv) > 1:
        return [Path(sys.argv[1])]
    cands = [
        BACKEND / "mlm_training_data" / "sage_plm.pt",
        PROJECT / "bundled_models" / "sage_plm.pt",
    ]
    try:
        from config import DATA_DIR                    # single source of truth
        cands.append(Path(str(DATA_DIR)) / "models" / "sage_plm.pt")
    except Exception:
        cands.append(PROJECT.parent / "sage_data" / "models" / "sage_plm.pt")
    return cands

for p in _candidates():
    print(f"Checking: {p}")
    if os.path.exists(p):
        print("  File exists")
        try:
            data = torch.load(p, map_location="cpu")
            print(f"  Loaded successfully. Keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
        except Exception as e:
            print(f"  Failed to load: {e}")
    else:
        print("  File does not exist")
    print()
