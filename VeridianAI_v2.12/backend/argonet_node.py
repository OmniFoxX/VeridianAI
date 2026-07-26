# argonet_node.py
# MentiSphere Software LLC d.b.a VeridianAI
# Argo-Net: Node — Capability Discovery & Peer Announcements
# "Keep an open MentiSphere."
# ---------------------------------------------------------------------------

import asyncio
import json
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional
from argonet_envelope import NodeCapability, Transport, ArgoEnvelope

logger = logging.getLogger("argonet.node")

# ---------------------------------------------------------------------------
# Peer record — what we know about another node on the mesh
# ---------------------------------------------------------------------------

@dataclass
class Peer:
    node_id: str
    transports: list[Transport]
    relay: bool
    aether_consent: bool
    pubkey: str = ""               # X25519 static public key (hex); enables DMs
    sign_pubkey: str = ""          # Ed25519 public key (hex); binds fingerprint
    last_seen: float = field(default_factory=time.time)
    hops_away: int = 0              # 0 = direct, 1+ = relayed

    def is_stale(self, timeout: float = 60.0) -> bool:
        """Peer hasn't announced in 60s — consider offline."""
        return (time.time() - self.last_seen) > timeout

    def refresh(self):
        """Update last_seen on fresh announcement."""
        self.last_seen = time.time()

    def best_transport(self, local_transports: list[Transport]) -> Optional[Transport]:
        """
        Pick the best shared transport between us and this peer.
        Priority: LAN > BLE > CLASSIC > AETHER > SERIAL
        Aether only if BOTH nodes have consented.
        """
        priority = [
            Transport.LAN,
            Transport.BLE,
            Transport.CLASSIC,
            Transport.SERIAL,
            Transport.AETHER,
        ]
        shared = set(self.transports) & set(local_transports)
        for t in priority:
            if t == Transport.AETHER and not self.aether_consent:
                continue    # Never route Aether without explicit peer consent
            if t in shared:
                return t
        return None


# ---------------------------------------------------------------------------
# Argo-Net Node
# The living presence of VeridianAI on the mesh.
# Announces itself, discovers peers, maintains the peer table.
# ---------------------------------------------------------------------------

class ArgoNode:

    ANNOUNCE_INTERVAL = 15.0        # Seconds between capability broadcasts
    PEER_PRUNE_INTERVAL = 30.0      # Seconds between stale peer cleanup
    PEER_TIMEOUT = 60.0             # Seconds before a peer is considered gone

    def __init__(
        self,
        identity_string: str,
        transports: list[Transport],
        relay: bool = True,
        aether_consent: bool = False,
        identity=None
    ):
        # v2.12.18: node identity is now an X25519 keypair (persisted in
        # sage_data), and node_id is the fingerprint of its PUBLIC KEY -- so the
        # fingerprint you verify out-of-band is cryptographically bound to the
        # key, and DMs can be encrypted to it. identity_string is kept only as a
        # human display label. `identity` may be injected (tests running several
        # nodes in one process); production loads/persists one per machine.
        from argonet_identity import ArgoIdentity
        self.identity = identity or ArgoIdentity.load_or_create()
        self._display_name = identity_string
        self.node_id = self.identity.fingerprint
        self.capability = NodeCapability(
            node_id=self.node_id,
            transports=transports,
            relay=relay,
            aether_consent=aether_consent,
            pubkey=self.identity.pubkey_hex,
            sign_pubkey=self.identity.sign_pubkey_hex,
        )
        self.revoked: dict = {}     # full_fingerprint -> revocation record (verified)
        self.peers: dict[str, Peer] = {}        # node_id → Peer
        self._running = False
        self._seen_envelopes: set[str] = set()  # Loop guard for relayed envelopes
        self._transport_handlers = {}           # Transport → envelope send coroutine
        self._announce_handlers = {}            # Transport → capability send coroutine
        self._revocation_handlers = {}          # Transport → revocation send coroutine
        self._message_callbacks = []            # Called when a message arrives for us
        self._revocation_callbacks = []         # Called when a valid revocation lands

        logger.info(f"[ArgoNode] Initialized | id={self.node_id} "
                    f"transports={[t.value for t in transports]}")

    # ------------------------------------------------------------------
    # Transport handler registration
    # Each transport adapter (BLE, LAN, Aether) registers itself here.
    # ------------------------------------------------------------------

    def register_transport(self, transport: Transport, send_fn):
        """
        Register a send coroutine for a transport.
        send_fn signature: async def send(data: bytes) -> bool
        """
        self._transport_handlers[transport] = send_fn
        logger.info(f"[ArgoNode] Transport registered: {transport.value}")

    def register_announce_transport(self, transport: Transport, announce_fn):
        """Register a DISTINCT sender for capability announcements.

        This exists because announcements and message envelopes need different
        wire framing (a capability is NOT an ArgoEnvelope). Historically the
        announce loop reused the envelope `send`, so announcements went out
        framed as envelopes and every receiver dropped them as malformed --
        which is why peers never discovered each other. Transports that supply
        an announce sender (LAN's send_capability) use it; others fall back to
        the envelope sender.
        announce_fn signature: async def send_capability(cap_json: str) -> bool
        """
        self._announce_handlers[transport] = announce_fn
        logger.info(f"[ArgoNode] Announce sender registered: {transport.value}")

    def register_message_callback(self, callback):
        """
        Register a callback for inbound messages addressed to this node.
        callback signature: async def on_message(plaintext, origin, private)
        """
        self._message_callbacks.append(callback)

    def register_revocation_transport(self, transport: Transport, revoke_fn):
        """Register a sender for signed self-revocations (own wire framing)."""
        self._revocation_handlers[transport] = revoke_fn
        logger.info(f"[ArgoNode] Revocation sender registered: {transport.value}")

    def register_revocation_callback(self, callback):
        """Notify when a NEW valid revocation is accepted. callback(full_fp, rev)."""
        self._revocation_callbacks.append(callback)

    async def broadcast_revocation(self, rev: dict) -> bool:
        """Send a signed self-revocation out on every transport that can carry
        it. Also record it locally so our own view updates immediately."""
        self.handle_revocation(rev)   # local-first (also self-verifies)
        cap_json = json.dumps(rev, separators=(",", ":"))
        sent = False
        for t in self._transport_handlers:
            fn = self._revocation_handlers.get(t)
            if fn is None:
                continue
            try:
                await fn(cap_json)
                sent = True
            except Exception as e:
                logger.warning(f"[ArgoNode] Revocation send failed on {t.value}: {e}")
        return sent

    def handle_revocation(self, rev, hops_away: int = 0) -> bool:
        """Verify + record a signed self-revocation. Returns True if it is valid
        and newly recorded. Invalid or third-party 'revocations' are ignored --
        only a signature from the key bound to the fingerprint counts."""
        try:
            if isinstance(rev, (bytes, bytearray)):
                rev = json.loads(rev.decode("utf-8"))
            elif isinstance(rev, str):
                rev = json.loads(rev)
        except Exception:
            return False
        from argonet_identity import ArgoIdentity
        full = ArgoIdentity.verify_revocation(rev)
        if not full:
            logger.warning("[ArgoNode] Rejected revocation (bad signature or "
                           "key/fingerprint mismatch).")
            return False
        if full in self.revoked:
            return False   # already known
        self.revoked[full] = rev
        logger.warning(f"[ArgoNode] Identity self-revoked: {full[:16]} "
                       f"(reason={rev.get('reason')})")
        for cb in self._revocation_callbacks:
            try:
                cb(full, rev)
            except Exception:
                pass
        return True

    # ------------------------------------------------------------------
    # Peer table management
    # ------------------------------------------------------------------

    def handle_capability_announcement(self, raw: str, hops_away: int = 0):
        """
        Process an incoming NodeCapability announcement.
        Called by transport adapters when they receive a broadcast.
        """
        try:
            cap = NodeCapability.from_json(raw)
            if cap.node_id == self.node_id:
                return  # That's us — ignore our own echo

            # Identity binding: the fingerprint MUST be the hash of BOTH
            # announced public keys (X25519 + Ed25519). This stops an impostor
            # from claiming someone's fingerprint with their own key, and stops
            # them from substituting their own signing key (which would let them
            # forge a self-revocation of the victim). Reject on any mismatch.
            if cap.pubkey and cap.sign_pubkey:
                try:
                    from argonet_identity import bind_node_id
                    if bind_node_id(bytes.fromhex(cap.pubkey),
                                    bytes.fromhex(cap.sign_pubkey)) != cap.node_id:
                        logger.warning("[ArgoNode] Rejected announcement: keys "
                                       "do not match fingerprint (spoof?) "
                                       f"{cap.node_id}")
                        return
                except Exception as e:
                    logger.warning(f"[ArgoNode] Bad keys in announcement: {e}")
                    return

            if cap.node_id in self.peers:
                p = self.peers[cap.node_id]
                p.refresh()
                if cap.pubkey and not p.pubkey:
                    p.pubkey = cap.pubkey     # learn the keys on a later announce
                if cap.sign_pubkey and not p.sign_pubkey:
                    p.sign_pubkey = cap.sign_pubkey
                logger.debug(f"[ArgoNode] Peer refreshed: {cap.node_id}")
            else:
                peer = Peer(
                    node_id=cap.node_id,
                    transports=cap.transports,
                    relay=cap.relay,
                    aether_consent=cap.aether_consent,
                    pubkey=cap.pubkey,
                    sign_pubkey=cap.sign_pubkey,
                    hops_away=hops_away
                )
                self.peers[cap.node_id] = peer
                logger.info(f"[ArgoNode] New peer discovered: {cap.node_id} "
                            f"via {[t.value for t in cap.transports]} "
                            f"hops_away={hops_away} "
                            f"dm={'yes' if cap.pubkey else 'no-key'}")
        except Exception as e:
            logger.warning(f"[ArgoNode] Bad capability announcement: {e}")

    def get_peer(self, node_id: str) -> Optional[Peer]:
        return self.peers.get(node_id)

    def active_peers(self) -> list[Peer]:
        """Return all peers that aren't stale."""
        return [p for p in self.peers.values() if not p.is_stale(self.PEER_TIMEOUT)]

    def _prune_stale_peers(self):
        stale = [nid for nid, p in self.peers.items()
                 if p.is_stale(self.PEER_TIMEOUT)]
        for nid in stale:
            logger.info(f"[ArgoNode] Peer timed out, removing: {nid}")
            del self.peers[nid]

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send(
        self,
        message: str,
        fernet_key: bytes,
        ttl: int = 7
    ) -> bool:
        """
        Send a message into the mesh.
        Envelope is created, encrypted, then handed to the best
        available transport for this node's peer table.
        """
        envelope = ArgoEnvelope.create(
            message=message,
            origin_node_id=self.node_id,
            fernet_key=fernet_key,
            ttl=ttl
        )
        return await self._broadcast_envelope(envelope)

    async def send_dm(self, message: str, recipient_fp: str,
                      ttl: int = 7) -> bool:
        """Send a private, end-to-end-encrypted message to one peer.

        Requires that we know the recipient's public key (learned from their
        capability announcement). The DM is sealed to that key; it travels the
        same mesh (relays carry it blind) but only the recipient can open it.
        """
        peer = self.peers.get(recipient_fp)
        if peer is None or not peer.pubkey:
            logger.warning("[ArgoNode] Cannot DM %s -- no known public key "
                           "(peer must be discovered first).", recipient_fp[:8])
            return False
        try:
            box = self.identity.seal_dm(message, peer.pubkey)
        except Exception as e:
            logger.error("[ArgoNode] DM seal failed: %s", e)
            return False
        envelope = ArgoEnvelope.create_dm(box, self.node_id, recipient_fp, ttl=ttl)
        return await self._broadcast_envelope(envelope)

    async def _broadcast_envelope(self, envelope: ArgoEnvelope) -> bool:
        """
        Broadcast an envelope on all available registered transports.
        Stamps this node as a hop first.
        """
        if not envelope.stamp_hop(self.node_id, self._primary_transport()):
            logger.warning("[ArgoNode] Envelope rejected at stamp — TTL or loop.")
            return False

        data = envelope.to_bytes()
        sent_any = False

        for transport, send_fn in self._transport_handlers.items():
            try:
                ok = await send_fn(data)
                if ok:
                    logger.debug(f"[ArgoNode] Envelope sent via {transport.value}")
                    sent_any = True
            except Exception as e:
                logger.warning(f"[ArgoNode] Send failed on {transport.value}: {e}")

        return sent_any

    def _primary_transport(self) -> Transport:
        """Return this node's highest-priority available transport."""
        priority = [
            Transport.LAN,
            Transport.BLE,
            Transport.CLASSIC,
            Transport.SERIAL,
            Transport.AETHER
        ]
        for t in priority:
            if t in self.capability.transports:
                return t
        return Transport.BLE  # Fallback

    # ------------------------------------------------------------------
    # Receiving & Relay
    # ------------------------------------------------------------------

    async def receive(self, data: bytes, fernet_key: bytes):
        """
        Called by transport adapters when raw envelope bytes arrive.
        Handles: dedup, relay, delivery to local callbacks.
        """
        try:
            envelope = ArgoEnvelope.from_bytes(data)
        except Exception as e:
            logger.warning(f"[ArgoNode] Malformed envelope received: {e}")
            return

        # Stale envelope — drop silently (HIPAA: don't linger)
        if envelope.is_expired():
            logger.debug(f"[ArgoNode] Dropped expired envelope {envelope.envelope_id[:8]}")
            return

        # Our OWN envelope coming home (LAN multicast loops back to the sender,
        # and with a shared mesh key we can even decrypt it). The composer
        # already echoed this message into the feed, so delivering the loopback
        # would double it -- and we must never relay our own traffic back out.
        # origin is the ORIGINATING node fingerprint; if it's us, drop it. This
        # is the reliable self-filter (the LAN adapter's source-IP check is
        # unreliable for multicast loopback on Windows).
        if envelope.origin == self.node_id:
            logger.debug("[ArgoNode] Dropped own envelope looping back "
                         f"{envelope.envelope_id[:8]}")
            return

        # Already processed this envelope — loop guard
        if envelope.envelope_id in self._seen_envelopes:
            return
        self._seen_envelopes.add(envelope.envelope_id)

        if envelope.recipient:
            # DIRECT MESSAGE. Only the addressed recipient tries to open it;
            # everyone else carries it blind (never attempt the group key on a
            # DM). Group members cannot read it -- only our static private key
            # completes the box.
            if envelope.recipient == self.node_id and envelope.dm:
                try:
                    text = self.identity.open_dm(
                        envelope.dm, expected_origin_fp=envelope.origin)
                    logger.info(f"[ArgoNode] DM received from {envelope.origin}")
                    for cb in self._message_callbacks:
                        await cb(text, envelope.origin, True)   # private=True
                except Exception as e:
                    logger.warning(f"[ArgoNode] DM open failed "
                                   f"(not for us / bad box): {e}")
        else:
            # BROADCAST. Try the shared group key; success = deliver.
            try:
                plaintext = ArgoEnvelope.decrypt_payload(
                    envelope.payload, fernet_key)
                logger.info(f"[ArgoNode] Message received from {envelope.origin}")
                for cb in self._message_callbacks:
                    await cb(plaintext, envelope.origin, False)  # private=False
            except Exception:
                # Not for us, or different key — that's fine, relay it
                pass

        # Relay if we're a relay node and envelope still has TTL. DMs relay too
        # (that's how they reach an out-of-earshot recipient), still blind.
        if self.capability.relay and not envelope.already_seen(self.node_id):
            await self._relay(envelope)

    async def _relay(self, envelope: ArgoEnvelope):
        """Stamp and rebroadcast on all transports. Relay nodes carry, never read."""
        transport = self._primary_transport()
        if not envelope.stamp_hop(self.node_id, transport):
            return  # TTL exhausted or loop — drop

        data = envelope.to_bytes()
        for t, send_fn in self._transport_handlers.items():
            try:
                await send_fn(data)
                logger.debug(f"[ArgoNode] Relayed envelope via {t.value}")
            except Exception as e:
                logger.warning(f"[ArgoNode] Relay failed on {t.value}: {e}")

    # ------------------------------------------------------------------
    # Background tasks — announce presence, prune stale peers
    # ------------------------------------------------------------------

    async def _announce_loop(self):
        """Periodically broadcast this node's capabilities."""
        while self._running:
            await self._announce_once()
            logger.debug(f"[ArgoNode] Announced capabilities on "
                         f"{len(self._transport_handlers)} transport(s)")
            await asyncio.sleep(self.ANNOUNCE_INTERVAL)

    async def _announce_once(self) -> None:
        """Broadcast this node's capability on every transport, using each
        transport's dedicated ANNOUNCE sender when it has one (correct framing);
        falling back to the envelope sender only if none was registered."""
        cap_json = self.capability.to_json()
        for t in self._transport_handlers:
            ann = self._announce_handlers.get(t)
            try:
                if ann is not None:
                    await ann(cap_json)
                else:
                    await self._transport_handlers[t](cap_json.encode("utf-8"))
            except Exception as e:
                logger.warning(f"[ArgoNode] Announce failed on {t.value}: {e}")

    async def _prune_loop(self):
        """Periodically remove stale peers from the table."""
        while self._running:
            await asyncio.sleep(self.PEER_PRUNE_INTERVAL)
            self._prune_stale_peers()

    async def start(self):
        """Start the node — begin announcing and pruning."""
        self._running = True
        logger.info(f"[ArgoNode] Starting | id={self.node_id}")
        await asyncio.gather(
            self._announce_loop(),
            self._prune_loop()
        )

    async def stop(self):
        """Graceful shutdown."""
        self._running = False
        logger.info(f"[ArgoNode] Stopped | id={self.node_id}")

    # ------------------------------------------------------------------
    # Status — for the VeridianAI UI panel
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return a clean status dict for the Toga UI or /metrics endpoint."""
        return {
            "node_id": self.node_id,
            "transports": [t.value for t in self.capability.transports],
            "relay": self.capability.relay,
            "aether_consent": self.capability.aether_consent,
            "active_peers": len(self.active_peers()),
            "peers": [
                {
                    "node_id": p.node_id,
                    "transports": [t.value for t in p.transports],
                    "hops_away": p.hops_away,
                    "last_seen_ago": round(time.time() - p.last_seen, 1)
                }
                for p in self.active_peers()
            ]
        }


# ---------------------------------------------------------------------------
# Sanity check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")

    print("=== Argo-Net Node Sanity Check ===\n")

    # Simulate a node with BLE + LAN
    node = ArgoNode(
        identity_string="veridianai-todd-workstation",
        transports=[Transport.BLE, Transport.LAN],
        relay=True,
        aether_consent=False
    )

    # Simulate a peer announcing itself
    peer_cap = NodeCapability(
        node_id=NodeCapability.make_node_id("veridianai-phone-relay"),
        transports=[Transport.BLE],
        relay=True,
        aether_consent=False
    )
    node.handle_capability_announcement(peer_cap.to_json(), hops_away=1)

    # Simulate a second peer on LAN
    peer_cap2 = NodeCapability(
        node_id=NodeCapability.make_node_id("veridianai-pi-gateway"),
        transports=[Transport.LAN, Transport.AETHER],
        relay=True,
        aether_consent=False     # Aether off by default — good
    )
    node.handle_capability_announcement(peer_cap2.to_json(), hops_away=0)

    # Check best transport resolution
    p1 = node.get_peer(NodeCapability.make_node_id("veridianai-phone-relay"))
    p2 = node.get_peer(NodeCapability.make_node_id("veridianai-pi-gateway"))

    print(f"Peer 1 best transport : "
          f"{p1.best_transport(node.capability.transports)}")
    print(f"Peer 2 best transport : "
          f"{p2.best_transport(node.capability.transports)}")

    # Node status
    print(f"\nNode status:\n{json.dumps(node.status(), indent=2)}")

    print("\n=== Node check passed. Peers discovered. ⚓ ===")