#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
craiid_author_v2.3.1.py — CRAIID Warm-Instance Reconstruction Author
==============================================================
Receives a warm-instance preparation task (payload dict) from
overseer_daemon.py and reconstructs a context snapshot from:

  1. chat_memory.json        — current thread context (hot)
       split into:
         chat_tail    — last _CHAT_TAIL_ENTRIES verbatim (Author owned)
         chat_history — older entries for Journalist scoring (warm)
  2. archives/archives_NNNN.json — recent archive entries (warm)
  3. vlts_archives/          — [STUB] very long-term storage (Phase 2)

Writes output to:
  <OracleAI_root>/backend/craiid/reconstructs/
    warm_instance_YYYYMMDD_HHMMSS.json

Returns a status dict to overseer. Never touches the task file —
overseer consumed that before calling us.

INVOCATION:
  Called by overseer_daemon.py:
    from craiid.Author.craiid_author import prepare_warm_instance
    result = prepare_warm_instance(task_payload)

  Or directly for testing:
    python craiid_author.py --test

VERSION: 2.3.1 (2026-06-06)
PHASE:   1 of 2 (VLTS stub present, compression import ready, not wired)

CHANGES FROM 2.3.0:
  - chat_memory split into tail (verbatim) + history (Journalist-scored)
  - _MAX_CHAT_ENTRIES replaced with _CHAT_TAIL_ENTRIES + _CHAT_HISTORY_ENTRIES
  - _build_reconstruction updated to reflect tail/history split in output doc
  - Conditional VLTSCompressor import added (Phase 2 VLTS wiring ready)
  - _load_vlts_stub promoted to _load_vlts with compression key detection
  - Summary counts extended with chat_tail_entries + chat_history_entries
"""


from __future__ import annotations


import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Self-locating paths — zero hardcoded drive letters or version strings
# ---------------------------------------------------------------------------
# craiid_author.py lives at:
#   <root>/backend/craiid/Author/craiid_author.py
# So:
#   _AUTHOR_DIR   = .../craiid/Author/
#   _CRAIID_DIR   = .../craiid/
#   _BACKEND_DIR  = .../backend/
#   _ROOT_DIR     = .../OracleAI_vXX/   (version-agnostic)

# Robust self-location (#69 fix): anchor on the 'backend' ancestor rather than a
# fixed parent-hop count, so paths resolve correctly whether this file lives at
# craiid/craiid_author.py or craiid/Author/craiid_author.py. The previous fixed
# hops assumed the Author/ layout and resolved _ROOT_DIR one level ABOVE the
# project, so chat_memory.json + archives were never found (always-empty
# reconstructions).
_AUTHOR_DIR:  Path = Path(__file__).resolve().parent
_BACKEND_DIR: Path = next(
    (p for p in Path(__file__).resolve().parents if p.name == "backend"),
    _AUTHOR_DIR.parent,            # fallback: .../craiid/ -> .../backend/
)
_ROOT_DIR:    Path = _BACKEND_DIR.parent
_CRAIID_DIR:  Path = _BACKEND_DIR / "craiid"

# Input sources
_CHAT_MEMORY_FILE: Path = _ROOT_DIR / "chat_memory.json"
_ARCHIVES_DIR:     Path = _ROOT_DIR / "archives"
_VLTS_DIR:         Path = _CRAIID_DIR / "vlts_archives"   # Phase 2

# Output
_RECONSTRUCTS_DIR: Path = _CRAIID_DIR / "reconstructs"

# Logging
_LOG_DIR:  Path = _CRAIID_DIR / "logs"
_LOG_FILE: Path = _LOG_DIR / "craiid_author.log"

# --- Hardening: keep memory-derived artifacts OUT of the project tree and
# encrypted at rest. Reconstructs -> sage_data/craiid/reconstructs (encrypted via
# atrest); the author log -> sage_data/logs. Falls back to the in-project paths if
# config/atrest are unavailable, so a standalone run never crashes. Existing
# plaintext under craiid/{reconstructs,logs} can be deleted once confirmed.
_atrest = None
try:
    if str(_BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(_BACKEND_DIR))
    from config import DATA_DIR as _DATA_DIR, LOG_DIR as _CFG_LOG_DIR
    _RECONSTRUCTS_DIR = Path(_DATA_DIR) / "craiid" / "reconstructs"
    _VLTS_DIR = Path(_DATA_DIR) / "vlts_archives"   # at-rest: out of project tree
    _LOG_DIR = Path(_CFG_LOG_DIR)
    _LOG_FILE = _LOG_DIR / "craiid_author.log"
    import atrest as _atrest
except Exception:
    _atrest = None  # keep in-project defaults; write plaintext


# ---------------------------------------------------------------------------
# Compression support — Phase 2 VLTS wiring (conditional import)
# ---------------------------------------------------------------------------
# VLTSCompressor is imported if available so the Author can decompress
# warm-instance chunks from vlts_archives when Phase 2 is wired.
# Absence of the module is non-fatal — VLTS simply stays stubbed.

try:
    from craiid_compression_core_v3 import VLTSCompressor
    _COMPRESSION_AVAILABLE = True
except ImportError:
    _COMPRESSION_AVAILABLE = False


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
_CHAT_TAIL_ENTRIES:    int = 10   # most recent N entries — verbatim, Author owned
_CHAT_HISTORY_ENTRIES: int = 30   # older entries — passed to Journalist for scoring
_MAX_ARCHIVE_FILES:    int = 5    # most recent N archive files to scan
_MAX_ARCHIVE_ENTRIES:  int = 60   # total archive entries across files
_AUTHOR_VERSION:       str = "2.3.1"


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging() -> logging.Logger:
    """Configure rotating-safe file + stderr logger for the author."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("craiid_author")
    if logger.handlers:
        return logger  # already configured (e.g. called twice in tests)
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] craiid_author: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S"
    )

    fh = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


log = _setup_logging()


# ---------------------------------------------------------------------------
# Source loaders
# ---------------------------------------------------------------------------

def _load_chat_memory(
    tail_entries: int = _CHAT_TAIL_ENTRIES,
    history_entries: int = _CHAT_HISTORY_ENTRIES,
) -> Tuple[List[Dict], List[Dict], str]:
    """
    Load chat_memory.json and split into tail and history.

    tail    = last `tail_entries` messages verbatim (hot context,
              Author owned — always included in warm instance).
    history = the `history_entries` messages before the tail
              (warm context — available for Journalist scoring,
              Phase 2 will pass these through ops detector before
              including in reconstruction).

    Returns:
        (tail, history, status)
        Both lists are empty on any failure — never raises.
    """
    if not _CHAT_MEMORY_FILE.exists():
        log.warning("chat_memory.json not found at %s", _CHAT_MEMORY_FILE)
        return [], [], "missing"

    try:
        raw = _CHAT_MEMORY_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to read chat_memory.json: %s", exc)
        return [], [], "error"

    # chat_memory.json may be a list directly or {"messages": [...]}
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        for key in ("messages", "history", "entries", "chat"):
            if key in data and isinstance(data[key], list):
                entries = data[key]
                break
        else:
            entries = [data]
    else:
        log.warning("chat_memory.json has unexpected structure: %s", type(data))
        return [], [], "unexpected_structure"

    total = len(entries)

    # Tail: last N entries verbatim
    tail = entries[-tail_entries:] if total > tail_entries else entries[:]

    # History: the window immediately before the tail
    history_end   = max(0, total - tail_entries)
    history_start = max(0, history_end - history_entries)
    history = entries[history_start:history_end]

    log.debug(
        "chat_memory: %d total → tail=%d history=%d (indices [%d:%d] / [%d:])",
        total, len(tail), len(history),
        history_start, history_end, max(0, total - tail_entries),
    )
    return tail, history, "ok"


def _find_archive_files(max_files: int = _MAX_ARCHIVE_FILES) -> List[Path]:
    """
    Return the most recent `max_files` archives_NNNN.json files,
    sorted newest-first by filename sequence number.
    Never raises.
    """
    if not _ARCHIVES_DIR.exists():
        log.warning("Archives directory not found at %s", _ARCHIVES_DIR)
        return []

    candidates = sorted(
        _ARCHIVES_DIR.glob("archive*.json"),   # FIX #69: real files are archive_<ts>.json
        key=lambda p: p.name,                  # chronological for timestamped names
        reverse=True
    )
    selected = candidates[:max_files]
    log.debug("archive files found: %d, using: %d", len(candidates), len(selected))
    return selected


def _archive_seq(path: Path) -> int:
    """Extract sequence number from archives_NNNN.json for sorting."""
    parts = path.stem.split("_")
    try:
        return int(parts[-1])
    except (ValueError, IndexError):
        return -1


def _load_archives(
    max_files: int = _MAX_ARCHIVE_FILES,
    max_entries: int = _MAX_ARCHIVE_ENTRIES,
) -> Tuple[List[Dict], List[str], str]:
    """
    Load archive entries from the most recent archive files.

    Returns:
        (entries_list, files_used_list, status_string)
        entries_list is empty on total failure — never raises.
    """
    archive_files = _find_archive_files(max_files)
    if not archive_files:
        return [], [], "no_files"

    all_entries: List[Dict] = []
    files_used: List[str]   = []

    for af in archive_files:
        try:
            raw  = af.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Skipping archive file %s: %s", af.name, exc)
            continue

        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            for key in ("entries", "records", "archive", "data"):
                if key in data and isinstance(data[key], list):
                    entries = data[key]
                    break
            else:
                entries = [data]
        else:
            log.warning("Unexpected structure in %s", af.name)
            continue

        all_entries.extend(entries)
        files_used.append(af.name)

        if len(all_entries) >= max_entries:
            break

    trimmed = all_entries[:max_entries]
    log.debug(
        "archives: loaded %d entries from %d files (trimmed to %d)",
        len(all_entries), len(files_used), len(trimmed),
    )
    return trimmed, files_used, "ok" if trimmed else "empty"


def _load_vlts() -> Tuple[Optional[List], str]:
    """
    VLTS loader — Phase 1 stub, Phase 2 ready.

    Phase 1: detects directory and compression key presence,
             logs status, returns None with descriptive status string.

    Phase 2 wiring (when vlts_archives contain real data):
      - Uncomment the decompression block below
      - Remove the early return
      - Ensure craiid_compression_core_v3.py is in the Python path

    Never raises.
    """
    if not _VLTS_DIR.exists():
        return None, "stub_not_present"

    # Directory exists — check for compression key
    key_path = _VLTS_DIR / "compression_key.json"
    if not key_path.exists():
        log.debug("VLTS directory present but no compression key found.")
        return None, "stub_directory_present"

    if not _COMPRESSION_AVAILABLE:
        log.warning(
            "VLTS compression key found at %s but "
            "craiid_compression_core_v3 is not importable. "
            "Skipping VLTS decompression.",
            key_path,
        )
        return None, "compression_module_missing"

    # --- Phase 2 (#69): decompress the VLTS chunks the Archivist worker wrote.
    # Robust: a missing key/chunk, a bad chunk, or a failed decompress is
    # skipped, never raised; empty store -> (None, "empty").
    try:
        max_vlts = 40
        compressor = VLTSCompressor(vlts_dir=str(_VLTS_DIR))
        if not compressor.load_key(str(key_path)):
            return None, "key_load_failed"
        entries: List[Dict] = []
        for chunk_file in sorted(_VLTS_DIR.glob("chunk_*.json")):
            try:
                if _atrest is not None:
                    # transparently decrypts; legacy plaintext still reads
                    doc = json.loads(_atrest.read_file_auto(str(chunk_file)))
                else:
                    doc = json.loads(chunk_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(doc, dict):
                rows = doc.get("entries", [])
            elif isinstance(doc, list):
                rows = doc
            else:
                rows = []
            for row in rows:
                comp = row.get("compressed") if isinstance(row, dict) else None
                if not comp:
                    continue
                try:
                    text = compressor.decompress_text(comp)
                except Exception:
                    continue
                if text and text.strip():
                    entries.append({"text": text, "source": "vlts"})
                if len(entries) >= max_vlts:
                    break
            if len(entries) >= max_vlts:
                break
        log.info("VLTS: decompressed %d entries from %s", len(entries), _VLTS_DIR.name)
        return (entries if entries else None), ("ok" if entries else "empty")
    except Exception as exc:
        log.error("VLTS decompression failed: %s", exc)
        return None, "decompression_error"


# ---------------------------------------------------------------------------
# Reconstruction builder
# ---------------------------------------------------------------------------

def _build_reconstruction(
    task_payload:       Dict[str, Any],
    chat_tail:          List[Dict],
    chat_history:       List[Dict],
    chat_status:        str,
    archive_entries:    List[Dict],
    archive_files_used: List[str],
    archive_status:     str,
    vlts_entries:       Optional[List],
    vlts_status:        str,
) -> Dict[str, Any]:
    """
    Assemble the warm-instance reconstruction document.
    This is the artifact Sage consumes on warm boot.

    Context layout:
      chat_tail    — verbatim recent messages (hot, always included)
      chat_history — older messages pre-scored by Journalist (warm)
      archives     — recent archive entries (warm)
      vlts         — decompressed long-term chunks (Phase 2)
    """
    now_utc = datetime.now(timezone.utc)

    return {
        "schema":        "craiid_warm_instance",
        "version":       _AUTHOR_VERSION,
        "generated_utc": now_utc.isoformat(),
        "generated_ts":  now_utc.timestamp(),

        # Provenance
        "task": {
            "trigger":      task_payload.get("trigger",      "unknown"),
            "fatigue_score": task_payload.get("fatigue_score", None),
            "requested_by": task_payload.get("requested_by", "overseer_daemon"),
            "task_id":      task_payload.get("task_id", None),
        },

        # Source metadata
        "sources": {
            "chat_memory": {
                "status":           chat_status,
                "file":             str(_CHAT_MEMORY_FILE),
                "tail_included":    len(chat_tail),
                "history_included": len(chat_history),
            },
            "archives": {
                "status":           archive_status,
                "files_used":       archive_files_used,
                "entries_included": len(archive_entries),
            },
            "vlts": {
                "status":           vlts_status,
                "entries_included": len(vlts_entries) if vlts_entries else 0,
            },
        },

        # Reconstructed context — ordered hot → warm → cold
        "context": {
            "chat_tail":    chat_tail,      # verbatim recent — Author owned
            "chat_history": chat_history,   # older — Journalist scored (Phase 2)
            "archives":     archive_entries,
            "vlts":         vlts_entries,   # Phase 2: decompressed VLTS chunks
        },

        # Summary counts for overseer to log without parsing context
        "summary": {
            "total_entries":         len(chat_tail) + len(chat_history) + len(archive_entries) + (len(vlts_entries) if vlts_entries else 0),
            "chat_tail_entries":     len(chat_tail),
            "chat_history_entries":  len(chat_history),
            "archive_entries":       len(archive_entries),
            "vlts_entries":          len(vlts_entries) if vlts_entries else 0,
            "sources_ok": sum([
                chat_status == "ok",
                archive_status == "ok",
                vlts_status not in ("stub_not_present", "stub_directory_present",
                                    "stub_key_present_awaiting_phase2",
                                    "compression_module_missing"),
            ]),
        },
    }


# ---------------------------------------------------------------------------
# Atomic writer
# ---------------------------------------------------------------------------

def _write_reconstruction(doc: Dict[str, Any]) -> Path:
    """
    Write the reconstruction document to the reconstructs directory.
    Uses temp-file + rename for crash safety.

    Returns the final output Path.
    Raises OSError on failure (caller handles).
    """
    _RECONSTRUCTS_DIR.mkdir(parents=True, exist_ok=True)

    ts       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"warm_instance_{ts}.json"
    final_path = _RECONSTRUCTS_DIR / filename

    tmp_fd, tmp_path_str = tempfile.mkstemp(
        dir=_RECONSTRUCTS_DIR,
        prefix=".tmp_warm_",
        suffix=".json",
    )
    tmp_path = Path(tmp_path_str)

    try:
        if _atrest is not None:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(_atrest.dump_json_encrypted(doc))
        else:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=2, ensure_ascii=False)
        shutil.move(str(tmp_path), str(final_path))
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    log.info("Reconstruction written: %s", final_path)
    return final_path


# ---------------------------------------------------------------------------
# Public entry point — called by overseer_daemon
# ---------------------------------------------------------------------------

def prepare_warm_instance(task_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Main entry point. Called by overseer_daemon after it has consumed
    the craiid task file.

    Args:
        task_payload: dict from overseer containing at minimum:
            {
                "trigger":       "fatigue_detected" | "manual" | ...,
                "fatigue_score": float,
                "requested_by":  "overseer_daemon",
                "task_id":       str (optional)
            }

    Returns:
        {
            "status":      "ok" | "partial" | "error",
            "output_path": str | None,
            "summary":     {...},
            "elapsed_s":   float,
            "error":       str | None
        }
    """
    start_ts = time.monotonic()
    log.info(
        "prepare_warm_instance called — trigger=%s fatigue_score=%s",
        task_payload.get("trigger",       "unknown"),
        task_payload.get("fatigue_score", "N/A"),
    )

    try:
        # --- Load sources ---
        chat_tail, chat_history, chat_status = _load_chat_memory()
        archive_entries, archive_files_used, archive_status = _load_archives()
        vlts_entries, vlts_status = _load_vlts()

        # --- Build document ---
        doc = _build_reconstruction(
            task_payload       = task_payload,
            chat_tail          = chat_tail,
            chat_history       = chat_history,
            chat_status        = chat_status,
            archive_entries    = archive_entries,
            archive_files_used = archive_files_used,
            archive_status     = archive_status,
            vlts_entries       = vlts_entries,
            vlts_status        = vlts_status,
        )

        # --- Write to disk ---
        output_path = _write_reconstruction(doc)
        elapsed     = time.monotonic() - start_ts

        # Determine overall status
        if chat_status == "ok" and archive_status == "ok":
            overall = "ok"
        elif chat_status == "error" and archive_status in ("no_files", "error"):
            overall = "error"
        else:
            overall = "partial"

        log.info(
            "prepare_warm_instance complete — status=%s elapsed=%.2fs entries=%d",
            overall,
            elapsed,
            doc["summary"] ["total_entries"],
        )

        return {
            "status":      overall,
            "output_path": str(output_path),
            "summary":     doc["summary"],
            "elapsed_s":   round(elapsed, 3),
            "error":       None,
        }

    except OSError as exc:
        elapsed = time.monotonic() - start_ts
        log.error("prepare_warm_instance OSError: %s", exc)
        return {
            "status":      "error",
            "output_path": None,
            "summary":     {},
            "elapsed_s":   round(elapsed, 3),
            "error":       f"OSError: {exc}",
        }
    except Exception as exc:
        elapsed = time.monotonic() - start_ts
        log.error("prepare_warm_instance unexpected error: %s", exc, exc_info=True)
        return {
            "status":      "error",
            "output_path": None,
            "summary":     {},
            "elapsed_s":   round(elapsed, 3),
            "error":       f"Unexpected: {exc}",
        }


# ---------------------------------------------------------------------------
# CLI test harness — python craiid_author.py --test
# ---------------------------------------------------------------------------

def _run_test() -> None:
    """
    Smoke-test: fires a synthetic prepare_warm_instance task and
    prints the result. Does NOT require overseer to be running.
    """
    print("\n=== craiid_author.py — self-test ===")
    print(f"  Author dir:        {_AUTHOR_DIR}")
    print(f"  CRAIID dir:        {_CRAIID_DIR}")
    print(f"  Backend dir:       {_BACKEND_DIR}")
    print(f"  Root dir:          {_ROOT_DIR}")
    print(f"  chat_memory:       {_CHAT_MEMORY_FILE}")
    print(f"  archives dir:      {_ARCHIVES_DIR}")
    print(f"  vlts dir:          {_VLTS_DIR}")
    print(f"  reconstructs:      {_RECONSTRUCTS_DIR}")
    print(f"  compression ready: {_COMPRESSION_AVAILABLE}")
    print()

    test_payload = {
        "trigger":       "test_harness",
        "fatigue_score": 0.0,
        "requested_by":  "cli_test",
        "task_id":       "test-0000",
    }

    result = prepare_warm_instance(test_payload)

    print("\n=== Result ===")
    print(json.dumps(result, indent=2))

    if result["status"] in ("ok", "partial"):
        print(f"\n✓ Reconstruction written to:\n  {result['output_path']}")
        print(f"\n  chat_tail_entries:    {result['summary'].get('chat_tail_entries', 0)}")
        print(f"  chat_history_entries: {result['summary'].get('chat_history_entries', 0)}")
        print(f"  archive_entries:      {result['summary'].get('archive_entries', 0)}")
        print(f"  vlts_entries:         {result['summary'].get('vlts_entries', 0)}")
        print(f"  compression_ready:    {_COMPRESSION_AVAILABLE}")
    else:
        print(f"\n✗ Reconstruction failed: {result['error']}")

    print("=== End self-test ===\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="CRAIID Warm-Instance Author — standalone test mode"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run smoke test without overseer",
    )
    args = parser.parse_args()

    if args.test:
        _run_test()
    else:
        print(
            "craiid_author.py is a library module.\n"
            "Run with --test for standalone smoke test,\n"
            "or import prepare_warm_instance from overseer_daemon.py."
        )
        sys.exit(0)