#!/usr/bin/env python3
"""
node_server.py - desktop-side "node surface" helpers for the Sage network.
Gated OFF by default; token-authenticated + Fernet-encrypted via node_trust.
Every message is an identity envelope {v, user, session, kind, body} - the
(user, session) fields are the multi-user seam (today user defaults to "owner";
the server ALWAYS echoes the session so a reply cannot be cross-delivered to the
wrong requester). Per-user keys + per-user memory isolation slot in later without
changing this protocol. The FastAPI endpoints in main.py are thin wrappers.
"""
from __future__ import annotations

import os
from typing import Any, Tuple

import node_trust

_warned = False


def node_enabled(config=None) -> bool:
    env = os.environ.get("NODE_SERVER_ENABLED", "").strip().lower()
    on = env in ("1", "true", "yes", "on")
    if not on and config is not None:
        try:
            on = bool(config.get("node_server_enabled", False))
        except Exception:
            on = False
    if on:
        _warn_once()
    return on


def _warn_once():
    global _warned
    if _warned:
        return
    _warned = True
    bar = "=" * 68
    print(
        "\n" + bar + "\n"
        "[SAGE NETWORK] node server is ENABLED - this machine will answer\n"
        "token-authenticated, Fernet-encrypted requests from your other nodes.\n"
        "  * Keep it on your LAN only (bind network.host to your LAN IP, never a\n"
        "    public interface). Do NOT port-forward this to the internet.\n"
        "  * Protect the .home_token file - anyone who can read it can use your\n"
        "    nodes. Share it only with machines you own.\n"
        "  * Internet exposure ('SageNet') is a separate, deliberate, future step\n"
        "    with its own warnings. This is NOT that.\n"
        + bar + "\n"
    )


def get_token(data_dir) -> str:
    return node_trust.load_or_create_home_token(data_dir)


def read_request(raw, token) -> Tuple[bool, Any]:
    """Unseal + parse an incoming node envelope. Returns (True, env) with keys
    v/user/session/kind/body, or (False, reason) on a wrong token, tampering,
    staleness, or a malformed envelope. NEVER raises."""
    ok, obj = node_trust.decrypt_payload(raw, token)
    if not ok:
        return False, obj
    if not isinstance(obj, dict) or not obj.get("session"):
        return False, "malformed envelope (missing session)"
    return True, {
        "v": obj.get("v", 1),
        "user": obj.get("user", "owner"),
        "session": obj.get("session"),
        "kind": obj.get("kind"),
        "body": obj.get("body") or {},
    }


def seal_response(env, ok, result, token) -> bytes:
    """Seal a response envelope that ECHOES the request's user + session, so the
    client can confirm it received the answer to ITS request (no cross-delivery)."""
    return node_trust.encrypt_payload({
        "v": 1,
        "user": (env or {}).get("user", "owner"),
        "session": (env or {}).get("session"),
        "ok": bool(ok),
        "result": result,
    }, token)


def capabilities(token, node_name, model_ids, has_comfyui) -> dict:
    return {
        "node_name": node_name,
        "models": list(model_ids or []),
        "has_comfyui": bool(has_comfyui),
        "fingerprint": node_trust.token_fingerprint(token),
        "version": "node-v1",
    }
