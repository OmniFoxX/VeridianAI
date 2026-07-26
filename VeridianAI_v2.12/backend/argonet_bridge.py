#!/usr/bin/env python3
"""Argo-Net Bridge — argonet_bridge.py
MentiSphere Software LLC d.b.a VeridianAI — "Keep an open MentiSphere."

Argo-Net as a first-class Socials channel. This is the seam that lets the
transport-agnostic mesh (LAN + BLE + consent-gated Aether) ride the exact
same SageChannelRouter plumbing every other channel uses: the channel list,
per-thread feed, auto-reply, peer display, connect/disconnect, per-profile
isolation, and the localhost access-policy chokepoint all come for free.

It replaces BitChatBridge as the machine's native mesh. Unlike BitChat, the
LAN transport works out of the box on any network with no dongle, no gateway,
and no configuration -- BLE simply adds phones when a peripheral-capable
adapter is present. Honest capability detection lives in ArgoNetManager.

Design notes:
  * Mesh encryption key: a DEDICATED Argo-Net Fernet key in sage_data (via
    secret_locator), generated on first use. Deliberately separate from the
    at-rest disk key -- network and disk concerns stay isolated, and the key
    lives OUTSIDE the project tree (Trinity separation).
  * Opt-in like BitChat: nothing touches the radio at boot. The mesh spins up
    only on the user's Connect click.
  * Trust model (v1): a single shared symmetric mesh key = an own-both-ends
    broadcast group (Todd's own devices). Every node decrypts every message,
    exactly like a public channel. Per-peer DM keys are a later concern.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import socket
import time
from collections import deque

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from sage_messaging_adapter import (
    ChannelMessage,
    ChannelProfile,
    SageMessagingAdapter,
)

logger = logging.getLogger("sage.argonet")

MESH_KEY_FILENAME = "argonet_mesh.key"

# Fixed application salt: both peers must derive the SAME key from the same
# secret without exchanging a salt, so the salt is a constant. PBKDF2 still
# adds brute-force cost over a bare hash. Bump the version suffix only with a
# deliberate, coordinated key-format change (it would split the mesh).
_MESH_KDF_SALT = b"argonet-mesh-kdf-v1"

# When NO mesh secret is set, the group ("public") channel uses this well-known
# key so every default node shares one OPEN public group and public messages
# just work across machines with zero configuration -- exactly like DMs. This
# is not a secret: a public channel is public by design (private conversations
# use DMs, and setting a mesh secret creates a PRIVATE group instead).
_DEFAULT_GROUP_SECRET = "argonet-open-public-group-v1"
_MESH_KDF_ITERATIONS = 200_000


def _derive_key_from_secret(secret: str) -> bytes:
    """Deterministically derive a Fernet key from a shared mesh secret.

    This is what makes an own-both-ends group actually share a key: every
    device that types the same secret derives the identical key and can decrypt
    the group's traffic. Friends agree on the secret out-of-band over a channel
    they already trust (the same context where reading a fingerprint aloud makes
    sense) -- it never crosses the mesh.
    """
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=_MESH_KDF_SALT, iterations=_MESH_KDF_ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(secret.encode("utf-8")))


def _load_or_create_mesh_key() -> bytes:
    """Return the Argo-Net mesh Fernet key, generating it on first use.

    Stored via secret_locator in sage_data (outside the project tree). If a
    legacy copy is found in the project it is migrated out once. Never raises
    into the connect path -- on any failure a process-local ephemeral key is
    returned so the mesh still forms (it just won't persist across restarts).
    """
    try:
        from config import DATA_DIR, PROJECT_DIR
        from secret_locator import resolve_secret_file
        path = resolve_secret_file(MESH_KEY_FILENAME, DATA_DIR, PROJECT_DIR)
        if path.exists():
            data = path.read_bytes().strip()
            if data:
                return data
        key = Fernet.generate_key()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key)
        try:
            os.chmod(path, 0o600)  # best-effort on POSIX; no-op on Windows
        except Exception:
            pass
        logger.info("[ArgoNet] generated new mesh key at %s", path)
        return key
    except Exception as exc:
        logger.warning("[ArgoNet] mesh key persistence unavailable (%s); "
                       "using an ephemeral in-memory key this session.", exc)
        return Fernet.generate_key()


def reset_mesh_key() -> bool:
    """Delete this machine's persisted per-machine mesh key so a fresh random
    one is generated on the next connect. Safe to call when the file is absent.

    Does NOT touch a shared mesh secret -- that is cleared separately through
    the socials config. Only affects the fallback key used when no secret is
    set. Returns True on success (removed or already gone)."""
    try:
        from config import DATA_DIR, PROJECT_DIR
        from secret_locator import resolve_secret_file
        path = resolve_secret_file(MESH_KEY_FILENAME, DATA_DIR, PROJECT_DIR,
                                   announce=False)
        if path.exists():
            path.unlink()
            logger.info("[ArgoNet] mesh key reset (removed %s)", path)
        return True
    except Exception as exc:
        logger.warning("[ArgoNet] mesh key reset failed: %s", exc)
        return False


def _default_identity() -> str:
    """Stable, non-PHI identity string for this node. Prefers the configured
    node_name, else the hostname. Never a person's name -- ArgoNode fingerprints
    it to a SHA-256 prefix before it ever touches the mesh."""
    try:
        from config_store import OracleConfig
        from config import PROJECT_DIR
        nn = str(OracleConfig.load(PROJECT_DIR / "config.json")
                 .network.node_name or "").strip()
        if nn:
            return f"veridianai-{nn}"
    except Exception:
        pass
    try:
        return f"veridianai-{socket.gethostname()}"
    except Exception:
        return "veridianai-node"


class ArgoNetBridge(SageMessagingAdapter):
    """Argo-Net mesh as a Socials channel adapter."""

    PROFILE = ChannelProfile(
        name="argonet",
        max_chars=800,           # generous; outbound fragmentation handles BLE
        strip_markdown=True,     # mesh peers are plain-text clients
        split_long=True,
        sage_prefix="",          # the feed already labels the sender
    )
    EXPERIMENTAL = False
    _MAX_DRAIN = 50

    def __init__(self, config: dict):
        super().__init__(config)
        self._identity   = (self.config.get("identity_string")
                            or _default_identity())
        self._enable_ble = bool(self.config.get("enable_ble", True))
        self._manager    = None
        self._task: asyncio.Task | None = None
        self._inbox: deque = deque(maxlen=200)
        self._key: bytes | None = None

    # --- config ---
    def update_config(self, cfg: dict) -> None:
        if not cfg:
            return
        self.config.update(cfg)
        if cfg.get("identity_string"):
            self._identity = cfg["identity_string"]
        if "enable_ble" in cfg:
            self._enable_ble = bool(cfg["enable_ble"])
        # mesh_secret is read from self.config at connect() time (a change only
        # takes effect on reconnect, since the key is derived there).

    # --- availability (LAN needs no special hardware) ---
    def available(self) -> bool:
        return True

    def unavailable_reason(self):
        # Not an error -- a standing note shown beside the channel. LAN carries
        # the mesh everywhere; BLE only adds phones when the adapter can do it.
        return "LAN mesh ready on any network; BLE adds phones if the adapter can advertise"

    def connected(self) -> bool:
        return bool(self._manager is not None
                    and getattr(self._manager, "_running", False))

    # --- inbound sink: manager -> feed ---
    def _on_mesh_message(self, plaintext: str, origin: str,
                         private: bool = False) -> None:
        """Called by ArgoNetManager for every message delivered to us.
        Sync callable (the manager invokes it synchronously). Never raises.
        `private` = True marks a DM, which threads into its own per-peer room
        ("dm:<origin>") and is labelled in the feed."""
        try:
            self._inbox.append(ChannelMessage(
                sender="peer:" + (origin[:8] if origin else "?"),
                channel=("dm:" + origin) if private else "general",
                content=plaintext,
                timestamp=time.time(),
                platform="argonet",
                raw={"origin": origin, "private": bool(private),
                     "peer_id": origin},
            ))
        except Exception as exc:
            logger.warning("[ArgoNet] inbound buffer error: %s", exc)

    # --- lifecycle ---
    async def connect(self) -> bool:
        try:
            from argonet_manager import ArgoNetManager
        except Exception as exc:
            self.last_error = f"argonet_manager import failed: {exc}"
            logger.error("[ArgoNet] %s", self.last_error)
            return False
        try:
            secret = (self.config.get("mesh_secret") or "").strip()
            if secret:
                self._key = _derive_key_from_secret(secret)
                logger.info("[ArgoNet] PRIVATE group: mesh secret set -- only "
                            "devices with the same secret read public messages")
            else:
                # No secret: join the OPEN public group (well-known key) so
                # public messages work across machines out of the box, like DMs.
                # Set a mesh secret to switch to a private group instead.
                self._key = _derive_key_from_secret(_DEFAULT_GROUP_SECRET)
                logger.info("[ArgoNet] OPEN public group (no mesh secret) -- "
                            "public messages readable by any default node")
            self._manager = ArgoNetManager(
                identity_string=self._identity,
                fernet_key=self._key,
                enable_ble=self._enable_ble,
                enable_lan=True,
                enable_aether=False,          # consent-gated; never on at connect
                on_message=lambda text, origin, private=False:
                    self._on_mesh_message(text, origin, private),
            )
            # start() awaits gather() forever, so run it as a background task.
            self._task = asyncio.create_task(self._manager.start())
            # Give the loops a beat to spin up and set _running / bind sockets.
            for _ in range(20):
                await asyncio.sleep(0.05)
                if getattr(self._manager, "_running", False):
                    break
            self._ready = self.connected()
            self.last_error = None if self._ready else "mesh failed to start"
            if self._ready:
                # Restore known self-revocations and persist any new ones that
                # arrive over the mesh.
                self._load_revocations()
                self._manager.node.register_revocation_callback(
                    lambda full, rev: self._persist_revocations())
                logger.info("[ArgoNet] mesh up | node=%s | transports=%s",
                            self._manager.node.node_id,
                            [t.value for t in self._manager._available_transports])
            return self._ready
        except Exception as exc:
            self.last_error = f"connect failed: {exc}"
            logger.error("[ArgoNet] %s", self.last_error)
            return False

    async def disconnect(self) -> None:
        try:
            if self._manager is not None:
                await self._manager.stop()
            if self._task is not None:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
        except Exception as exc:
            logger.warning("[ArgoNet] disconnect error: %s", exc)
        finally:
            self._manager = None
            self._task = None
            self._ready = False

    async def send(self, message: str, channel: str = "general") -> bool:
        """Send to the group (channel 'general') or privately to one peer
        (channel 'dm:<fingerprint>'). The UI's To: picker sets the channel."""
        if not self.connected():
            return False
        try:
            ch = (channel or "general")
            if ch.startswith("dm:"):
                recipient_fp = ch[3:].strip()
                if not recipient_fp:
                    logger.warning("[ArgoNet] DM with no recipient -- dropped.")
                    return False
                return bool(await self._manager.send_dm(message, recipient_fp))
            return bool(await self._manager.send(message))
        except Exception as exc:
            logger.error("[ArgoNet] send error: %s", exc)
            return False

    async def receive(self, timeout: float = 5.0) -> list:
        """Drain buffered inbound messages, honoring the router's poll timeout.

        CRITICAL: the router's _poll loop has no sleep of its own -- it paces
        itself entirely on this coroutine blocking for up to `timeout`. If we
        return instantly on an empty inbox (the manager fills it out-of-band),
        _poll becomes a tight non-yielding loop that starves the whole asyncio
        event loop -- every other request, including the connect response and a
        UI reload, stalls behind it. So when idle we AWAIT (yielding control),
        waking early the moment a message lands.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while not self._inbox:
            if time.monotonic() >= deadline or not self.connected():
                return []
            await asyncio.sleep(0.1)   # yield to the loop; ~0.1s feed latency
        out = []
        while self._inbox and len(out) < self._MAX_DRAIN:
            out.append(self._inbox.popleft())
        return out

    async def peers(self) -> list:
        if not self.connected():
            return []
        try:
            return [p.node_id[:12] for p in self._manager.node.active_peers()]
        except Exception as exc:
            logger.warning("[ArgoNet] peers() error: %s", exc)
            return []

    @staticmethod
    def _peer_full_fp(p) -> str:
        """Recompute a peer's FULL 256-bit fingerprint from its bound keys, or
        "" if we don't have both keys yet."""
        try:
            from argonet_identity import bind_full
            if getattr(p, "pubkey", "") and getattr(p, "sign_pubkey", ""):
                return bind_full(bytes.fromhex(p.pubkey), bytes.fromhex(p.sign_pubkey))
        except Exception:
            pass
        return ""

    async def identity(self) -> dict:
        """Fingerprints for out-of-band verification. The `fingerprint` is the
        FULL 256-bit id (bound to both of the peer's keys) -- that's what you
        read aloud and what the trust store keys on. `peer_id` stays the short
        node_id used for DM addressing. `revoked` = a valid signed
        self-revocation has been seen for this identity."""
        if not self.connected():
            return {"available": False}
        try:
            node = self._manager.node
            revoked = node.revoked
            peers = []
            for p in node.active_peers():
                full = self._peer_full_fp(p) or p.node_id
                peers.append({
                    "peer_id": p.node_id,
                    "nickname": p.node_id[:12],
                    "fingerprint": full,
                    "verified": True,       # session encryption is inherent
                    "trusted": False,       # owner confirms out-of-band (overlaid)
                    "revoked": full in revoked,
                    "revoked_reason": (revoked.get(full, {}) or {}).get("reason", ""),
                    # can_dm: we hold this peer's public key, so a private
                    # message can be sealed to it. Drives the composer picker.
                    "can_dm": bool(getattr(p, "pubkey", "")),
                })
            return {
                "available": True,
                "nickname": self._identity,
                "fingerprint": node.identity.full_fingerprint,
                "node_id": node.node_id,
                "peers": peers,
            }
        except Exception as exc:
            logger.warning("[ArgoNet] identity() error: %s", exc)
            return {"available": False}

    # --- self-revocation + persistence -------------------------------
    def _revocations_path(self):
        from config import DATA_DIR
        return Path(DATA_DIR) / "argonet_revocations.json"

    def _load_revocations(self) -> None:
        """Load previously-seen valid revocations into the node on connect."""
        try:
            import json
            p = self._revocations_path()
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            for rev in (data or {}).values():
                self._manager.node.handle_revocation(rev)  # re-verifies each
        except Exception as exc:
            logger.warning("[ArgoNet] could not load revocations: %s", exc)

    def _persist_revocations(self) -> None:
        try:
            import json
            p = self._revocations_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self._manager.node.revoked, indent=2),
                         encoding="utf-8")
        except Exception as exc:
            logger.warning("[ArgoNet] could not persist revocations: %s", exc)

    async def revoke_self(self, reason: str = "compromised") -> dict:
        """Publish a signed self-revocation of this node's identity."""
        if not self.connected():
            return {}
        rev = await self._manager.revoke_self(reason)
        self._persist_revocations()
        return rev
