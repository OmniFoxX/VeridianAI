#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config.py — OracleAI centralized path & port configuration
------------------------------------------------------------
All paths are relative to project root — works on any machine,
any drive, any OS. sage_data is intentionally placed OUTSIDE the
project folder to prevent daemon/watchdog conflicts.

Directory structure created on first run:
    <project_root>/
        OracleAI/           ← project lives here
            backend/
                config.py   ← this file
                main.py
                sage_daemon.py
                ...
        sage_data/          ← auto-created OUTSIDE project
            memory_log/
            logs/
            uploads/
            models/
            snapshots/      ← read-only codebase snapshots for introspection
"""

from pathlib import Path
import os

# --- Oracle backend selection --------------------------
USE_OLLAMA_FOR_ORACLE = os.environ.get("USE_OLLAMA_FOR_ORACLE", "true").lower() == "true"

# --- Root Paths ---------------------------
BACKEND_DIR  = Path(__file__).resolve().parent        # .../OracleAI/backend/
PROJECT_DIR  = BACKEND_DIR.parent                     # .../OracleAI/
DATA_DIR     = PROJECT_DIR.parent / "sage_data"       # .../sage_data/  (OUTSIDE project)

# --- Data Subdirectories ----------------------
MEMORY_DIR     = DATA_DIR / "memory_log"
LOG_DIR        = DATA_DIR / "logs"
UPLOAD_DIR     = DATA_DIR / "uploads"
MODEL_DIR      = DATA_DIR / "models"
SNAPSHOT_DIR   = DATA_DIR / "snapshots"   # safe read-only codebase copies
PROCEDURAL_DIR = DATA_DIR / "procedural_memory"   # v2.1.4: procedural knowledge base
PROMPTS_DIR    = DATA_DIR / "prompts"             # v2.2 #68: user-tunable prompt files (system.txt etc.)

# --- Fernet key (v2.1.4) ------------------
# Symmetric encryption key for MemoryLogger content fields. Generated on first
# boot and persisted here. Lives beside the backend because it is tied to this
# install; losing the file makes pre-existing encrypted entries unreadable
# (they remain tamper-evident via the hash chain, just not decryptable).
# Back it up with the rest of the install if long-term log readability matters.
# v2.9 hardening: the key now lives in sage_data (OUTSIDE the project), migrated
# out of backend/ on first boot, so a copied/synced project folder carries no live
# key. (It thereby co-locates with memory_log in sage_data -- an accepted trade vs.
# the realistic project-leak threat.)
from secret_locator import resolve_secret_file as _resolve_secret_file
FERNET_KEY_FILE = _resolve_secret_file(".fernet_key", DATA_DIR, BACKEND_DIR)

# --- Log Files ---------------------------
DAEMON_LOG   = LOG_DIR / "sage_daemon.log"
ENGINE_LOG   = LOG_DIR / "sage_engine.log"
APP_LOG      = LOG_DIR / "oracle.log"

# --- Model Paths (relative to MODEL_DIR -- works on any machine) ----------------
# v2.2 (2026-05-29): the hardcoded default filenames previously embedded
# here ("all_hands_openhands..." and "qwen2.5_coder_1.5b_base.gguf")
# leaked Dev-specific install state into every distribution copy --
# users without those exact files saw "model not found" errors pointing
# at filenames they had never heard of. start.bat is now the single
# source of truth for the filenames (via SAGE_MODEL_FILE and
# DAEMON_MODEL_FILE env vars). If the env var is unset (someone imported
# config.py outside the launcher), MODEL_SAGE / MODEL_DAEMON is None and
# callers must handle that -- see build_llama_server_command which
# guards against it explicitly.
_sage_file   = os.environ.get("SAGE_MODEL_FILE")
_daemon_file = os.environ.get("DAEMON_MODEL_FILE")
MODEL_SAGE   = (MODEL_DIR / _sage_file)   if _sage_file   else None
MODEL_DAEMON = (MODEL_DIR / _daemon_file) if _daemon_file else None

# --- Default Model Names (for Ollama instances) ----------------
MODEL_ORACLE_NAME  = os.environ.get("ORACLE_MODEL",  "")
MODEL_SAGE_NAME    = os.environ.get("SAGE_MODEL",    "")
MODEL_DAEMON_NAME  = os.environ.get("DAEMON_MODEL",  "")

# --- Ports -----------------------------
# #68 loose-end fix (Phase E completion): ports are now read from
# config.json via OracleConfig, with env-var override and a hardcoded
# fallback. Precedence (highest to lowest):
#   1. environment variable          -- ephemeral override for advanced users / CI
#   2. config.json network.ports.*   -- the canonical user setting
#   3. dataclass default             -- conventional fallback (8000, 11434, etc.)
#
# Reading config.json at import time has the same edge-case behavior as
# OracleConfig.load itself: missing file -> defaults, malformed file ->
# warning to stderr + defaults, v1 flat file -> auto-converted in-memory
# (v1 didn't have a network section, so defaults apply for ports). No
# circular import risk: config_store.py does not import this module.
def _resolve_port(env_name: str, cfg_value: int, fallback: int) -> int:
    """Resolve a port with env > config > fallback precedence. Any of:
    env var unset / config value None / parse error all fall through to
    the next tier. Never raises — a bad port shouldn't prevent boot."""
    env_raw = os.environ.get(env_name)
    if env_raw:
        try:
            return int(env_raw)
        except (TypeError, ValueError):
            pass  # fall through
    if isinstance(cfg_value, int) and 1 <= cfg_value <= 65535:
        return cfg_value
    return fallback

try:
    from config_store import OracleConfig as _OracleConfig
    _user_cfg = _OracleConfig.load(PROJECT_DIR / "config.json")
    _ports = _user_cfg.network.ports
    _handoff_cfg = _user_cfg.handoff_security
except Exception as _e:
    # If config_store can't load for any reason, every _resolve_port call
    # below falls through to the hardcoded fallback. Same effective
    # behavior as the original env-only code.
    _ports = None
    _handoff_cfg = None
    print(f"[config] WARN: could not load user config for port resolution: {_e}", flush=True)

PORT_APP           = _resolve_port("ORACLE_APP_PORT",    getattr(_ports, "app",           None) if _ports else None, 8000)   # FastAPI/uvicorn
PORT_IPC_BROWSER   = _resolve_port("ORACLE_IPC_PORT",    getattr(_ports, "ipc_browser",   None) if _ports else None, 9999)   # privacy browser IPC
PORT_DAEMON        = _resolve_port("ORACLE_DAEMON_PORT", getattr(_ports, "sage_daemon",   None) if _ports else None, 9998)   # sage_daemon
PORT_OLLAMA_ORACLE = _resolve_port("OLLAMA_ORACLE_PORT", getattr(_ports, "ollama_oracle", None) if _ports else None, 11434)  # Oracle Ollama
PORT_LLAMA_SAGE    = _resolve_port("LLAMA_SAGE_PORT",    getattr(_ports, "llama_sage",    None) if _ports else None, 11435)  # Sage llama-server
PORT_LLAMA_DAEMON  = _resolve_port("LLAMA_DAEMON_PORT",  getattr(_ports, "llama_daemon",  None) if _ports else None, 11436)  # Daemon llama-server
PORT_LLAMA_EMBED   = _resolve_port("LLAMA_EMBED_PORT",   getattr(_ports, "llama_embed",   None) if _ports else None, 11437)  # nomic-embed llama-server (reserved)

# --- Handoff hardening knobs (#69) -- config.json single source of truth -----
HANDOFF_CADENCE_MAX         = getattr(_handoff_cfg, "cadence_max",         5)     if _handoff_cfg else 5
HANDOFF_CADENCE_WINDOW_SEC  = getattr(_handoff_cfg, "cadence_window_sec",  300.0) if _handoff_cfg else 300.0
HANDOFF_REQUIRE_SOCKET_AUTH = getattr(_handoff_cfg, "require_socket_auth", False) if _handoff_cfg else False
HANDOFF_VERIFY_RESPAWN_HASH = getattr(_handoff_cfg, "verify_respawn_hash", True)  if _handoff_cfg else True
HANDOFF_STRICT_RESPAWN      = getattr(_handoff_cfg, "strict_respawn",      False) if _handoff_cfg else False

# --- Server URLs (built from ports) ----------------------
OLLAMA_ORACLE_URL  = f"http://127.0.0.1:{PORT_OLLAMA_ORACLE}"
LLAMA_SAGE_URL    = f"http://127.0.0.1:{PORT_LLAMA_SAGE}"
LLAMA_DAEMON_URL  = f"http://127.0.0.1:{PORT_LLAMA_DAEMON}"
LLAMA_EMBED_URL   = f"http://127.0.0.1:{PORT_LLAMA_EMBED}"

# --- llama-server executable (ships with OracleAI backend) ---------------
LLAMA_SERVER_EXE   = BACKEND_DIR / "llama-server.exe"

# --- Per-tier context sizes (Phase 1D) ------------------------
# Each llama-server tier has a maximum context size (the model's trained window)
# and a default context size (our chosen sensible default). The UI exposes a
# single global `n_ctx` which scales Sage up/down, clamped to SAGE_CTX_MAX.
# Daemon is INTENTIONALLY fixed at its default regardless of the global slider,
# because daemon work is mechanical and small -- wasting RAM there gives no
# benefit to the user.
#
# These defaults must match the ones set in start.bat so first-boot consistency
# is guaranteed. The launcher passes the values through environment variables;
# if not set, these defaults take effect.

SAGE_CTX_MAX       = 10000000   # nemotron-3-super:120b max trained context window 256k (1M-Max)
SAGE_CTX_DEFAULT   = int(os.environ.get("SAGE_CTX_SIZE",   32768))
DAEMON_CTX_MAX     = 8192   # gemma4:31b max trained context window 128k
DAEMON_CTX_DEFAULT = int(os.environ.get("DAEMON_CTX_SIZE", 4096))



def compute_sage_ctx(global_n_ctx=None):
    """Return Sage tier ctx_size. Scales with global_n_ctx, capped at SAGE_CTX_MAX.
    If global_n_ctx is None or falsy, returns SAGE_CTX_DEFAULT."""
    if not global_n_ctx:
        return SAGE_CTX_DEFAULT
    try:
        return min(int(global_n_ctx), SAGE_CTX_MAX)
    except (TypeError, ValueError):
        return SAGE_CTX_DEFAULT


def compute_daemon_ctx(global_n_ctx=None):
    """Return Daemon tier ctx_size. Fixed at DAEMON_CTX_DEFAULT regardless of
    global_n_ctx -- daemon work is small and consistent, scaling it up wastes RAM.
    global_n_ctx is accepted for interface symmetry but ignored."""
    return DAEMON_CTX_DEFAULT


# --- llama-server command builder (Phase 1D) -------------------
# Single source of truth for the llama-server command line. Consumed by both
# start.bat (via a Python one-liner in Step 3) and the FastAPI tier restart
# endpoints (Step 4). Returns a list suitable for subprocess.Popen().

def build_llama_server_command(tier, ctx_size=None):
    """Build argv for spawning a llama-server for a given tier.

    tier:     "sage" or "daemon"
    ctx_size: override context size; if None, uses the tier's current default

    Returns a list of strings, ready for subprocess.Popen(..., shell=False).
    """
    tier = tier.lower().strip()
    if tier == "sage":
        model = MODEL_SAGE
        port  = PORT_LLAMA_SAGE
        ctx   = ctx_size if ctx_size is not None else SAGE_CTX_DEFAULT
    elif tier == "daemon":
        model = MODEL_DAEMON
        port  = PORT_LLAMA_DAEMON
        ctx   = ctx_size if ctx_size is not None else DAEMON_CTX_DEFAULT
    else:
        raise ValueError(f"Unknown tier: {tier!r}. Expected 'sage' or 'daemon'.")

    # v2.2 guard: model is None if the relevant env var was unset (e.g.,
    # someone called this from a Python REPL instead of going through
    # start.bat). Refuse to build a command with a None model rather than
    # spawn a llama-server pointed at a nonsense path.
    if model is None:
        env_var = "SAGE_MODEL_FILE" if tier == "sage" else "DAEMON_MODEL_FILE"
        raise RuntimeError(
            f"Cannot build llama-server command for tier {tier!r}: "
            f"{env_var} is unset, so the model path is undefined. "
            f"Set {env_var} (start.bat does this normally) or pass the "
            f"model path explicitly."
        )

    return [
        str(LLAMA_SERVER_EXE),
        "-m",         str(model),
        "--host",     "127.0.0.1",
        "--port",     str(port),
        "--ctx-size", str(ctx),
        "-ngl",       "0",
        "--metrics",
    ]

# --- Auto-create all data directories on import ----------
_REQUIRED_DIRS = [
    MEMORY_DIR,
    LOG_DIR,
    UPLOAD_DIR,
    MODEL_DIR,
    SNAPSHOT_DIR,
    PROCEDURAL_DIR,
    PROMPTS_DIR,
]

for _dir in _REQUIRED_DIRS:
    _dir.mkdir(parents=True, exist_ok=True)
