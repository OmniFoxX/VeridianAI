"""
mcp_handlers.py -- shared MCP (Model Context Protocol) tool dispatch
====================================================================

v2.3 (2026-06-03): exposes Sage's tool tags to external MCP clients
(Continue.dev, Claude Desktop, etc.) via a single shared dispatch
module used by both the HTTP route (main.py /mcp/v1/jsonrpc) and the
stdio entry (mcp_server.py).

Every Sage tool that's available via her tool-tag system is exposed
here: search, search_general, search_memory, weather, browse,
web_search, code, save_file, verify_file, recall, remember,
remember_fail. Calls flow through the SAME underlying functions her
agentic loop uses, so MCP invocations get the same Trinity treatment
(Fernet encryption on memory writes, hash-chain witnessing on
successful procedures, procedural KB participation).

DISTRIBUTION-SAFE DESIGN
------------------------
- No hardcoded paths -- imports rely on backend.config / sage_engine.
- No persona prompt injection at this layer -- callers (HTTP or stdio)
  decide whether to wrap responses in Sage's voice. MCP tool calls
  return raw tool results; conversation framing is the chat-completion
  endpoint's concern, not MCP's.
- Source-tagged provenance -- every procedural memory write made via
  MCP gets metadata `{"source": "mcp"}` so it can be filtered or
  cleared later without touching chat-side KB entries.

PROTOCOL SHAPE (MCP / JSON-RPC 2.0)
-----------------------------------
This module exposes three high-level callables consumed by both
transports:

    list_tools() -> list[dict]
        Returns MCP tool descriptors. Each dict has:
            name: str
            description: str
            inputSchema: JSON-Schema dict describing args

    call_tool(name: str, arguments: dict) -> dict
        Invokes the named tool with arguments. Returns:
            {"content": [{"type": "text", "text": "..."}],
             "isError": bool}

    server_info() -> dict
        Returns MCP initialize handshake response.

Errors never raise to the caller; they are translated into MCP-shaped
error envelopes so a misbehaving client cannot crash the server.
"""
from __future__ import annotations

import json
import traceback
from typing import Any, Callable, Dict, List

# ---------------------------------------------------------------------------
# Lazy imports of the Sage substrate. Done at call time (not module load) so
# importing mcp_handlers from a stdio subprocess does not eagerly pull in the
# entire main.py initialisation chain. Each tool function imports what it
# needs the first time it runs.
# ---------------------------------------------------------------------------

def _sage():
    import sage_engine  # noqa: WPS433 -- intentional lazy import
    return sage_engine


def _procedural_memory():
    """Return the active ProceduralMemory singleton.

    Prefers main.procedural if main has been imported (chat / HTTP path).
    Falls back to constructing a fresh instance bound to config-resolved
    paths (stdio path -- main has not been imported).
    """
    try:
        import main as _main  # noqa: WPS433
        if getattr(_main, "procedural", None) is not None:
            return _main.procedural
    except Exception:
        pass
    # Fallback: construct from config (stdio subprocess scenario)
    from procedural_memory import ProceduralMemory
    from memory_logger_surprise import MemoryLogger
    from config import PROCEDURAL_DIR, MEMORY_DIR
    logger = MemoryLogger(storage_dir=str(MEMORY_DIR), baseline_temp=0.5)
    return ProceduralMemory(
        storage_dir=str(PROCEDURAL_DIR),
        memory_logger=logger,
    )


# ---------------------------------------------------------------------------
# Server identity (returned from initialize handshake)
# ---------------------------------------------------------------------------

MCP_SERVER_NAME = "oracleai-sage"
MCP_SERVER_VERSION = "2.7.1"
MCP_PROTOCOL_VERSION = "2025-03-26"  # MCP spec version this implementation targets


def server_info() -> Dict[str, Any]:
    """Response payload for the MCP `initialize` handshake."""
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {
            "tools": {"listChanged": False},
            "logging": {},
        },
        "serverInfo": {
            "name": MCP_SERVER_NAME,
            "version": MCP_SERVER_VERSION,
        },
    }


# ---------------------------------------------------------------------------
# Tool descriptors -- the MCP tools/list response
# ---------------------------------------------------------------------------

TOOL_DESCRIPTORS: List[Dict[str, Any]] = [
    {
        "name": "search",
        "description": (
            "Search current news via Tavily. Returns a brief synthesis of "
            "the top results. Use for time-sensitive / current-events "
            "queries. Hard-capped by Tavily budget."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "num_results": {
                    "type": "integer", "default": 5, "minimum": 1, "maximum": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_general",
        "description": (
            "Search general web information via Tavily (not specifically "
            "news). Good for weather, travel facts, definitions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "num_results": {
                    "type": "integer", "default": 5, "minimum": 1, "maximum": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_memory",
        "description": (
            "Keyword-search past OracleAI conversation archives. Memory "
            "READ only -- no chain writes. Returns matching archive "
            "excerpts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "weather",
        "description": (
            "Get current weather for a city. Returns conditions, "
            "temperature, and a brief forecast. Use this rather than "
            "guessing weather from training data."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City name, optionally with state/country",
                },
            },
            "required": ["location"],
        },
    },
    {
        "name": "browse",
        "description": (
            "Fetch and extract text from a URL via the headless browser. "
            "Returns cleaned page text suitable for LLM context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_chars": {
                    "type": "integer",
                    "default": 0,
                    "description": "0 = no cap",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "DuckDuckGo web search via the headless browser. "
            "No Tavily budget consumed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "num_results": {
                    "type": "integer", "default": 5, "minimum": 1, "maximum": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "code",
        "description": (
            "Execute Python in OracleAI's sandboxed subprocess. "
            "Returns labelled stdout + stderr. UTF-8 throughout. "
            "DOWNLOADS_DIR / BASE_DIR are exposed as variables. "
            "Pass RAW Python only -- no markdown fences, no language tag."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "timeout": {
                    "type": "integer", "default": 56000, "minimum": 1,
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": "save_file",
        "description": (
            "Save a file to the user's downloads folder. Filename and "
            "content are separate arguments (unlike the tag form which "
            "uses pipe-delimited content)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["filename", "content"],
        },
    },
    {
        "name": "verify_file",
        "description": (
            "Verify that a file exists and (if .py) AST-parses cleanly. "
            "Returns success / failure with diagnostic text. Use this "
            "after every save to confirm the write actually happened."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "recall",
        "description": (
            "Look up a procedural-memory entry by key (fuzzy match). "
            "Returns the stored procedure or 'not found'. Use to check "
            "whether OracleAI has learned a pattern for this task before."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search key or fuzzy phrase",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Record a SUCCESSFUL procedure in procedural memory. Chain-"
            "witnessed via the Fernet+SHA3 hash chain. Use for insights "
            "or patterns you want OracleAI to recall in future sessions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Short searchable slug",
                },
                "value": {
                    "type": "string",
                    "description": "The lesson, heuristic, or pattern body",
                },
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "remember_fail",
        "description": (
            "Record an UNSUCCESSFUL approach in procedural memory. "
            "Local-only (NOT chain-witnessed). Use for dead-ends you "
            "want OracleAI to avoid in future."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["key", "reason"],
        },
    },
]


def list_tools() -> List[Dict[str, Any]]:
    """MCP `tools/list` response payload."""
    return list(TOOL_DESCRIPTORS)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _result_text(text: str, is_error: bool = False) -> Dict[str, Any]:
    """MCP tools/call response envelope. Text content only for now."""
    return {
        "content": [{"type": "text", "text": text}],
        "isError": bool(is_error),
    }


def _tool_search(args: Dict[str, Any]) -> Dict[str, Any]:
    q = str(args.get("query", "")).strip()
    n = int(args.get("num_results", 5))
    if not q:
        return _result_text("[error] empty query", is_error=True)
    out = _sage().web_search(q, num_results=n, search_type="news")
    return _result_text(str(out))


def _tool_search_general(args: Dict[str, Any]) -> Dict[str, Any]:
    q = str(args.get("query", "")).strip()
    n = int(args.get("num_results", 5))
    if not q:
        return _result_text("[error] empty query", is_error=True)
    out = _sage().web_search(q, num_results=n, search_type="general")
    return _result_text(str(out))


def _tool_search_memory(args: Dict[str, Any]) -> Dict[str, Any]:
    q = str(args.get("query", "")).strip()
    if not q:
        return _result_text("[error] empty query", is_error=True)
    archives = _sage().search_all_archives(q)
    if not archives:
        return _result_text("[no archive matches]")
    # archives are dicts; summarise as JSON
    return _result_text(json.dumps(archives[:5], default=str, indent=2))


def _tool_weather(args: Dict[str, Any]) -> Dict[str, Any]:
    loc = str(args.get("location", "")).strip()
    if not loc:
        return _result_text("[error] empty location", is_error=True)
    out = _sage().get_weather(loc)
    return _result_text(str(out))


def _tool_browse(args: Dict[str, Any]) -> Dict[str, Any]:
    url = str(args.get("url", "")).strip()
    max_chars = int(args.get("max_chars", 0))
    if not url:
        return _result_text("[error] empty url", is_error=True)
    out = _sage().browse_url(url, max_chars=max_chars)
    return _result_text(str(out))


def _tool_web_search(args: Dict[str, Any]) -> Dict[str, Any]:
    q = str(args.get("query", "")).strip()
    n = int(args.get("num_results", 5))
    if not q:
        return _result_text("[error] empty query", is_error=True)
    out = _sage().web_search_browser(q, num_results=n)
    return _result_text(str(out))


def _tool_code(args: Dict[str, Any]) -> Dict[str, Any]:
    code = str(args.get("code", ""))
    timeout = int(args.get("timeout", 56000))
    if not code.strip():
        return _result_text("[error] empty code", is_error=True)
    out = _sage().execute_python(code, timeout=timeout)
    return _result_text(str(out))


def _tool_save_file(args: Dict[str, Any]) -> Dict[str, Any]:
    fname = str(args.get("filename", "")).strip()
    content = args.get("content", "")
    if not fname:
        return _result_text("[error] empty filename", is_error=True)
    if not isinstance(content, str):
        content = str(content)
    result = _sage().save_to_downloads(fname, content)
    if isinstance(result, dict) and result.get("success"):
        return _result_text(
            f"Saved {result.get('filename')} to downloads "
            f"({result.get('size')} bytes). "
            f"Use verify_file to confirm."
        )
    return _result_text(
        f"[save failed] {result.get('error', 'unknown') if isinstance(result, dict) else result!r}",
        is_error=True,
    )


def _tool_verify_file(args: Dict[str, Any]) -> Dict[str, Any]:
    path = str(args.get("path", "")).strip()
    if not path:
        return _result_text("[error] empty path", is_error=True)
    out = _sage().verify_written_file(path)
    is_err = "VERIFY FAILED" in str(out)
    return _result_text(str(out), is_error=is_err)


def _tool_recall(args: Dict[str, Any]) -> Dict[str, Any]:
    q = str(args.get("query", "")).strip()
    if not q:
        return _result_text("[error] empty query", is_error=True)
    pm = _procedural_memory()
    # Try exact key first
    entry = pm.get_procedure(q, category="successful")
    if entry is None:
        entry = pm.get_procedure(q, category="unsuccessful")
    if entry is None:
        # Fuzzy: list keys and find substring matches
        succ = pm.list_procedures("successful")
        fails = pm.list_procedures("unsuccessful")
        matches = [k for k in (succ + fails) if q.lower() in k.lower()]
        if not matches:
            return _result_text(f"[not found] no procedural entry matching {q!r}")
        return _result_text(
            "Possible matches (use one as exact key):\n" + "\n".join(matches[:10])
        )
    return _result_text(json.dumps(entry, default=str, indent=2))


def _tool_remember(args: Dict[str, Any]) -> Dict[str, Any]:
    key = str(args.get("key", "")).strip()
    value = args.get("value", "")
    if not key:
        return _result_text("[error] empty key", is_error=True)
    pm = _procedural_memory()
    try:
        pm.add_procedure(
            key=key,
            value=value,
            success=True,
            metadata={"source": "mcp"},
        )
        return _result_text(
            f"Recorded successful procedure {key!r} "
            f"(chain-witnessed; source=mcp)."
        )
    except Exception as e:
        return _result_text(
            f"[remember failed] {type(e).__name__}: {e}", is_error=True,
        )


def _tool_remember_fail(args: Dict[str, Any]) -> Dict[str, Any]:
    key = str(args.get("key", "")).strip()
    reason = args.get("reason", "")
    if not key:
        return _result_text("[error] empty key", is_error=True)
    pm = _procedural_memory()
    try:
        pm.add_procedure(
            key=key,
            value=reason,
            success=False,
            metadata={"source": "mcp"},
        )
        return _result_text(
            f"Recorded dead-end {key!r} "
            f"(local-only, not chain-witnessed; source=mcp)."
        )
    except Exception as e:
        return _result_text(
            f"[remember_fail failed] {type(e).__name__}: {e}", is_error=True,
        )


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_DISPATCH: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "search":          _tool_search,
    "search_general":  _tool_search_general,
    "search_memory":   _tool_search_memory,
    "weather":         _tool_weather,
    "browse":          _tool_browse,
    "web_search":      _tool_web_search,
    "code":            _tool_code,
    "save_file":       _tool_save_file,
    "verify_file":     _tool_verify_file,
    "recall":          _tool_recall,
    "remember":        _tool_remember,
    "remember_fail":   _tool_remember_fail,
}


def call_tool(name: str, arguments: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """MCP `tools/call` dispatcher.

    Returns the MCP result envelope. Never raises -- exceptions inside
    tools are caught and translated to error envelopes so a misbehaving
    tool cannot crash the server.
    """
    arguments = arguments or {}
    fn = _DISPATCH.get(name)
    if fn is None:
        return _result_text(
            f"[unknown tool] {name!r}. "
            f"Available: {sorted(_DISPATCH.keys())}",
            is_error=True,
        )
    try:
        return fn(arguments)
    except Exception as e:
        tb = traceback.format_exc(limit=3)
        return _result_text(
            f"[tool '{name}' raised] {type(e).__name__}: {e}\n{tb}",
            is_error=True,
        )


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 envelope helpers -- used by both HTTP route and stdio entry
# ---------------------------------------------------------------------------

def handle_jsonrpc(request: Dict[str, Any]) -> Dict[str, Any] | None:
    """Process one JSON-RPC 2.0 request and return the response envelope.

    Returns None if the request is a notification (no `id`), per JSON-RPC.
    Never raises -- malformed requests get an error envelope back.
    """
    req_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}

    def _ok(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _err(code, message, data=None):
        env = {
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message},
        }
        if data is not None:
            env["error"]["data"] = data
        return env

    if not method or not isinstance(method, str):
        return _err(-32600, "Invalid Request: missing method")

    try:
        if method == "initialize":
            return _ok(server_info())
        if method == "initialized" or method == "notifications/initialized":
            # MCP notification, no response expected
            return None if req_id is None else _ok({})
        if method == "tools/list":
            return _ok({"tools": list_tools()})
        if method == "tools/call":
            tname = params.get("name")
            targs = params.get("arguments") or {}
            return _ok(call_tool(tname, targs))
        if method == "ping":
            return _ok({})
        return _err(-32601, f"Method not found: {method}")
    except Exception as e:
        return _err(-32603, f"Internal error: {type(e).__name__}: {e}")
