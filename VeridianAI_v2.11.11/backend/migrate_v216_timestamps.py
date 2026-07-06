"""
migrate_v216_timestamps.py — one-shot timestamp normalisation for v2.1.6.

Walks the non-chain data files (procedural.json, chat_memory.json, archives,
chain_digest.json) and re-stamps every timestamp it finds into the
canonical iso_z form (TimeManager.normalise_iso). Idempotent — running it
twice is harmless; entries already in canonical form are left alone.

DOES NOT TOUCH:
  * memory_chain.log — the SHA3-chained log. Re-stamping any entry would
    change its hash and break verify_chain(). Old entries stay as-is;
    new entries written via TimeManager are already canonical.

Run from anywhere:
    cd E:\\OracleAI_v2.1.5\\backend
    py migrate_v216_timestamps.py            # dry-run report
    py migrate_v216_timestamps.py --apply    # write changes

The dry-run mode shows what WOULD change without touching disk. Use it
first; review the report; then re-run with --apply.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))
from time_manager import TimeManager  # noqa: E402

# Resolve sage_data via config so the script works on any install.
import config  # noqa: E402
PROJECT_ROOT = BACKEND.parent
PROCEDURAL_FILE = config.PROCEDURAL_DIR / "procedural.json"
CHAT_MEMORY_FILE = PROJECT_ROOT / "chat_memory.json"
ARCHIVES_DIR = PROJECT_ROOT / "archives"
CHAIN_DIGEST_FILE = config.MEMORY_DIR / "chain_digest.json"


# ---- field names that store timestamps in our data files ---------- #
TS_FIELD_NAMES = {
    "timestamp", "ts", "generated_at", "anomaly_first_ts",
    "last_consolidate_ts", "last_digest_ts", "last_verify_ts",
    "added_at", "updated_at", "created_at",
}


def _normalise_value(v):
    """Return (new_value, changed) for a field value. Only re-stamps
    string values that parse as ISO; leaves other types untouched."""
    if not isinstance(v, str):
        return v, False
    norm = TimeManager.normalise_iso(v)
    if norm is None:
        return v, False
    if norm == v:
        return v, False
    return norm, True


def _walk(obj, path: str = "<root>"):
    """Recursively walk obj and re-stamp any TS_FIELD_NAMES values it
    encounters. Yields (path, old, new) tuples for changed entries.
    Mutates obj in place."""
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            sub_path = f"{path}.{k}"
            if k in TS_FIELD_NAMES:
                new_v, changed = _normalise_value(v)
                if changed:
                    obj[k] = new_v
                    yield sub_path, v, new_v
            elif isinstance(v, (dict, list)):
                yield from _walk(v, sub_path)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from _walk(item, f"{path}[{i}]")


def _migrate_file(path: Path, apply: bool):
    """Migrate a single JSON file. Returns (changed_count, error_msg)."""
    if not path.exists():
        return 0, f"(not present: {path})"
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except Exception as e:
        return 0, f"read/parse failed: {e}"

    changes = list(_walk(data))
    if not changes:
        return 0, None

    if apply:
        try:
            tmp = path.with_suffix(path.suffix + ".v216tmp")
            tmp.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as e:
            return 0, f"write failed: {e}"

    return len(changes), changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--apply", action="store_true",
        help="Write changes (default is dry-run report only)",
    )
    args = ap.parse_args()

    targets = [PROCEDURAL_FILE, CHAT_MEMORY_FILE, CHAIN_DIGEST_FILE]
    if ARCHIVES_DIR.exists():
        targets.extend(sorted(ARCHIVES_DIR.glob("*.json")))

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== v2.1.6 timestamp migration [{mode}] ===")
    total = 0
    for path in targets:
        count, detail = _migrate_file(path, args.apply)
        if isinstance(detail, str) and detail.startswith("("):
            print(f"  [skip] {path.name}: {detail}")
            continue
        if isinstance(detail, str):
            print(f"  [ERROR] {path.name}: {detail}")
            continue
        if count == 0:
            print(f"  [ok]   {path.name}: already canonical "
                  "(no changes needed)")
            continue
        total += count
        verb = "rewrote" if args.apply else "would rewrite"
        print(f"  [{'done' if args.apply else 'pre' :>4}] {path.name}: "
              f"{verb} {count} timestamp(s)")
        # Show first few examples
        for sub_path, old, new in detail[:3]:
            print(f"           {sub_path}: {old!r} -> {new!r}")
        if len(detail) > 3:
            print(f"           ... and {len(detail) - 3} more")
    print(f"=== total: {total} timestamp(s) "
          f"{'rewritten' if args.apply else 'would be rewritten'} ===")
    if not args.apply and total > 0:
        print("Re-run with --apply to write the changes.")


if __name__ == "__main__":
    main()
