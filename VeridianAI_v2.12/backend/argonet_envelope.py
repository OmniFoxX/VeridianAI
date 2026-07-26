# argonet_envelope.py
# MentiSphere Software LLC d.b.a VeridianAI
# Argo-Net: Transport-Agnostic Mesh Protocol — Envelope Core
# "Keep an open MentiSphere."
# ---------------------------------------------------------------------------

import uuid
import json
import time
import hashlib
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Optional
from cryptography.fernet import Fernet


# ---------------------------------------------------------------------------
# Transport capability flags
# A node announces what it has. Routing adapts to reality.
# ---------------------------------------------------------------------------

class Transport(str, Enum):
    BLE       = "BLE"        # Bluetooth Low Energy advertising/scan
    LAN       = "LAN"        # Wi-Fi / Ethernet local network (Toga Network)
    AETHER    = "AETHER"     # Internet gateway (opt-in, consent-gated)
    CLASSIC   = "CLASSIC"    # Bluetooth Classic RFCOMM (paired devices)
    SERIAL    = "SERIAL"     # USB / Serial (air-gap fallback)


# ---------------------------------------------------------------------------
# Node capability advertisement
# Broadcast this so the mesh knows how to route through you.
# NO PHI in here — fingerprint only, never names or identifiers.
# ---------------------------------------------------------------------------

@dataclass
class NodeCapability:
    node_id: str                          # SHA-256 fingerprint of the PUBLIC KEY
    transports: list[Transport]           # What this node can carry
    relay: bool = True                    # Will this node relay for others?
    aether_consent: bool = False          # Explicit opt-in required — never default True
    pubkey: str = ""                      # X25519 static public key (hex); DMs
    sign_pubkey: str = ""                 # Ed25519 public key (hex); signatures
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        d = asdict(self)
        d["transports"] = [t.value for t in self.transports]
        return json.dumps(d, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "NodeCapability":
        d = json.loads(raw)
        d["transports"] = [Transport(t) for t in d["transports"]]
        return cls(**d)

    @staticmethod
    def make_node_id(identity_string: str) -> str:
        """Derive a node fingerprint from any stable identity string.
        Never store raw identity in the mesh — fingerprint only."""
        return hashlib.sha256(identity_string.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# The Argo-Net Message Envelope
# One format. Every transport. Every hop.
# A relay node carries this — it never reads the payload.
# ---------------------------------------------------------------------------

@dataclass
class ArgoEnvelope:
    payload: str                              # Fernet-encrypted payload (base64 str); "" for a DM
    origin: str                               # Originating node fingerprint (NO PHI)
    envelope_id: str = field(
        default_factory=lambda: str(uuid.uuid4())
    )
    hops: list[str] = field(default_factory=list)   # Node fingerprints that relayed
    transport_path: list[str] = field(default_factory=list)  # Transport types used
    ttl: int = 7                              # Max hops before discard
    timestamp: float = field(default_factory=time.time)
    version: str = "argonet-1.0"
    # Direct-message fields. recipient == "" is a broadcast (group-key payload);
    # a non-empty recipient fingerprint marks a DM whose ciphertext lives in the
    # `dm` box (see argonet_identity.seal_dm). Relays carry a DM without reading
    # it; only the recipient's static private key can open the box.
    recipient: str = ""
    dm: Optional[dict] = None

    # ------------------------------------------------------------------
    # Hop management
    # ------------------------------------------------------------------

    def stamp_hop(self, node_id: str, transport: Transport) -> bool:
        """
        Record this node as a relay hop.
        Returns False if TTL is exhausted or loop detected — drop the packet.
        HIPAA note: node_id is a fingerprint, never a name or identifier.
        """
        if self.ttl <= 0:
            return False  # TTL exhausted — discard silently
        if node_id in self.hops:
            return False  # Loop detected — discard silently
        self.hops.append(node_id)
        self.transport_path.append(transport.value)
        self.ttl -= 1
        return True

    def is_expired(self, max_age_seconds: float = 300.0) -> bool:
        """Envelopes older than 5 minutes are stale. Drop them.
        Keeps relay nodes clean — no PHI lingers."""
        return (time.time() - self.timestamp) > max_age_seconds

    def already_seen(self, node_id: str) -> bool:
        """Loop guard — has this node already relayed this envelope?"""
        return node_id in self.hops

    # ------------------------------------------------------------------
    # Serialization — compact for BLE, readable for LAN/Aether
    # ------------------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    def to_bytes(self) -> bytes:
        """BLE-ready — compact UTF-8 bytes for fragmentation."""
        return self.to_json().encode("utf-8")

    @classmethod
    def from_json(cls, raw: str) -> "ArgoEnvelope":
        return cls(**json.loads(raw))

    @classmethod
    def from_bytes(cls, data: bytes) -> "ArgoEnvelope":
        return cls.from_json(data.decode("utf-8"))

    # ------------------------------------------------------------------
    # Payload encryption/decryption
    # Uses VeridianAI's existing Fernet key — no new crypto needed.
    # ------------------------------------------------------------------

    @staticmethod
    def encrypt_payload(message: str, fernet_key: bytes) -> str:
        """Encrypt before envelope creation. Relay nodes never see plaintext."""
        f = Fernet(fernet_key)
        return f.encrypt(message.encode()).decode()

    @staticmethod
    def decrypt_payload(encrypted_payload: str, fernet_key: bytes) -> str:
        """Decrypt only at the intended recipient. End-to-end."""
        f = Fernet(fernet_key)
        return f.decrypt(encrypted_payload.encode()).decode()

    # ------------------------------------------------------------------
    # Factory — the clean way to build a new outbound envelope
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        message: str,
        origin_node_id: str,
        fernet_key: bytes,
        ttl: int = 7
    ) -> "ArgoEnvelope":
        """
        Build a ready-to-send envelope.
        Message is encrypted before it enters the mesh.
        Origin is a fingerprint — never a name.
        """
        encrypted = cls.encrypt_payload(message, fernet_key)
        return cls(
            payload=encrypted,
            origin=origin_node_id,
            ttl=ttl
        )

    @classmethod
    def create_dm(
        cls,
        dm_box: dict,
        origin_node_id: str,
        recipient_fp: str,
        ttl: int = 7
    ) -> "ArgoEnvelope":
        """Build a direct-message envelope. The ciphertext + ephemeral/sender
        keys live in `dm_box` (from ArgoIdentity.seal_dm); `payload` is empty.
        Only the recipient can open it -- relays carry it blind."""
        return cls(
            payload="",
            origin=origin_node_id,
            recipient=recipient_fp,
            dm=dm_box,
            ttl=ttl,
        )

    def __repr__(self) -> str:
        return (
            f"<ArgoEnvelope id={self.envelope_id[:8]}... "
            f"origin={self.origin} hops={len(self.hops)} ttl={self.ttl} "
            f"path={'>'.join(self.transport_path) if self.transport_path else 'new'}>"
        )


# ---------------------------------------------------------------------------
# Quick sanity check — run this file directly to confirm it works
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== Argo-Net Envelope Sanity Check ===\n")

    # Generate a Fernet key (in real use, this comes from VeridianAI's key store)
    key = Fernet.generate_key()

    # Simulate node fingerprints — no real names, ever
    node_a = NodeCapability.make_node_id("veridianai-todd-workstation")
    node_b = NodeCapability.make_node_id("veridianai-phone-relay")
    node_c = NodeCapability.make_node_id("veridianai-pi-gateway")

    print(f"Node A fingerprint : {node_a}")
    print(f"Node B fingerprint : {node_b}")
    print(f"Node C fingerprint : {node_c}\n")

    # Node A creates an envelope
    env = ArgoEnvelope.create(
        message="Hello from Argo-Net! The crew finds a way.",
        origin_node_id=node_a,
        fernet_key=key,
        ttl=7
    )
    print(f"Created  : {env}\n")

    # Hop through Node B via BLE
    relayed = env.stamp_hop(node_b, Transport.BLE)
    print(f"Hop B (BLE)  relayed={relayed} : {env}")

    # Hop through Node C via LAN
    relayed = env.stamp_hop(node_c, Transport.LAN)
    print(f"Hop C (LAN)  relayed={relayed} : {env}\n")

    # Loop guard test — Node B tries to relay again
    loop = env.stamp_hop(node_b, Transport.BLE)
    print(f"Loop guard (Node B again) relayed={loop}  ← should be False\n")

    # Serialize / deserialize round-trip
    raw = env.to_json()
    restored = ArgoEnvelope.from_json(raw)
    print(f"Round-trip : {restored}\n")

    # Decrypt at destination
    message = ArgoEnvelope.decrypt_payload(restored.payload, key)
    print(f"Decrypted payload : '{message}'\n")

    # Capability broadcast
    cap = NodeCapability(
        node_id=node_a,
        transports=[Transport.BLE, Transport.LAN],
        relay=True,
        aether_consent=False
    )
    print(f"Node capability : {cap.to_json()}\n")

    print("=== All checks passed. Argo-Net is go. ⚓ ===")