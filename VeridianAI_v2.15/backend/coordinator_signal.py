# coordinator_signal.py
import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime

# ----------------------------------------------------------------------
# 1️⃣  Run the fatigue detector as a subprocess and capture its JSON output
# ----------------------------------------------------------------------
def run_detector(archives_dir: str, window_turns: int = 5) -> dict:
    cmd = [
        sys.executable,
        str(Path(__file__).with_name("context_fatigue_detector.py")),
        "--archives-dir", archives_dir,
        "--window-turns", str(window_turns),
        "--output", "-"   # make the detector print JSON to stdout
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    # The detector prints a JSON object followed by a human‑readable line.
    # We’ll extract the first line that looks like JSON.
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            return json.loads(line)
    raise RuntimeError("Could not parse JSON from detector output")

# ----------------------------------------------------------------------
# 2️⃣  Build the prioritiser task if fatigue is detected
# ----------------------------------------------------------------------
def build_task(fatigue_info: dict) -> dict:
    # FIX (#69, 2026-06-08): the overseer's _poll_craiid_task() validates
    # `schema == "craiid_warm_instance"` and reads task/summary/sources
    # sub-objects. The old flat {fn,payload,type,urgency} shape carried no
    # `schema` key, so every overseer poll logged
    #   "CRAIID task file has unexpected schema 'None' — skipping"
    # and the task was never consumed (it just sat on disk being re-warned).
    # We now emit the schema the consumer expects while RETAINING the legacy
    # top-level fields (fn/payload/type/urgency) for backward compatibility
    # with any other reader. This makes the task actually get consumed and
    # silences the perpetual warning.
    metrics = fatigue_info.get("metrics", {})
    timestamp = fatigue_info.get("timestamp", "")
    reasons = fatigue_info.get("reasons", [])
    fatigue_score = metrics.get("repetition_ratio", "n/a")
    return {
        # --- schema the overseer validates + consumes ---
        "schema": "craiid_warm_instance",
        "version": "2.5.2",
        "task": {
            "task_id": f"fatigue_{timestamp or 'unknown'}",
            "trigger": "context_fatigue",
            "requested_by": "coordinator_signal",
            "fatigue_score": fatigue_score,
            "reasons": reasons,
        },
        "summary": {
            "total_entries": 0,
            "sources_ok": 0,
        },
        "sources": {},
        # --- legacy fields retained for backward compatibility ---
        "fn": "prepare_warm_instance",
        "payload": {
            "checkpoint_trigger": True,
            "reason": "context fatigue detected",
            "metrics": metrics,
            "timestamp": timestamp,
        },
        "type": "context_restore",
        "urgency": "high",
    }

# ----------------------------------------------------------------------
# 3️⃣  Main entry point
# ----------------------------------------------------------------------
def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Run fatigue detector and emit a coordinator task if needed."
    )
    parser.add_argument(
        "--archives-dir",
        required=True,
        help="Path to the archive directory (e.g. E:\\OracleAI_v2.2\\archives)"
    )
    parser.add_argument(
        "--window-turns",
        type=int,
        default=5,
        help="Number of recent user turns to analyse"
    )
    parser.add_argument(
        "--out",
        type=str,
        default="Downloads\\coordinator_task.json",
        help="Where to write the task JSON (if fatigue detected)"
    )
    args = parser.parse_args()

    try:
        info = run_detector(args.archives_dir, args.window_turns)
    except Exception as e:
        print(f"[ERROR] Fatigue detector failed: {e}")
        sys.exit(1)

    if info.get("fatigue_detected"):
        task = build_task(info)
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(task, indent=2))
        print(f"[INFO] Fatigue detected → task written to {out_path}")
        print(json.dumps(task, indent=2))
    else:
        print("[INFO] No fatigue detected – no task emitted.")
        # Optionally still output the metrics for logging
        print(json.dumps(info["metrics"], indent=2))

if __name__ == "__main__":
    main()
