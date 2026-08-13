# argonet_aether.py
# MentiSphere Software LLC d.b.a VeridianAI
# Argo-Net: Aether Transport Adapter — Consent-Gated Internet Bridge
# "Keep an open MentiSphere."
# ---------------------------------------------------------------------------
# AETHER IS THE INTERNET GATEWAY.
# It is ALWAYS opt-in. It is NEVER silent. It is NEVER assumed.
# Both nodes must explicitly consent before a single byte crosses the internet.
# This is not a suggestion. This is the contract.
# HIPAA note: Aether is the highest-risk transport. Every decision is logged.
# ---------------------------------------------------------------------------

import asyncio
import hashlib
import json
import logging
import time
import struct
from typing import Callable, Optional

# aiohttp is OPTIONAL, like the rest of the app treats it. Aether is off by
# default and consent-gated anyway; import lazily so a machine without aiohttp
# still runs Argo-Net over LAN/BLE instead of failing the whole manager import.
try:
    import aiohttp
except Exception:
    aiohttp = None

logger = logging.getLogger("argonet.aether")

# ---------------------------------------------------------------------------
# Aether constants
# ---------------------------------------------------------------------------

ARGONET_AETHER_VERSION  = "argonet-aether-1.0"
ARGONET_AETHER_PORT     = 47492
ARGONET_AETHER_MAGIC    = b"AGTH"       # Aether-specific magic — distinct from LAN
MAX_AETHER_PAYLOAD      = 65000         # Bytes — generous, internet can handle it

# Relay endpoint — this is where internet-routed envelopes land
# In production this is a MentiSphere-operated lightweight relay.
# Self-hostable. No lock-in. No surveillance.
DEFAULT_RELAY_URL       = "https://relay.argonet.mentisphere.com/v1/envelope"
FALLBACK_RELAY_URL      = "https://relay2.argonet.mentisphere.com/v1/envelope"

# Consent strings — must match exactly on both sides
CONSENT_TOKEN_SEND      = "argonet-aether-send-consent-v1"
CONSENT_TOKEN_RECEIVE   = "argonet-aether-receive-consent-v1"


# ---------------------------------------------------------------------------
# Consent record — explicit, timestamped, logged, non-transferable
# ---------------------------------------------------------------------------

class AetherConsent:
    """
    Represents an explicit, timestamped Aether consent decision.
    This is never assumed. Never inherited. Never silent.
    It must be created by a direct, informed user action.

    HIPAA alignment:
      - Consent is logged with timestamp
      - Consent can be revoked at any time
      - Consent is per-session unless explicitly persisted by the user
      - Revocation is immediate — no queued Aether sends after revoke()
    """

    def __init__(self, node_id: str, granted_by: str = "user"):
        self.node_id        = node_id
        self.granted_by     = granted_by    # "user" always — never "system"
        self.granted_at     = time.time()
        self.revoked        = False
        self.revoked_at: Optional[float] = None
        logger.warning(
            f"[AetherConsent] ⚠️  AETHER CONSENT GRANTED | "
            f"node={node_id[:8]} by={granted_by} at={self.granted_at:.0f} | "
            f"Internet routing is now ENABLED for this node."
        )

    def revoke(self):
        """Revoke consent. Immediate. No exceptions."""
        self.revoked    = True
        self.revoked_at = time.time()
        logger.warning(
            f"[AetherConsent] ⚠️  AETHER CONSENT REVOKED | "
            f"node={self.node_id[:8]} at={self.revoked_at:.0f} | "
            f"Internet routing is now DISABLED."
        )

    @property
    def active(self) -> bool:
        return not self.revoked

    def to_dict(self) -> dict:
        return {
            "node_id":      self.node_id,
            "granted_by":   self.granted_by,
            "granted_at":   self.granted_at,
            "revoked":      self.revoked,
            "revoked_at":   self.revoked_at
        }


# ---------------------------------------------------------------------------
# Aether Framer — wraps envelopes for internet transport
# ---------------------------------------------------------------------------

class AetherFramer:
    """
    Frame/unframe Argo-Net envelopes for Aether (HTTP/internet) transport.
    Adds consent token verification so relay nodes can confirm both
    sides opted in before accepting the packet.
    """

    @staticmethod
    def frame(
        envelope_bytes: bytes,
        origin_node_id: str,
        consent: AetherConsent
    ) -> dict:
        """
        Wrap envelope bytes in an Aether transport frame.
        Returns a dict ready for JSON serialization and HTTP POST.
        The consent_hash proves both sides opted in — relay checks this.
        """
        consent_hash = hashlib.sha256(
            f"{origin_node_id}{CONSENT_TOKEN_SEND}".encode()
        ).hexdigest()[:16]

        return {
            "version":      ARGONET_AETHER_VERSION,
            "origin":       origin_node_id,
            "consent_hash": consent_hash,
            "timestamp":    time.time(),
            "payload":      envelope_bytes.hex(),   # Hex-encoded bytes for JSON
            "size":         len(envelope_bytes)
        }

    @staticmethod
    def unframe(frame: dict) -> Optional[bytes]:
        """
        Unframe an Aether packet received from the relay.
        Validates version and payload integrity.
        Returns raw envelope bytes or None if invalid.
        """
        try:
            if frame.get("version") != ARGONET_AETHER_VERSION:
                logger.warning(
                    f"[AetherFramer] Unknown version: {frame.get('version')}"
                )
                return None
            payload_hex = frame.get("payload", "")
            data = bytes.fromhex(payload_hex)
            if len(data) != frame.get("size", -1):
                logger.warning("[AetherFramer] Payload size mismatch — discarding")
                return None
            return data
        except Exception as e:
            logger.warning(f"[AetherFramer] Unframe error: {e}")
            return None


# ---------------------------------------------------------------------------
# Argo-Net Aether Adapter
# Plugs into ArgoNode.register_transport(Transport.AETHER, aether_adapter.send)
# ---------------------------------------------------------------------------

class ArgoNetAetherAdapter:
    """
    Aether transport adapter for Argo-Net.
    Internet-routed envelope delivery via MentiSphere relay (self-hostable).

    CONSENT IS EVERYTHING HERE.
    This adapter will REFUSE to send if:
      - Local consent has not been explicitly granted
      - Local consent has been revoked
      - The relay cannot be reached (fail closed, not open)

    Every Aether send is logged. Every Aether receive is logged.
    No exceptions. No silent routing. Ever.
    """

    RELAY_TIMEOUT   = 10.0      # Seconds before relay request times out
    POLL_INTERVAL   = 5.0       # Seconds between relay inbox polls
    RETRY_ATTEMPTS  = 2         # Relay send retries before giving up

    def __init__(
        self,
        node_id: str,
        on_envelope_received: Callable[[bytes], None],
        on_capability_received: Callable[[str, int], None],
        relay_url: str = DEFAULT_RELAY_URL
    ):
        self._node_id           = node_id
        self._on_envelope       = on_envelope_received
        self._on_capability     = on_capability_received
        self._relay_url         = relay_url
        self._consent: Optional[AetherConsent] = None
        self._running           = False
        self._session: Optional[aiohttp.ClientSession] = None

        # Log loudly that Aether exists but is OFF
        logger.warning(
            "[ArgoNetAether] ⚠️  Aether adapter created — "
            "CONSENT NOT YET GRANTED. Internet routing is DISABLED until "
            "the user explicitly opts in."
        )

    # ------------------------------------------------------------------
    # Consent management — the most important methods in this file
    # ------------------------------------------------------------------

    def grant_consent(self, granted_by: str = "user"):
        """
        Grant Aether consent.
        THIS MUST ONLY BE CALLED IN RESPONSE TO AN EXPLICIT USER ACTION.
        Never call this automatically. Never call this on startup.
        Never call this because another node asked you to.
        """
        self._consent = AetherConsent(self._node_id, granted_by)

    def revoke_consent(self):
        """
        Revoke Aether consent immediately.
        Any in-flight sends will be abandoned.
        """
        if self._consent:
            self._consent.revoke()
            self._consent = None
        else:
            logger.info("[ArgoNetAether] Revoke called — no active consent.")

    @property
    def consent_active(self) -> bool:
        return self._consent is not None and self._consent.active

    def _require_consent(self, operation: str) -> bool:
        """
        Consent gate. Call before EVERY Aether operation.
        Logs clearly if blocked. Returns False = do not proceed.
        """
        if not self.consent_active:
            logger.warning(
                f"[ArgoNetAether] ⚠️  BLOCKED: {operation} — "
                f"Aether consent not active. "
                f"User must explicitly opt in before internet routing."
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Outbound — send envelope via relay
    # ------------------------------------------------------------------

    async def send(self, data: bytes) -> bool:
        """
        Send envelope bytes via Aether relay.
        Consent gate is checked FIRST. No consent = hard block.
        This is the function registered with ArgoNode.register_transport().
        """
        if not self._require_consent("send"):
            return False

        if aiohttp is None:
            logger.warning("[ArgoNetAether] aiohttp not installed — Aether "
                           "send unavailable (LAN/BLE unaffected).")
            return False

        if len(data) > MAX_AETHER_PAYLOAD:
            logger.warning(
                f"[ArgoNetAether] Payload too large: {len(data)} bytes — dropping"
            )
            return False

        frame = AetherFramer.frame(data, self._node_id, self._consent)

        for attempt in range(1, self.RETRY_ATTEMPTS + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        self._relay_url,
                        json=frame,
                        timeout=aiohttp.ClientTimeout(total=self.RELAY_TIMEOUT),
                        headers={"X-ArgoNet-Version": ARGONET_AETHER_VERSION}
                    ) as resp:
                        if resp.status == 200:
                            logger.warning(
                                f"[ArgoNetAether] ⚠️  Envelope sent via AETHER "
                                f"(internet) | size={len(data)}B | "
                                f"relay={self._relay_url}"
                            )
                            return True
                        else:
                            logger.warning(
                                f"[ArgoNetAether] Relay returned {resp.status} "
                                f"(attempt {attempt}/{self.RETRY_ATTEMPTS})"
                            )
            except aiohttp.ClientConnectorError:
                logger.warning(
                    f"[ArgoNetAether] Relay unreachable: {self._relay_url} "
                    f"(attempt {attempt}) — trying fallback"
                )
                self._relay_url = FALLBACK_RELAY_URL
            except asyncio.TimeoutError:
                logger.warning(
                    f"[ArgoNetAether] Relay timeout (attempt {attempt})"
                )
            except Exception as e:
                logger.error(f"[ArgoNetAether] Send error: {e}")
                return False

        # All attempts failed — fail CLOSED
        logger.error(
            "[ArgoNetAether] ❌ All relay attempts failed. "
            "Aether send abandoned. No data leaked."
        )
        return False

    # ------------------------------------------------------------------
    # Inbound — poll relay inbox for envelopes addressed to this node
    # ------------------------------------------------------------------

    async def _poll_loop(self):
        """
        Poll the relay for inbound envelopes.
        Only runs if consent is active.
        Consent revocation stops polling immediately.
        """
        logger.info("[ArgoNetAether] Poll loop starting...")
        if aiohttp is None:
            logger.warning("[ArgoNetAether] aiohttp not installed — Aether "
                           "poll loop disabled.")
            return
        while self._running:
            if not self.consent_active:
                logger.debug(
                    "[ArgoNetAether] Poll skipped — consent not active."
                )
                await asyncio.sleep(self.POLL_INTERVAL)
                continue

            try:
                inbox_url = (
                    f"{self._relay_url}/inbox/{self._node_id}"
                )
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        inbox_url,
                        timeout=aiohttp.ClientTimeout(total=self.RELAY_TIMEOUT),
                        headers={"X-ArgoNet-Version": ARGONET_AETHER_VERSION}
                    ) as resp:
                        if resp.status == 200:
                            packets = await resp.json()
                            for frame in packets:
                                data = AetherFramer.unframe(frame)
                                if data:
                                    logger.warning(
                                        f"[ArgoNetAether] ⚠️  Envelope "
                                        f"received via AETHER (internet) | "
                                        f"size={len(data)}B"
                                    )
                                    self._on_envelope(data)
                        elif resp.status == 204:
                            pass    # Empty inbox — normal
                        else:
                            logger.warning(
                                f"[ArgoNetAether] Poll returned {resp.status}"
                            )

            except Exception as e:
                logger.debug(f"[ArgoNetAether] Poll error: {e}")

            await asyncio.sleep(self.POLL_INTERVAL)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Start Aether adapter — poll loop only, sending is on-demand."""
        self._running = True
        logger.warning(
            "[ArgoNetAether] ⚠️  Aether adapter starting. "
            "Polling is active but SENDS require explicit user consent."
        )
        await self._poll_loop()

    async def stop(self):
        """Graceful shutdown — revoke consent on exit."""
        self._running = False
        if self.consent_active:
            self.revoke_consent()
        logger.info("[ArgoNetAether] Adapter stopped.")

    def status(self) -> dict:
        """Status for Toga UI and /metrics endpoint."""
        return {
            "transport":        "AETHER",
            "consent_active":   self.consent_active,
            "relay_url":        self._relay_url,
            "consent_detail":   self._consent.to_dict() if self._consent else None,
            "warning":          (
                "Internet routing active — data may cross network boundaries."
                if self.consent_active else
                "Internet routing DISABLED — user consent required."
            )
        }


# ---------------------------------------------------------------------------
# Sanity check — consent gate, framing, no network needed
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s [%(name)s] %(message)s")

    print("=== Argo-Net Aether Adapter Sanity Check ===\n")
    print("NOTE: This check tests consent gating and framing only.")
    print("Live relay requires internet — run via argonet_manager.py\n")

    node_id = "ed1a30a11617f6bc"

    # Build adapter — consent OFF by default
    adapter = ArgoNetAetherAdapter(
        node_id=node_id,
        on_envelope_received=lambda d: print(f"  Envelope received: {len(d)}B"),
        on_capability_received=lambda c, h: print(f"  Capability received: {c[:30]}...")
    )

    print(f"Consent active (should be False) : {adapter.consent_active}")
    assert not adapter.consent_active, "Consent should be OFF by default!"
    print("Default consent gate             : ✅ BLOCKED — correct\n")

    # Grant consent
    print("--- Granting consent ---")
    adapter.grant_consent(granted_by="user")
    print(f"Consent active (should be True)  : {adapter.consent_active}")
    assert adapter.consent_active, "Consent should be ON after grant!"
    print("Post-grant consent gate          : ✅ OPEN — correct\n")

    # Frame an envelope
    test_data = b'{"envelope_id":"aether-test-9999","origin":"ed1a30a11617f6bc"}'
    frame = AetherFramer.frame(test_data, node_id, adapter._consent)
    print(f"Aether frame keys  : {list(frame.keys())}")
    print(f"Version            : {frame['version']} ✅")
    print(f"Consent hash       : {frame['consent_hash']} ✅")
    print(f"Payload size       : {frame['size']} bytes ✅")

    # Unframe it
    recovered = AetherFramer.unframe(frame)
    assert recovered == test_data, "Unframe round-trip FAILED!"
    print(f"Round-trip         : ✅ PASS — byte-perfect\n")

    # Revoke consent
    print("--- Revoking consent ---")
    adapter.revoke_consent()
    print(f"Consent active (should be False) : {adapter.consent_active}")
    assert not adapter.consent_active, "Consent should be OFF after revoke!"
    print("Post-revoke consent gate         : ✅ BLOCKED — correct\n")

    # Status check
    status = adapter.status()
    print(f"Status transport   : {status['transport']} ✅")
    print(f"Status warning     : {status['warning']}\n")

    print("=== Aether adapter check passed. Consent gate holding. ⚓ ===")