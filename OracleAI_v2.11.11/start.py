#!/usr/bin/env python3
"""
OracleAI Launcher v2.11.11
Usage: python start.py [--port 8000] [--host 127.0.0.1] [--no-browser]
"""

import argparse, os, sys, subprocess, threading, time, webbrowser
from pathlib import Path

BASE_DIR    = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR / "backend"
REQ_FILE    = BACKEND_DIR / "requirements.txt"


def check_dependencies():
    missing = []
    for pkg in ("fastapi", "uvicorn", "httpx", "requests", "psutil"):
        try: __import__(pkg)
        except ImportError: missing.append(pkg)
    if missing:
        print(f"[OracleAI] Installing: {', '.join(missing)} ...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "-r", str(REQ_FILE),
                 "--no-cache-dir", "--quiet"])
            print("[OracleAI] Dependencies ready.")
        except subprocess.CalledProcessError as e:
            print(f"[OracleAI] pip failed: {e}")


def print_banner():
    print("""
  +-------------------------------------------+
  |     O R A C L E   A I    v2.11.11         |
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
    parser = argparse.ArgumentParser(description="Launch OracleAI")
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
        print(f"\n[ERROR] {e}")
        print("  Ensure you run from the OracleAI folder.")
        input("Press Enter to exit...")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[OracleAI] Stopped. Goodbye.")


if __name__ == "__main__":
    main()
