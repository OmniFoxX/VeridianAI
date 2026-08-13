"""
_tier_config_reader.py -- helper read by start.bat at boot.

Phase 1D Step 3 (original): emit per-tier ctx sizes so start.bat can pass
them to llama-server.

#68 Phase E Step 5 (2026-05-23): rerouted through OracleConfig.load so
n_ctx is read from the v2 nested location (cfg.inference.n_ctx).

#68 Loose-end fix (this commit): also emit network ports so start.bat
and start.py can launch each tier on the user-configured port instead
of hardcoded literals. Closes the second half of audit Bug 2.

Output format (one line, comma-separated):
    SAGE_CTX,DAEMON_CTX,APP_PORT,OLLAMA_ORACLE_PORT,LLAMA_SAGE_PORT,LLAMA_DAEMON_PORT,INFERENCE_BACKEND

start.bat parses with:
    for /f "tokens=1,2,3,4,5,6,7 delims=," %%a in ('python _tier_config_reader.py') do (
        set "SAGE_CTX_SIZE=%%a"
        set "DAEMON_CTX_SIZE=%%b"
        set "APP_PORT=%%c"
        ...
        set "INFERENCE_BACKEND=%%g"
    )

The INFERENCE_BACKEND value (v2.2 addition) lets start.bat skip the
Sage llama-server tier when the user's config says Sage chat is served
by Ollama. Saves ~7 GB RAM on a typical install where backend="ollama"
and the llama-server Sage tier would otherwise sit idle.

If the helper fails (Python missing, config_store import failure, malformed
config.json, etc.), the for /f loop body simply does not execute and
start.bat's tunables-block defaults take effect. No silent broken state.
"""

import sys
from pathlib import Path


def main():
    backend_dir = Path(__file__).resolve().parent
    project_dir = backend_dir.parent
    config_json_path = project_dir / "config.json"

    sys.path.insert(0, str(backend_dir))
    try:
        from config import compute_sage_ctx, compute_daemon_ctx
        from config_store import OracleConfig
    except ImportError as e:
        print(f"ERROR: cannot import config modules: {e}", file=sys.stderr)
        sys.exit(1)

    cfg = OracleConfig.load(config_json_path)

    # ctx sizes — clamped/computed by config.py helpers
    sage_ctx = compute_sage_ctx(cfg.inference.n_ctx)
    daemon_ctx = compute_daemon_ctx(cfg.inference.n_ctx)

    # ports — straight read from the schema (defaults are 8000/11434/etc.
    # at the dataclass level, so missing v1 fields fall through cleanly).
    p = cfg.network.ports

    # v2.2: emit inference.backend so start.bat can skip the Sage llama-
    # server tier when Sage chat is served by Ollama. Defaults to "ollama"
    # (the dataclass default in config_store.py) so a fresh install with
    # no config.json yet picks the lighter startup path.
    backend = (cfg.inference.backend or "ollama").strip().lower() or "ollama"

    print(f"{sage_ctx},{daemon_ctx},{p.app},{p.ollama_oracle},{p.llama_sage},{p.llama_daemon},{backend}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
