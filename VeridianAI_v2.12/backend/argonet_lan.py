# argonet_lan.py
# MentiSphere Software LLC d.b.a VeridianAI
# Argo-Net: LAN Transport Adapter — Toga Network integration
# "Keep an open MentiSphere."
# ---------------------------------------------------------------------------
# Toga Network is already built and proven.
# This is just giving it its Argo-Net collar.
# ---------------------------------------------------------------------------

import asyncio
import json
import logging
import socket
import struct
import time
from typing import Callable, Optional

logger = logging.getLogger("argonet.lan")

# ---------------------------------------------------------------------------
# Argo-Net LAN constants
# Own namespace — clean, no collisions
# ---------------------------------------------------------------------------

ARGONET_LAN_PORT        = 47490         # Argo-Net LAN multicast port
ARGONET_MULTICAST_GROUP = "239.47.49.0" # Argo-Net multicast address
ARGONET_LAN_MAGIC       = b"ARGO"       # 4-byte packet identifier
ARGONET_LAN_VERSION     = 1
MAX_LAN_PACKET          = 65507         # Max UDP payload bytes
ANNOUNCE_PORT           = 47491         # Separate port for capability announcements


# ---------------------------------------------------------------------------
# Packet framing
# LAN has generous limits — most envelopes fit in one UDP datagram.
# We still frame them cleanly for future multi-packet support.
# ---------------------------------------------------------------------------

class LANFramer:
    """
    Frame/unframe Argo-Net envelopes for UDP transport.
    Header: [MAGIC(4)] [VERSION(1)] [TYPE(1)] [LENGTH(4)] = 10 bytes
    """

    TYPE_ENVELOPE   = 0x01
    TYPE_ANNOUNCE   = 0x02
    TYPE_HEARTBEAT  = 0x03
    TYPE_REVOCATION = 0x04

    HEADER_SIZE = 10

    @staticmethod
    def frame(data: bytes, packet_type: int) -> bytes:
        """Wrap data in Argo-Net LAN frame."""
        header = (
            ARGONET_LAN_MAGIC +
            struct.pack("BB", ARGONET_LAN_VERSION, packet_type) +
            struct.pack(">I", len(data))    # Big-endian length
        )
        return header + data

    @staticmethod
    def unframe(packet: bytes) -> Optional[tuple[int, bytes]]:
        """
        Unframe a received packet.
        Returns (packet_type, data) or None if invalid.
        """
        if len(packet) < LANFramer.HEADER_SIZE:
            return None
        magic = packet[:4]
        if magic != ARGONET_LAN_MAGIC:
            return None     # Not Argo-Net — ignore
        version = packet[4]
        if version != ARGONET_LAN_VERSION:
            logger.warning(f"[LANFramer] Unknown version: {version}")
            return None
        packet_type = packet[5]
        length = struct.unpack(">I", packet[6:10])[0]
        data = packet[10:10 + length]
        if len(data) != length:
            logger.warning("[LANFramer] Truncated packet — discarding")
            return None
        return (packet_type, data)


# ---------------------------------------------------------------------------
# Argo-Net LAN Adapter
# Plugs into ArgoNode.register_transport(Transport.LAN, lan_adapter.send)
# Uses UDP multicast — same pattern as Toga Network, Argo-Net native framing
# ---------------------------------------------------------------------------

class ArgoNetLANAdapter:
    """
    LAN transport adapter for Argo-Net.
    UDP multicast on 239.47.49.0:47490 — works on any local network.
    No server. No broker. No configuration.
    Every VeridianAI instance is a peer.

    Toga Network is already proven. This adapter inherits that foundation
    and speaks Argo-Net envelope framing natively.
    """

    HEARTBEAT_INTERVAL  = 10.0      # Seconds between heartbeats
    SOCKET_TIMEOUT      = 0.5       # Non-blocking receive timeout

    def __init__(
        self,
        on_envelope_received: Callable[[bytes], None],
        on_capability_received: Callable[[str, int], None],
        on_revocation_received: Optional[Callable[[str, int], None]] = None,
        bind_address: str = "0.0.0.0"
    ):
        """
        on_envelope_received : called with raw envelope bytes
        on_capability_received: called with (capability_json, hops_away=0)
        on_revocation_received: called with (revocation_json, hops_away=0)
        bind_address         : interface to bind — 0.0.0.0 for all
        """
        self._on_envelope    = on_envelope_received
        self._on_capability  = on_capability_received
        self._on_revocation  = on_revocation_received
        self._bind_address   = bind_address
        self._running        = False
        self._sock: Optional[socket.socket] = None
        self._announce_sock: Optional[socket.socket] = None
        self._local_ip       = self._detect_local_ip()
        logger.info(f"[ArgoNetLAN] Adapter initialized | "
                    f"local_ip={self._local_ip} "
                    f"multicast={ARGONET_MULTICAST_GROUP}:{ARGONET_LAN_PORT}")

    # ------------------------------------------------------------------
    # Network setup
    # ------------------------------------------------------------------

    def _detect_local_ip(self) -> str:
        """Detect this machine's LAN IP — works without internet."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("10.255.255.255", 1))
                return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"

    def _build_multicast_socket(self) -> socket.socket:
        """Build and configure UDP multicast socket for receiving.

        Bind hardening (2026-07-26, CodeQL py/bind-socket-all-network-
        interfaces): we bind to the MULTICAST GROUP address where the OS
        allows it (Linux/macOS). A group-bound socket receives ONLY
        datagrams addressed to the group — unicast sent straight at the
        port from any interface is refused by the kernel, which is
        exactly the exposure CodeQL flags. Windows cannot bind a group
        address (WSAEADDRNOTAVAIL) and needs INADDR_ANY to receive
        multicast, so it falls back to self._bind_address — that path is
        covered by the _is_lan_source() guard in the receive loop, which
        drops any datagram from a non-private source before parsing.
        """
        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
            socket.IPPROTO_UDP
        )
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass    # Windows doesn't have SO_REUSEPORT — that's fine

        try:
            # Preferred: group-bound socket (unicast physically undeliverable)
            sock.bind((ARGONET_MULTICAST_GROUP, ARGONET_LAN_PORT))
            logger.info("[ArgoNetLAN] Bound to multicast group address "
                        "(unicast to this port is kernel-refused)")
        except OSError:
            # Windows: INADDR_ANY is required for multicast reception.
            # Unicast reaching this socket is filtered by _is_lan_source().
            sock.bind((self._bind_address, ARGONET_LAN_PORT))
            logger.info("[ArgoNetLAN] Group-bind unsupported here — bound to "
                        f"{self._bind_address!r}; source-address guard active")

        # Join multicast group
        group = socket.inet_aton(ARGONET_MULTICAST_GROUP)
        iface = socket.inet_aton(self._local_ip)
        mreq = group + iface
        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            mreq
        )
        sock.settimeout(self.SOCKET_TIMEOUT)
        logger.info(f"[ArgoNetLAN] Joined multicast group "
                    f"{ARGONET_MULTICAST_GROUP}:{ARGONET_LAN_PORT}")
        return sock

    @staticmethod
    def _is_lan_source(ip: str) -> bool:
        """True iff a datagram source belongs on a LAN mesh: RFC1918
        private, link-local (169.254/16), or loopback. Everything else —
        which on a firewall-less machine means internet-sourced unicast —
        is dropped BEFORE any parsing. Part of the 0.0.0.0-bind hardening
        (see _build_multicast_socket)."""
        try:
            import ipaddress
            a = ipaddress.ip_address(ip)
            return a.is_private or a.is_link_local or a.is_loopback
        except ValueError:
            return False

    def _build_send_socket(self) -> socket.socket:
        """Build UDP socket for sending multicast."""
        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
            socket.IPPROTO_UDP
        )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 32)
        return sock

    # ------------------------------------------------------------------
    # Outbound — send envelope bytes over LAN multicast
    # ------------------------------------------------------------------

    async def send(self, data: bytes) -> bool:
        """
        Frame and broadcast envelope bytes via UDP multicast.
        This is the function registered with ArgoNode.register_transport().
        LAN is high bandwidth — no fragmentation needed for typical envelopes.
        """
        try:
            if len(data) > MAX_LAN_PACKET - LANFramer.HEADER_SIZE:
                logger.warning(
                    f"[ArgoNetLAN] Envelope too large for single UDP datagram "
                    f"({len(data)} bytes) — splitting not yet implemented, dropping."
                )
                return False

            framed = LANFramer.frame(data, LANFramer.TYPE_ENVELOPE)
            send_sock = self._build_send_socket()
            send_sock.sendto(
                framed,
                (ARGONET_MULTICAST_GROUP, ARGONET_LAN_PORT)
            )
            send_sock.close()
            logger.debug(f"[ArgoNetLAN] Sent envelope "
                         f"({len(framed)} bytes) via multicast")
            return True

        except Exception as e:
            logger.error(f"[ArgoNetLAN] Send error: {e}")
            return False

    async def send_revocation(self, revocation_json: str) -> bool:
        """Broadcast a signed self-revocation on the LAN (its own frame type)."""
        try:
            framed = LANFramer.frame(revocation_json.encode("utf-8"),
                                     LANFramer.TYPE_REVOCATION)
            send_sock = self._build_send_socket()
            send_sock.sendto(framed, (ARGONET_MULTICAST_GROUP, ARGONET_LAN_PORT))
            send_sock.close()
            logger.debug("[ArgoNetLAN] Revocation broadcast")
            return True
        except Exception as e:
            logger.error(f"[ArgoNetLAN] Revocation send error: {e}")
            return False

    async def send_capability(self, capability_json: str) -> bool:
        """
        Broadcast this node's capability announcement on the LAN.
        Called by ArgoNode._announce_loop() via registered transport.
        Peers receive this and update their peer tables.
        """
        try:
            data = capability_json.encode("utf-8")
            framed = LANFramer.frame(data, LANFramer.TYPE_ANNOUNCE)
            send_sock = self._build_send_socket()
            send_sock.sendto(
                framed,
                (ARGONET_MULTICAST_GROUP, ARGONET_LAN_PORT)
            )
            send_sock.close()
            logger.debug("[ArgoNetLAN] Capability announcement broadcast")
            return True
        except Exception as e:
            logger.error(f"[ArgoNetLAN] Capability send error: {e}")
            return False

    # ------------------------------------------------------------------
    # Inbound — receive loop
    # ------------------------------------------------------------------

    async def _receive_loop(self):
        """
        Continuous UDP receive loop.
        Runs in asyncio — yields control every SOCKET_TIMEOUT seconds.
        """
        logger.info("[ArgoNetLAN] Receive loop starting...")
        self._sock = self._build_multicast_socket()

        while self._running:
            try:
                # Run blocking recv in executor to stay async-friendly
                loop = asyncio.get_event_loop()
                packet, addr = await loop.run_in_executor(
                    None,
                    lambda: self._sock.recvfrom(MAX_LAN_PACKET)
                )

                # Ignore our own broadcasts
                if addr[0] == self._local_ip:
                    continue

                # Non-LAN source: drop before parsing (0.0.0.0-bind guard;
                # debug level so a WAN scanner can't flood the log).
                if not self._is_lan_source(addr[0]):
                    logger.debug(f"[ArgoNetLAN] Dropped non-LAN datagram "
                                 f"from {addr[0]}")
                    continue

                result = LANFramer.unframe(packet)
                if result is None:
                    continue

                packet_type, data = result

                if packet_type == LANFramer.TYPE_ENVELOPE:
                    logger.debug(f"[ArgoNetLAN] Envelope received "
                                 f"({len(data)}B) from {addr[0]}")
                    self._on_envelope(data)

                elif packet_type == LANFramer.TYPE_ANNOUNCE:
                    try:
                        cap_json = data.decode("utf-8")
                        logger.debug(f"[ArgoNetLAN] Capability from {addr[0]}")
                        # LAN peers are direct — hops_away = 0
                        self._on_capability(cap_json, 0)
                    except Exception as e:
                        logger.warning(f"[ArgoNetLAN] Bad capability: {e}")

                elif packet_type == LANFramer.TYPE_REVOCATION:
                    if self._on_revocation is not None:
                        try:
                            self._on_revocation(data.decode("utf-8"), 0)
                        except Exception as e:
                            logger.warning(f"[ArgoNetLAN] Bad revocation: {e}")

                elif packet_type == LANFramer.TYPE_HEARTBEAT:
                    logger.debug(f"[ArgoNetLAN] Heartbeat from {addr[0]}")

            except socket.timeout:
                continue    # Normal — just no packets this window
            except Exception as e:
                if self._running:
                    logger.warning(f"[ArgoNetLAN] Receive error: {e}")
                await asyncio.sleep(0.1)

    async def _heartbeat_loop(self):
        """Periodic heartbeat so peers know we're still on the LAN."""
        while self._running:
            try:
                framed = LANFramer.frame(
                    struct.pack(">d", time.time()),
                    LANFramer.TYPE_HEARTBEAT
                )
                send_sock = self._build_send_socket()
                send_sock.sendto(
                    framed,
                    (ARGONET_MULTICAST_GROUP, ARGONET_LAN_PORT)
                )
                send_sock.close()
                logger.debug("[ArgoNetLAN] Heartbeat sent")
            except Exception as e:
                logger.warning(f"[ArgoNetLAN] Heartbeat error: {e}")
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Start the LAN adapter — receive loop + heartbeat."""
        self._running = True
        logger.info("[ArgoNetLAN] Starting...")
        await asyncio.gather(
            self._receive_loop(),
            self._heartbeat_loop()
        )

    async def stop(self):
        """Graceful shutdown — leave multicast group cleanly."""
        self._running = False
        if self._sock:
            try:
                group = socket.inet_aton(ARGONET_MULTICAST_GROUP)
                iface = socket.inet_aton(self._local_ip)
                self._sock.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_DROP_MEMBERSHIP,
                    group + iface
                )
                self._sock.close()
            except Exception:
                pass
        logger.info("[ArgoNetLAN] Adapter stopped — left multicast group.")


# ---------------------------------------------------------------------------
# Sanity check — framing round-trip, no network hardware needed
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")

    print("=== Argo-Net LAN Adapter Sanity Check ===\n")
    print("NOTE: This check tests framing only.")
    print("Live multicast requires a network — run via argonet_manager.py\n")

    # Simulate an envelope payload
    test_envelope = (
        b'{"envelope_id":"lan-test-5678","origin":"ed1a30a11617f6bc",'
        b'"payload":"gAAAAABmocked_encrypted_payload_lan_test==",'
        b'"hops":["ed1a30a11617f6bc"],"transport_path":["LAN"],'
        b'"ttl":6,"timestamp":1784881598.0,"version":"argonet-1.0"}'
    )

    print(f"Original envelope : {len(test_envelope)} bytes")

    # Frame it
    framed = LANFramer.frame(test_envelope, LANFramer.TYPE_ENVELOPE)
    print(f"Framed packet     : {len(framed)} bytes "
          f"(+{len(framed)-len(test_envelope)}B header)")

    # Verify magic
    assert framed[:4] == ARGONET_LAN_MAGIC, "Magic mismatch!"
    print(f"Magic             : {framed[:4]} ✅")

    # Unframe it
    result = LANFramer.unframe(framed)
    assert result is not None, "Unframe returned None!"
    ptype, data = result
    assert ptype == LANFramer.TYPE_ENVELOPE, "Packet type mismatch!"
    assert data == test_envelope, "Data mismatch!"
    print(f"Packet type       : ENVELOPE (0x{ptype:02X}) ✅")
    print(f"Unframed data     : {len(data)} bytes ✅")
    print(f"Round-trip        : ✅ PASS — byte-perfect")

    # Capability announcement framing
    cap_json = '{"node_id":"ed1a30a11617f6bc","transports":["LAN"],"relay":true,"aether_consent":false}'
    cap_data = cap_json.encode("utf-8")
    framed_cap = LANFramer.frame(cap_data, LANFramer.TYPE_ANNOUNCE)
    result_cap = LANFramer.unframe(framed_cap)
    assert result_cap is not None
    ptype_cap, data_cap = result_cap
    assert ptype_cap == LANFramer.TYPE_ANNOUNCE
    assert data_cap == cap_data
    print(f"\nCapability frame  : {len(framed_cap)} bytes")
    print(f"Packet type       : ANNOUNCE (0x{ptype_cap:02X}) ✅")
    print(f"Round-trip        : ✅ PASS — byte-perfect")

    # Local IP detection
    adapter = ArgoNetLANAdapter(
        on_envelope_received=lambda d: None,
        on_capability_received=lambda c, h: None
    )
    print(f"\nLocal IP detected : {adapter._local_ip} ✅")
    print(f"Multicast group   : {ARGONET_MULTICAST_GROUP}:{ARGONET_LAN_PORT} ✅")
    print(f"Argo-Net magic    : {ARGONET_LAN_MAGIC} ✅")

    print("\n=== LAN adapter check passed. Ready to multicast. ⚓ ===")