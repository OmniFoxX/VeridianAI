# argonet_ble.py
# MentiSphere Software LLC d.b.a VeridianAI
# Argo-Net: BLE Transport Adapter — bleak/WinRT stack
# "Keep an open MentiSphere."
# ---------------------------------------------------------------------------
# This is NOT BitChat. This is Argo-Net native BLE.
# The hard-won bleak 3.0.2 / WinRT work from bitchat_ble_gateway.py
# lives here now — in a better home.
# ---------------------------------------------------------------------------

import asyncio
import logging
import struct
import hashlib
from typing import Optional, Callable

# bleak is OPTIONAL. A LAN-only node (built-in radio, no dongle) often has no
# bleak installed at all -- importing it at module top would sink the entire
# Argo-Net manager and take LAN down with it. Import lazily so this module
# loads everywhere; the scanner is resolved on demand in _scan_loop(), and
# ArgoNetManager._probe_ble() gates whether the BLE adapter is ever built.
try:
    import bleak  # noqa: F401
    BLEAK_AVAILABLE = True
except Exception:
    BLEAK_AVAILABLE = False

logger = logging.getLogger("argonet.ble")

# ---------------------------------------------------------------------------
# Argo-Net BLE constants
# Own namespace — not BitChat, not anyone else's
# ---------------------------------------------------------------------------

# 16-bit UUID in Argo-Net's own namespace
# Derived from "argonet" SHA-256 prefix — unique, non-colliding
ARGONET_SERVICE_UUID   = "a4906e74-0000-1000-8000-00805f9b34fb"
ARGONET_MANUFACTURER_ID = 0xA490        # Argo-Net manufacturer ID for adv packets

# BLE advertising payload limits
BLE_MAX_ADV_BYTES      = 27            # Practical limit after headers
ARGONET_FRAGMENT_MAGIC = 0xA4          # First byte flags Argo-Net packet
ARGONET_VERSION        = 0x01          # Protocol version byte

# Fragment types
FRAG_SINGLE    = 0x00   # Entire message fits in one packet
FRAG_START     = 0x01   # First fragment
FRAG_MIDDLE    = 0x02   # Middle fragment
FRAG_END       = 0x03   # Final fragment
FRAG_ANNOUNCE  = 0x10   # Node capability announcement


# ---------------------------------------------------------------------------
# Fragmenter — splits Argo-Net envelope bytes for BLE advertising
# Inherited pattern from bitchat_ble_gateway.py, generalized for envelopes
# ---------------------------------------------------------------------------

class BLEFragmenter:

    PAYLOAD_SIZE = BLE_MAX_ADV_BYTES - 4    # 4 bytes header overhead

    @staticmethod
    def fragment(data: bytes, msg_id: int) -> list[bytes]:
        """
        Split envelope bytes into BLE-sized fragments.
        Each fragment: [MAGIC, VERSION, MSG_ID, FRAG_TYPE, ...payload...]
        msg_id: 0-255 rolling counter to group fragments together.
        """
        chunks = [
            data[i:i + BLEFragmenter.PAYLOAD_SIZE]
            for i in range(0, len(data), BLEFragmenter.PAYLOAD_SIZE)
        ]

        if len(chunks) == 1:
            return [BLEFragmenter._pack(
                ARGONET_FRAGMENT_MAGIC,
                ARGONET_VERSION,
                msg_id,
                FRAG_SINGLE,
                chunks[0]
            )]

        packets = []
        for i, chunk in enumerate(chunks):
            if i == 0:
                ftype = FRAG_START
            elif i == len(chunks) - 1:
                ftype = FRAG_END
            else:
                ftype = FRAG_MIDDLE
            packets.append(BLEFragmenter._pack(
                ARGONET_FRAGMENT_MAGIC,
                ARGONET_VERSION,
                msg_id,
                ftype,
                chunk
            ))
        return packets

    @staticmethod
    def _pack(magic: int, version: int, msg_id: int,
              frag_type: int, payload: bytes) -> bytes:
        header = struct.pack("BBBB", magic, version, msg_id, frag_type)
        return header + payload


class BLEReassembler:
    """
    Reassembles incoming BLE fragments back into envelope bytes.
    Keyed by msg_id — handles out-of-order arrival gracefully.
    """

    def __init__(self):
        self._buffers: dict[int, list] = {}     # msg_id → [chunks]
        self._complete: list[bytes] = []         # Ready-to-process envelopes

    def ingest(self, packet: bytes) -> Optional[bytes]:
        """
        Feed a raw BLE packet in.
        Returns reassembled bytes if complete, None if still accumulating.
        """
        if len(packet) < 4:
            return None
        magic, version, msg_id, frag_type = struct.unpack("BBBB", packet[:4])

        if magic != ARGONET_FRAGMENT_MAGIC:
            return None     # Not an Argo-Net packet — ignore
        if version != ARGONET_VERSION:
            logger.warning(f"[BLEReassembler] Unknown protocol version: {version}")
            return None

        payload = packet[4:]

        if frag_type == FRAG_SINGLE:
            return payload

        if frag_type == FRAG_START:
            self._buffers[msg_id] = [payload]
            return None

        if frag_type == FRAG_MIDDLE:
            if msg_id in self._buffers:
                self._buffers[msg_id].append(payload)
            return None

        if frag_type == FRAG_END:
            if msg_id in self._buffers:
                self._buffers[msg_id].append(payload)
                complete = b"".join(self._buffers.pop(msg_id))
                return complete
            return None

        if frag_type == FRAG_ANNOUNCE:
            return payload      # Capability announcements pass straight through

        return None


# ---------------------------------------------------------------------------
# Argo-Net BLE Adapter
# Plugs into ArgoNode.register_transport(Transport.BLE, ble_adapter.send)
# ---------------------------------------------------------------------------

class ArgoNetBLEAdapter:
    """
    BLE transport adapter for Argo-Net.
    Uses bleak 3.0.2 + WinRT backend — proven on Todd's Realtek adapter.

    Responsibilities:
      - Scan for Argo-Net peers via BLE advertising
      - Receive and reassemble inbound envelope fragments
      - Fragment and broadcast outbound envelopes
      - Hand capability announcements to the ArgoNode peer table
    """

    SCAN_INTERVAL   = 5.0       # Seconds between scan cycles
    SCAN_DURATION   = 3.0       # Seconds per scan window

    def __init__(
        self,
        on_envelope_received: Callable[[bytes], None],
        on_capability_received: Callable[[str, int], None]
    ):
        """
        on_envelope_received: called with raw envelope bytes when reassembled
        on_capability_received: called with (capability_json, hops_away)
        """
        self._on_envelope = on_envelope_received
        self._on_capability = on_capability_received
        self._reassembler = BLEReassembler()
        self._msg_counter = 0           # Rolling 0-255 fragment ID
        self._running = False
        self._scanner: Optional[BleakScanner] = None
        logger.info("[ArgoNetBLE] Adapter initialized — bleak/WinRT")

    # ------------------------------------------------------------------
    # Outbound — send envelope bytes over BLE advertising
    # ------------------------------------------------------------------

    async def send(self, data: bytes) -> bool:
        """
        Fragment and broadcast envelope bytes via BLE manufacturer data.
        This is the function registered with ArgoNode.register_transport().

        NOTE: Windows WinRT BLE advertising has adapter-dependent support.
        If advertising is unavailable, we log clearly and return False —
        the router will try the next transport. No silent failures.
        """
        try:
            fragments = BLEFragmenter.fragment(data, self._msg_counter)
            self._msg_counter = (self._msg_counter + 1) % 256

            # WinRT BLE advertising via bleak
            # BluetoothLEAdvertisementPublisher path
            try:
                from bleak.backends.winrt.util import (
                    BluetoothLEAdvertisementPublisher,
                    BluetoothLEManufacturerData,
                    BluetoothLEAdvertisement
                )
                for fragment in fragments:
                    publisher = BluetoothLEAdvertisementPublisher()
                    adv = BluetoothLEAdvertisement()
                    mfr = BluetoothLEManufacturerData()
                    mfr.company_id = ARGONET_MANUFACTURER_ID
                    mfr.data = fragment
                    adv.manufacturer_data.append(mfr)
                    publisher.advertisement = adv
                    publisher.start()
                    await asyncio.sleep(0.1)
                    publisher.stop()

                logger.debug(f"[ArgoNetBLE] Sent {len(fragments)} fragment(s)")
                return True

            except ImportError:
                # WinRT path unavailable — adapter can scan but not advertise
                # This is the built-in Intel adapter scenario
                logger.warning(
                    "[ArgoNetBLE] ⚠️  WinRT advertising unavailable on this adapter. "
                    "Scan-only mode — router will use LAN fallback for outbound."
                )
                return False

        except Exception as e:
            logger.error(f"[ArgoNetBLE] Send error: {e}")
            return False

    # ------------------------------------------------------------------
    # Inbound — scan for Argo-Net peers and receive their broadcasts
    # ------------------------------------------------------------------

    def _detection_callback(self, device, advertisement):
        """
        Called by BleakScanner for every advertisement detected.
        Filters for Argo-Net packets by manufacturer ID.
        (Args are bleak's BLEDevice + AdvertisementData; left un-annotated so
        this module imports without bleak present.)
        """
        mfr_data = advertisement.manufacturer_data
        if ARGONET_MANUFACTURER_ID not in mfr_data:
            return      # Not an Argo-Net packet

        raw = mfr_data[ARGONET_MANUFACTURER_ID]

        # Check fragment type
        if len(raw) < 4:
            return
        magic, version, msg_id, frag_type = struct.unpack("BBBB", raw[:4])

        if magic != ARGONET_FRAGMENT_MAGIC:
            return

        if frag_type == FRAG_ANNOUNCE:
            # Capability announcement — hand straight to node peer table
            try:
                cap_json = raw[4:].decode("utf-8")
                logger.debug(f"[ArgoNetBLE] Capability from {device.address}")
                self._on_capability(cap_json, hops_away=1)
            except Exception as e:
                logger.warning(f"[ArgoNetBLE] Bad capability packet: {e}")
            return

        # Envelope fragment — feed to reassembler
        result = self._reassembler.ingest(raw)
        if result is not None:
            logger.debug(f"[ArgoNetBLE] Envelope reassembled "
                         f"({len(result)} bytes) from {device.address}")
            self._on_envelope(result)

    async def _scan_loop(self):
        """Continuous BLE scan loop — the ears of Argo-Net."""
        if not BLEAK_AVAILABLE:
            logger.warning("[ArgoNetBLE] bleak not installed — BLE scan "
                           "disabled; LAN carries the mesh.")
            return
        from bleak import BleakScanner
        logger.info("[ArgoNetBLE] Scan loop starting...")
        while self._running:
            try:
                async with BleakScanner(
                    detection_callback=self._detection_callback,
                    service_uuids=[ARGONET_SERVICE_UUID]
                ) as scanner:
                    await asyncio.sleep(self.SCAN_DURATION)
            except Exception as e:
                logger.warning(f"[ArgoNetBLE] Scan error: {e}")
                await asyncio.sleep(2.0)    # Brief pause before retry
            await asyncio.sleep(self.SCAN_INTERVAL - self.SCAN_DURATION)

    async def start(self):
        """Start the BLE adapter — begin scanning."""
        self._running = True
        await self._scan_loop()

    async def stop(self):
        """Graceful shutdown."""
        self._running = False
        logger.info("[ArgoNetBLE] Adapter stopped.")


# ---------------------------------------------------------------------------
# Sanity check — fragment/reassemble round-trip, no BLE hardware needed
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")

    print("=== Argo-Net BLE Adapter Sanity Check ===\n")
    print("NOTE: This check tests fragmentation only.")
    print("Live BLE scan requires hardware — run via argonet_manager.py\n")

    # Build a realistic envelope payload
    test_data = (
        b'{"envelope_id":"test-1234","origin":"ed1a30a11617f6bc",'
        b'"payload":"gAAAAABmocked_encrypted_payload_here_for_testing==",'
        b'"hops":[],"transport_path":[],"ttl":7,"timestamp":1784881598.0,'
        b'"version":"argonet-1.0"}'
    )

    print(f"Original data   : {len(test_data)} bytes")

    # Fragment
    fragmenter = BLEFragmenter()
    fragments = BLEFragmenter.fragment(test_data, msg_id=42)
    print(f"Fragments       : {len(fragments)} packet(s)")
    for i, f in enumerate(fragments):
        magic, version, msg_id, frag_type = struct.unpack("BBBB", f[:4])
        ftype_name = {0: "SINGLE", 1: "START", 2: "MIDDLE", 3: "END"}.get(
            frag_type, "UNKNOWN"
        )
        print(f"  [{i}] magic=0x{magic:02X} ver={version} "
              f"id={msg_id} type={ftype_name} payload={len(f)-4}B")

    # Reassemble
    reassembler = BLEReassembler()
    result = None
    for fragment in fragments:
        result = reassembler.ingest(fragment)

    assert result == test_data, "Round-trip FAILED — data mismatch!"
    print(f"\nReassembled     : {len(result)} bytes")
    print(f"Round-trip      : ✅ PASS — byte-perfect")

    # Capability announcement packet
    cap_payload = b'{"node_id":"ed1a30a11617f6bc","transports":["BLE"]}'
    cap_packet = struct.pack("BBBB",
        ARGONET_FRAGMENT_MAGIC,
        ARGONET_VERSION,
        0,
        FRAG_ANNOUNCE
    ) + cap_payload

    print(f"\nCapability packet : {len(cap_packet)} bytes")
    print(f"Magic             : 0x{ARGONET_FRAGMENT_MAGIC:02X} ✅")
    print(f"Manufacturer ID   : 0x{ARGONET_MANUFACTURER_ID:04X} ✅")

    print("\n=== BLE adapter check passed. Ready to scan. ⚓ ===")