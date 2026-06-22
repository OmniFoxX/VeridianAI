#!/usr/bin/env python3
"""
mcp_server.py -- stdio MCP entry point for OracleAI
====================================================

Standalone subprocess that speaks MCP (Model Context Protocol) over
stdin/stdout. Designed to be launched by an MCP-aware client like
Continue.dev or Claude Desktop using a config such as:

    {
      "mcpServers": {
        "oracleai-sage": {
          "command": "py",
          "args": ["E:\\\\OracleAI_v2.3\\\\backend\\\\mcp_server.py"]
        }
      }
    }

Protocol:
  - JSON-RPC 2.0 framed one message per line (newline-delimited JSON).
  - Each request becomes one response on stdout.
  - Notifications (no id) get no response.
  - All logging goes to stderr (NEVER stdout, which is the protocol
    channel -- writing diagnostic output to stdout would corrupt the
    JSON-RPC stream and confuse the client).

This entrypoint shares dispatch logic with the HTTP MCP route in
main.py via mcp_handlers.py -- the protocol envelope handling lives
there. This file is only the stdio framing.

v2.3 (2026-06-03): initial implementation.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Ensure the backend dir is on sys.path so `import mcp_handlers` works
# whether the script is launched from the project root or from elsewhere.
_BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BACKEND_DIR))

import mcp_handlers  # noqa: E402  (path setup happens above)


def _log(msg: str) -> None:
    """Diagnostic line to stderr. NEVER stdout (protocol channel)."""
    print(f"[mcp_server] {msg}", file=sys.stderr, flush=True)


def _read_request() -> dict | None:
    """Read one JSON-RPC request from stdin.

    Returns the parsed dict, or None on EOF / malformed input. Lines
    that fail to parse are logged to stderr and skipped (we do not
    error-respond to unparseable input because we have no id to attach
    the response to).
    """
    line = sys.stdin.readline()
    if not line:
        return None  # EOF
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError as e:
        _log(f"malformed JSON on stdin (skipped): {e}: line={line[:80]!r}")
        return None


def _write_response(envelope: dict) -> None:
    """Write one JSON-RPC response to stdout."""
    payload = json.dumps(envelope, separators=(",", ":"), default=str)
    sys.stdout.write(payload + "\n")
    sys.stdout.flush()


def main() -> int:
    _log(f"OracleAI MCP server (stdio) starting; pid={os.getpid()}")
    _log(f"protocol={mcp_handlers.MCP_PROTOCOL_VERSION} "
         f"server={mcp_handlers.MCP_SERVER_NAME}/"
         f"{mcp_handlers.MCP_SERVER_VERSION}")
    _log(f"tools exposed: {[t['name'] for t in mcp_handlers.list_tools()]}")

    while True:
        try:
            request = _read_request()
        except KeyboardInterrupt:
            _log("KeyboardInterrupt; exiting")
            return 0
        except Exception as e:
            _log(f"unexpected stdin error: {type(e).__name__}: {e}")
            return 1

        if request is None:
            # EOF or unparseable line; for EOF we exit, for unparseable
            # we already logged and continue.
            if sys.stdin.closed:
                _log("stdin closed; exiting cleanly")
                return 0
            continue

        response = mcp_handlers.handle_jsonrpc(request)
        if response is not None:
            try:
                _write_response(response)
            except Exception as e:
                _log(f"failed to write response: {type(e).__name__}: {e}")
                # Try to send an error envelope referencing the original id
                try:
                    _write_response({
                        "jsonrpc": "2.0",
                        "id": request.get("id"),
                        "error": {
                            "code": -32603,
                            "message": f"response serialization failed: {e}",
                        },
                    })
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
