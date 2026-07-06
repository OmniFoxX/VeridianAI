#!/usr/bin/env python3
"""
node_client.py - client side of the Sage network (e.g. the laptop).
Wraps each call in an identity envelope {v, user, session, kind, body}, encrypts
it with the shared home token, POSTs to a remote node, decrypts the reply, and
VERIFIES the reply's session matches the request (rejects any cross-delivered or
mismatched response). Sync core (urllib) for testability; call via
asyncio.to_thread from async code. DEFENSIVE: down/slow/wrong-token -> (False,
reason); NEVER raises.
"""
from __future__ import annotations

import urllib.error
import urllib.request
import uuid
from typing import Any, Tuple

import node_trust


def call_node(base_url: str, token: str, kind: str, body: Any,
              user: str = "owner", timeout: float = 120) -> Tuple[bool, Any]:
    """Encrypt an identity envelope -> POST -> decrypt -> verify session.
    Returns (ok, result) or (False, reason)."""
    session = uuid.uuid4().hex
    path = {"info": "/api/node/info", "infer": "/api/node/infer",
            "generate_image": "/api/node/generate-image"}.get(kind)
    if not path:
        return False, f"unknown node call kind: {kind}"
    env = {"v": 1, "user": user, "session": session, "kind": kind, "body": body}
    try:
        blob = node_trust.encrypt_payload(env, token)
        req = urllib.request.Request(
            base_url.rstrip("/") + path, data=blob,
            headers={"Content-Type": "application/octet-stream"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rblob = resp.read()
    except urllib.error.HTTPError as e:
        return False, f"node rejected the request (HTTP {e.code})"
    except urllib.error.URLError as e:
        return False, f"could not reach node at {base_url}: {e}"
    except Exception as e:
        return False, f"node call failed: {type(e).__name__}: {e}"
    ok, resp = node_trust.decrypt_payload(rblob, token)
    if not ok:
        return False, resp
    if not isinstance(resp, dict) or resp.get("session") != session:
        return False, "response/session mismatch (possible cross-delivery) - rejected"
    return bool(resp.get("ok")), resp.get("result")


def node_info(base_url, token, user="owner", timeout=15):
    return call_node(base_url, token, "info", {}, user, timeout)


def node_infer(base_url, token, model_id, messages, options=None,
               user="owner", timeout=300, urgent=False):
    # v2.11.13: urgent rides at BODY level (options are sanitized/whitelisted
    # server-side, so a flag inside options would be silently dropped). The
    # serving node applies its per-peer quota before honoring it.
    return call_node(base_url, token, "infer",
                     {"model_id": model_id, "messages": messages,
                      "options": options or {}, "urgent": bool(urgent)},
                     user, timeout)


def node_generate_image(base_url, token, prompt, user="owner", timeout=600, **opts):
    body = {"prompt": prompt}
    body.update({k: v for k, v in opts.items() if v is not None})
    return call_node(base_url, token, "generate_image", body, user, timeout)
