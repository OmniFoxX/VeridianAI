"""Spawn the VeridianAI inference tiers + Python daemons with console visibility
driven by Developer Mode.

  Dev Mode ON  -> each tier gets its own console window.
  Dev Mode OFF -> tiers are spawned WINDOWLESS (CREATE_NO_WINDOW), so normal users
                  get a clean desktop. This works regardless of Windows Terminal,
                  because no console window is ever created to begin with.

Called by start.bat, which has already resolved the paths / ports / models into
the environment (LLAMA_SERVER, SAGE_MODEL, DAEMON_MODEL, *_PORT, *_CTX_SIZE,
DAEMON_MODEL_PRESENT, PYTHON_CMD, VAI_ROOT). Dev Mode is a RESTART-to-apply
setting. Fully defensive: each tier is best-effort so one failure never blocks
the others, and start.bat's readiness probes still report any tier that's down.

v2.11.12 zombie-process fix:
  1. Every spawned PID is registered in the .oracle_pids.json ledger
     (pid_registry.py) so shutdown_cleanup.py can reap it on quit or on
     the next boot. This launcher exits right after spawning, orphaning
     its children -- the ledger is the ONLY reliable way to find them
     again. (Root cause of the zombie python/llama-server/window mess.)
  2. Dev-visible spawns no longer go through `start "Title" ...` +
     shell=True. That made the Popen handle point at a transient cmd.exe
     whose PID was useless -- the real tier process was unrecorded and
     thus unkillable. Now we use CREATE_NEW_CONSOLE on the real argv:
     same visible console, but the PID we get is the tier itself.
     (Cosmetic tradeoff: the console title is the exe name, not our
     custom label. devmode's hide/show works on PIDs, unaffected.)

v2.11.12 NPU tier (Ryzen AI):
  If inference.npu_enabled is on AND an NPU LLM runtime is installed
  (AMD Lemonade Server -- the official Ryzen AI OpenAI-compatible server),
  spawn it on network.ports.npu_llm (default 11438). model_manager picks
  it up as a fourth tier; the Hardware panel toggle turns routing on/off
  live, and this launcher decides at boot whether the server itself runs.
"""
import os
import json
import re
import shutil
import sys
import subprocess
import tempfile
import time
from pathlib import Path
# v2.13: make this importable no matter how the script is invoked. The
# BUNDLED (embeddable) Python builds sys.path ONLY from python*._pth -- it does
# not add the script's own directory and it ignores PYTHONPATH -- so running
# `python backend/tier_launcher.py` left backend/ off sys.path entirely and
# every sibling import failed with ModuleNotFoundError.
import sys as _sys
_here = str(Path(__file__).resolve().parent)
if _here not in _sys.path:
    _sys.path.insert(0, _here)
from state_paths import STATE_DIR, CONFIG_FILE, PID_REGISTRY, CHAT_MEMORY_FILE, LOCK_DIR, HASH_CHAIN_LOG  # v2.13 read-only-install support

ROOT = Path(os.environ.get("VAI_ROOT") or Path(__file__).resolve().parent.parent)
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


# Every tier this process started, so _report_early_exits can look back at
# them once they have all had a moment to fail.
_SPAWNED = []


def _tier_log_path(title: str) -> Path:
    """Where a windowless tier's stdout/stderr goes.

    Hidden tiers used to inherit the launcher's handles and, under
    CREATE_NO_WINDOW, that meant their output went nowhere at all. A tier could
    die on arrival and leave no trace whatsoever: Popen returned a live-looking
    proc, _spawn printed nothing (it only speaks up on an exception), and the
    launcher log looked clean while the port stayed dead.

    That blind spot cost a full build cycle in v2.13 -- a missing MSVC runtime
    killed llama-server before main() ran, so there was not even a usage string
    to find. Hidden tiers now always have somewhere to talk.
    """
    base = None
    try:
        from state_paths import data_dir  # type: ignore
        base = Path(data_dir()) / "logs" / "tiers"
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        base = None
    if base is None:
        base = Path(tempfile.gettempdir())
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", title) or "tier"
    return base / f"VeridianAI-tier-{safe}.log"


def _eos_args(model_path) -> list:
    """llama-server argv fragment correcting a GGUF that declares an EOS its
    own chat template never emits. [] when the file is self-consistent.

    Never fatal: a model we cannot probe is a model that runs with its declared
    EOS, exactly as before this check existed.
    """
    if not model_path:
        return []
    try:
        from gguf_probe import eos_override_args
        return eos_override_args(model_path)
    except Exception as e:
        print(f"[tier_launcher] EOS probe skipped for {model_path}: {e}")
        return []


def _reasoning_args(tier: str) -> list:
    """llama-server argv fragment bounding how long a model may think.

    v2.15.2. THIS is the spawner that matters: start.bat and store_launch.py
    both run tier_launcher at boot, so these are the servers the user actually
    talks to. config.build_llama_server_command is only reached when
    tier_lifecycle respawns a tier after a ctx change.

    The thinking budget originally went into that other builder alone. The
    flags were right and no running tier ever got one, because a booted install
    never takes that path -- the same "right code, wrong coverage" shape as the
    reasoning hook that sat on the streaming path while every agentic turn went
    around it. Both callers now share config.reasoning_args, and
    test_reasoning_budget asserts the two emit identical flags.

    Never fatal, exactly like _eos_args above: a tier that cannot compute a
    budget should still start, unbounded, the way it did before this existed.
    """
    try:
        from config import reasoning_args
        return reasoning_args(tier)
    except Exception as e:
        print(f"[tier_launcher] reasoning budget skipped for {tier}: {e}")
        return []


def _spawn(title: str, argv: list, extra_env: dict = None):
    """Start one tier. Visible -> new console; hidden -> windowless + logged.
    v2.11.12: spawns the REAL argv in both modes (no `start` shell trick)
    so the returned PID is the tier process, then registers it.
    v2.13: when hidden, stdout/stderr are captured to a per-tier log so a
    tier that dies is diagnosable instead of merely absent."""
    env = {**os.environ, **(extra_env or {})}
    log_path = None
    fh = None
    try:
        if IS_WIN:
            flags = NEW_CONSOLE if VISIBLE else NO_WINDOW
        else:
            flags = 0
        if not VISIBLE:
            log_path = _tier_log_path(title)
            try:
                fh = open(log_path, "w", encoding="utf-8", errors="replace")
            except Exception:
                fh, log_path = None, None
        proc = subprocess.Popen(
            argv, creationflags=flags, cwd=str(ROOT), env=env,
            stdout=(fh if fh else None),
            stderr=(subprocess.STDOUT if fh else None))
        _register(proc, title, argv[0] if argv else "")
        # Keep fh referenced: closing it here would not hurt the child (the
        # handle is already inherited) but we want it for the flush in
        # _report_early_exits.
        _SPAWNED.append((title, proc, log_path, fh))
        return proc
    except Exception as e:
        print(f"[tier_launcher] failed to start {title}: {e}")
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        return None


# Windows NTSTATUS codes that a process reports as its exit code when the
# LOADER, not the program, refused it. These never produce any output at all,
# because the program's main() is never reached.
_LOADER_FAILURES = {
    0xC0000135: ("STATUS_DLL_NOT_FOUND",
                 "a DLL it links against is missing. llama-server.exe needs "
                 "MSVCP140.dll, VCRUNTIME140.dll and VCRUNTIME140_1.dll beside "
                 "it in backend/ (they ship with the app) or the Microsoft "
                 "Visual C++ 2015-2022 Redistributable installed system-wide."),
    0xC0000139: ("STATUS_ENTRYPOINT_NOT_FOUND",
                 "a DLL was found but is the wrong version -- most often a "
                 "mismatched ggml/llama DLL set, or an older MSVC runtime on "
                 "PATH shadowing the bundled one."),
    0xC0000142: ("STATUS_DLL_INIT_FAILED",
                 "a dependent DLL failed to initialise."),
    0xC000007B: ("STATUS_INVALID_IMAGE_FORMAT",
                 "a 32-bit/64-bit mismatch between the exe and one of its DLLs."),
}


def _report_early_exits(wait: float = 2.0) -> None:
    """Say so, loudly, when a tier dies on arrival.

    Popen succeeding only means Windows CREATED the process. A missing DLL, an
    unreadable model or a flag this llama-server build does not accept all kill
    it microseconds later -- and none of those printed anything before v2.13.
    The launcher looked healthy, and the only symptom was a port that never
    opened, several layers away from the cause.

    Degrading gracefully is right. Degrading silently is the bug we keep paying
    for, so this is deliberately noisy: a tier that failed says why, here, at
    the point of failure.
    """
    if not _SPAWNED:
        return
    time.sleep(wait)
    for title, proc, log_path, fh in _SPAWNED:
        try:
            rc = proc.poll()
        except Exception:
            continue
        if rc is None:
            continue  # still running -- the normal case
        u = rc & 0xFFFFFFFF
        print(f"[tier_launcher] {title} EXITED IMMEDIATELY "
              f"(code {rc} / 0x{u:08X})")
        known = _LOADER_FAILURES.get(u)
        if known:
            name, why = known
            print(f"[tier_launcher]   {name}: {why}")
        try:
            if fh is not None:
                fh.flush()
        except Exception:
            pass
        lines = []
        try:
            if log_path is not None and Path(log_path).exists():
                lines = Path(log_path).read_text(
                    encoding="utf-8", errors="replace").splitlines()
        except Exception:
            pass
        if lines:
            print(f"[tier_launcher]   last output ({log_path}):")
            for ln in lines[-15:]:
                print(f"[tier_launcher]   | {ln}")
        elif log_path is not None:
            print(f"[tier_launcher]   no output at all -- it died before its "
                  f"own main() ran, which means the loader rejected it rather "
                  f"than the program refusing its arguments. Empty log: "
                  f"{log_path}")


# --- NPU tier (Ryzen AI via Lemonade Server) --------------------------------

def _npu_tier_config():
    """(enabled, port, ctx) from config.json. Defensive defaults: off, 11438, 16384."""
    try:
        from config_store import OracleConfig
        cfg = OracleConfig.load(CONFIG_FILE)
        enabled = bool(getattr(cfg.inference, "npu_enabled", False))
        port = int(getattr(cfg.network.ports, "npu_llm", 11438) or 11438)
        ctx = int(getattr(cfg.inference, "npu_ctx", 16384) or 16384)
        return enabled, port, ctx
    except Exception:
        return False, 11438, 16384


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


def _ollama_registry_env() -> dict:
    """v2.11.15b: read OLLAMA_MODELS (and OLLAMA_HOST if the user set one)
    straight from the Windows registry -- machine level, then user level.

    Why: these are often set as MACHINE env vars (Todd's models live at
    E:\\Ollamas\\.ollama\\models via one). A spawned process only inherits
    the environment its parent chain captured at ITS launch -- so whether
    our Ollama saw the models depended on how/when VeridianAI happened to be
    started. That roulette is how 31 models 'vanished' from the picker.
    The registry value is authoritative; read it directly, always."""
    out = {}
    if os.name != "nt":
        return out
    try:
        import winreg
        hives = [
            (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, r"Environment"),   # user overrides machine
        ]
        for hive, keypath in hives:
            try:
                with winreg.OpenKey(hive, keypath) as k:
                    for name in ("OLLAMA_MODELS",):
                        try:
                            val, _t = winreg.QueryValueEx(k, name)
                            if val:
                                out[name] = os.path.expandvars(str(val))
                        except OSError:
                            pass
            except OSError:
                continue
    except Exception:
        pass
    return out


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


def _pin_lemonade_ctx(ctx: int) -> None:
    """Best-effort: pin ctx_size in the config of the Lemonade instance WE
    spawn. v10's CLI is `lemond [cache_dir] [--port]` -- our argv
    `serve --port N` (run with cwd=ROOT) makes v10 read `serve` as the
    CACHE DIR positional, so this instance's config lives at
    ROOT/serve/config.json (self-contained in the project -- accidental
    but useful, and why the user-level %USERPROFILE%\\.cache\\lemonade
    config does NOT govern our tier). Only touches the one key, only when
    it differs, and never blocks the spawn on failure. If the file doesn't
    exist yet (very first boot -- Lemonade creates it with defaults on
    first run), we skip rather than guess the schema; the pin lands on
    the next boot."""
    try:
        cfgp = ROOT / "serve" / "config.json"
        if not cfgp.exists():
            print(f"[tier_launcher] Lemonade config not found at {cfgp}; "
                  "skipping ctx_size pin (first boot? pin applies next boot)")
            return
        raw = json.loads(cfgp.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        try:
            current = int(raw.get("ctx_size", -1))
        except (TypeError, ValueError):
            current = -1
        if current == int(ctx):
            return
        raw["ctx_size"] = int(ctx)
        cfgp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        print(f"[tier_launcher] Lemonade ctx_size {current} -> {ctx} ({cfgp})")
    except Exception as e:
        print(f"[tier_launcher] could not pin Lemonade ctx_size: {e}")


def _spawn_npu_tier():
    enabled, port, ctx = _npu_tier_config()
    if not enabled:
        print("[tier_launcher] NPU tier skipped (npu_enabled is off)")
        return
    lemonade = _find_lemonade()
    if not lemonade:
        print("[tier_launcher] NPU tier skipped (Lemonade Server not installed -- "
              "install AMD's Lemonade Server to run models on the Ryzen AI NPU)")
        return
    # v2.12.3: pin Lemonade's ctx_size. Its v10.x auto-update loads models
    # with a small default context (observed 4096), which the Toga system
    # prompt overflows -- the RyzenAI hybrid backend then hangs on prefill
    # instead of erroring. v10's serve CLI accepts only --port/--host (no
    # --ctx-size flag; passing one kills the spawn with a usage error), so
    # the pin goes into Lemonade's own config.json before the spawn.
    _pin_lemonade_ctx(ctx)
    print(f"[tier_launcher] NPU tier: Lemonade Server on :{port} (ctx {ctx})")
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
    embed_model = os.environ.get("EMBED_MODEL", "")
    p_embed = os.environ.get("LLAMA_EMBED_PORT", "11437")
    embed_ctx = os.environ.get("EMBED_CTX_SIZE", "2048")
    embed_enabled = os.environ.get("EMBED_ENABLED", "1") != "0"

    print(f"[tier_launcher] Developer Mode {'ON (consoles visible)' if VISIBLE else 'OFF (consoles hidden)'}")

    # Tier 1 - Oracle (Ollama). Env mirrors start.bat's inline `set`s.
    # NOTE: if the user already runs their own Ollama on this port, this
    # spawn fails to bind and exits on its own -- and because only OUR
    # (dead) PID is in the ledger, cleanup never touches theirs.
    # v2.11.15: resolve the exe explicitly. On a machine where the Setup
    # Assistant JUST installed Ollama, PATH in this process tree is stale
    # until the user logs out/in -- bare "ollama" would fail on the very
    # first launch, which is exactly the run that matters most.
    _spawn("Ollama-Oracle", [_resolve_ollama(), "serve"], extra_env={
        **_ollama_registry_env(),      # OLLAMA_MODELS from the registry (authoritative)
        "OLLAMA_HOST": f"127.0.0.1:{p_oracle}",
        "OLLAMA_MAX_LOADED_MODELS": "1",
        "OLLAMA_NUM_GPU": "1",
        "OLLAMA_GPU_OVERHEAD": "536870912",
    })

    # Tier 2 - Toga (llama-server, agentic engine).
    if llama and sage_model:
        _spawn("Llama-Toga", [llama, "-m", sage_model, "--host", "127.0.0.1",
                              "--port", p_sage, "--ctx-size", sage_ctx, "-ngl", "0", "--metrics"]
                             + _eos_args(sage_model)
                             + _reasoning_args("sage"))
    else:
        print("[tier_launcher] Toga tier skipped (LLAMA_SERVER/SAGE_MODEL not set)")

    # Tier 3 - Daemon (llama-server, tiny) - only if its model is present.
    if daemon_present and llama and daemon_model:
        _spawn("Llama-Daemon", [llama, "-m", daemon_model, "--host", "127.0.0.1",
                                "--port", p_daemon, "--ctx-size", daemon_ctx, "-ngl", "0"]
                               + _eos_args(daemon_model)
                               + _reasoning_args("daemon"))
    else:
        print("[tier_launcher] Daemon tier skipped (no model)")

    # Tier 3b - Embed (llama-server, nomic-embed). Serves BOTH consumers:
    # craiid/journalist.py's warm-handoff turn selection and sage_rag's
    # semantic search, via backend/embeddings.py.
    #
    # --embedding is MANDATORY: without it llama-server does not expose
    # /v1/embeddings at all and the endpoint 404s on a server that otherwise
    # looks perfectly healthy. --pooling mean yields one vector per input.
    #
    # Always-on rather than on-demand: embedding models are stateless, so the
    # footprint is flat (~200-300 MB) instead of growing with context, and a
    # cold start would otherwise land exactly when CRAIID is under fatigue
    # pressure. Disable with inference.embed_enabled=false.
    if embed_enabled and llama and embed_model:
        _spawn("Llama-Embed", [llama, "-m", embed_model, "--host", "127.0.0.1",
                               "--port", p_embed, "--ctx-size", embed_ctx,
                               "-ngl", "0", "--embedding", "--pooling", "mean"])
    elif not embed_enabled:
        print("[tier_launcher] Embed tier skipped (embed_enabled=false)")
    else:
        print("[tier_launcher] Embed tier skipped (LLAMA_SERVER/EMBED_MODEL not set) "
              "-- semantic search and CRAIID handoff selection will fall back to lexical")

    # Tier 4 (optional) - NPU (Ryzen AI via Lemonade Server).
    _spawn_npu_tier()

    # Toga Daemon (Python mechanics service).
    _spawn("Toga-Daemon", [py, str(BACKEND / "sage_daemon.py")])

    # Overseer Daemon (Python supervisor).
    _spawn("Overseer", [py, str(BACKEND / "overseer_daemon.py")])

    # Last: give everything a moment, then name anything that died on arrival.
    _report_early_exits()


if __name__ == "__main__":
    main()
