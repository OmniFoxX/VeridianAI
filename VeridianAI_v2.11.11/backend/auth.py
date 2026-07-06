#!/usr/bin/env python3
"""
auth.py -- OracleAI bearer-token auth for external-surface endpoints
=====================================================================

v2.3.1 (2026-06-06): closes plain-text token storage gap.
Tokens are now stored as prefix (8 chars, plain) + SHA-256 hash.
The raw token is generated once, shown once, never persisted.

Legacy plain-text entries (v2.3.0 keystores) are still accepted
during verification but emit a warning. Run rotate_api_key.py once
to upgrade all entries to hashed storage.

v2.3.0 (2026-05-31): closes the security gap created by the new
external-surface endpoints (`/v1/chat/completions`, `/v1/models`,
`/mcp/v1/jsonrpc`). Without this module, any process on the user's
machine -- including a browser visiting a malicious site that POSTs
to `localhost:8000` -- could invoke Sage's full toolkit. With it,
every request to the new endpoints must carry a valid bearer token.

THREAT MODEL ADDRESSED
----------------------
1. Browser CSRF: malicious site cannot make authenticated requests
   because browsers do NOT auto-attach Authorization headers (unlike
   cookies). The token is unknowable to an attacker.
2. Cross-process probing on the same machine: any process can hit
   `localhost:8000` but cannot guess the 256-bit random token.
3. Network reachability: orthogonal -- already mitigated by binding
   uvicorn to 127.0.0.1 only. Auth is defense in depth.
4. Keystore exfiltration: hashed storage means a leaked keystore
   file does not directly yield usable tokens.

THREAT MODEL DELIBERATELY NOT ADDRESSED
---------------------------------------
- Filesystem access by other users on the same OS account: anyone
  with read access to `backend/.api_keystore.json` (and the Fernet
  key, the chain log, ...) has already won. This is an OS-level
  concern, solved by filesystem permissions on the user's home
  directory, not by application-layer auth.
- Existing `/api/*` and `/ws/chat` routes: those serve the local
  Electron UI and are NOT touched by this module. Adding bearer
  there would require updating the frontend to manage tokens;
  scope-creep from Todd's "do not break current functionality"
  directive. Their CORS exposure is mitigated by the strict Origin
  check in the new routes alone -- malicious sites cannot ALSO
  successfully CSRF /api/* without same-origin tools that are
  themselves limited.

DESIGN
------
- Persistent keystore at `backend/.api_keystore.json` (FERNET KEY
  SIBLING -- back them up together; see Trinity).
- Tokens are 32-byte URL-safe random strings, prefixed `ora_`.
- Each keystore entry stores:
    prefix    : first 8 chars of raw token (plain, for fast lookup)
    hash      : SHA-256(raw token) hex digest (for verification)
  The raw token is NEVER written to disk.
- Each token carries a SCOPES list. Currently used scopes:
    "*"        : universal (default token gets this)
    "chat:*"   : /v1/chat/completions and /v1/models
    "mcp:*"    : /mcp/v1/jsonrpc (all MCP methods)
- Scope satisfaction: "*" satisfies any required scope; otherwise
  exact-match or prefix-with-wildcard ("chat:*" satisfies "chat:read").
- First-boot: if no keystore exists, generate one default token with
  ["*"] scope and print it ONCE on the console with a copy-paste
  banner. Subsequent boots reuse the existing token silently.
- Defense-in-depth Origin check: if the request carries an Origin
  header AND that origin isn't in ALLOWED_ORIGINS, reject. Requests
  with NO Origin (curl, MCP clients, Continue.dev's stdio bridge)
  bypass the Origin check -- those are not browser-CSRF threats
  because the bearer token alone is sufficient.

PROVENANCE
----------
- This file is part of the Trinity-extended backup set:
    backend/.fernet_key            (encrypts memory chain content)
    backend/.api_keystore.json     (authenticates external requests)
    sage_data/memory_log/memory_chain.log
    sage_data/procedural_memory/procedural.json
- Lose `.api_keystore.json` and all external clients (Continue.dev,
  Claude Desktop) need to be re-keyed. The keystore is NOT shipped
  via `prep_distribution.bat` (per-install secret); distribution
  recipients get a fresh token generated on their first boot.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from fastapi import HTTPException, Request


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parent
from secret_locator import resolve_secret_file as _resolve_secret_file
# v2.9 hardening: bearer-token store migrates out of the project into sage_data.
KEYSTORE_PATH = _resolve_secret_file(
    ".api_keystore.json", _BACKEND_DIR.parent.parent / "sage_data", _BACKEND_DIR)

# Tokens are prefixed so they're visually identifiable as OracleAI tokens
# (mirrors OpenAI's `sk-` convention).
TOKEN_PREFIX = "ora_"
TOKEN_BYTES = 32  # 256 bits -- well past brute-force range

# Origin allowlist for the defense-in-depth check. Requests with NO
# Origin header (server-to-server, curl, stdio clients) bypass this
# check and rely on the bearer token alone. Browser-origin requests
# MUST match one of these.
#
# To extend (e.g., a different IDE accessing OracleAI via web view):
# users can edit the keystore manually -- see _allowed_origins().
ALLOWED_ORIGINS_DEFAULT = (
    "null",                       # file://, custom electron protocols
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "vscode-webview://*",         # Continue.dev's webview origin pattern
)


# ---------------------------------------------------------------------------
# Keystore I/O
# ---------------------------------------------------------------------------

def _new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def _hash_token(raw: str) -> str:
    """One-way SHA-256 hash for at-rest storage."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _make_token_entry(raw: str, scopes: list, label: str) -> dict:
    """Build a hashed keystore entry. The raw token is never stored."""
    return {
        "prefix":     raw[:8],
        "hash":       _hash_token(raw),
        "scopes":     scopes,
        "label":      label,
        "created":    time.strftime("%Y-%m-%dT%H:%M:%S"),
        "last_used":  None,
    }


@dataclass
class TokenEntry:
    """In-memory representation of a keystore entry (hashed format)."""
    prefix:     str
    hash:       str
    scopes:     List[str] = field(default_factory=lambda: ["*"])
    label:      str = "default"
    created:    str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    last_used:  Optional[str] = None


def _empty_keystore() -> dict:
    return {
        "version": 2,   # bumped: v1 = plain-text tokens, v2 = hashed
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "allowed_origins": list(ALLOWED_ORIGINS_DEFAULT),
        "tokens": [],
    }


def _load_keystore() -> Optional[dict]:
    if not KEYSTORE_PATH.exists():
        return None
    try:
        with open(KEYSTORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "tokens" not in data:
            return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def _save_keystore(data: dict) -> None:
    # Atomic write: stage to .tmp, fsync, rename. Prevents a half-written
    # keystore on power loss / crash.
    tmp = KEYSTORE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, KEYSTORE_PATH)
    # Tighten file permissions where supported. POSIX: 0o600. Windows
    # respects this loosely; full Windows ACLs would need pywin32.
    try:
        os.chmod(KEYSTORE_PATH, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Public: ensure_keystore (called once at module init in main.py)
# ---------------------------------------------------------------------------

def ensure_keystore() -> dict:
    """Load the keystore if it exists; otherwise create one with a
    fresh default token and print a one-time setup banner.

    Returns the in-memory keystore. Called once at FastAPI app
    initialisation so the banner appears in the console window the
    user is already watching during boot.
    """
    store = _load_keystore()
    if store is not None:
        return store

    # First boot: generate a default token, persist, and announce.
    store = _empty_keystore()
    token = _new_token()
    store["tokens"].append(
        _make_token_entry(token, ["*"], "default (auto-generated on first boot)")
    )
    _save_keystore(store)
    _print_first_run_banner(token)
    return store


def _print_first_run_banner(token: str) -> None:
    bar = "=" * 72
    print()
    print(bar)
    print("  OracleAI v2.3 -- API KEY CREATED (FIRST BOOT)")
    print(bar)
    print()
    print("  An API key was just generated for the new external-surface")
    print("  endpoints (Continue.dev, Claude Desktop, curl, etc.).")
    print()
    print("  Copy this key into your MCP / OpenAI client configuration:")
    print()
    print(f"      {token}")
    print()
    print("  This is the ONLY time this key is shown. It is stored in:")
    print(f"      {KEYSTORE_PATH}")
    print()
    print("  To use:")
    print("    curl         :  -H 'Authorization: Bearer <token>'")
    print("    Continue.dev :  requestOptions.headers.Authorization: Bearer <token>")
    print("    Claude Desktop:  set apiKey in MCP server config")
    print()
    print("  To rotate (if compromise suspected):")
    print("      run rotate_api_key.bat in the project folder")
    print()
    print("  This key is part of the BACKUP SET. Back it up alongside:")
    print("      backend/.fernet_key")
    print("      sage_data/memory_log/memory_chain.log")
    print("      sage_data/procedural_memory/procedural.json")
    print(bar)
    print()


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _scope_satisfies(required: str, granted: List[str]) -> bool:
    """Return True if any `granted` scope satisfies `required`.

    Rules:
      - "*" satisfies everything.
      - Exact match: "chat:read" satisfies "chat:read".
      - Trailing wildcard: "chat:*" satisfies "chat:read", "chat:write".
      - No partial / regex / non-trailing wildcards.
    """
    for g in granted:
        if g == "*":
            return True
        if g == required:
            return True
        if g.endswith(":*"):
            prefix = g[:-1]   # "chat:" from "chat:*"
            if required.startswith(prefix):
                return True
    return False


def _verify_token(token: str, store: dict) -> Optional[List[str]]:
    """Return granted scopes if token is valid, else None.

    Supports both hashed entries (v2 keystore) and legacy plain-text
    entries (v1 keystore). Plain-text entries still work but emit a
    deprecation warning. Run rotate_api_key.py to upgrade.

    Updates last_used in memory. Caller is responsible for persisting
    if they want last_used durability -- we skip the disk write on
    every verify to avoid I/O storms on a hot endpoint.
    """
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
        return None

    prefix     = token[:8]
    token_hash = _hash_token(token)

    for entry in store.get("tokens", []):
        # --- v2: hashed entry ---
        if "hash" in entry:
            if entry.get("prefix") != prefix:
                continue  # fast prefix filter before the hash compare
            if secrets.compare_digest(token_hash, entry["hash"]):
                entry["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                scopes = entry.get("scopes", [])
                return list(scopes) if isinstance(scopes, list) else ["*"]

        # --- v1 legacy: plain-text entry ---
        elif "token" in entry:
            if secrets.compare_digest(token, entry["token"]):
                warnings.warn(
                    f"Keystore entry '{entry.get('label', '?')}' uses plain-text "
                    "storage (v1 format). Run rotate_api_key.py to upgrade to "
                    "hashed storage.",
                    UserWarning,
                    stacklevel=2,
                )
                entry["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                scopes = entry.get("scopes", [])
                return list(scopes) if isinstance(scopes, list) else ["*"]

    return None


def _allowed_origins(store: dict) -> List[str]:
    val = store.get("allowed_origins")
    if isinstance(val, list) and val:
        return [str(x) for x in val]
    return list(ALLOWED_ORIGINS_DEFAULT)


def _origin_allowed(origin: str, allowed: List[str]) -> bool:
    """Match the Request Origin against the allowlist. Supports the
    same wildcard-suffix convention as scope checks.
    """
    if origin == "":
        return True   # no Origin header -- non-browser caller
    for entry in allowed:
        if entry == "*":
            return True
        if entry == origin:
            return True
        if entry.endswith("/*") or entry.endswith(":*"):
            prefix = entry[:-1]
            if origin.startswith(prefix):
                return True
    return False


# ---------------------------------------------------------------------------
# FastAPI dependency factory
# ---------------------------------------------------------------------------

def require_scope(scope: str) -> Callable:
    """Return a FastAPI dependency that enforces bearer auth + scope.

    Usage:
        @app.post("/some/route",
                  dependencies=[Depends(require_scope("mcp:*"))])
        async def my_route(...):
            ...

    Raises HTTPException 401 on missing / invalid token,
    403 on insufficient scope, 403 on disallowed Origin.
    """
    async def _dep(request: Request):
        # Lazy import the keystore from main so we share one instance
        # across the process. main.py installs it as `app.state.keystore`.
        store = getattr(request.app.state, "keystore", None)
        if store is None:
            raise HTTPException(503, "auth keystore not initialised")

        # Origin check (defense in depth). Browser CSRF attempt would
        # carry a foreign Origin; non-browser callers (curl, MCP) carry
        # none and pass through.
        origin = request.headers.get("origin", "")
        if not _origin_allowed(origin, _allowed_origins(store)):
                        raise HTTPException(403, f"origin not allowed: {origin}")

        # Bearer token
        auth_hdr = request.headers.get("authorization", "")
        if not auth_hdr.lower().startswith("bearer "):
            raise HTTPException(
                401, "bearer token required; see first-run banner",
            )
        token = auth_hdr[7:].strip()
        granted = _verify_token(token, store)
        if granted is None:
            raise HTTPException(401, "invalid token")

        # Scope check
        if not _scope_satisfies(scope, granted):
            raise HTTPException(
                403, f"token lacks required scope: {scope}",
            )

        return {"scopes": granted}

    # Give FastAPI a nice name in the OpenAPI dependency tree (if exposed).
    _dep.__name__ = f"require_scope__{scope.replace(':', '_').replace('*', 'any')}"
    return _dep


# ---------------------------------------------------------------------------
# Rotation helper (called by rotate_api_key.py)
# ---------------------------------------------------------------------------

def rotate_default_token() -> str:
    """Revoke the existing 'default' token entry and issue a fresh one.

    Returns the new raw token (caller is responsible for displaying it).
    Writes a hashed entry to the keystore -- raw token is never stored.
    Does NOT touch additional tokens with other labels -- only entries
    whose label starts with 'default' are rotated. Other labelled tokens
    (e.g. a scoped Continue.dev key) survive the rotation unchanged.
    """
    store = _load_keystore() or _empty_keystore()
    new_token = _new_token()

    # Remove existing default(s)
    store["tokens"] = [
        t for t in store.get("tokens", [])
        if str(t.get("label", "")).strip().lower() != "default"
        and not str(t.get("label", "")).lower().startswith("default ")
    ]

    store["tokens"].append(
        _make_token_entry(new_token, ["*"], "default (rotated)")
    )
    _save_keystore(store)
    return new_token