#!/usr/bin/env python3
"""
ComfyUI process launcher for OracleAI (OWNED lifecycle, OFF by default).
========================================================================

OracleAI can drive ComfyUI for image GENERATION (see comfyui_client.py), but it
cannot CREATE images unless a ComfyUI server is up. This module lets OracleAI
spawn ComfyUI itself and OWN the process, so:

  * generation works without the user launching ComfyUI by hand, and
  * closing OracleAI reaps ComfyUI (kills the whole tree) -- which destroys its
    in-memory job-queue box. That queue-wipe-on-close is a deliberate PRIVACY
    win: nothing lingers in the ComfyUI web UI after a session.

DESIGN (matches comfyui_client.py's discipline):
  * OFF by default. Does nothing unless config.comfyui_autostart_enabled is true.
  * HEALTH-GATED. If a ComfyUI is already listening on the port, we DO NOT launch
    a second one (and we don't claim ownership of a process we didn't start).
  * HEADLESS-FIRST. Resolution order prefers the bare Python server (main.py)
    over the Electron desktop app. "Headless" means no browser tab, no console
    window, no tray icon -- just the HTTP API on localhost. This matches how
    Intel's AI Playground ran ComfyUI, and how Ollama runs its server.
  * ZERO HARDCODING / distribution-safe. The launch command is resolved by
    precedence: explicit config.comfyui_launch_cmd  ->  python main.py under
    COMFYUI_HOME (true headless)  ->  portable run_*.bat under COMFYUI_HOME
    (semi-headless)  ->  Start Menu shortcut  ->  known desktop-app install
    paths. The last two open an Electron window and are last-resort fallbacks.
  * FULLY DEFENSIVE. Every entry point swallows its own errors and returns a
    status dict; nothing here raises into the app's startup/shutdown path.

This is intentionally an *option*: it is image-backend #1, not a dependency. A
future in-house diffusers engine slots in beside it the same way.
"""
from __future__ import annotations

import atexit
import os
import socket
import subprocess
import time
from urllib.parse import urlparse

# Module-owned process state. We only ever stop() what WE started.
_proc         = None   # subprocess.Popen of the ComfyUI we launched
_owned        = False  # True only when _proc is a process we spawned
_started_cmd  = None   # the resolved command we launched (for diagnostics)
_atexit_armed = False  # register the atexit reaper exactly once
_accel_cache  = None   # cached run mode: "cuda" | "directml" | "cpu"


# --------------------------------------------------------------------------- #
#  base URL / health probe (same default as comfyui_client.py)
# --------------------------------------------------------------------------- #
def _base_url() -> str:
    return (os.environ.get("COMFYUI_URL") or "http://127.0.0.1:8188").rstrip("/")


def _hostport(base: str):
    u = urlparse(base)
    return (u.hostname or "127.0.0.1"), int(u.port or 8188)


def is_running(base: str = None, timeout: float = 1.5) -> bool:
    """True if something is already listening on the ComfyUI port. A plain TCP
    connect is enough to gate against double-launch and is fast + dependency-free."""
    try:
        host, port = _hostport(base or _base_url())
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
#  hardware-aware run mode (cuda / directml / cpu)
# --------------------------------------------------------------------------- #
def _has_torch_directml(python_exe: str) -> bool:
    """True if the install's Python can import torch_directml (so --directml will
    actually work). Never raises."""
    try:
        if not python_exe or not os.path.exists(python_exe):
            return False
        r = subprocess.run([python_exe, "-c", "import torch_directml"],
                           capture_output=True, timeout=40)
        return r.returncode == 0
    except Exception:
        return False


def _resolve_accel(python_exe: str = None) -> str:
    """Pick ComfyUI's run mode for THIS machine: 'cuda' (NVIDIA), 'directml'
    (AMD/Intel GPU -- only if torch_directml is present) or 'cpu'. Cached after
    the first call. Honors a COMFYUI_ACCEL env override. Never raises.

    NVIDIA's portable runs CUDA with no flag. The official Windows portable has
    no AMD/Intel variant, so those vendors accelerate via ComfyUI's --directml
    (when torch_directml is installed); otherwise everything falls back to --cpu.
    """
    global _accel_cache
    if _accel_cache is not None:
        return _accel_cache
    override = (os.environ.get("COMFYUI_ACCEL") or "").strip().lower()
    if override in ("cuda", "directml", "cpu"):
        _accel_cache = override
        return override
    vendor = "cpu"
    try:
        import hw_utils
        hw = hw_utils.detect_hardware()
        if hw.get("nvidia", {}).get("available"):
            vendor = "nvidia"
        elif hw.get("amd", {}).get("available"):
            vendor = "amd"
        elif hw.get("intel", {}).get("available"):
            vendor = "intel"
    except Exception:
        vendor = "cpu"
    if vendor == "nvidia":
        accel = "cuda"
    elif vendor in ("amd", "intel"):
        # DirectML runs from a SEPARATE Python 3.12 env (see comfyui_directml);
        # it's "directml" only once that env is provisioned, else CPU.
        accel = "cpu"
        try:
            import comfyui_directml
            home = (os.environ.get("COMFYUI_HOME")
                    or os.environ.get("COMFYUI_PATH") or "")
            if comfyui_directml.is_provisioned(home):
                accel = "directml"
        except Exception:
            accel = "cpu"
    else:
        accel = "cpu"
    _accel_cache = accel
    return accel


# --------------------------------------------------------------------------- #
#  launch-command resolution (headless-first)
# --------------------------------------------------------------------------- #
def _headless_python_server():
    """
    Locate ComfyUI's main.py and build a true headless argv using the Python
    interpreter that ships with the install.

    Portable installs (the most common Windows setup) ship their own embedded
    Python under python_embeded/. We prefer that over a venv or system Python
    so the right packages are always in scope.

    Returns a list (argv) ready for subprocess.Popen, or None if main.py cannot
    be found.
    """
    home = os.environ.get("COMFYUI_HOME") or os.environ.get("COMFYUI_PATH")
    if not home or not os.path.isdir(home):
        return None

    main_py = os.path.join(home, "main.py")
    if not os.path.exists(main_py):
        return None

    # Prefer the interpreter bundled with the install; fall back to system python.
    # Portable releases nest as <root>/ComfyUI/main.py + <root>/python_embeded/,
    # so COMFYUI_HOME (where main.py lives) is the INNER folder and python_embeded
    # sits one level UP. Check the env-provided python root, then home, then parent.
    parent  = os.path.dirname(home.rstrip("\\/"))
    py_root = os.environ.get("COMFYUI_PYTHON_ROOT") or ""
    python_candidates = [
        os.path.join(py_root, "python_embeded", "python.exe") if py_root else "",
        os.path.join(home,    "python_embeded", "python.exe"),  # flat portable
        os.path.join(parent,  "python_embeded", "python.exe"),  # nested portable (most common)
        os.path.join(home,    ".venv", "Scripts", "python.exe"), # venv Windows
        os.path.join(home,    ".venv", "bin", "python"),         # venv Linux/Mac
    ]
    python = next(
        (p for p in python_candidates if p and os.path.exists(p)),
        "python",                                               # system fallback
    )

    # Hardware-aware run mode. AMD/Intel DirectML runs from a SEPARATE Python 3.12
    # env (comfyui_directml) against THIS same main.py; NVIDIA uses the portable's
    # CUDA Python (no flag); CPU-only adds --cpu.
    accel = _resolve_accel()
    if accel == "directml":
        try:
            import comfyui_directml
            _dml = comfyui_directml.directml_python(home)
            if _dml and os.path.exists(_dml):
                return [_dml, main_py,
                        "--listen", "127.0.0.1", "--port", "8188",
                        "--disable-auto-launch", "--dont-print-server", "--directml"]
        except Exception:
            pass
        accel = "cpu"   # DirectML requested but env missing -> safe CPU fallback

    base_argv = [
        python, main_py,
        "--listen",          "127.0.0.1",  # localhost only -- no LAN exposure
        "--port",            "8188",
        "--disable-auto-launch",            # no browser tab
        "--dont-print-server",              # suppress stdout noise
    ]
    if accel == "cpu":
        base_argv.append("--cpu")
    return base_argv


def _resolve_lnk_target(lnk_path: str):
    """Resolve a Windows .lnk shortcut to its TargetPath via the WScript.Shell COM
    object (PowerShell one-liner). Returns an existing path or None."""
    try:
        safe = lnk_path.replace("'", "''")
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
              "Write-Output $s.TargetPath" % safe)
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=12,
        )
        target = (out.stdout or "").strip()
        return target if target and os.path.exists(target) else None
    except Exception:
        return None


def _find_start_menu_comfy():
    """Look for a 'ComfyUI*.lnk' in the user + all-users Start Menu and resolve its
    target. Last-resort fallback -- the Electron app opens a window.
    Windows-only; returns a path or None."""
    if os.name != "nt":
        return None
    roots = []
    for env in ("APPDATA", "ProgramData"):
        base = os.environ.get(env)
        if base:
            roots.append(os.path.join(base, "Microsoft", "Windows",
                                      "Start Menu", "Programs"))
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            for dirpath, _dirs, files in os.walk(root):
                for fn in files:
                    if fn.lower().endswith(".lnk") and "comfyui" in fn.lower():
                        target = _resolve_lnk_target(os.path.join(dirpath, fn))
                        if target:
                            return target
        except Exception:
            continue
    return None


def _known_desktop_paths():
    """Standard ComfyUI Desktop (Electron) install locations. Last-resort fallback
    -- opens an Electron window. Returns first hit or None."""
    cands = []
    la = os.environ.get("LOCALAPPDATA")
    if la:
        cands.append(os.path.join(la, "Programs", "@comfyorgcomfyui-electron", "ComfyUI.exe"))
        cands.append(os.path.join(la, "Programs", "comfyui-electron", "ComfyUI.exe"))
        cands.append(os.path.join(la, "Programs", "ComfyUI", "ComfyUI.exe"))
    pf = os.environ.get("ProgramFiles")
    if pf:
        cands.append(os.path.join(pf, "ComfyUI", "ComfyUI.exe"))
    for c in cands:
        try:
            if os.path.exists(c):
                return c
        except Exception:
            continue
    return None


def _known_portable():
    """A portable ComfyUI under COMFYUI_HOME/COMFYUI_PATH (the run_*.bat launcher).
    Semi-headless: no window on Windows if launched via cmd /c, but still a
    bat-file process. Preferred over Electron, not over main.py."""
    home = os.environ.get("COMFYUI_HOME") or os.environ.get("COMFYUI_PATH")
    if not home or not os.path.isdir(home):
        return None
    for name in ("run_nvidia_gpu.bat", "run_cpu.bat", "run.bat"):
        p = os.path.join(home, name)
        try:
            if os.path.exists(p):
                return p
        except Exception:
            continue
    return None


def resolve_command(config=None):
    """Resolve how to start ComfyUI. Headless-first precedence:
       1. explicit config.comfyui_launch_cmd  (user override, highest trust)
       2. python main.py under COMFYUI_HOME   (true headless -- preferred)
       3. portable run_*.bat under COMFYUI_HOME (semi-headless)
       4. Start Menu ComfyUI shortcut target  (last resort -- opens Electron)
       5. known desktop-app install path      (last resort -- opens Electron)
    Returns a list (argv) or a command string, or None if nothing was found."""
    try:
        explicit = ""
        if config is not None:
            explicit = (config.get("comfyui_launch_cmd", "") or "").strip()
        if explicit:
            return explicit
    except Exception:
        pass

    return (
        _headless_python_server()   # true headless -- no window, no browser tab
        or _known_portable()        # semi-headless bat
        or _find_start_menu_comfy() # fallback -- opens Electron window
        or _known_desktop_paths()   # fallback -- opens Electron window
    )


# --------------------------------------------------------------------------- #
#  spawn / reap
# --------------------------------------------------------------------------- #
def _register_spawn(proc):
    """v2.11.12 zombie fix: record ComfyUI in the shared PID ledger so
    shutdown_cleanup.py can reap it on quit / next boot. ComfyUI is
    installed OUTSIDE the project folder, so the path-based sweep can't
    find it — the ledger is the only handle. Best-effort, never raises."""
    try:
        import pid_registry
        if proc is not None and getattr(proc, "pid", None):
            pid_registry.register(proc.pid, "ComfyUI", "comfyui")
    except Exception:
        pass
    return proc


def _spawn(command):
    """Spawn ComfyUI headless. Accepts a list (argv) or a string (path/shell cmd).

    CREATE_NO_WINDOW suppresses the console window on Windows -- this is the flag
    that actually hides the process visually. CREATE_NEW_PROCESS_GROUP (the old
    flag) controls signal propagation but does NOT hide anything.

    Output is discarded (DEVNULL) so the subprocess never blocks on a full pipe."""
    # CREATE_NO_WINDOW: console window never appears. Safe to OR with 0 on non-NT.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    dn = subprocess.DEVNULL

    if isinstance(command, list):
        # Pre-resolved argv from _headless_python_server() -- most reliable path.
        # cwd = directory containing main.py so relative imports inside ComfyUI work.
        cwd = os.path.dirname(command[1]) if len(command) > 1 else None
        return _register_spawn(subprocess.Popen(
            command, stdout=dn, stderr=dn, stdin=dn,
            cwd=cwd, creationflags=flags,
        ))

    if isinstance(command, str) and os.path.exists(command):
        cwd = os.path.dirname(command) or None
        if os.name == "nt" and command.lower().endswith((".bat", ".cmd")):
            args = ["cmd", "/c", command]
        else:
            args = [command]
        return _register_spawn(subprocess.Popen(
            args, stdout=dn, stderr=dn, stdin=dn,
            cwd=cwd, creationflags=flags,
        ))

    # Explicit shell command string from config (user-supplied).
    return _register_spawn(subprocess.Popen(
        command, shell=True, stdout=dn, stderr=dn, stdin=dn,
        creationflags=flags,
    ))


# --------------------------------------------------------------------------- #
#  warm-wait helper
# --------------------------------------------------------------------------- #
def wait_until_ready(base: str = None, timeout: float = 30.0,
                     interval: float = 0.5) -> bool:
    """Block until ComfyUI is accepting TCP connections or timeout expires.

    Optional but useful when you need a synchronous guarantee that ComfyUI is
    ready before submitting the first job (e.g. in a CLI flow or a test). In
    normal OracleAI operation the generation client health-checks before each
    render, so this is not strictly required -- but it makes start() composable:

        if launcher.start(config)["launched"]:
            if not launcher.wait_until_ready(timeout=45):
                # surface a user-visible warning; ComfyUI is slow to start
                ...
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_running(base):
            return True
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------- #
#  enabled check
# --------------------------------------------------------------------------- #
def _enabled(config) -> bool:
    try:
        return bool(config.get("comfyui_autostart_enabled", False)) if config else False
    except Exception:
        return False


# --------------------------------------------------------------------------- #
#  public API: start / stop / status
# --------------------------------------------------------------------------- #
def start(config=None, force=False) -> dict:
    """Launch ComfyUI as an OracleAI-owned headless process, if enabled and not
    already up. Non-blocking (no warm-wait here); ComfyUI warms in parallel with
    the rest of boot. Call wait_until_ready() after if you need a sync guarantee.
    Never raises."""
    global _proc, _owned, _started_cmd, _atexit_armed

    if not force and not _enabled(config):
        return {"launched": False, "reason": "autostart disabled"}

    base = _base_url()
    if is_running(base):
        # Already up (user-launched or prior run). Don't double-spawn, don't
        # claim ownership of a process we didn't start.
        return {"launched": False, "owned": False, "reason": "ComfyUI already running"}

    cmd = resolve_command(config)
    if not cmd:
        return {
            "launched": False,
            "reason": (
                "ComfyUI not found. Set COMFYUI_HOME to your ComfyUI directory "
                "or set comfyui_launch_cmd to your launcher path."
            ),
        }

    try:
        proc = _spawn(cmd)
    except Exception as e:
        return {"launched": False, "reason": "spawn failed: %s: %s" % (type(e).__name__, e)}

    _proc, _owned, _started_cmd = proc, True, cmd

    if not _atexit_armed:
        try:
            atexit.register(stop)   # backstop for non-graceful interpreter exit
            _atexit_armed = True
        except Exception:
            pass

    # Surface whether we got a true headless launch or a fallback Electron launch
    # so the caller / UI can warn the user if needed.
    headless = isinstance(cmd, list)
    return {
        "launched": True,
        "owned":    True,
        "headless": headless,
        "pid":      proc.pid,
        "cmd":      cmd if isinstance(cmd, str) else " ".join(cmd),
    }


def owns_process() -> bool:
    """True when we hold a ComfyUI process we are responsible for reaping."""
    return bool(_proc) and _owned


def ensure_running(config=None, ready_timeout: float = 60.0) -> dict:
    """Guarantee a ComfyUI server is up for an ON-DEMAND generation, regardless of
    the autostart-at-boot setting. If one is already listening we do nothing; else
    we launch headless (owned) and block until it accepts connections. This is the
    front half of the per-job lifecycle: ensure_running() -> generate -> respawn().
    Never raises."""
    try:
        base = _base_url()
        if is_running(base):
            return {"running": True, "launched": False, "owned": owns_process()}
        res = start(config, force=True)
        if not res.get("launched"):
            return {"running": False, "launched": False,
                    "reason": res.get("reason", "could not launch ComfyUI")}
        ready = wait_until_ready(base, timeout=ready_timeout)
        return {"running": ready, "launched": True, "owned": True,
                "headless": res.get("headless", False), "pid": res.get("pid"),
                "reason": None if ready else "ComfyUI launched but was not ready in time"}
    except Exception as e:
        return {"running": False, "launched": False,
                "reason": "%s: %s" % (type(e).__name__, e)}


def stop() -> dict:
    """Reap the ComfyUI process WE started (whole tree), destroying its job queue.
    No-op + safe if we never launched one. Idempotent. Never raises."""
    global _proc, _owned, _started_cmd
    p = _proc
    if not p or not _owned:
        _proc, _owned, _started_cmd = None, False, None
        return {"stopped": False, "reason": "no owned process"}
    try:
        if p.poll() is None:
            if os.name == "nt":
                # /T reaps the child tree (ComfyUI spawns worker processes).
                # This is what actually wipes the in-memory job queue -- the
                # whole process tree dies, taking every prompt_id with it.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(p.pid)],
                    capture_output=True, timeout=15,
                )
            else:
                # On Linux/Mac, SIGTERM the process group so children die too.
                import signal
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except Exception:
                    p.terminate()
            try:
                p.wait(timeout=10)
            except Exception:
                pass
        return {"stopped": True, "pid": p.pid}
    except Exception as e:
        return {"stopped": False, "reason": "%s: %s" % (type(e).__name__, e)}
    finally:
        _proc, _owned, _started_cmd = None, False, None


def status(config=None) -> dict:
    """Introspection helper for UI/diagnostics. Surfaces whether the resolved
    command is a true headless launch or a fallback Electron launch, so the
    UI can warn the user if ComfyUI will open a window. Never raises."""
    try:
        cmd = resolve_command(config)
        headless = isinstance(cmd, list)  # list = python main.py argv = true headless
        return {
            "enabled":      _enabled(config),
            "running":      is_running(),
            "owned":        owns_process(),
            "headless":     headless,
            "resolved_cmd": ((" ".join(cmd) if isinstance(cmd, list) else cmd) or ""),
            "base_url":     _base_url(),
        }
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}


# --------------------------------------------------------------------------- #
#  spawn-kill-respawn helper (for per-job queue privacy)
# --------------------------------------------------------------------------- #
def respawn(config=None, wait_ready: bool = True,
            ready_timeout: float = 45.0) -> dict:
    """Kill the currently owned ComfyUI process and immediately spawn a fresh one.

    This is the privacy-preserving per-job cycle Todd designed:
      1. Job completes (comfyui_client fetches the image).
      2. respawn() kills the old process -- job queue wiped from memory.
      3. Fresh ComfyUI warms up in the background while the user views the result.
      4. Next generation request hits a clean, ready instance.

    Returns a result dict with keys: stopped (bool), launched (bool),
    headless (bool), pid (int), ready (bool if wait_ready=True). Never raises.
    """
    stop_result = stop()

    # Brief pause -- give the OS a moment to release the port before we re-bind.
    time.sleep(0.75)

    start_result = start(config, force=True)
    if not start_result.get("launched"):
        return {
            "stopped":  stop_result.get("stopped", False),
            "launched": False,
            "reason":   start_result.get("reason", "unknown"),
        }

    ready = None
    if wait_ready:
        ready = wait_until_ready(timeout=ready_timeout)

    return {
        "stopped":  stop_result.get("stopped", False),
        "launched": True,
        "headless": start_result.get("headless", False),
        "pid":      start_result.get("pid"),
        "ready":    ready,
        "cmd":      start_result.get("cmd", ""),
    }
        
# --------------------------------------------------------------------------- #
#  CLI diagnostic (python comfyui_launcher.py)
# --------------------------------------------------------------------------- #
if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        print(json.dumps(status(), indent=2))

    elif cmd == "start":
        result = start({"comfyui_autostart_enabled": True})
        print(json.dumps(result, indent=2))
        if result.get("launched"):
            print("Waiting for ComfyUI to be ready...", flush=True)
            ready = wait_until_ready(timeout=45)
            print("Ready." if ready else "Timed out waiting for ComfyUI.")

    elif cmd == "stop":
        print(json.dumps(stop(), indent=2))

    elif cmd == "respawn":
        result = respawn({"comfyui_autostart_enabled": True})
        print(json.dumps(result, indent=2))

    elif cmd == "resolve":
        resolved = resolve_command()
        print("Resolved command:", resolved or "(nothing found)")
        print("Headless:", isinstance(resolved, list))

    else:
        print("Usage: python comfyui_launcher.py [status|start|stop|respawn|resolve]")