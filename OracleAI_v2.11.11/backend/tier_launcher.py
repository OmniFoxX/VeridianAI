"""Spawn the OracleAI inference tiers + Python daemons with console visibility
driven by Developer Mode.

  Dev Mode ON  -> each tier gets its own console window.
  Dev Mode OFF -> tiers are spawned WINDOWLESS (CREATE_NO_WINDOW), so normal users
                  get a clean desktop. This works regardless of Windows Terminal,
                  because no console window is ever created to begin with.

Called by start.bat, which has already resolved the paths / ports / models into
the environment (LLAMA_SERVER, SAGE_MODEL, DAEMON_MODEL, *_PORT, *_CTX_SIZE,
DAEMON_MODEL_PRESENT, PYTHON_CMD, OAI_ROOT). Dev Mode is a RESTART-to-apply
setting. Fully defensive: each tier is best-effort so one failure never blocks
the others, and start.bat's readiness probes still report any tier that's down.

v2.11.12 zombie-process fix:
  1. Every spawned PID is registered in the .oracle_pids.json ledger
     (pid_registry.py) so shutdown_cleanup.py can reap it on quit or on
     the next boot. This launcher exits right after spawning, orphaning
     its children — the ledger is the ONLY reliable way to find them
     again. (Root cause of the zombie python/llama-server/window mess.)
  2. Dev-visible spawns no longer go through `start "Title" ...` +
     shell=True. That made the Popen handle point at a transient cmd.exe
     whose PID was useless — the real tier process was unrecorded and
     thus unkillable. Now we use CREATE_NEW_CONSOLE on the real argv:
     same visible console, but the PID we get is the tier itself.
     (Cosmetic tradeoff: the console title is the exe name, not our
     custom label. devmode's hide/show works on PIDs, unaffected.)

v2.11.12 NPU tier (Ryzen AI):
  If inference.npu_enabled is on AND an NPU LLM runtime is installed
  (AMD Lemonade Server — the official Ryzen AI OpenAI-compatible server),
  spawn it on network.ports.npu_llm (default 11438). model_manager picks
  it up as a fourth tier; the Hardware panel toggle turns routing on/off
  live, and this launcher decides at boot whether the server itself runs.
"""
import os
import shutil
import sys
import subprocess
from pathlib import Path

ROOT = Path(os.environ.get("OAI_ROOT") or Path(__file__).resolve().parent.parent)
BACKEND = ROOT / "backend"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

IS_WIN = (os.name == "nt")
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _dev_visible() -> bool:
    """Developer Mode flag (sage_data/ui_prefs.json). Default False = hidden."""
    try:
        import devmode
        return bool(devmode.is_enabled())
    except Exception:
        return False


VISIBLE = _dev_visible()


def _register(proc, title: str, argv0: str) -> None:
    """Record the spawn in the PID ledger. Best-effort, never raises."""
    try:
        import pid_registry
        if proc is not None and getattr(proc, "pid", None):
            pid_registry.register(proc.pid, title, argv0)
    except Exception as e:
        print(f"[tier_launcher] pid_registry failed for {title}: {e}")


def _spawn(title: str, argv: list, extra_env: dict = None):
    """Start one tier. Visible -> new console; hidden -> windowless.
    v2.11.12: spawns the REAL argv in both modes (no `start` shell trick)
    so the returned PID is the tier process, then registers it."""
    env = {**os.environ, **(extra_env or {})}
    try:
        if IS_WIN:
            flags = NEW_CONSOLE if VISIBLE else NO_WINDOW
        else:
            flags = 0
        proc = subprocess.Popen(argv, creationflags=flags, cwd=str(ROOT), env=env)
        _register(proc, title, argv[0] if argv else "")
        return proc
    except Exception as e:
        print(f"[tier_launcher] failed to start {title}: {e}")
        return None


# --- NPU tier (Ryzen AI via Lemonade Server) --------------------------------

def _npu_tier_config():
    """(enabled, port) from config.json. Defensive defaults: off, 11438."""
    try:
        from config_store import OracleConfig
        cfg = OracleConfig.load(ROOT / "config.json")
        enabled = bool(getattr(cfg.inference, "npu_enabled", False))
        port = int(getattr(cfg.network.ports, "npu_llm", 11438) or 11438)
        return enabled, port
    except Exception:
        return False, 11438


def _find_lemonade():
    """Locate AMD's Lemonade Server CLI. Returns argv prefix or None.
    v2.11.12c: delegates to hw_utils.find_lemonade_server (PATH ->
    conventional dirs -> uninstall registry) so the hardware panel's
    'runtime present' and this launcher always agree. Keeps a minimal
    PATH check as fallback if hw_utils can't import."""
    try:
        from hw_utils import find_lemonade_server
        exe = find_lemonade_server()
        return [exe] if exe else None
    except Exception:
        exe = shutil.which("lemonade-server") or shutil.which("lemonade-server.exe")
        return [exe] if exe else None


def _resolve_ollama() -> str:
    """Full path to ollama.exe: PATH first, then the standard install dirs
    (fresh installs have a stale PATH until next login). Falls back to the
    bare name so behavior is unchanged where it already worked."""
    exe = shutil.which("ollama")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    for cand in (Path(local) / "Programs" / "Ollama" / "ollama.exe" if local else None,
                 Path(pf) / "Ollama" / "ollama.exe"):
        if cand and cand.exists():
            return str(cand)
    return "ollama"


def _spawn_npu_tier():
    enabled, port = _npu_tier_config()
    if not enabled:
        print("[tier_launcher] NPU tier skipped (npu_enabled is off)")
        return
    lemonade = _find_lemonade()
    if not lemonade:
        print("[tier_launcher] NPU tier skipped (Lemonade Server not installed — "
              "install AMD's Lemonade Server to run models on the Ryzen AI NPU)")
        return
    print(f"[tier_launcher] NPU tier: Lemonade Server on :{port}")
    _spawn("NPU-Lemonade", lemonade + ["serve", "--port", str(port)])


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
    # NOTE: if the user already runs their own Ollama on this port, this
    # spawn fails to bind and exits on its own — and because only OUR
    # (dead) PID is in the ledger, cleanup never touches theirs.
    # v2.11.15: resolve the exe explicitly. On a machine where the Setup
    # Assistant JUST installed Ollama, PATH in this process tree is stale
    # until the user logs out/in — bare "ollama" would fail on the very
    # first launch, which is exactly the run that matters most.
    _spawn("Ollama-Oracle", [_resolve_ollama(), "serve"], extra_env={
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

    # Tier 4 (optional) - NPU (Ryzen AI via Lemonade Server).
    _spawn_npu_tier()

    # Sage Daemon (Python mechanics service).
    _spawn("Sage-Daemon", [py, str(BACKEND / "sage_daemon.py")])

    # Overseer Daemon (Python supervisor).
    _spawn("Overseer", [py, str(BACKEND / "overseer_daemon.py")])


if __name__ == "__main__":
    main()
