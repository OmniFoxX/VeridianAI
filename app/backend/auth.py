#!/usr/bin/env python3
"""
auth.py -- VeridianAI bearer-token auth for external-surface endpoints
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
to `localhost:8000` -- could invoke Toga's full toolkit. With it,
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
- Tokens are 32-byte URL-safe random strings, prefixed `vai_`
  (`ora_` from before the rename is still accepted -- see LEGACY_TOKEN_PREFIXES).
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


def _keystore_dir():
    """v2.13: was hardcoded to _BACKEND_DIR.parent.parent / "sage_data",
    which BYPASSED config.DATA_DIR and therefore the VERIDIAN_DATA_DIR
    override. Caught on 2026-08-07 when a boot with the override set still
    announced the keystore at the old sibling path -- under MSIX that
    resolves inside C:\\Program Files\\WindowsApps and the write fails.

    Every other module here already does the try-config/except-fallback
    dance; this one was the outlier."""
    try:
        from config import DATA_DIR as _DD
        return Path(_DD)
    except Exception:
        return _BACKEND_DIR.parent.parent / "sage_data"


# v2.9 hardening: bearer-token store migrates out of the project into sage_data.
KEYSTORE_PATH = _resolve_secret_file(
    ".api_keystore.json", _keystore_dir(), _BACKEND_DIR)

# Tokens are prefixed so they're visually identifiable as VeridianAI tokens
# (mirrors OpenAI's `sk-` convention).
TOKEN_PREFIX = "vai_"

# `ora_` keys were minted under the old name and are in people's Continue.dev
# and Claude Desktop configs right now. Changing the prefix without accepting
# the old one would revoke every existing key silently -- the client would just
# start getting 401s with nothing to explain it. New keys are `vai_`; old ones
# keep working until their holder rotates.
LEGACY_TOKEN_PREFIXES = ("ora_",)
ACCEPTED_TOKEN_PREFIXES = (TOKEN_PREFIX,) + LEGACY_TOKEN_PREFIXES
TOKEN_BYTES = 32  # 256 bits -- well past brute-force range

# Origin allowlist for the defense-in-depth check. Requests with NO
# Origin header (server-to-server, curl, stdio clients) bypass this
# check and rely on the bearer token alone. Browser-origin requests
# MUST match one of these.
#
# To extend (e.g., a different IDE accessing VeridianAI via web view):
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


# v2.14: OWNER_NS -- which profile a token acts as.
#
# Until now entries carried a *label* and nothing else, so every API request
# was "someone holding a key" and resolved to the default namespace. UI actions
# were attributable (a cookie session names the user); API actions were not.
# That is an access-control gap as much as an audit one: any valid token
# reached the owner's data.
#
# The convention deliberately matches main._session_ns:
#
#   owner_ns is None   -> the owner / shared store  (what _session_ns returns
#                         for the owner, and in single-user mode)
#   owner_ns is "abc"  -> that profile's namespace, and nothing else
#
# so a token principal drops into the existing namespace plumbing without a
# parallel notion of identity. The KEY ABSENCE of "owner_ns" means a legacy
# pre-v2.14 entry that was never bound -- distinct from a bound-to-owner entry
# whose value is None. _migrate_ownership resolves those, loudly.
OWNER_NS_KEY = "owner_ns"


def _make_token_entry(raw: str, scopes: list, label: str,
                      owner_ns=None) -> dict:
    """Build a hashed keystore entry. The raw token is never stored."""
    return {
        "prefix":     raw[:8],
        "hash":       _hash_token(raw),
        "scopes":     scopes,
        "label":      label,
        OWNER_NS_KEY: owner_ns,
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
        # Upgrading installs arrive here with unbound tokens. Migrate on the
        # way in, so nothing downstream ever has to reason about the absence.
        if migrate_ownership(store):
            _save_keystore(store)
        return store

    # First boot: generate a default token, persist, and announce.
    store = _empty_keystore()
    token = _new_token()
    store["tokens"].append(
        _make_token_entry(token, ["*"],
                          "default (auto-generated on first boot)",
                          owner_ns=None)   # explicitly the owner, not merely unset
    )
    _save_keystore(store)
    _print_first_run_banner(token)
    return store


def _backup_set_paths() -> list:
    """The files that must be snapshotted together, absolute and resolved.

    The Fernet key, the memory chain log and the procedural store are only
    meaningful as a SET: the key without the chain decrypts nothing, the chain
    without the key is opaque, and a procedural store from a different run
    matches neither. Resolved rather than relative because MSIX redirection
    means the literal path and the real path are different directories.
    """
    # Ask the modules that OWN these paths rather than rebuilding them here.
    # The rebuilt version named PROJECT_DIR/backend/.fernet_key and
    # STATE_DIR/memory_log, and under a relocated data directory (VERIDIAN_DATA_DIR,
    # the portable build, MSIX redirection) all three pointed at directories
    # that were empty or absent while the real files sat elsewhere. Telling
    # someone to back up a path that is not the path is worse than saying
    # nothing: they come away believing the set is safe.
    try:
        import atrest  # type: ignore
        from config import MEMORY_DIR, PROCEDURAL_DIR  # type: ignore
        cands = [Path(atrest._key_path()),
                 Path(MEMORY_DIR) / "memory_chain.log",
                 Path(PROCEDURAL_DIR) / "procedural.json"]
    except Exception:
        return ["<at-rest key>", "<memory chain>", "<procedural store>"]
    out = []
    for p in cands:
        try:
            rp = str(p.resolve())
        except Exception:
            rp = str(p)
        # Say so when a file is not there yet. First boot creates them in
        # order, and a path printed without comment reads as a promise.
        try:
            if not p.exists():
                rp += "   (not created yet)"
        except Exception:
            pass
        out.append(rp)
    return out


def _print_first_run_banner(token: str) -> None:
    bar = "=" * 72
    print()
    print(bar)
    # No version number here on purpose: it went stale at v2.3 and sat there
    # for eleven releases. The version lives in electron/package.json.
    print("  VeridianAI -- API KEY CREATED (FIRST BOOT)")
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
    # Absolute and RESOLVED, not the old relative sketch.
    #
    # Under MSIX these three do not live where the relative form implies:
    # writes to %APPDATA% are redirected into the package's LocalCache, so
    # "sage_data/memory_log/..." names a directory the user can open and find
    # empty while the real chain sits somewhere they have never seen. Telling
    # someone to back up a path that is not the path is worse than saying
    # nothing -- they would come away believing the chain was safe.
    for _p in _backup_set_paths():
        print(f"      {_p}")
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


def _principal(entry: dict) -> dict:
    """The identity behind a verified token.

    Returned instead of a bare scope list so the caller learns WHO is acting,
    not merely what they may do. `bound` is False for a legacy entry that
    predates ownership and has never been migrated -- callers treat that as
    untrusted-for-namespace-purposes rather than silently as the owner.
    """
    scopes = entry.get("scopes", [])
    return {
        "scopes":   list(scopes) if isinstance(scopes, list) else ["*"],
        "owner_ns": entry.get(OWNER_NS_KEY),
        "label":    entry.get("label", "?"),
        "prefix":   entry.get("prefix", ""),
        "bound":    OWNER_NS_KEY in entry,
    }


def _verify_token(token: str, store: dict) -> Optional[dict]:
    """Return granted scopes if token is valid, else None.

    Supports both hashed entries (v2 keystore) and legacy plain-text
    entries (v1 keystore). Plain-text entries still work but emit a
    deprecation warning. Run rotate_api_key.py to upgrade.

    Updates last_used in memory. Caller is responsible for persisting
    if they want last_used durability -- we skip the disk write on
    every verify to avoid I/O storms on a hot endpoint.
    """
    if not isinstance(token, str) or not token.startswith(ACCEPTED_TOKEN_PREFIXES):
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
                return _principal(entry)

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
                return _principal(entry)

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
        principal = _verify_token(token, store)
        if principal is None:
            raise HTTPException(401, "invalid token")

        # Scope check
        if not _scope_satisfies(scope, principal["scopes"]):
            raise HTTPException(
                403, f"token lacks required scope: {scope}",
            )

        # Publish the principal for the rest of the request. main._session_ns
        # and main._is_owner read this, which is what confines an API caller
        # to the profile its token belongs to. Without it, every token
        # resolved to the default namespace regardless of who held it.
        try:
            request.state.api_principal = principal
        except Exception:
            pass

        # v2.14.3: open this profile's data key with the token itself.
        #
        # The unwrap happens HERE, not in a handler, because this is the last
        # place the raw token exists. Only the resulting KEY is published --
        # the token is never stashed on request.state, because anything left
        # there is a candidate for being logged, and a bearer token in a log
        # is a live credential in a log.
        #
        # Layering note: auth verifies identity and a data key is arguably a
        # storage concern, so this import is deliberately lazy and one-way.
        # The alternative -- handing the raw token onward for somebody else to
        # unwrap -- trades a clean boundary for a leakable secret.
        if principal.get("owner_ns"):
            try:
                import profile_keys as _pk
                _dek = _pk.unlock_with_token(principal["owner_ns"],
                                             principal.get("prefix", ""), token)
                if _dek:
                    request.state.api_dek = _dek
            except Exception:
                # A token issued before wraps existed, or a profile with no
                # key: both are normal. The request proceeds and reads through
                # the shared-key fallback.
                pass   # a request object without .state is not worth failing over

        return {"scopes": principal["scopes"], "principal": principal}

    # Give FastAPI a nice name in the OpenAPI dependency tree (if exposed).
    _dep.__name__ = f"require_scope__{scope.replace(':', '_').replace('*', 'any')}"
    return _dep


# ---------------------------------------------------------------------------
# Rotation helper (called by rotate_api_key.py)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# v2.14: per-profile token ownership
# ---------------------------------------------------------------------------

def _norm_ns(owner_ns):
    """Normalise a namespace. Empty string and None both mean the owner."""
    if owner_ns is None:
        return None
    ns = str(owner_ns).strip()
    return ns or None


def migrate_ownership(store: dict) -> int:
    """Bind pre-v2.14 entries to the owner, and say so.

    An install upgrading from 2.13 has one token that belongs to nobody. It
    cannot be left unbound -- unbound would either be refused, breaking the
    user's working editor integration on upgrade, or accepted as the owner,
    which is the gap this release exists to close.

    So it becomes the owner's: the owner created it and has been using it.
    What matters is that this is ANNOUNCED rather than done quietly. A token
    silently acquiring an identity it did not previously have is exactly what
    an audit trail should be able to see. It is stamped on the entry too, so
    the keystore carries its own history.

    Returns the number of entries migrated.
    """
    n = 0
    for entry in store.get("tokens", []):
        if OWNER_NS_KEY in entry:
            continue
        entry[OWNER_NS_KEY] = None            # owner / shared store
        entry["migrated_from_unowned"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        n += 1
    if n:
        print("[AUTH] v2.14 migration: %d API token(s) had no owning profile "
              "and have been bound to the OWNER profile." % n, flush=True)
        print("[AUTH] If any of them are used by someone other than the owner, "
              "rotate them -- they now act as the owner.", flush=True)
    return n


def issue_token(owner_ns=None, label: str = "api", scopes=None) -> str:
    """Mint a token bound to one profile. Returns the raw token ONCE."""
    store = _load_keystore() or _empty_keystore()
    migrate_ownership(store)
    raw = _new_token()
    store["tokens"].append(
        _make_token_entry(raw, list(scopes or ["*"]), label,
                          owner_ns=_norm_ns(owner_ns)))
    _save_keystore(store)
    return raw


def rotate_token_for(owner_ns=None, label: str = None) -> str:
    """Rotate the tokens belonging to ONE profile, leaving the rest alone.

    This is the point of the exercise. Rotation used to be a system action --
    one shared key, so replacing it broke every integration on the machine for
    every profile, which is precisely why the button had to be owner-gated.
    Bound tokens make it personal: a user who thinks their key leaked can
    replace their own without asking anyone or disrupting anybody else.
    """
    ns = _norm_ns(owner_ns)
    store = _load_keystore() or _empty_keystore()
    migrate_ownership(store)

    kept, revoked = [], 0
    for t in store.get("tokens", []):
        if t.get(OWNER_NS_KEY) == ns:
            revoked += 1
            continue
        kept.append(t)
    store["tokens"] = kept

    raw = _new_token()
    store["tokens"].append(
        _make_token_entry(raw, ["*"],
                          label or ("default (rotated)" if ns is None
                                    else "rotated"),
                          owner_ns=ns))
    _save_keystore(store)
    print("[AUTH] rotated %d token(s) for profile %s"
          % (revoked, ns or "(owner)"), flush=True)
    return raw


def revoke_tokens_for(owner_ns) -> int:
    """Revoke every token belonging to a profile. Called on user deletion.

    A deleted account whose API key still works is deleted only in the UI.
    Refuses to act on the owner namespace, because None is also what a
    mistaken caller passes, and wiping the owner's keys by accident is not a
    recoverable mistake.
    """
    ns = _norm_ns(owner_ns)
    if ns is None:
        raise ValueError("refusing to revoke the owner's tokens by namespace; "
                         "call rotate_token_for(None) deliberately instead")
    store = _load_keystore()
    if not store:
        return 0
    before = len(store.get("tokens", []))
    store["tokens"] = [t for t in store.get("tokens", [])
                       if t.get(OWNER_NS_KEY) != ns]
    removed = before - len(store["tokens"])
    if removed:
        _save_keystore(store)
        print("[AUTH] revoked %d token(s) for deleted profile %s"
              % (removed, ns), flush=True)
    return removed


# v2.14.3: the scope PRESETS the UI offers.
#
# Only three scopes are enforced anywhere in the app -- chat:read, chat:write
# and mcp:* -- so the honest menu is short. Presets rather than a free-text
# field on purpose: a typo in a scope string does not error, it silently grants
# nothing (locking someone out of their own integration) or, if they type "*",
# silently grants everything. Neither failure announces itself.
#
# "full" stays the default because it is what every key did before this
# existed, and a key that suddenly cannot do what it used to is a worse first
# impression than a key that can do too much.
SCOPE_PRESETS = {
    "full":      ["*"],            # everything, including endpoints added later
    "chat":      ["chat:*"],       # Continue.dev, Claude Desktop: models + completions
    "chat_read": ["chat:read"],    # can list models, CANNOT send messages
    "mcp":       ["mcp:*"],        # MCP tools only, no chat
}

SCOPE_LABELS = {
    "full":      "Full access",
    "chat":      "Chat (read + write)",
    "chat_read": "Chat, read-only",
    "mcp":       "MCP tools only",
}


def scopes_for_preset(preset: str):
    """Resolve a preset name to its scope list. Unknown -> None (caller errors).

    Deliberately does NOT fall back to 'full': silently upgrading an
    unrecognised preset to everything is exactly the failure this design is
    meant to avoid.
    """
    if not isinstance(preset, str):
        return None
    return SCOPE_PRESETS.get(preset.strip().lower())


def revoke_token(prefix: str, owner_ns=None, require_owner: bool = False) -> bool:
    """Revoke ONE token by its public prefix, within a profile.

    `owner_ns` scopes the search: a profile can only revoke its own keys, so
    alice cannot delete the owner's -- or bob's -- by guessing a prefix. The
    prefix is not a secret (it is shown in the key list), which is precisely
    why the namespace check has to do the real work.

    require_owner=True additionally refuses unless owner_ns is None, for the
    caller that wants to be explicit about touching the shared/owner keys.

    Returns True if something was revoked.
    """
    if not isinstance(prefix, str) or not prefix.strip():
        return False
    ns = _norm_ns(owner_ns)
    if require_owner and ns is not None:
        return False
    store = _load_keystore()
    if not store:
        return False
    migrate_ownership(store)
    keep, removed = [], 0
    for t in store.get("tokens", []):
        if t.get("prefix") == prefix and t.get(OWNER_NS_KEY) == ns:
            removed += 1
            continue
        keep.append(t)
    if not removed:
        return False
    store["tokens"] = keep
    _save_keystore(store)
    print("[AUTH] revoked token %s for profile %s"
          % (prefix, ns or "(owner)"), flush=True)
    return True


def _preset_name(scopes):
    """Best-effort reverse lookup so the UI can show 'Chat, read-only' rather
    than a raw scope array. Returns None for a custom set -- which is honest:
    a key minted by an older version or by hand is not one of our presets."""
    try:
        for name, sc in SCOPE_PRESETS.items():
            if sorted(sc) == sorted(scopes or []):
                return name
    except Exception:
        pass
    return None


def list_tokens(owner_ns=None, all_profiles: bool = False):
    """Token METADATA for the UI. Never returns a hash or a raw token."""
    store = _load_keystore() or _empty_keystore()
    ns = _norm_ns(owner_ns)
    out = []
    for t in store.get("tokens", []):
        if not all_profiles and t.get(OWNER_NS_KEY) != ns:
            continue
        out.append({
            "label":     t.get("label", "?"),
            "prefix":    t.get("prefix", ""),
            "owner_ns":  t.get(OWNER_NS_KEY),
            "scopes":    t.get("scopes", []),
            "preset":    _preset_name(t.get("scopes", [])),
            "created":   t.get("created"),
            "last_used": t.get("last_used"),
            "bound":     OWNER_NS_KEY in t,
            "migrated":  t.get("migrated_from_unowned"),
        })
    return out


def rotate_default_token() -> str:
    """Back-compat shim for rotate_api_key.py and pre-v2.14 call sites.

    Kept because the standalone script is named in the first-run banner and in
    the Store notes. It now routes through the per-profile path, so there is
    one rotation implementation rather than two that can drift apart.
    """
    return rotate_token_for(None, label="default (rotated)")
