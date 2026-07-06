"""
tier_lifecycle.py — Phase 1D Step 4
----------------------------------------------------------------
llama-server tier process management: find, kill, spawn, restart.

WHY THIS EXISTS:
llama-server.exe takes its --ctx-size from the command line at startup and
cannot resize its context window on a running process. When the user changes
n_ctx in the UI and clicks Refresh Models, the only way to apply the change
is to kill the existing llama-server for the affected tier and respawn it
with new flags. This module owns that workflow.

CALLED BY:
main.py's `/api/tiers/*` routes and `/api/models/refresh` route.

ARCHITECTURE NOTE (Pragmatic, per user decision):
The running llama-server processes were spawned by start.bat at boot time,
not by this Python process. That means we don't have subprocess.Popen handles
for them. We find them by port-based PID lookup using psutil, then kill and
respawn. When we spawn a new one ourselves, we DO get a Popen handle — but
we still rely on port-based lookup for the NEXT restart rather than tracking
the handle, because it's simpler and robust to process crashes.
"""

import asyncio
import pathlib
import subprocess
import sys
import time
from typing import Dict, Optional

import httpx
import psutil

from config import (
    PORT_LLAMA_SAGE,
    PORT_LLAMA_DAEMON,
    LLAMA_SAGE_URL,
    LLAMA_DAEMON_URL,
    SAGE_CTX_DEFAULT,
    DAEMON_CTX_DEFAULT,
    compute_sage_ctx,
    compute_daemon_ctx,
    build_llama_server_command,
)

# --- Tier registry -----------
TIER_PORTS: Dict[str, int] = {
    "sage":    PORT_LLAMA_SAGE,
    "daemon":  PORT_LLAMA_DAEMON,
    "bitchat": 8080,                  # BitChat BLE gateway
}

TIER_URLS: Dict[str, str] = {
    "sage":    LLAMA_SAGE_URL,
    "daemon":  LLAMA_DAEMON_URL,
    "bitchat": "http://127.0.0.1:8080",
}

# --- ctx_size cache ----------
# Cache of last-known ctx_size per tier. Initialized at FastAPI startup via
# init_cache() from the same compute_*_ctx helpers that start.bat used, so
# the cache always reflects what's actually running.
#
# On a successful restart, the cache is updated to the new value. This lets
# /api/models/refresh skip a restart when nothing has changed (idempotent).
_tier_ctx_cache: Dict[str, int] = {}


# -------------
#  PUBLIC API
# -------------
def init_cache(config: dict) -> None:
    """Seed the ctx cache from the loaded config dict. Call once at
    FastAPI startup from an @app.on_event('startup') handler."""
    n_ctx = config.get("n_ctx")
    _tier_ctx_cache["sage"]   = compute_sage_ctx(n_ctx)
    _tier_ctx_cache["daemon"] = compute_daemon_ctx(n_ctx)
    print(f"[tier] ctx cache initialized: "
          f"sage={_tier_ctx_cache['sage']}, "
          f"daemon={_tier_ctx_cache['daemon']}")


def get_cached_ctx(tier: str) -> Optional[int]:
    """Return the cached ctx_size for a tier, or None if not initialized."""
    return _tier_ctx_cache.get(tier)


def tier_status_snapshot() -> Dict[str, dict]:
    """Return a dict of tier → {port, running, pid, ctx_size} for all tiers.
    Used by GET /api/tiers."""
    out: Dict[str, dict] = {}
    for tier_name, port in TIER_PORTS.items():
        pid = find_pid_by_port(port)
        out[tier_name] = {
            "port":     port,
            "running":  pid is not None,
            "pid":      pid,
            "ctx_size": _tier_ctx_cache.get(tier_name),
        }
    return out


async def restart_tier(tier: str, desired_ctx: int) -> dict:
    """Kill existing llama-server for `tier`, spawn new with `desired_ctx`,
    wait for readiness. Returns a status dict with status in {"ok", "failed"}.

    On failure, the tier will NOT be running — caller should be aware that
    the tier is now offline and may want to retry with a safer ctx_size.
    """
    tier = tier.lower().strip()
    if tier not in TIER_PORTS:
        return {"status": "failed", "tier": tier,
                "message": f"Unknown tier: {tier!r}"}

    # BitChat gateway has its own restart path
    if tier == "bitchat":
        return await ensure_bitchat_gateway(force_restart=True)

    port = TIER_PORTS[tier]
    url  = TIER_URLS[tier]

    # Step 1: find and kill existing process (if any)
    old_pid = find_pid_by_port(port)
    if old_pid is not None:
        print(f"[tier] {tier}: killing existing PID {old_pid}")
        if not kill_process_graceful(old_pid):
            return {"status": "failed", "tier": tier,
                    "message": f"Could not kill PID {old_pid}"}
        if not wait_port_free(port, timeout=10.0):
            return {"status": "failed", "tier": tier,
                    "message": f"Port {port} still busy after kill"}
        print(f"[tier] {tier}: port {port} freed")
    else:
        print(f"[tier] {tier}: no existing process on port {port}")

    # Step 2: spawn new process
    try:
        proc = spawn_llama_server(tier, desired_ctx)
        print(f"[tier] {tier}: spawned PID {proc.pid} with ctx_size={desired_ctx}")
    except Exception as e:
        return {"status": "failed", "tier": tier,
                "message": f"Spawn failed: {e}"}

    # Step 3: wait for readiness (model load + HTTP server bind)
    ready = await wait_tier_ready(url, timeout=60.0)
    if not ready:
        return {"status": "failed", "tier": tier,
                "message": f"{tier} did not respond to /v1/models within 60s",
                "pid": proc.pid}

    # Step 4: success — update cache
    _tier_ctx_cache[tier] = desired_ctx
    return {"status": "ok", "tier": tier, "pid": proc.pid,
            "ctx_size": desired_ctx,
            "message": f"{tier} tier restarted with ctx_size={desired_ctx}"}


async def refresh_if_needed(config: dict) -> dict:
    """Combined routine called by /api/models/refresh.

    For each llama-server tier, check whether the desired ctx_size (computed
    from config.json's n_ctx) matches the cached value. If yes, skip. If no,
    restart the tier with the new value. Returns a list of tiers that were
    actually restarted, plus any warnings from failed restarts.
    """
    global_n_ctx = config.get("n_ctx")
    restarted = []
    warnings  = []

    tier_desired = {
        "sage":   compute_sage_ctx(global_n_ctx),
        "daemon": compute_daemon_ctx(global_n_ctx),
    }

    for tier, desired in tier_desired.items():
        cached = _tier_ctx_cache.get(tier)
        if cached == desired:
            print(f"[tier] {tier}: ctx_size unchanged ({desired}), skipping restart")
            continue

        print(f"[tier] {tier}: ctx_size changed {cached} -> {desired}, restarting")
        result = await restart_tier(tier, desired)
        if result["status"] == "ok":
            restarted.append({"tier": tier, "ctx_size": desired})
        else:
            warnings.append(f"{tier}: {result.get('message', 'restart failed')}")

    return {"restarted": restarted, "warnings": warnings}


async def ensure_bitchat_gateway(force_restart: bool = False) -> dict:
    """Start BitChat BLE gateway if not already running.
    Safe to call at every startup — checks health before spawning.
    Pass force_restart=True to kill and respawn unconditionally."""
    url = "http://127.0.0.1:8080"

    if not force_restart:
        # Already up and healthy?
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.get(f"{url}/health")
                if r.status_code == 200:
                    print("[tier] bitchat: gateway already running")
                    return {"status": "ok", "message": "already running"}
        except Exception:
            pass
    else:
        # Kill existing if force restart requested
        pid = find_pid_by_port(8080)
        if pid is not None:
            print(f"[tier] bitchat: force-killing PID {pid}")
            kill_process_graceful(pid)
            wait_port_free(8080, timeout=10.0)

    # Spawn fresh
    try:
        proc = spawn_bitchat_gateway()
        print(f"[tier] bitchat: gateway spawned PID {proc.pid}")
    except Exception as e:
        return {"status": "failed", "message": f"Spawn failed: {e}"}

    # Wait for readiness
    ready = await wait_tier_ready(url, timeout=30.0)
    if not ready:
        return {"status": "failed",
                "message": "BitChat gateway did not respond within 30s"}

    return {"status": "ok", "pid": proc.pid,
            "message": "BitChat BLE gateway started"}


async def stop_bitchat_gateway() -> dict:
    """Stop the BitChat BLE gateway so scanning fully ceases.
    Tries a graceful /shutdown first, then guarantees the process is gone."""
    url = "http://127.0.0.1:8080"
    # Graceful: ask the gateway to leave the mesh and exit cleanly.
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            await c.post(f"{url}/shutdown")
    except Exception:
        pass  # gateway may already be exiting or down

    # Guarantee: kill anything still bound to the port.
    pid = find_pid_by_port(8080)
    if pid is not None:
        print(f"[tier] bitchat: stopping PID {pid}")
        kill_process_graceful(pid)
        wait_port_free(8080, timeout=10.0)
    freed = find_pid_by_port(8080) is None
    return {"status": "ok" if freed else "failed",
            "message": "BitChat gateway stopped" if freed
                       else "port 8080 still busy"}


# ------------------
#  LOW-LEVEL HELPERS
# ------------------

def find_pid_by_port(port: int) -> Optional[int]:
    """Locate any process listening on the given port.
    Works for both llama-server.exe and python (bitchat gateway)."""
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            for conn in proc.connections(kind='inet'):
                if (conn.status == psutil.CONN_LISTEN and
                        conn.laddr and conn.laddr.port == port):
                    return proc.pid
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue
    return None


def kill_process_graceful(pid: int, timeout: float = 5.0) -> bool:
    """Terminate pid gracefully, wait up to timeout, then force-kill."""
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True  # already dead, fine

    try:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2.0)
            except psutil.TimeoutExpired:
                return False
        return True
    except psutil.NoSuchProcess:
        return True
    except Exception as e:
        print(f"[tier] kill error for PID {pid}: {e}")
        return False


def wait_port_free(port: int, timeout: float = 10.0) -> bool:
    """Poll synchronously until no process is listening on the port."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if find_pid_by_port(port) is None:
            return True
        time.sleep(0.5)
    return False


async def wait_tier_ready(base_url: str, timeout: float = 90.0) -> bool:
    """Poll until the tier responds 200 OK.
    Tries /health first (BitChat gateway), falls back to /v1/models
    (llama-server). Async-safe — yields between polls."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                # Try /health first (gateway), then /v1/models (llama-server)
                for path in ("/health", "/v1/models"):
                    try:
                        r = await c.get(f"{base_url}{path}")
                        if r.status_code == 200:
                            return True
                    except Exception:
                        continue
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return False


def _dm_creationflags() -> int:
    """Visible new console when Developer Mode is on, windowless when off
    (so model/gateway respawns honor the Settings toggle). 0 off-Windows."""
    if sys.platform != "win32":
        return 0
    try:
        import devmode
        return devmode.console_creationflags()
    except Exception:
        return subprocess.CREATE_NEW_CONSOLE


def spawn_llama_server(tier: str, ctx_size: int) -> subprocess.Popen:
    """Spawn a new llama-server for a tier. Console visibility follows the
    Developer Mode toggle. Returns the Popen handle."""
    cmd = build_llama_server_command(tier, ctx_size=ctx_size)
    return subprocess.Popen(cmd, creationflags=_dm_creationflags())


_GATEWAY_PY: Optional[list] = None


def _gateway_python() -> list:
    """Return a Python interpreter (as a Popen argv prefix) that actually has
    the gateway's BLE dependencies (bleak + winrt).

    We CANNOT assume sys.executable works: start.bat launches the app with the
    first `py`/`python` on PATH, which on this machine is a 3.13 that has no
    bleak, while the BLE stack (bleak/winrt) lives in Python 3.14. The old
    manual workflow ran the gateway from that 3.14 by hand. So we probe a few
    candidate interpreters and pick the first that can import bleak+winrt. The
    result is cached for the process (probing is done once, on first Connect)."""
    global _GATEWAY_PY
    if _GATEWAY_PY is not None:
        return _GATEWAY_PY
    import os
    # Probe for the FULL set the gateway imports. winrt is split per-namespace,
    # so an interpreter can have winrt.windows.devices.bluetooth yet still lack
    # winrt.windows.security.cryptography — exactly what broke auto-start here
    # (the pythoncore-3.14 env had bluetooth but not security; C:\Python314 has
    # the complete set). A shallow probe would wrongly accept the partial env.
    probe = (
        "import bleak, fastapi, uvicorn, cryptography;"
        "import winrt.windows.devices.bluetooth;"
        "import winrt.windows.devices.bluetooth.genericattributeprofile;"
        "import winrt.windows.storage.streams;"
        "import winrt.windows.security.cryptography"
    )
    candidates = [[sys.executable]]
    if sys.platform == "win32":
        # py-launcher targets, then bare names, then common install locations.
        candidates += [["py", "-3.14"], ["py", "-3.13"], ["py", "-3.12"], ["py"]]
    candidates += [["python"], ["python3"]]
    if sys.platform == "win32":
        _la = os.environ.get("LOCALAPPDATA", "")
        for _base in (r"C:\Python314", r"C:\Python313", r"C:\Python312",
                      os.path.join(_la, r"Programs\Python\Python314") if _la else "",
                      os.path.join(_la, r"Programs\Python\Python313") if _la else ""):
            if _base:
                candidates.append([os.path.join(_base, "python.exe")])
    hidden = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    seen, chosen = set(), None
    for c in candidates:
        key = tuple(c)
        if key in seen:
            continue
        seen.add(key)
        try:
            r = subprocess.run(c + ["-c", probe], capture_output=True,
                               timeout=25, creationflags=hidden)
            if r.returncode == 0:
                chosen = c
                break
        except Exception:
            continue
    _GATEWAY_PY = chosen or [sys.executable]
    if chosen and chosen != [sys.executable]:
        print(f"[tier] bitchat: gateway interpreter -> {chosen} "
              f"(app interpreter {sys.executable} lacks bleak/winrt)")
    elif not chosen:
        print("[tier] bitchat: WARNING no interpreter with bleak/winrt found; "
              f"falling back to {sys.executable} — gateway will likely fail. "
              "Install bleak+winrt into that Python, or a 3.14 on PATH.")
    return _GATEWAY_PY


def spawn_bitchat_gateway() -> subprocess.Popen:
    """Spawn the BitChat gateway. Console visibility follows Developer Mode.

    Uses the WinRT peripheral gateway (Sage advertises as a real BLE peer that
    the phones handshake with) and falls back to the legacy central-role BLE
    gateway only if the WinRT script is missing. Both expose the identical
    :8080 WS/HTTP contract, so ensure_/stop_bitchat_gateway are unaffected.
    Runs under an interpreter that actually has the BLE deps (see
    _gateway_python) — NOT necessarily the app's own Python. This is what lets
    BitChat auto-start from the UI toggle instead of a manual terminal launch."""
    here = pathlib.Path(__file__).parent
    gateway = here / "bitchat_winrt_gateway.py"
    if not gateway.exists():
        gateway = here / "bitchat_ble_gateway.py"
    return subprocess.Popen(
        _gateway_python() + [str(gateway)],
        creationflags=_dm_creationflags(),
    )