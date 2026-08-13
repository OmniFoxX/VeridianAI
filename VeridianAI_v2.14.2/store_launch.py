#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
store_launch.py -- the Microsoft Store build's launcher.

WHY THIS EXISTS
---------------
The portable build launches through start.bat, which sets up the tier
environment, runs tier_launcher.py, and then hands off to start.py.

An MSIX package cannot do that. Windows propagates PACKAGE IDENTITY only to
child processes that live INSIDE the package; cmd.exe lives in System32, so it
starts without identity and the WindowsApps ACL then refuses it execute access
to anything in the package. Every python invocation from start.bat died with
"Access is denied" (see docs/README/TROUBLESHOOTING.md).

Electron therefore spawns THIS file with the bundled python.exe directly.
Identity flows: VeridianAI.exe (in package) -> python.exe (in package) ->
llama-server.exe (in package). Each link stays inside, so each keeps the rights
it needs.

WHAT IT DOES
------------
The same job as start.bat, minus the shell:

  1. Resolve paths and ports the way start.bat does.
  2. Locate models -- sage_data/models first, then the bundled fallback.
  3. Launch the inference tiers via tier_launcher.py.
  4. Hand off to start.py, which runs uvicorn.

Deliberately NOT done here:
  * Ollama. It is not in the package and cannot be installed at runtime, so the
    Oracle tier is simply absent in a Store install. Toga and Daemon run on the
    BUNDLED llama-server.exe, which is all this build needs.
  * Readiness probes. Electron already polls /api/health; a second timing loop
    only delays the window.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"

# Console encoding. Windows hands a cp1252 stdout to a bare python.exe, so any
# non-ASCII a child prints comes back through the log pipe as mojibake -- the
# 2.12.16 boot log rendered "--" as "i(1/2)" and "->" as "a+'". Unreadable logs
# do not get read, and this one is the primary diagnostic for a Store install
# nobody can open the install directory of.
#
# Forced here AND inherited by every child (build_env copies os.environ), with
# boot-path strings kept ASCII regardless. Two cheap guards rather than one,
# because the failure is silent and only visible after the fact.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def _log(msg: str) -> None:
    print(f"[store_launch] {msg}", flush=True)


# --------------------------------------------------------------------------
# Paths and ports -- mirrors start.bat's tunables block.
# --------------------------------------------------------------------------
def _data_dir() -> Path:
    """Where sage_data lives. Electron passes VERIDIAN_DATA_DIR; state_paths
    applies the identical rule if it did not."""
    env = os.environ.get("VERIDIAN_DATA_DIR", "").strip()
    if env:
        return Path(env)
    sys.path.insert(0, str(BACKEND))
    try:
        from state_paths import data_dir  # type: ignore
        return data_dir()
    except Exception:
        return ROOT.parent / "sage_data"


def _resolve_model(models_dir: Path, filenames) -> tuple[str, bool]:
    """sage_data/models first (user-supplied), then bundled_models (shipped).

    `filenames` may be one name or an ordered list of candidates, best first.
    The list exists so a model can be UPGRADED without a flag day: ship the
    preferred file alongside the old one and it takes over on next boot; ship
    neither and the tier skips cleanly. Nothing is ever left in a state where
    the code names a file that is not there yet.

    Returns (path_or_empty, present). An empty path tells tier_launcher to skip
    that tier, which it handles cleanly.
    """
    if isinstance(filenames, str):
        filenames = [filenames]
    filenames = [f for f in (filenames or []) if f]
    if not filenames:
        return "", False

    # User copies win over bundled ones for EVERY candidate before falling back
    # to bundled -- a user who supplied their own preferred model should not be
    # overridden by a bundled second choice.
    for where, base in (("user", models_dir), ("bundled", ROOT / "bundled_models")):
        for name in filenames:
            p = base / name
            if p.exists():
                _log(f"model found ({where}): {p}")
                return str(p), True

    _log(f"model NOT found: {' / '.join(filenames)} -- that tier will be skipped")
    return "", False


def build_env() -> dict:
    env = dict(os.environ)
    data_dir = _data_dir()
    models_dir = data_dir / "models"
    try:
        models_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _log(f"WARNING: could not create {models_dir}: {e}")

    llama_server = ROOT / "backend" / "llama-server.exe"
    if not llama_server.exists():
        _log(f"WARNING: llama-server.exe missing at {llama_server} -- "
             "Toga and Daemon tiers cannot start")
        llama_server_str = ""
    else:
        llama_server_str = str(llama_server)

    sage_file = os.environ.get("SAGE_MODEL_FILE",
                               "all_hands_openhands_lm_7b_v0_1_Q6_K_L.gguf")
    # Ordered preference, best first. The INSTRUCT build is preferred: the base
    # checkpoint carries an instruct chat template but the base eos_token_id, so
    # it closes each turn with a token llama-server does not stop on and then
    # keeps generating (observed: a greeting followed by five minutes of
    # invented restaurant queries). gguf_probe corrects that at launch, so the
    # base model is safe to run -- but a base coder model is a code-completion
    # engine being asked to hold a conversation, and it shows.
    _daemon_env = os.environ.get("DAEMON_MODEL_FILE", "").strip()
    daemon_file = [_daemon_env] if _daemon_env else [
        "qwen2.5_coder_1.5b_instruct.gguf",
        "qwen2.5_coder_1.5b_base.gguf",
    ]
    # Ordered preference, best first -- same no-flag-day rule as the daemon
    # slot. v2-moe is multilingual and MoE (8 experts, 2 active); v1.5 is
    # English-focused with a 2048 trained context against v2's 512. Both are
    # 768-dimensional, which is exactly why the vector index is tagged with the
    # MODEL and not just the source: nothing about the shape of the output
    # would reveal a swap.
    _embed_env = os.environ.get("EMBED_MODEL_FILE", "").strip()
    embed_file = [_embed_env] if _embed_env else [
        "nomic_embed_text_v2_moe.gguf",
        "nomic_embed_text_latest.gguf",
    ]

    sage_model, _sage_present = _resolve_model(models_dir, sage_file)
    daemon_model, daemon_present = _resolve_model(models_dir, daemon_file)
    embed_model, _embed_present = _resolve_model(models_dir, embed_file)

    # Playwright's bundled Chromium, when it is present.
    #
    # ONLY when present. Setting this to a directory that does not exist does
    # not fall back gracefully -- it tells Playwright "browsers live here",
    # here is empty, and it then refuses to use the system Chrome/Edge it would
    # otherwise have found. A tree built without tools/bundle_playwright.ps1
    # must still be able to browse via an installed browser.
    _pw = ROOT / "playwright-browsers"
    if _pw.is_dir() and any(_pw.iterdir()):
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(_pw)
        _log(f"playwright browsers: bundled ({_pw})")
    else:
        _log("playwright browsers: none bundled -- will use an installed browser")

    env.update({
        "OAI_ROOT":             str(ROOT),
        "PYTHON_CMD":           sys.executable,
        "LLAMA_SERVER":         llama_server_str,
        "SAGE_MODEL":           sage_model,
        "DAEMON_MODEL":         daemon_model,
        "DAEMON_MODEL_PRESENT": "1" if daemon_present else "0",
        "EMBED_MODEL":          embed_model,
        "EMBED_MODEL_FILE":     (embed_file[0] if isinstance(embed_file, list) else embed_file),
        "APP_PORT":             os.environ.get("APP_PORT", "8000"),
        "OLLAMA_ORACLE_PORT":   os.environ.get("OLLAMA_ORACLE_PORT", "11434"),
        "LLAMA_SAGE_PORT":      os.environ.get("LLAMA_SAGE_PORT", "11435"),
        "LLAMA_DAEMON_PORT":    os.environ.get("LLAMA_DAEMON_PORT", "11436"),
        "SAGE_CTX_SIZE":        os.environ.get("SAGE_CTX_SIZE", "16384"),
        "DAEMON_CTX_SIZE":      os.environ.get("DAEMON_CTX_SIZE", "4096"),
        "LLAMA_EMBED_PORT":     os.environ.get("LLAMA_EMBED_PORT", "11437"),
        "EMBED_CTX_SIZE":       os.environ.get("EMBED_CTX_SIZE", "2048"),
        "VERIDIAN_DATA_DIR":    str(data_dir),
        "VERIDIAN_STORE_BUILD": "1",
    })
    return env


# --------------------------------------------------------------------------
def launch_tiers(env: dict) -> None:
    """Run tier_launcher.py. Never fatal: a missing tier degrades the app, it
    does not justify refusing to start the backend (learned the hard way --
    start.bat used to abort the whole launch when a tier was slow)."""
    launcher = BACKEND / "tier_launcher.py"
    if not launcher.exists():
        _log(f"tier_launcher.py not found at {launcher} -- skipping tiers")
        return
    _log("launching inference tiers...")
    try:
        r = subprocess.run([sys.executable, str(launcher)],
                           cwd=str(ROOT), env=env, timeout=120)
        _log(f"tier_launcher exited {r.returncode}")
    except subprocess.TimeoutExpired:
        _log("tier_launcher timed out after 120s -- continuing without it")
    except Exception as e:
        _log(f"tier_launcher failed: {e} -- continuing without it")


def run_backend(env: dict) -> int:
    """Hand off to start.py. Replaces this process so Electron's child handle
    tracks uvicorn directly and its exit code means what it says."""
    port = env.get("APP_PORT", "8000")
    _log(f"starting backend on port {port}")
    cmd = [sys.executable, "-u", str(ROOT / "start.py"),
           "--port", str(port), "--no-browser"]
    return subprocess.call(cmd, cwd=str(ROOT), env=env)


def main() -> int:
    _log(f"root={ROOT}")
    _log(f"python={sys.executable}")
    env = build_env()
    _log(f"data_dir={env['VERIDIAN_DATA_DIR']}")
    launch_tiers(env)
    return run_backend(env)


if __name__ == "__main__":
    sys.exit(main())
