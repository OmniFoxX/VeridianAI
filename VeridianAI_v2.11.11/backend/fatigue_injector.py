# fatigue_injector.py | CRAIID validation test utility
import time, pathlib, random, json
LOG_DIR = pathlib.Path("logs")
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "craidd_latest.log"

def append_fatigue(duration_sec=30, interval_sec=2):
    """Append synthetic fatigue signals to the log for a deterministic test."""
    end = time.time() + duration_sec
    while time.time() < end:
        # Example fatigue marker: high latency + error spike
        entry = {
            "ts": time.time(),
            "latency_ms": random.randint(120, 200),   # > baseline ~80 ms → fatigue cue
            "error_rate": random.uniform(0.15, 0.30), # elevated errors
            "msg": "synthetic fatigue spike"
        }
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        time.sleep(interval_sec)

if __name__ == "__main__":
    append_fatigue()
|