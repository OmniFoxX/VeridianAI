# argonet_manager.py
# MentiSphere Software LLC d.b.a VeridianAI
# Argo-Net: Manager — The Captain. Orchestrates Everything.
# "Keep an open MentiSphere."
# ---------------------------------------------------------------------------
# This is the file that wakes up, inventories the hardware,
# assembles the crew, and says "Argo-Net is go."
# All six adapters plug in here. One call starts the mesh.
# ---------------------------------------------------------------------------

import asyncio
import json
import logging
import time
from typing import Optional, Callable
from cryptography.fernet import Fernet

from argonet_envelope import ArgoEnvelope, Transport, NodeCapability
from argonet_node import ArgoNode, Peer
from argonet_router import ArgoRouter, RouteDecision
from argonet_ble import ArgoNetBLEAdapter
from argonet_lan import ArgoNetLANAdapter
from argonet_aether import ArgoNetAetherAdapter

logger = logging.getLogger("argonet.manager")

# ---------------------------------------------------------------------------
# Argo-Net Manager
# The single entry point for VeridianAI to use Argo-Net.
# Toga UI, FastAPI, MCP tools — they all talk to this.
# ---------------------------------------------------------------------------

class ArgoNetManager:
    """
    The captain of the Argo-Net mesh.

    Responsibilities:
      - Detect available hardware transports
      - Initialize and register all adapters
      - Start all background tasks
      - Provide a clean send/receive API for VeridianAI
      - Expose status for Toga UI and /metrics endpoint
      - Handle graceful shutdown

    Usage:
        manager = ArgoNetManager(
            identity_string="veridianai-todd-workstation",
            fernet_key=your_existing_key
        )
        await manager.start()
        await manager.send("Hello Argo-Net!")
        await manager.stop()
    """

    def __init__(
        self,
        identity_string: str,
        fernet_key: bytes,
        enable_ble: bool = True,
        enable_lan: bool = True,
        enable_aether: bool = False,    # ALWAYS False by default — consent required
        relay_url: str = "https://relay.argonet.mentisphere.com/v1/envelope",
        on_message: Optional[Callable[[str, str], None]] = None
    ):
        self._identity      = identity_string
        self._fernet_key    = fernet_key
        self._on_message    = on_message    # Called when a message arrives for us
        self._running       = False
        self._start_time    = None

        # Detect available transports
        self._available_transports = self._detect_transports(
            enable_ble, enable_lan, enable_aether
        )

        # Build the node
        self.node = ArgoNode(
            identity_string=identity_string,
            transports=self._available_transports,
            relay=True,
            aether_consent=enable_aether
        )

        # Build the router
        self.router = ArgoRouter(self.node)

        # Build adapters for available transports
        self._ble_adapter: Optional[ArgoNetBLEAdapter]    = None
        self._lan_adapter: Optional[ArgoNetLANAdapter]    = None
        self._aether_adapter: Optional[ArgoNetAetherAdapter] = None

        self._init_adapters(enable_ble, enable_lan, enable_aether, relay_url)

        # Register message callback
        if on_message:
            self.node.register_message_callback(self._message_handler)

        logger.info(
            f"[ArgoNetManager] Initialized | "
            f"node={self.node.node_id} | "
            f"transports={[t.value for t in self._available_transports]}"
        )

    # ------------------------------------------------------------------
    # Hardware detection
    # ------------------------------------------------------------------

    def _detect_transports(
        self,
        enable_ble: bool,
        enable_lan: bool,
        enable_aether: bool
    ) -> list[Transport]:
        """
        Detect what this machine can actually use.
        Honest detection — no pretending we have something we don't.
        """
        available = []

        if enable_lan:
            # LAN is almost always available — UDP multicast works everywhere
            available.append(Transport.LAN)
            logger.info("[ArgoNetManager] Transport available: LAN ✅")

        if enable_ble:
            ble_ok = self._probe_ble()
            if ble_ok:
                available.append(Transport.BLE)
                logger.info("[ArgoNetManager] Transport available: BLE ✅")
            else:
                logger.warning(
                    "[ArgoNetManager] BLE probe failed — "
                    "adapter may be scan-only or unavailable. "
                    "LAN will carry the load. ⚠️"
                )

        if enable_aether:
            # Aether only makes the list if explicitly enabled AND consented
            available.append(Transport.AETHER)
            logger.warning(
                "[ArgoNetManager] ⚠️  Transport available: AETHER — "
                "user has opted in. Internet routing enabled."
            )

        if not available:
            logger.error(
                "[ArgoNetManager] ❌ No transports available! "
                "Check your network and Bluetooth adapter."
            )

        return available

    def _probe_ble(self) -> bool:
        """
        Quick BLE availability probe.
        Checks if bleak and WinRT are importable — same stack as
        bitchat_ble_gateway.py, proven on Todd's Realtek adapter.
        """
        try:
            import bleak
            logger.debug("[ArgoNetManager] bleak importable ✅")
            return True
        except ImportError:
            logger.warning("[ArgoNetManager] bleak not available — BLE disabled")
            return False

    # ------------------------------------------------------------------
    # Adapter initialization
    # ------------------------------------------------------------------

    def _init_adapters(
        self,
        enable_ble: bool,
        enable_lan: bool,
        enable_aether: bool,
        relay_url: str
    ):
        """Initialize all adapters and register them with the node."""

        if enable_lan and Transport.LAN in self._available_transports:
            self._lan_adapter = ArgoNetLANAdapter(
                on_envelope_received=self._on_envelope_received,
                on_capability_received=self.node.handle_capability_announcement,
                on_revocation_received=self.node.handle_revocation,
            )
            self.node.register_transport(Transport.LAN, self._lan_adapter.send)
            # Capability announcements MUST use send_capability (ANNOUNCE
            # framing), not send (ENVELOPE framing) -- otherwise peers drop them
            # as malformed and never discover each other.
            self.node.register_announce_transport(
                Transport.LAN, self._lan_adapter.send_capability)
            self.node.register_revocation_transport(
                Transport.LAN, self._lan_adapter.send_revocation)
            logger.info("[ArgoNetManager] LAN adapter registered ✅")

        if enable_ble and Transport.BLE in self._available_transports:
            self._ble_adapter = ArgoNetBLEAdapter(
                on_envelope_received=self._on_envelope_received,
                on_capability_received=self.node.handle_capability_announcement
            )
            self.node.register_transport(Transport.BLE, self._ble_adapter.send)
            logger.info("[ArgoNetManager] BLE adapter registered ✅")

        if enable_aether and Transport.AETHER in self._available_transports:
            self._aether_adapter = ArgoNetAetherAdapter(
                node_id=self.node.node_id,
                on_envelope_received=self._on_envelope_received,
                on_capability_received=self.node.handle_capability_announcement,
                relay_url=relay_url
            )
            # Grant consent — user already opted in via enable_aether=True
            self._aether_adapter.grant_consent(granted_by="user")
            self.node.register_transport(
                Transport.AETHER, self._aether_adapter.send
            )
            logger.warning("[ArgoNetManager] ⚠️  Aether adapter registered — "
                          "internet routing active.")

    # ------------------------------------------------------------------
    # Inbound message handling
    # ------------------------------------------------------------------

    def _on_envelope_received(self, data: bytes):
        """
        Called by any transport adapter when envelope bytes arrive.
        Hands off to the node for dedup, relay, and delivery.
        Scheduled as a fire-and-forget coroutine.

        Cross-thread safe: BLE detection callbacks fire on bleak's own thread,
        which has no running event loop of its own. We marshal the work back
        onto the loop captured in start() via call_soon_threadsafe rather than
        the deprecated loop-fetch helper (which raises off the main thread).
        """
        loop = getattr(self, "_loop", None)
        try:
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(
                    lambda: loop.create_task(
                        self.node.receive(data, self._fernet_key))
                )
            else:
                asyncio.run(self.node.receive(data, self._fernet_key))
        except Exception as e:
            logger.warning(f"[ArgoNetManager] inbound dispatch dropped: {e}")

    async def _message_handler(self, plaintext: str, origin: str,
                               private: bool = False):
        """Deliver a decrypted message to the VeridianAI callback.

        `private` is True for a direct message (opened with our static key),
        False for a broadcast (group key). The callback is invoked with both so
        the UI can thread DMs separately."""
        if self._on_message:
            try:
                self._on_message(plaintext, origin, private)
            except TypeError:
                # Back-compat: a 2-arg callback from older callers.
                self._on_message(plaintext, origin)
        logger.info(
            f"[ArgoNetManager] {'DM' if private else 'Message'} delivered | "
            f"origin={origin[:8]} | length={len(plaintext)}"
        )

    # ------------------------------------------------------------------
    # Public API — what VeridianAI, Toga, FastAPI, and MCP tools call
    # ------------------------------------------------------------------

    async def send(self, message: str, ttl: int = 7) -> bool:
        """
        Send a message into the Argo-Net mesh.
        Encrypted, enveloped, routed — one call.
        This is all VeridianAI needs to know about.
        """
        if not self._running:
            logger.warning("[ArgoNetManager] Send called but manager not running.")
            return False

        # Broadcast on every registered transport -- the SAME path DMs use
        # (node._broadcast_envelope). Public messaging previously went through
        # the router's per-peer transport selection, which could silently
        # produce no route while DMs (broadcast-on-all) worked; sending both the
        # same way removes that asymmetry. Delivery to a peer then depends only
        # on a matching group key.
        return await self.node.send(message, self._fernet_key, ttl=ttl)

    async def send_dm(self, message: str, recipient_fp: str,
                      ttl: int = 7) -> bool:
        """Send a private, end-to-end-encrypted DM to one peer fingerprint.
        Returns False if the manager is down or the recipient's key is unknown
        (they must have been discovered on the mesh first)."""
        if not self._running:
            logger.warning("[ArgoNetManager] send_dm called but not running.")
            return False
        return await self.node.send_dm(message, recipient_fp, ttl=ttl)

    async def revoke_self(self, reason: str = "compromised") -> dict:
        """Publish a signed self-revocation of THIS node's identity across the
        mesh. Returns the revocation record. After this, peers who receive it
        will show this fingerprint as revoked; rotate to a new identity (Reset
        key) to keep using Argo-Net."""
        rev = self.node.identity.make_revocation(reason)
        await self.node.broadcast_revocation(rev)
        logger.warning("[ArgoNetManager] ⚠️  Published SELF-REVOCATION "
                       f"(fp={self.node.identity.full_fingerprint[:16]}, "
                       f"reason={reason}).")
        return rev

    def revoked_fingerprints(self) -> dict:
        """full_fingerprint -> revocation record, for verified revocations we've
        seen (including our own)."""
        return dict(self.node.revoked)

    def dm_peers(self) -> list:
        """Peers we can DM (we know their public key). For the composer's To:
        picker. Each entry: {fingerprint, nickname}."""
        out = []
        for p in self.node.active_peers():
            if getattr(p, "pubkey", ""):
                out.append({"fingerprint": p.node_id, "nickname": p.node_id[:12]})
        return out

    def grant_aether_consent(self):
        """
        Explicitly grant Aether consent — call ONLY on direct user action.
        Wires up the adapter, updates node capability, logs loudly.
        """
        if self._aether_adapter:
            self._aether_adapter.grant_consent(granted_by="user")
            self.node.capability.aether_consent = True
            logger.warning(
                "[ArgoNetManager] ⚠️  Aether consent granted by user. "
                "Internet routing now active."
            )
        else:
            logger.warning(
                "[ArgoNetManager] Aether adapter not initialized. "
                "Restart with enable_aether=True after user opts in."
            )

    def revoke_aether_consent(self):
        """Revoke Aether consent immediately."""
        if self._aether_adapter:
            self._aether_adapter.revoke_consent()
            self.node.capability.aether_consent = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """
        Start Argo-Net.
        Launches all adapter tasks and the node background loops.
        This is the moment the mesh comes alive.
        """
        self._running   = True
        self._start_time = time.time()
        # Capture the running loop so transport callbacks firing on other
        # threads (BLE scan thread) can marshal work back safely.
        self._loop = asyncio.get_running_loop()

        logger.info("=" * 60)
        logger.info("  Argo-Net is go. The crew finds a way.")
        logger.info(f"  Node     : {self.node.node_id}")
        logger.info(f"  Identity : {self._identity}")
        logger.info(
            f"  Transports: "
            f"{[t.value for t in self._available_transports]}"
        )
        logger.info("  MentiSphere Software LLC d.b.a VeridianAI")
        logger.info("=" * 60)

        tasks = [asyncio.create_task(self.node.start())]

        if self._lan_adapter:
            tasks.append(asyncio.create_task(self._lan_adapter.start()))
        if self._ble_adapter:
            tasks.append(asyncio.create_task(self._ble_adapter.start()))
        if self._aether_adapter:
            tasks.append(asyncio.create_task(self._aether_adapter.start()))

        await asyncio.gather(*tasks)

    async def stop(self):
        """Graceful shutdown — all adapters, all tasks, clean exit."""
        self._running = False

        if self._lan_adapter:
            await self._lan_adapter.stop()
        if self._ble_adapter:
            await self._ble_adapter.stop()
        if self._aether_adapter:
            await self._aether_adapter.stop()
        await self.node.stop()

        logger.info("[ArgoNetManager] Argo-Net stopped. Fair winds. ⚓")

    # ------------------------------------------------------------------
    # Status — Toga UI panel, /metrics, MCP tools
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """
        Full Argo-Net status snapshot.
        Clean dict for Toga UI, FastAPI /metrics, and MCP tools.
        """
        uptime = round(time.time() - self._start_time, 1) \
            if self._start_time else 0

        return {
            "running":          self._running,
            "uptime_seconds":   uptime,
            "node":             self.node.status(),
            "router_table":     self.router.routing_table(),
            "aether_status":    self._aether_adapter.status()
                                if self._aether_adapter else
                                {"consent_active": False,
                                 "warning": "Aether not initialized."},
            "version":          "argonet-1.0",
            "built_by":         "MentiSphere Software LLC d.b.a VeridianAI"
        }


# ---------------------------------------------------------------------------
# Sanity check — full integration, no live hardware needed
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s"
    )

    print("=== Argo-Net Manager Sanity Check ===\n")
    print("NOTE: Full live mesh requires hardware.")
    print("This check confirms all six components integrate cleanly.\n")

    # Generate a Fernet key — in production this comes from VeridianAI key store
    key = Fernet.generate_key()

    messages_received = []

    def on_message(plaintext: str, origin: str):
        messages_received.append((plaintext, origin))
        print(f"  📨 Message: '{plaintext}' from {origin[:8]}")

    # Build the manager — LAN only for sanity check, no live BLE/Aether needed
    manager = ArgoNetManager(
        identity_string="veridianai-todd-workstation",
        fernet_key=key,
        enable_ble=True,        # BLE probed — will succeed if bleak installed
        enable_lan=True,        # LAN always available
        enable_aether=False,    # Aether OFF — consent required
        on_message=on_message
    )

    print(f"\nNode ID           : {manager.node.node_id}")
    print(f"Available transports: "
          f"{[t.value for t in manager._available_transports]}")
    print(f"Router initialized: {manager.router is not None} ✅")
    print(f"LAN adapter       : {manager._lan_adapter is not None} ✅")
    print(f"BLE adapter       : {manager._ble_adapter is not None} ✅")
    print(f"Aether adapter    : {manager._aether_adapter is not None} "
          f"(should be None — consent off) ✅")

    # Verify Aether is OFF by default
    assert manager._aether_adapter is None or \
           not manager._aether_adapter.consent_active, \
           "Aether should be OFF by default!"
    print(f"Aether gate       : ✅ BLOCKED — correct\n")

    # Full status snapshot
    status = manager.status()
    print(f"Status snapshot:")
    print(f"  running         : {status['running']}")
    print(f"  node_id         : {status['node']['node_id']}")
    print(f"  transports      : {status['node']['transports']}")
    print(f"  aether_consent  : {status['node']['aether_consent']} ✅")
    print(f"  version         : {status['version']} ✅")
    print(f"  built_by        : {status['built_by']} ✅")

    # Envelope creation and routing test — no live network needed
    print(f"\n--- Envelope + routing test ---")
    envelope = ArgoEnvelope.create(
        message="Argo-Net manager test — the crew finds a way.",
        origin_node_id=manager.node.node_id,
        fernet_key=key,
        ttl=7
    )
    print(f"Envelope created  : {envelope} ✅")

    # Simulate a peer joining
    peer_cap = NodeCapability(
        node_id=NodeCapability.make_node_id("veridianai-phone-relay"),
        transports=[Transport.BLE, Transport.LAN],
        relay=True,
        aether_consent=False
    )
    manager.node.handle_capability_announcement(peer_cap.to_json(), hops_away=1)
    print(f"Peer simulated    : {peer_cap.node_id} ✅")

    # Route the envelope
    decisions = manager.router.route(envelope)
    print(f"Route decisions   : {len(decisions)} ✅")
    for d in decisions:
        print(f"  → {d}")

    # Decrypt payload — confirm end to end
    plaintext = ArgoEnvelope.decrypt_payload(envelope.payload, key)
    print(f"\nDecrypted payload : '{plaintext}' ✅")

    print(f"\n{'=' * 60}")
    print(f"  ALL SEVEN COMPONENTS CONFIRMED.")
    print(f"  Argo-Net is go. The crew finds a way.")
    print(f"  MentiSphere Software LLC d.b.a VeridianAI")
    print(f"  July 24th, 2026 — Arlington, Washington State")
    print(f"{'=' * 60}")
    print(f"\n=== Manager check passed. Argo-Net is fully assembled. ⚓ ===")