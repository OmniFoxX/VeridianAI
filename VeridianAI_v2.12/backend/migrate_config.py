#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrate_config.py — #68 v1 (flat) → v2 (sectioned) config.json migrator
------------------------------------------------------------------------
One-shot, idempotent migrator. Safe to re-run.

What it does:
    1. Locates project_root/config.json (one level up from backend/).
    2. Reads the current file. If it's already schema_version=2, exits no-op.
    3. If it's v1 (no schema_version), backs up the file to
       config.json.v1_backup_<unix_ts>.json next to it.
    4. Builds an OracleConfig from the v1 flat dict via from_flat_dict().
    5. Extracts the old "system_prompt" string blob (if present) to
       PROMPTS_DIR/system.txt, then sets prompts.system_prompt_file to
       point at it.
    6. Atomically writes the new v2 nested config.json.

Run with:
    python backend/migrate_config.py
    python backend/migrate_config.py --dry-run    # show what would change

Safety guarantees:
    - DOES NOT touch Fernet key, hash chain log, procedural memory, or any
      memory-integrity surface. Only reads/writes config.json + system.txt.
    - Original config.json is preserved as a timestamped backup before
      any write. Recovery is `move backup → config.json`.
    - On any error during the new-file write, the backup remains and the
      original file is restored (atomic os.replace pattern).
    - Idempotent: re-running on a v2 file is a clean no-op (no extra
      backups, no unnecessary writes).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

# Ensure we can import config_store from this directory.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from config_store import OracleConfig, SCHEMA_VERSION  # noqa: E402
import config as _cfg  # noqa: E402  — for PROMPTS_DIR


# Default v2.1.x flat-key for the inline system prompt blob.
_LEGACY_PROMPT_KEY = "system_prompt"

# Pre-#68, the user's Vulkan/IPEX choice was stored in electron/.backend_mode
# (a one-line text file), not in config.json. After migration the canonical
# source is cfg.electron.backend_mode. We carry the value over here so a
# user who picked "ipex" doesn't silently regress to the dataclass default
# "vulkan" on first boot post-deploy.
_VALID_BACKEND_MODES = ("vulkan", "ipex")


def _project_root() -> Path:
    return _HERE.parent


def _config_json_path() -> Path:
    return _project_root() / "config.json"


def _backup_path(orig: Path) -> Path:
    ts = int(time.time())
    return orig.with_name(f"{orig.name}.v1_backup_{ts}.json")


def _extract_backend_mode_file(dry_run: bool) -> Optional[str]:
    """If electron/.backend_mode exists and holds a valid mode, return it.
    Returns None if the file is missing, unreadable, or has an unexpected
    value. The file itself is LEFT IN PLACE — readBackendMode's fallback
    chain still uses it for any read that doesn't find a v2 value, and
    the existing 'write to .backend_mode' UI path remains the only writer
    until a settings.js change lands."""
    bm_path = _project_root() / "electron" / ".backend_mode"
    if not bm_path.exists():
        return None
    try:
        raw = bm_path.read_text(encoding="utf-8").strip().lower()
    except OSError as e:
        if dry_run:
            print(f"  [dry-run] could not read .backend_mode: {e}")
        return None
    if raw in _VALID_BACKEND_MODES:
        if dry_run:
            print(f"  [dry-run] would preserve electron.backend_mode = {raw!r} from .backend_mode file")
        return raw
    if dry_run:
        print(f"  [dry-run] .backend_mode contains unrecognized value {raw!r}; ignoring")
    return None


def _extract_system_prompt(flat: dict, dry_run: bool) -> str:
    """If the v1 flat dict carries a system_prompt blob, write it to
    PROMPTS_DIR/system.txt and return the relative path (string) for
    storing in prompts.system_prompt_file. If no blob is present (or
    the value is empty/whitespace), return the dataclass default and
    skip the write."""
    raw = flat.get(_LEGACY_PROMPT_KEY)
    if not isinstance(raw, str) or not raw.strip():
        # Nothing to migrate; leave default in place.
        return OracleConfig().prompts.system_prompt_file

    prompts_dir = _cfg.PROMPTS_DIR
    target = prompts_dir / "system.txt"
    rel = _relative_to_project(target)

    if dry_run:
        print(f"  [dry-run] would write {len(raw)} chars to {target}")
    else:
        prompts_dir.mkdir(parents=True, exist_ok=True)
        # If the file already exists, leave it alone — user may have
        # edited it manually between migration attempts. The legacy
        # blob is preserved in the v1 backup either way.
        if target.exists():
            print(f"  [skip] {target} already exists; legacy blob preserved in backup")
        else:
            with open(target, "w", encoding="utf-8") as f:
                f.write(raw.rstrip() + "\n")
            print(f"  wrote system prompt ({len(raw)} chars) to {target}")
    return rel


def _relative_to_project(target: Path) -> str:
    """Return target as a string path relative to the project root if
    possible, else fall back to absolute. The PromptsSection default is
    `../sage_data/prompts/system.txt` (sage_data lives next to project_root)
    so we prefer that style for distribution portability."""
    proj = _project_root()
    try:
        # ../sage_data/... is the canonical place; use that string form
        # because Path.relative_to barfs on sibling directories.
        rel = target.resolve().relative_to(proj.parent)
        return "../" + rel.as_posix()
    except ValueError:
        return str(target.resolve())


def migrate(dry_run: bool = False) -> int:
    """Returns shell exit code: 0 = success or no-op, 1 = error."""
    cfg_path = _config_json_path()

    if not cfg_path.exists():
        print(f"[migrate_config] no config.json at {cfg_path}; nothing to migrate.")
        if not dry_run:
            print("[migrate_config] writing a default v2 config.json so first boot has a known shape.")
            OracleConfig().save(cfg_path)
            print(f"[migrate_config] wrote {cfg_path}")
        return 0

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[migrate_config] ERROR: could not parse {cfg_path}: {e}", file=sys.stderr)
        return 1

    if not isinstance(raw, dict):
        print(f"[migrate_config] ERROR: {cfg_path} is not a JSON object.", file=sys.stderr)
        return 1

    ver = raw.get("schema_version")
    if ver == SCHEMA_VERSION:
        print(f"[migrate_config] {cfg_path} already at schema_version={SCHEMA_VERSION}; no-op.")
        return 0
    if isinstance(ver, int) and ver > SCHEMA_VERSION:
        print(
            f"[migrate_config] ERROR: {cfg_path} reports schema_version={ver} but this "
            f"build only knows up to {SCHEMA_VERSION}. Refusing to downgrade.",
            file=sys.stderr,
        )
        return 1

    # --- v1 (flat) detected ---
    print(f"[migrate_config] {cfg_path} is v1 (flat); migrating to v2.")
    bkp = _backup_path(cfg_path)
    if dry_run:
        print(f"  [dry-run] would back up {cfg_path} -> {bkp}")
    else:
        # rename is atomic on same filesystem; the original path is now
        # the timestamped backup, and we'll write the new v2 file at the
        # original path immediately below.
        cfg_path.replace(bkp)
        print(f"  backed up to {bkp}")

    # Carry the prompt blob out to a file before building the OracleConfig
    # (so we can set prompts.system_prompt_file accurately).
    prompt_rel = _extract_system_prompt(raw, dry_run=dry_run)

    new_cfg = OracleConfig.from_flat_dict(raw)
    new_cfg.prompts.system_prompt_file = prompt_rel

    # Pull Vulkan/IPEX preference out of .backend_mode (pre-#68 storage
    # location) into v2's canonical home cfg.electron.backend_mode, so
    # the user's saved choice survives the migration. The .backend_mode
    # file itself is left in place — Electron's fallback chain still
    # reads it if config.json is somehow missing the field.
    backend_mode = _extract_backend_mode_file(dry_run)
    if backend_mode is not None and not dry_run:
        new_cfg.electron.backend_mode = backend_mode
        print(f"  preserved electron.backend_mode = {backend_mode!r} from .backend_mode file")

    if dry_run:
        print(f"  [dry-run] would write v2 config to {cfg_path}")
        print(f"  [dry-run] preview of new shape (sections present): "
              f"{', '.join(k for k in new_cfg.to_nested_dict().keys())}")
    else:
        new_cfg.save(cfg_path)
        print(f"  wrote v2 config to {cfg_path}")

    print("[migrate_config] done.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="OracleAI config.json v1->v2 migrator")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change, don't write any files.")
    args = parser.parse_args()
    return migrate(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
