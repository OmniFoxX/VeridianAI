"""
pid_registry.py — v2.11.12 zombie-process fix (shared PID ledger).

THE PROBLEM: tier_launcher.py spawns the five tier processes (Ollama,
Llama-Sage, Llama-Daemon, Sage-Daemon, Overseer) and then EXITS. On
Windows, when the parent dies the children are re-parented, so Electron's
`taskkill /T` on the start.bat tree can never see them. Result: orphan
python.exe / llama-server.exe / ollama.exe processes that survive quit,
hold ports 11434-11436, and make the next launch fail 3-5 times until
the user gives up and reboots.

THE FIX: every process OracleAI spawns is recorded here, in a JSON ledger
at <project root>/.oracle_pids.json, together with its psutil create_time.
Shutdown (shutdown_cleanup.py) walks the ledger and kills each entry —
but ONLY after verifying the PID still belongs to the process we started
(create_time match), so a recycled PID belonging to something else is
never touched. Policy per Todd (2026-07-02): only kill what OracleAI
started — a user-launched Ollama must survive our shutdown.

Writers: tier_launcher.py, comfyui_launcher.py, and any future Popen site.
Readers: shutdown_cleanup.py (kill + clear), Electron via the cleanup
script, start.bat via the same script at boot (stale-zombie sweep).

Fully defensive: registry corruption or psutil absence never raises out
of these helpers — worst case we fall back to the name-based sweep in
shutdown_cleanup.py.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ledger lives at the project root, next to start.bat, so Electron and
# start.bat can both find it without knowing about sage_data's layout.
ROOT = Path(os.environ.get("OAI_ROOT") or Path(__file__).resolve().parent.parent)
REGISTRY_FILE = ROOT / ".oracle_pids.json"


def _load_raw() -> List[Dict[str, Any]]:
    try:
        if REGISTRY_FILE.exists():
            data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [e for e in data if isinstance(e, dict) and e.get("pid")]
    except Exception:
        pass
    return []


def _save_raw(entries: List[Dict[str, Any]]) -> None:
    try:
        tmp = REGISTRY_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        os.replace(tmp, REGISTRY_FILE)
    except Exception:
        pass


def _create_time(pid: int) -> Optional[float]:
    try:
        import psutil
        return psutil.Process(pid).create_time()
    except Exception:
        return None


def register(pid: int, label: str, argv0: str = "") -> None:
    """Record one spawned process. Called right after Popen. Best-effort."""
    if not pid or pid <= 0:
        return
    entries = _load_raw()
    # De-dup: same pid re-registered replaces the old entry.
    entries = [e for e in entries if e.get("pid") != pid]
    entries.append({
        "pid": int(pid),
        "label": str(label),
        "argv0": str(argv0),
        "create_time": _create_time(pid),   # None if psutil unavailable
        "registered_at": time.time(),
    })
    _save_raw(entries)


def load() -> List[Dict[str, Any]]:
    """All ledger entries (may include already-dead PIDs — callers verify)."""
    return _load_raw()


def clear() -> None:
    """Wipe the ledger (after a successful cleanup, or on fresh boot)."""
    _save_raw([])


def remove(pid: int) -> None:
    entries = [e for e in _load_raw() if e.get("pid") != pid]
    _save_raw(entries)
