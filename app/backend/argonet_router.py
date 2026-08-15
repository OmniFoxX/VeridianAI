# argonet_router.py
# MentiSphere Software LLC d.b.a VeridianAI
# Argo-Net: Router — Transport Decision Logic
# "Keep an open MentiSphere."
# ---------------------------------------------------------------------------

import logging
from dataclasses import dataclass
from typing import Optional
from argonet_envelope import ArgoEnvelope, Transport
from argonet_node import ArgoNode, Peer

logger = logging.getLogger("argonet.router")

# ---------------------------------------------------------------------------
# Routing decision — what the router returns for each envelope
# ---------------------------------------------------------------------------

@dataclass
class RouteDecision:
    envelope: ArgoEnvelope
    transport: Transport
    peer: Optional[Peer]        # None = broadcast to all
    reason: str                 # Human-readable — for logs and UI

    def __repr__(self):
        peer_id = self.peer.node_id[:8] if self.peer else "broadcast"
        return (f"<RouteDecision transport={self.transport.value} "
                f"peer={peer_id} reason='{self.reason}'>")


# ---------------------------------------------------------------------------
# Argo-Net Router
# Looks at the peer table, the envelope, and the available transports.
# Makes one clean decision: how does this envelope travel next?
# ---------------------------------------------------------------------------

class ArgoRouter:

    # Transport priority order — router always prefers left over right
    # Aether is last and consent-gated — it never sneaks in
    TRANSPORT_PRIORITY = [
        Transport.LAN,
        Transport.BLE,
        Transport.CLASSIC,
        Transport.SERIAL,
        Transport.AETHER,
    ]

    def __init__(self, node: ArgoNode):
        self.node = node
        logger.info("[ArgoRouter] Initialized.")

    # ------------------------------------------------------------------
    # Primary routing entry point
    # ------------------------------------------------------------------

    def route(self, envelope: ArgoEnvelope) -> list[RouteDecision]:
        """
        Given an envelope, return a list of RouteDecisions.
        One decision per transport the envelope should go out on.
        Empty list = envelope is unroutable (drop it cleanly).

        HIPAA note: routing decisions are based on fingerprints and
        transport capabilities only. No PHI touches the router.
        """
        if envelope.is_expired():
            logger.debug(f"[ArgoRouter] Dropping expired envelope "
                         f"{envelope.envelope_id[:8]}")
            return []

        if envelope.ttl <= 0:
            logger.debug(f"[ArgoRouter] Dropping TTL-exhausted envelope "
                         f"{envelope.envelope_id[:8]}")
            return []

        active_peers = self.node.active_peers()

        if not active_peers:
            logger.info("[ArgoRouter] No active peers — envelope queued locally.")
            return self._local_only_route(envelope)

        decisions = self._build_decisions(envelope, active_peers)

        if not decisions:
            logger.warning(f"[ArgoRouter] No viable route for envelope "
                           f"{envelope.envelope_id[:8]}")

        return decisions

    # ------------------------------------------------------------------
    # Decision builder
    # ------------------------------------------------------------------

    def _build_decisions(
        self,
        envelope: ArgoEnvelope,
        peers: list[Peer]
    ) -> list[RouteDecision]:
        """
        Build routing decisions for all reachable peers.
        Groups peers by best shared transport to minimize redundant broadcasts.
        Aether requires explicit consent from BOTH this node and the peer.
        """
        # Group peers by their best transport
        transport_groups: dict[Transport, list[Peer]] = {}

        for peer in peers:
            # Skip peers this envelope already visited
            if envelope.already_seen(peer.node_id):
                logger.debug(f"[ArgoRouter] Skipping already-seen peer "
                             f"{peer.node_id[:8]}")
                continue

            best = peer.best_transport(self.node.capability.transports)

            if best is None:
                logger.info(f"[ArgoRouter] No shared transport with peer "
                            f"{peer.node_id[:8]} — unreachable this hop")
                continue

            # Hard Aether gate — both nodes must consent
            if best == Transport.AETHER:
                if not self._aether_clear(peer):
                    logger.info(f"[ArgoRouter] Aether blocked for peer "
                                f"{peer.node_id[:8]} — consent not confirmed")
                    continue

            transport_groups.setdefault(best, []).append(peer)

        # One broadcast decision per transport group
        decisions = []
        for transport, group_peers in transport_groups.items():
            reason = (f"{transport.value} broadcast to "
                      f"{len(group_peers)} peer(s)")
            decisions.append(RouteDecision(
                envelope=envelope,
                transport=transport,
                peer=None,          # Broadcast — all peers on this transport
                reason=reason
            ))
            logger.info(f"[ArgoRouter] Route: {transport.value} → "
                        f"{len(group_peers)} peer(s) | "
                        f"envelope {envelope.envelope_id[:8]}")

        return decisions

    # ------------------------------------------------------------------
    # Aether consent gate
    # Both nodes must have explicitly opted in.
    # This is called loudly — no silent Aether routing, ever.
    # ------------------------------------------------------------------

    def _aether_clear(self, peer: Peer) -> bool:
        """
        Returns True ONLY if both this node and the peer have
        explicitly consented to Aether routing.
        If either side hasn't consented — hard block.
        """
        local_consent = self.node.capability.aether_consent
        peer_consent = peer.aether_consent

        if not local_consent:
            logger.warning("[ArgoRouter] ⚠️  Aether blocked — "
                           "LOCAL node has not consented.")
            return False
        if not peer_consent:
            logger.warning(f"[ArgoRouter] ⚠️  Aether blocked — "
                           f"PEER {peer.node_id[:8]} has not consented.")
            return False

        logger.info(f"[ArgoRouter] ✅ Aether consent confirmed — "
                    f"both nodes opted in. Routing via Aether.")
        return True

    # ------------------------------------------------------------------
    # Local-only fallback — no peers available
    # ------------------------------------------------------------------

    def _local_only_route(self, envelope: ArgoEnvelope) -> list[RouteDecision]:
        """
        No peers on the mesh right now.
        Return a local-hold decision so the manager can queue it.
        """
        return [RouteDecision(
            envelope=envelope,
            transport=self.node._primary_transport(),
            peer=None,
            reason="No active peers — holding locally for retry"
        )]

    # ------------------------------------------------------------------
    # Diagnostic — what would the router do right now?
    # Useful for the Toga status panel and /metrics endpoint.
    # ------------------------------------------------------------------

    def routing_table(self) -> list[dict]:
        """
        Return a human-readable routing table for all active peers.
        Shows what transport the router would use for each peer right now.
        For the Toga UI panel and /metrics.
        """
        table = []
        for peer in self.node.active_peers():
            best = peer.best_transport(self.node.capability.transports)
            aether_ok = (best == Transport.AETHER and
                         self._aether_clear(peer))
            table.append({
                "peer_id": peer.node_id,
                "hops_away": peer.hops_away,
                "best_transport": best.value if best else "UNREACHABLE",
                "aether_consented": aether_ok,
                "peer_transports": [t.value for t in peer.transports],
                "relay": peer.relay
            })
        return table


# ---------------------------------------------------------------------------
# Sanity check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import logging
    from cryptography.fernet import Fernet
    from argonet_envelope import NodeCapability

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")

    print("=== Argo-Net Router Sanity Check ===\n")

    # Build a node with BLE + LAN
    node = ArgoNode(
        identity_string="veridianai-argo-networknode",
        transports=[Transport.BLE, Transport.LAN],
        relay=True,
        aether_consent=False        # Aether off — gate should hold
    )

    # Add a BLE-only peer (phone, 1 hop away)
    cap_phone = NodeCapability(
        node_id=NodeCapability.make_node_id("veridianai-phone-relay"),
        transports=[Transport.BLE],
        relay=True,
        aether_consent=False
    )
    node.handle_capability_announcement(cap_phone.to_json(), hops_away=1)

    # Add a LAN peer (Pi, direct)
    cap_pi = NodeCapability(
        node_id=NodeCapability.make_node_id("veridianai-pi-gateway"),
        transports=[Transport.LAN, Transport.AETHER],
        relay=True,
        aether_consent=True         # Pi consented — but WE haven't, gate holds
    )
    node.handle_capability_announcement(cap_pi.to_json(), hops_away=0)

    # Add an Aether peer where BOTH sides consented
    cap_aether = NodeCapability(
        node_id=NodeCapability.make_node_id("veridianai-remote-node"),
        transports=[Transport.AETHER],
        relay=False,
        aether_consent=True
    )
    node.handle_capability_announcement(cap_aether.to_json(), hops_away=99)

    # Build the router
    router = ArgoRouter(node)

    # Create an envelope
    key = Fernet.generate_key()
    envelope = ArgoEnvelope.create(
        message="Argo-Net router test — the crew finds a way.",
        origin_node_id=node.node_id,
        fernet_key=key,
        ttl=7
    )

    print(f"Envelope : {envelope}\n")

    # Route it
    decisions = router.route(envelope)
    print(f"\nRouting decisions ({len(decisions)}):")
    for d in decisions:
        print(f"  {d}")

    # Routing table
    print(f"\nRouting table:")
    print(json.dumps(router.routing_table(), indent=2))

    # Aether gate test — local node has no consent, should block
    print("\n--- Aether gate test ---")
    aether_peer = node.get_peer(
        NodeCapability.make_node_id("veridianai-remote-node")
    )
    blocked = not router._aether_clear(aether_peer)
    print(f"Aether blocked (expected True): {blocked}")

    print("\n=== Router check passed. Routes decided. ⚓ ===")