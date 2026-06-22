"""Spawn the OracleAI inference tiers + Python daemons with console visibility
driven by Developer Mode.

  Dev Mode ON  -> each tier gets its own TITLED console (like start.bat always did).
  Dev Mode OFF -> tiers are spawned WINDOWLESS (CREATE_NO_WINDOW), so normal users
                  get a clean desktop. This works regardless of Windows Terminal,
                  because no console window is ever created to begin with.

Called by start.bat, which has already resolved the paths / ports / models into
the environment (LLAMA_SERVER, SAGE_MODEL, DAEMON_MODEL, *_PORT, *_CTX_SIZE,
DAEMON_MODEL_PRESENT, PYTHON_CMD, OAI_ROOT). Dev Mode is a RESTART-to-apply
setting. Fully defensive: each tier is best-effort so one failure never blocks
the others, and start.bat's readiness probes still report any tier that's down.
"""
import os
import sys
import subprocess
from pathlib import Path

ROOT = Path(os.environ.get("OAI_ROOT") or Path(__file__).resolve().parent.parent)
BACKEND = ROOT / "backend"

IS_WIN = (os.name == "nt")
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _dev_visible() -> bool:
    """Developer Mode flag (sage_data/ui_prefs.json). Default False = hidden."""
    try:
        if str(BACKEND) not in sys.path:
            sys.path.insert(0, str(BACKEND))
        import devmode
        return bool(devmode.is_enabled())
    except Exception:
        return False


VISIBLE = _dev_visible()


def _spawn(title: str, argv: list, extra_env: dict = None):
    """Start one tier. Visible -> titled console; hidden -> windowless."""
    env = {**os.environ, **(extra_env or {})}
    try:
        if IS_WIN and VISIBLE:
            # Titled visible console, mirroring start.bat's `start "Title" ...`.
            # shell=True runs via cmd, so `start` parses the title; list2cmdline
            # quotes any path that contains spaces.
            cmdline = 'start "' + title + '" ' + subprocess.list2cmdline(argv)
            return subprocess.Popen(cmdline, shell=True, cwd=str(ROOT), env=env)
        flags = NO_WINDOW if IS_WIN else 0
        return subprocess.Popen(argv, creationflags=flags, cwd=str(ROOT), env=env)
    except Exception as e:
        print(f"[tier_launcher] failed to start {title}: {e}")
        return None


def main():
    py = os.environ.get("PYTHON_CMD") or sys.executable
    llama = os.environ.get("LLAMA_SERVER", "")
    sage_model = os.environ.get("SAGE_MODEL", "")
    daemon_model = os.environ.get("DAEMON_MODEL", "")
    daemon_present = os.environ.get("DAEMON_MODEL_PRESENT", "0") == "1"
    p_oracle = os.environ.get("OLLAMA_ORACLE_PORT", "11434")
    p_sage = os.environ.get("LLAMA_SAGE_PORT", "11435")
    p_daemon = os.environ.get("LLAMA_DAEMON_PORT", "11436")
    sage_ctx = os.environ.get("SAGE_CTX_SIZE", "16384")
    daemon_ctx = os.environ.get("DAEMON_CTX_SIZE", "4096")

    print(f"[tier_launcher] Developer Mode {'ON (consoles visible)' if VISIBLE else 'OFF (consoles hidden)'}")

    # Tier 1 - Oracle (Ollama). Env mirrors start.bat's inline `set`s.
    _spawn("Ollama-Oracle", ["ollama", "serve"], extra_env={
        "OLLAMA_HOST": f"127.0.0.1:{p_oracle}",
        "OLLAMA_MAX_LOADED_MODELS": "1",
        "OLLAMA_NUM_GPU": "1",
        "OLLAMA_GPU_OVERHEAD": "536870912",
    })

    # Tier 2 - Sage (llama-server, agentic engine).
    if llama and sage_model:
        _spawn("Llama-Sage", [llama, "-m", sage_model, "--host", "127.0.0.1",
                              "--port", p_sage, "--ctx-size", sage_ctx, "-ngl", "0", "--metrics"])
    else:
        print("[tier_launcher] Sage tier skipped (LLAMA_SERVER/SAGE_MODEL not set)")

    # Tier 3 - Daemon (llama-server, tiny) - only if its model is present.
    if daemon_present and llama and daemon_model:
        _spawn("Llama-Daemon", [llama, "-m", daemon_model, "--host", "127.0.0.1",
                                "--port", p_daemon, "--ctx-size", daemon_ctx, "-ngl", "0"])
    else:
        print("[tier_launcher] Daemon tier skipped (no model)")

    # Sage Daemon (Python mechanics service).
    _spawn("Sage-Daemon", [py, str(BACKEND / "sage_daemon.py")])

    # Overseer Daemon (Python supervisor).
    _spawn("Overseer", [py, str(BACKEND / "overseer_daemon.py")])


if __name__ == "__main__":
    main()
