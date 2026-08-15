#!/usr/bin/env python3
"""
VeridianAI Launcher v2.13
Usage: python start.py [--port 8000] [--host 127.0.0.1] [--no-browser]
"""

import argparse, os, sys, subprocess, threading, time, webbrowser

# ---------------------------------------------------------------------------
# v2.13 HANG DIAGNOSTIC.
#
# A packaged (MSIX) launch reached the banner and then produced NOTHING for
# four minutes -- no output, no traceback, no exit. That is a HANG, and a hang
# leaves no evidence: PYTHONFAULTHANDLER only fires on a crash, and -u cannot
# flush output that was never produced.
#
# dump_traceback_later prints a full stack for every thread if the process is
# still alive after N seconds, then repeats. Whatever import or call is stuck
# names itself, with file and line. A healthy boot never reaches the first
# dump, so this costs nothing in normal use.
#
# Set VERIDIAN_HANG_DUMP_SEC=0 to disable.
# ---------------------------------------------------------------------------
try:
    import faulthandler
    _hang_sec = float(os.environ.get("VERIDIAN_HANG_DUMP_SEC", "60") or 0)
    if _hang_sec > 0:
        faulthandler.dump_traceback_later(_hang_sec, repeat=True, exit=False)
except Exception:
    pass


def cancel_hang_dump() -> None:
    """Disarm the watchdog once the backend is actually serving.

    repeat=True means this fires FOREVER, not once -- the comment above used to
    claim "a healthy boot never reaches the first dump", which is only true of a
    non-repeating timer. In practice every 60 seconds it dumped a full stack for
    every thread, for the life of the process: 43 KB of tracebacks in a routine
    log, burying the lines someone actually needed to read.
    #
    A watchdog that keeps barking after the danger has passed trains you to stop
    listening to it, which is worse than not having one. It exists to catch a
    boot that never finishes; once uvicorn is serving, the boot finished.
    """
    try:
        import faulthandler
        faulthandler.cancel_dump_traceback_later()
        print("[boot] hang watchdog disarmed - backend is serving", flush=True)
    except Exception:
        pass
from pathlib import Path

BASE_DIR    = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR / "backend"
REQ_FILE    = BACKEND_DIR / "requirements.txt"


# ---------------------------------------------------------------------------
# v2.13.17 -- OPTIONAL EXTRAS DIRECTORY.
#
# Some capabilities are not bundled because they are enormous, not because they
# are unsupported: speech recognition alone pulls openai-whisper and torch,
# which is gigabytes. Leaving them out is right. Leaving them UNADDABLE is not.
#
# On a Store install there was previously nowhere for them to go. The embeddable
# interpreter builds sys.path exclusively from python*._pth, every entry of
# which lives inside the package, and the package is read-only. `pip --user`
# does not help (embeddable ignores user site) and PYTHONPATH does not help
# (._pth overrides it). So the honest answer to "can I add voice later?" was no
# -- for want of one writable directory.
#
# This is that directory. It sits in sage_data, which is user-owned and
# writable in every install shape, and it goes on sys.path FIRST so an extra
# can also override a bundled package if someone needs a newer one.
#
#     pip install --target "<sage_data>\pylibs" openai-whisper sounddevice
#
# Then restart. Same shape as Ollama and ComfyUI: the user installs it, into
# their own space, and VeridianAI detects it. We never download anything, which
# is also what keeps this inside Store policy -- the app is not fetching code,
# the user is.
#
# CAVEAT worth stating plainly: native wheels must match this interpreter
# (CPython 3.12, win_amd64). Installing with a mismatched Python produces
# imports that fail at runtime rather than at install time.
# ---------------------------------------------------------------------------
def _extras_dir() -> Path:
    env = (os.environ.get("VERIDIAN_DATA_DIR") or "").strip()
    if env:
        return Path(env) / "pylibs"
    try:
        sys.path.insert(0, str(BACKEND_DIR))
        from state_paths import data_dir  # type: ignore
        return Path(data_dir()) / "pylibs"
    except Exception:
        return BASE_DIR.parent / "sage_data" / "pylibs"


def _enable_extras() -> None:
    try:
        d = _extras_dir()
        d.mkdir(parents=True, exist_ok=True)
        s = str(d)
        if s not in sys.path:
            sys.path.insert(0, s)
        # Announced, not silent: if an extra shadows a bundled package we want
        # that visible in the log rather than discovered by its symptoms.
        try:
            n = sum(1 for _ in d.iterdir())
        except Exception:
            n = 0
        # Always name the interpreter, not just the folder.
        #
        # The portable launcher resolves Python through `py`, which selects the
        # NEWEST installed version. Install a new Python and every package you
        # pip-installed under the old one silently disappears from the app's
        # view -- nothing was uninstalled, a different interpreter is simply
        # looking somewhere else. That failure presents as "the app broke when
        # I updated it", and without this line there is nothing to contradict
        # that reading.
        #
        # Binary wheels are ABI-specific too, so extras installed for 3.12 will
        # not import under 3.13+ even from this folder. Printing the version
        # every boot makes the mismatch a one-line diagnosis.
        ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        print(f"[extras] python {ver} at {sys.executable}", flush=True)
        if n:
            print(f"[extras] {n} item(s) on sys.path from {s}", flush=True)
    except Exception as e:
        print(f"[extras] optional extras dir unavailable: {e}", flush=True)


_enable_extras()


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


def _app_version() -> str:
    """The version, READ rather than hardcoded.

    This banner said "v2.13" while the app was at 2.14.0, because it was a
    fourth place the version lived and _bump_version.py did not know about it.
    A version printed at startup is the one users quote back when reporting a
    bug, so a stale one costs real time -- and this week a stale version string
    in a comment sent two people looking for files that were not missing.

    electron/package.json is the single source of truth: _bump_version.py owns
    it and it becomes the MSIX package version. Reading it means this line can
    never disagree with the build again.
    """
    try:
        import json
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "electron", "package.json"),
                  encoding="utf-8") as fh:
            v = json.load(fh).get("version", "")
        return ("v" + v) if v else ""
    except Exception:
        # Never fail a launch over a banner. An empty version is honest;
        # a wrong one is not.
        return ""


def print_banner():
    ver = _app_version()
    line = "V E R I D I A N   A I" + (("   " + ver) if ver else "")
    print("""
  +-------------------------------------------+
  |{0}|
  |       Local AI Inference + Toga           |
  +-------------------------------------------+
""".format(line.center(43)))


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
    # "user didn't pass --port" -- only the latter falls through to config.
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
        # Imports are the part that can hang -- that is what the watchdog was
        # armed for, and reaching this line means it did not. Disarm before
        # uvicorn.run() blocks, or it barks every 60s for the whole session.
        cancel_hang_dump()
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
