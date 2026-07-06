"""
shutdown_cleanup.py — v2.11.12 zombie-process fix (the reaper).

Kills every process OracleAI itself started, and nothing else. Called:

  1. By Electron's stopBackend() on quit (synchronously, so the app
     doesn't exit before the reaping finishes).
  2. By start.bat and Electron BEFORE launching the stack, so a previous
     crash / unclean quit can never block the next start (this is the
     "takes 3-5 tries to restart" fix — stale port-holders die first).
  3. Manually: python backend\\shutdown_cleanup.py

Two passes:

  PASS 1 — PID ledger (.oracle_pids.json via pid_registry). Each entry
  is killed ONLY if its psutil create_time matches what we recorded at
  spawn, so recycled PIDs belonging to other software are never touched.
  This is what protects a user-launched Ollama: it was never registered,
  so it is never killed (policy per Todd, 2026-07-02).

  PASS 2 — project-path sweep (safety net for anything that predates the
  ledger, or a corrupted ledger). Any surviving process whose command
  line references THIS project folder (backend scripts, the bundled
  llama-server.exe, start.py) is terminated. A system-wide Ollama or an
  unrelated python.exe does not reference this folder, so it survives.

Exit code 0 always (cleanup is best-effort; a failure to reap must never
block a launch). Pass --quiet to silence per-process output.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(os.environ.get("OAI_ROOT") or Path(__file__).resolve().parent.parent)
QUIET = "--quiet" in sys.argv
MY_PID = os.getpid()


def _log(msg: str) -> None:
    if not QUIET:
        print(f"[cleanup] {msg}")


def _is_gone(proc) -> bool:
    """Dead, or a zombie awaiting reap by its real parent (POSIX only —
    a zombie can't hold ports or files, so for our purposes it's gone;
    Windows has no zombie state)."""
    import psutil
    try:
        if not proc.is_running():
            return True
        return proc.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True
    except Exception:
        return False


def _kill_proc(proc, label: str) -> bool:
    """Terminate, then kill after grace. Returns True if it's gone."""
    import psutil
    try:
        proc.terminate()
    except psutil.NoSuchProcess:
        return True
    except Exception:
        pass
    try:
        proc.wait(timeout=4)
        _log(f"terminated  {label} (pid {proc.pid})")
        return True
    except Exception:
        if _is_gone(proc):
            _log(f"terminated  {label} (pid {proc.pid})")
            return True
    try:
        proc.kill()
        proc.wait(timeout=3)
        _log(f"force-killed {label} (pid {proc.pid})")
        return True
    except psutil.NoSuchProcess:
        return True
    except Exception as e:
        if _is_gone(proc):
            _log(f"force-killed {label} (pid {proc.pid})")
            return True
        _log(f"FAILED to kill {label} (pid {proc.pid}): {e}")
        return False


def _pass1_registry() -> int:
    """Kill ledger entries whose identity still matches. Returns kill count."""
    import psutil
    import pid_registry

    killed = 0
    for entry in pid_registry.load():
        pid = int(entry.get("pid", 0))
        label = entry.get("label", "?")
        if not pid or pid == MY_PID:
            continue
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            continue                      # already gone — fine
        except Exception:
            continue
        # Identity check: create_time recorded at spawn must match (60ms
        # slack for float rounding). Missing recorded time -> fall back to
        # a name/cmdline sanity check against the project path or argv0.
        rec_ct = entry.get("create_time")
        if rec_ct is not None:
            try:
                if abs(proc.create_time() - float(rec_ct)) > 0.06:
                    _log(f"pid {pid} recycled by another process — skipping")
                    continue
            except Exception:
                continue
        else:
            argv0 = (entry.get("argv0") or "").lower()
            try:
                cmdline = " ".join(proc.cmdline()).lower()
            except Exception:
                cmdline = ""
            if argv0 and argv0 not in cmdline and str(ROOT).lower() not in cmdline:
                _log(f"pid {pid} identity unverifiable — skipping")
                continue
        # v2.11.12e ordering fix: snapshot the child list BEFORE killing
        # the parent (once the parent dies, children are re-parented and
        # proc.children() returns nothing), then kill PARENT FIRST. The
        # old children-first order left a window where a supervisor
        # (Overseer) could respawn a just-killed daemon before we got to
        # it — the likely source of the invisible leftover python
        # processes holding the CRAIID/sage log files after quit.
        children = []
        try:
            children = proc.children(recursive=True)
        except Exception:
            pass
        if _kill_proc(proc, label):
            killed += 1
        for child in children:
            if child.pid != MY_PID:
                _kill_proc(child, f"{label}:child")
    return killed


def _pass2_sweep() -> int:
    """Kill any survivor that is unambiguously part of the OracleAI stack:
    a python/llama-server process running from <root>\\backend or start.py.
    Deliberately NARROW — a user's own Ollama, an MCP server run from the
    MCP/ folder, or any unrelated python.exe never matches."""
    import psutil

    root_l = str(ROOT).lower().rstrip("\\/")
    backend_l = os.path.join(root_l, "backend").lower()
    start_py_l = os.path.join(root_l, "start.py").lower()
    killed = 0
    protected = {MY_PID, os.getppid()}
    for proc in psutil.process_iter(["pid", "name", "cmdline", "exe"]):
        try:
            if proc.pid in protected:
                continue
            name = (proc.info.get("name") or "").lower()
            # Only ever consider our own process families — never sweep
            # explorer.exe or similar just because a path matched.
            # v2.11.15: lemonade + ollama re-added (they were dropped when
            # this filter was tightened, which let a detached Lemonade
            # server outlive quit). Safe: the path/cwd conditions below
            # still require the process to be rooted in THIS project —
            # tier_launcher spawns with cwd=<root>, while a user's own
            # tray Lemonade or standalone Ollama runs from its install
            # dir and never matches.
            if not any(k in name for k in
                       ("python", "llama-server", "lemonade", "ollama")):
                continue
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            exe = (proc.info.get("exe") or "").lower()
            hit = (backend_l in cmdline or backend_l in exe
                   or start_py_l in cmdline)
            # v2.11.12e: cwd-based match. Catches every stack process whose
            # command line does NOT carry the project path: uvicorn launched
            # as relative `py start.py`, multiprocessing workers (cmdline is
            # `... -c from multiprocessing...spawn_main`), and daemon
            # generations respawned with mangled argv. They all INHERIT
            # cwd from their spawner, and every OracleAI spawn site uses
            # cwd=<root> (tier_launcher, overseer) or <root>\backend
            # (start.py after chdir). Still deliberately narrow: only
            # python/llama-server processes (name filter above) sitting
            # EXACTLY in root or backend — an MCP server run from MCP\
            # or any tool with its own cwd never matches.
            if not hit:
                try:
                    cwd = os.path.normcase(proc.cwd().rstrip("\\/"))
                    if cwd in (os.path.normcase(str(ROOT).rstrip("\\/")),
                               os.path.normcase(backend_l)):
                        hit = True
                except Exception:
                    pass
            if not hit:
                continue
            # Don't sweep the process running THIS script or its parent
            # (Electron / cmd) — protected above; also skip cleanup peers.
            if "shutdown_cleanup" in cmdline and proc.pid != MY_PID:
                continue
            if _kill_proc(proc, f"sweep:{name}"):
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    return killed


def main() -> int:
    try:
        import psutil  # noqa: F401
    except ImportError:
        _log("psutil not installed — cannot clean up. (pip install psutil)")
        return 0

    t0 = time.time()
    k1 = _pass1_registry()
    k2 = _pass2_sweep()

    try:
        import pid_registry
        pid_registry.clear()
    except Exception:
        pass

    _log(f"done: {k1} registered + {k2} swept in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
