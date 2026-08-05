#!/usr/bin/env python3
"""
VeridianAI Launcher v2.13
Usage: python start.py [--port 8000] [--host 127.0.0.1] [--no-browser]
"""

import argparse, os, sys, subprocess, threading, time, webbrowser
from pathlib import Path

BASE_DIR    = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR / "backend"
REQ_FILE    = BACKEND_DIR / "requirements.txt"


# v2.12.17 (2026-07-30): bound the dependency install so a machine with no
# network cannot stall the launcher. pip's default backoff is 5 retries with
# exponential sleep PER PACKAGE, so an offline run used to grind for minutes
# with no output before giving up -- and this call sits directly in front of
# uvicorn.run(), so nothing was listening on the app port the whole time.
# VeridianAI is a local-inference app: it has to reach its own UI offline.
# Deliberately BELOW Electron's backend health timeout (HEALTH_TIMEOUT_MS in
# electron/main.js). If pip outlasts that, the user gets the "backend is slow"
# dialog no matter what we do here, so failing fast and booting is strictly
# better than waiting.
PIP_TIMEOUT_SEC = 90


def check_dependencies():
    missing = []
    for pkg in ("fastapi", "uvicorn", "httpx", "requests", "psutil"):
        # Broad except on purpose: an interrupted or half-rolled-back pip leaves
        # packages that raise OSError (bad DLL), AttributeError (version skew),
        # or worse at import time. ImportError alone let those escape and kill
        # the launcher before uvicorn ever ran.
        try:
            __import__(pkg)
        except Exception:
            missing.append(pkg)
    if not missing:
        return
    print(f"[VeridianAI] Installing: {', '.join(missing)} ...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", str(REQ_FILE),
             "--no-cache-dir", "--quiet",
             # fail fast when there is no route to PyPI
             "--retries", "1", "--timeout", "15"],
            timeout=PIP_TIMEOUT_SEC)
        print("[VeridianAI] Dependencies ready.")
    except subprocess.TimeoutExpired:
        print(f"[VeridianAI] pip timed out after {PIP_TIMEOUT_SEC}s "
              f"(offline?) -- continuing with what is installed.")
    except subprocess.CalledProcessError as e:
        print(f"[VeridianAI] pip failed: {e} -- continuing with what is installed.")
    except Exception as e:
        # Never let dependency housekeeping stop the app from launching.
        print(f"[VeridianAI] pip could not run: {e} -- continuing.")


def print_banner():
    print("""
  +-------------------------------------------+
  |    V E R I D I A N   A I   v2.13          |
  |       Local AI Inference + Sage           |
  +-------------------------------------------+
""")


def _resolve_default_port() -> int:
    """When --port isn't given, fall through to backend.config.PORT_APP,
    which itself respects env var > config.json > 8000. Standalone runs
    of start.py honor the same port the rest of the stack uses."""
    try:
        sys.path.insert(0, str(BACKEND_DIR))
        from config import PORT_APP
        return PORT_APP
    except Exception:
        return 8000


def _resolve_default_host() -> str:
    """When --host isn't given, read network.host from config.json so the UI's
    'Bind to LAN' toggle actually takes effect on restart. Env ORACLE_APP_HOST
    wins; defaults to 127.0.0.1 (localhost-only)."""
    import os as _os
    env = _os.environ.get("ORACLE_APP_HOST")
    if env:
        return env
    try:
        import json as _json
        with open(BACKEND_DIR.parent / "config.json", "r", encoding="utf-8") as f:
            raw = _json.load(f)
        h = (raw.get("network", {}) or {}).get("host")
        if h:
            return str(h)
    except Exception:
        pass
    return "127.0.0.1"


def main():
    parser = argparse.ArgumentParser(description="Launch VeridianAI")
    # default=None so we can distinguish "user typed --port 8000" from
    # "user didn't pass --port" — only the latter falls through to config.
    parser.add_argument("--port",       type=int, default=None)
    parser.add_argument("--host",       default=None)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.port is None:
        args.port = _resolve_default_port()
    if args.host is None:
        args.host = _resolve_default_host()

    print_banner()
    check_dependencies()

    # 0.0.0.0 is a BIND address, not a connectable one - point the browser at
    # localhost even when we bind to all interfaces for LAN serving.
    _browser_host = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host
    url = f"http://{_browser_host}:{args.port}"
    print(f"  URL  : {url}")
    print(f"  Stop : Ctrl+C\n")

    if not args.no_browser:
        def _open():
            time.sleep(1.8)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    sys.path.insert(0, str(BACKEND_DIR))
    os.chdir(str(BACKEND_DIR))

    try:
        import uvicorn
        from main import app
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    except ImportError as e:
        # v2.12.17: this used to block on input("Press Enter to exit..."). With a
        # real console attached (Developer Mode on) that waits forever, so
        # Electron sat out its full health timeout and showed the offline screen
        # instead of this message. Exit non-zero and let the launcher report it.
        print(f"\n[ERROR] {e}")
        print("  A required package is missing or broken.")
        print("  Ensure you are running from the VeridianAI folder, then try:")
        print(f"    {sys.executable} -m pip install -r {REQ_FILE}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[VeridianAI] Stopped. Goodbye.")


if __name__ == "__main__":
    main()
