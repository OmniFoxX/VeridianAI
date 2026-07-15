#!/usr/bin/env python3
"""
bitchat_winrt_gateway.py - BitChat gateway using a WinRT BLE *peripheral*.

Windows can't advertise via bleak, but a peripheral-capable USB dongle CAN via
WinRT's GattServiceProvider (proven: phones discover + connect + write to us).
This gateway advertises the BitChat service, and routes:
  - phone WRITE   -> BitchatClient.notification_handler (parse + protocol)
  - Sage send     -> characteristic.notify_value_async  (to subscribed phones)
Everything else (parsing, TLV announce, Noise prologue/transport/handshake) is
the already-verified bitchat.py, reused verbatim via a transport shim.

Sage is a PURE RESPONDER here: the phone connects and initiates; Sage answers.

Exposes the SAME WS contract as bitchat_ble_gateway.py (:8080), so OracleAI's
bitchat_bridge.py talks to it unchanged. Run this INSTEAD of the bleak gateway.

Requires (already present for the winrt test): winrt-Windows.Devices.Bluetooth.*,
winrt-Windows.Storage.Streams, winrt-Windows.Security.Cryptography, plus the
bitchat-python stack + fastapi/uvicorn/cryptography.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Set

_THIS_DIR = Path(__file__).resolve().parent
_ROOT = Path(os.environ.get("BITCHAT_PYTHON_ROOT", _THIS_DIR.parent))
_BITCHAT_PYTHON = _ROOT / "bitchat-python" / "bitchat"


def _force_local_import(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_c = _THIS_DIR / "bitchat_compression.py"
if _c.exists():
    _force_local_import("bitchat_compression", _c)
_force_local_import("fragmentation", _BITCHAT_PYTHON / "fragmentation.py")
_force_local_import("persistence", _BITCHAT_PYTHON / "persistence.py")
_force_local_import("encryption", _BITCHAT_PYTHON / "encryption.py")
_force_local_import("terminal_ux", _BITCHAT_PYTHON / "terminal_ux.py")
_force_local_import("bitchat", _BITCHAT_PYTHON / "bitchat.py")

from bitchat import (  # noqa: E402
    BitchatClient, BitchatMessage, BitchatPacket, MessageType,
    create_bitchat_packet_with_recipient,
    BITCHAT_SERVICE_UUID, BITCHAT_CHARACTERISTIC_UUID,
)

# v2.12.2: protocol constants come from bitchat_protocol_constants.json (the
# drift checker's single source of truth); the vendored bitchat.py literals
# remain the built-in fallback. Rebinds BOTH our local names and the bitchat
# module attributes (bitchat.py reads them at call time).
try:
    from bitchat_drift import load_constants as _load_pc, \
        active_service_uuid as _active_svc
    import bitchat as _bc_mod
    _pc = _load_pc()
    BITCHAT_SERVICE_UUID = _active_svc(_pc)
    BITCHAT_CHARACTERISTIC_UUID = _pc["characteristic_uuid"]
    _bc_mod.BITCHAT_SERVICE_UUID = BITCHAT_SERVICE_UUID
    _bc_mod.BITCHAT_CHARACTERISTIC_UUID = BITCHAT_CHARACTERISTIC_UUID
    print(f"[BitChat-WinRT] protocol constants from JSON "
          f"({_pc.get('active_network')}): svc={BITCHAT_SERVICE_UUID}")
except Exception as _pc_err:
    print(f"[BitChat-WinRT] constants JSON unavailable, using built-ins: {_pc_err}")

from winrt.windows.devices.bluetooth import BluetoothError  # noqa: E402
from winrt.windows.devices.bluetooth.genericattributeprofile import (  # noqa: E402
    GattServiceProvider, GattLocalCharacteristicParameters,
    GattCharacteristicProperties, GattServiceProviderAdvertisingParameters,
    GattProtectionLevel, GattWriteOption,
)
from winrt.windows.storage.streams import DataWriter  # noqa: E402
from winrt.windows.security.cryptography import CryptographicBuffer  # noqa: E402

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
import uvicorn  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [BitChat-WinRT] %(levelname)s %(message)s")
logger = logging.getLogger("bitchat.winrt")

def _configured_nickname() -> str:
    """v2.12.2: BLE announce name follows the owner's assistant_name (the
    v2.12.1 socials-reply rename never reached the BLE announce path -- this
    closes that gap). Falls back to 'Sage' on any config trouble."""
    try:
        with open(Path(__file__).resolve().parent.parent / "config.json",
                  encoding="utf-8") as f:
            return (json.load(f).get("sage", {}).get("assistant_name")
                    or "Sage").strip() or "Sage"
    except Exception:
        return "Sage"


NICKNAME = _configured_nickname()
WS_HOST = os.environ.get("BITCHAT_WS_HOST", "127.0.0.1")
WS_PORT = int(os.environ.get("BITCHAT_WS_PORT", "8080"))
_ble_error: Optional[str] = None   # set when peripheral advertising fails

ws_clients: Set[WebSocket] = set()
ws_nicknames: dict = {}
inbound_queue: asyncio.Queue = asyncio.Queue(maxsize=500)

_loop: Optional[asyncio.AbstractEventLoop] = None
_provider = None
_characteristic = None
_sage_client: Optional["SagePeripheralClient"] = None
_CHAR_SENTINEL = object()


# ---------------------------------------------------------------------------
# Transport shim: BitchatClient thinks it's writing to a bleak char; we notify.
# ---------------------------------------------------------------------------
class _WinRTTransport:
    def __init__(self):
        self.advertising = False
        self.subscribers = 0

    @property
    def is_connected(self) -> bool:
        return self.advertising

    _last_notify_log = 0.0

    async def write_gatt_char(self, characteristic, data, response=False):
        if _characteristic is None:
            return
        try:
            writer = DataWriter()
            writer.write_bytes(bytes(data))
            buf = writer.detach_buffer()
            results = await _characteristic.notify_value_async(buf)
            now = time.time()
            if now - self._last_notify_log > 6:
                self._last_notify_log = now
                try:
                    lst = list(results)
                    logger.info("[TX] notify len=%d -> %d client(s) status=%s",
                                len(data), len(lst), [int(r.status) for r in lst])
                except Exception as e:
                    logger.info("[TX] notify len=%d (result read err: %s)", len(data), e)
        except Exception as exc:
            logger.warning("[TX] notify failed (len=%d): %s", len(data), exc)

    async def disconnect(self):
        return None


class SagePeripheralClient(BitchatClient):
    def __init__(self, transport: _WinRTTransport):
        super().__init__()
        self.nickname = NICKNAME
        self.client = transport
        self.characteristic = _CHAR_SENTINEL

    async def display_message(self, message: BitchatMessage,
                             packet: BitchatPacket, is_private: bool):
        sender_nick = (
            (self.peers.get(packet.sender_id_str) and
             self.peers[packet.sender_id_str].nickname)
            or packet.sender_id_str
        )
        content = message.content
        if message.is_encrypted and message.channel:
            if message.channel in self.channel_keys:
                try:
                    creator_fp = self.channel_creators.get(message.channel, "")
                    content = self.encryption_service.decrypt_from_channel(
                        message.encrypted_content, message.channel,
                        self.channel_keys[message.channel], creator_fp)
                except Exception:
                    content = "[Encrypted - decryption failed]"
            else:
                content = "[Encrypted - join channel with password]"
        logger.info("[GW] RECV from %s: %s", sender_nick, (content or "")[:80])
        try:
            inbound_queue.put_nowait({
                "type": "message", "sender": sender_nick,
                "channel": message.channel or "general", "content": content,
                "timestamp": time.time(), "peer_id": packet.sender_id_str,
                "private": is_private})
        except asyncio.QueueFull:
            logger.warning("[GW] inbound_queue full")
        if is_private:
            self.chat_context.last_private_sender = (packet.sender_id_str, sender_nick)
            self.chat_context.add_dm(sender_nick, packet.sender_id_str)

    def feed_incoming(self, data: bytes):
        # A BLE central can split one packet across multiple writes (observed:
        # a 183-byte fragment arriving as 180 + 3). Reassemble by the packet's
        # declared length before parsing, or the partial fails and DM
        # handshakes / encrypted fragments get silently dropped.
        data = bytes(data)
        pending = getattr(self, "_rx_pending", b"")
        if pending:
            data = pending + data
            self._rx_pending = b""
        v = data[0] if data else 0
        if len(data) >= 16 and v in (1, 2):
            flags = data[11]
            if v == 2:                              # 16-byte header, 4-byte len
                payload_len = int.from_bytes(data[12:16], "big")
                need = 24 + payload_len             # 16 header + 8 sender
                off  = 24
                if flags & 0x01:                    # HAS_RECIPIENT
                    need += 8; off += 8
                if flags & 0x08:                    # HAS_ROUTE (v2)
                    if len(data) <= off:
                        self._rx_pending = data      # need the hop-count byte
                        return
                    need += 1 + data[off] * 8
                if flags & 0x02:                    # HAS_SIGNATURE
                    need += 64
            else:                                   # 14-byte header, 2-byte len
                payload_len = (data[12] << 8) | data[13]
                need = 22 + payload_len             # 14 header + 8 sender
                if flags & 0x01:
                    need += 8
                if flags & 0x02:
                    need += 64
            if 22 <= need <= 65536 and len(data) < need:
                self._rx_pending = data             # hold; wait for the rest
                return
        asyncio.create_task(self.notification_handler(None, data))

    _last_announce = 0.0

    async def announce_self(self, force: bool = False):
        now = time.time()
        if not force and now - self._last_announce < 8:   # debounce periodic only
            return
        self._last_announce = now
        try:
            await self.handshake()
        except Exception as exc:
            logger.warning("announce_self failed: %s", exc)


# ---------------------------------------------------------------------------
# WinRT peripheral
# ---------------------------------------------------------------------------
async def _handle_write(args):
    try:
        request = await args.get_request_async()
        data = bytes(CryptographicBuffer.copy_to_byte_array(request.value))
        try:
            _t = data[1] if len(data) > 1 else -1
            _names = {0x01: "ANNOUNCE", 0x02: "MESSAGE", 0x03: "LEAVE",
                      0x10: "NOISE_HANDSHAKE", 0x11: "NOISE_ENCRYPTED",
                      0x20: "FRAGMENT", 0x21: "REQUEST_SYNC", 0x22: "FILE_TRANSFER"}
            _fullhex = data.hex() if _t in (0x01, 0x20, 0x10) else data[:32].hex()
            logger.info("[RX-RAW] %d bytes type=%s hex=%s",
                        len(data), _names.get(_t, hex(_t)), _fullhex)
        except Exception:
            pass
        if _sage_client:
            _sage_client.feed_incoming(data)
        if request.option == GattWriteOption.WRITE_WITH_RESPONSE:
            request.respond()
    except Exception as exc:
        logger.error("[RX] write error: %s", exc)


def _on_write_requested(sender, args):
    deferral = args.get_deferral()
    fut = asyncio.run_coroutine_threadsafe(_handle_write(args), _loop)
    fut.add_done_callback(lambda _f: deferral.complete())


def _on_subscribers_changed(sender, args):
    try:
        n = len(sender.subscribed_clients)
    except Exception:
        n = 0
    if _sage_client:
        _sage_client.client.subscribers = n
    logger.info("[BLE] subscriber count -> %s", n)
    if n and _sage_client:
        asyncio.run_coroutine_threadsafe(_sage_client.announce_self(force=True), _loop)


async def _start_peripheral(transport: _WinRTTransport):
    global _provider, _characteristic
    result = await GattServiceProvider.create_async(uuid.UUID(BITCHAT_SERVICE_UUID))
    if result.error != BluetoothError.SUCCESS:
        raise RuntimeError(f"create_async: {result.error}")
    _provider = result.service_provider
    params = GattLocalCharacteristicParameters()
    params.characteristic_properties = (
        GattCharacteristicProperties.WRITE
        | GattCharacteristicProperties.WRITE_WITHOUT_RESPONSE
        | GattCharacteristicProperties.NOTIFY
    )
    params.write_protection_level = GattProtectionLevel.PLAIN
    params.read_protection_level = GattProtectionLevel.PLAIN
    cres = await _provider.service.create_characteristic_async(
        uuid.UUID(BITCHAT_CHARACTERISTIC_UUID), params)
    if cres.error != BluetoothError.SUCCESS:
        raise RuntimeError(f"create_characteristic: {cres.error}")
    _characteristic = cres.characteristic
    _characteristic.add_write_requested(_on_write_requested)
    _characteristic.add_subscribed_clients_changed(_on_subscribers_changed)

    adv = GattServiceProviderAdvertisingParameters()
    adv.is_connectable = True
    adv.is_discoverable = True
    _provider.start_advertising_with_parameters(adv)
    await asyncio.sleep(2)
    status = int(_provider.advertisement_status)
    transport.advertising = (status == 2)
    logger.info("[BLE] advertising '%s' service %s (status=%s)",
                NICKNAME, BITCHAT_SERVICE_UUID, status)
    if status != 2:
        logger.warning("[BLE] advertising status not 'Started' (%s)", status)


async def _initiate_pending():
    """LAZY Noise handshake (stock BitChat behaviour): only reach out to a peer
    we actually have a queued DM for. Proactively handshaking every connected
    peer collided with the phone's own handshake (two msg1's cross -> "insufficient
    data for static key" -> DM fails until a retry). Now the phone initiates for
    its DMs and Sage just responds; Sage only initiates when the user sends a DM
    (send_private_message queues it into pending_private_messages)."""
    while True:
        await asyncio.sleep(3)
        if not (_sage_client and _sage_client.client.is_connected):
            continue
        es = _sage_client.encryption_service
        pending = getattr(_sage_client, "pending_private_messages", {}) or {}
        for pid in list(pending.keys()):
            try:
                if es.is_session_established(pid):
                    continue
                if pid in es.handshake_states:
                    continue
                hs = es.initiate_handshake(pid)
                pkt = create_bitchat_packet_with_recipient(
                    _sage_client.my_peer_id, pid,
                    MessageType.NOISE_HANDSHAKE_INIT, hs, None)
                d = bytearray(pkt); d[2] = 3
                await _sage_client.send_packet(bytes(d))
                logger.info("[GW] initiated Noise handshake with %s (pending DM)", pid)
            except Exception as exc:
                logger.warning("[GW] initiate handshake failed for %s: %s", pid, exc)


async def _advertising_watchdog():
    """v2.12.6: keep Toga DISCOVERABLE while phones are connected.

    Field observation (Todd, 2 iPhones + S22): the first phone to connect
    got exclusive access -- everyone else stopped seeing Toga until a
    gateway restart, which then flipped exclusivity to whoever connected
    next. Root cause: many BLE adapters PAUSE advertising while a central
    connection is active, and Windows does not always resume it. This
    watchdog polls the advertisement status and re-asserts advertising so
    additional phones can still discover and connect (true mesh behaviour).
    If the adapter genuinely cannot advertise during a connection, the log
    says so explicitly instead of leaving a silent one-phone limit."""
    global _ble_error
    _last_status = None
    _fail_streak = 0
    while True:
        await asyncio.sleep(10)
        if not _provider:
            continue
        try:
            status = int(_provider.advertisement_status)
        except Exception:
            continue
        if status != _last_status:
            logger.info("[BLE] advertisement status -> %s (2=Started)", status)
            _last_status = status
        if status == 2:                       # advertising fine
            _fail_streak = 0
            continue
        # Not advertising. Try to re-assert (stop is best-effort; a provider
        # that was never started tolerates it).
        try:
            try:
                _provider.stop_advertising()
            except Exception:
                pass
            await asyncio.sleep(0.5)
            adv = GattServiceProviderAdvertisingParameters()
            adv.is_connectable = True
            adv.is_discoverable = True
            _provider.start_advertising_with_parameters(adv)
            await asyncio.sleep(1.5)
            new_status = int(_provider.advertisement_status)
            if new_status == 2:
                logger.info("[BLE] watchdog RESTARTED advertising -- Toga is "
                            "discoverable again (subscribers: %s)",
                            _sage_client.client.subscribers if _sage_client else "?")
                _fail_streak = 0
            else:
                _fail_streak += 1
                logger.warning("[BLE] watchdog could not restart advertising "
                               "(status=%s, attempt %d)", new_status, _fail_streak)
                if _fail_streak == 3:
                    logger.error("[BLE] adapter appears UNABLE to advertise while "
                                 "a central is connected -- that means ONE phone "
                                 "at a time. A dongle with concurrent-advertising "
                                 "support (or a second dongle) lifts the limit.")
        except Exception as exc:
            logger.warning("[BLE] watchdog error: %s", exc)


async def _periodic_announce():
    last = 0
    while True:
        await asyncio.sleep(3)
        if not (_sage_client and _provider):
            continue
        now = time.time()
        if now - last > 15:
            last = now
            if _sage_client.client.is_connected:
                await _sage_client.announce_self()


# ---------------------------------------------------------------------------
# WS contract (identical to bitchat_ble_gateway.py)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sage_client, _loop
    _loop = asyncio.get_running_loop()
    transport = _WinRTTransport()
    _sage_client = SagePeripheralClient(transport)
    logger.info("[GW] Sage peer_id=%s", _sage_client.my_peer_id)
    try:
        await _start_peripheral(transport)
    except Exception as exc:
        # v2.12.2: don't just log and play healthy -- remember the failure so
        # /health and /api/info tell the truth. Field case (Todd's machine):
        # the peripheral-capable Realtek adapter lost its driver, Windows fell
        # back to a central-only Broadcom, and this error was the ONLY sign
        # BitChat was down while everything else reported "ready".
        global _ble_error
        _ble_error = "peripheral advertising failed"  # generic; full exception is logged below -> keeps exception text out of the /api/info & /health responses (defense-in-depth if the gateway is ever bound non-loopback)
        logger.error("[BLE] peripheral start failed: %s", exc)
        logger.error("[BLE] Sage will NOT be discoverable. If this adapter "
                     "doesn't support the BLE peripheral role, the central-"
                     "role fallback gateway can still reach phones "
                     "(tier_lifecycle picks it automatically at next spawn).")
    relay = asyncio.create_task(_inbound_relay_loop())
    ann = asyncio.create_task(_periodic_announce())
    initk = asyncio.create_task(_initiate_pending())
    advw = asyncio.create_task(_advertising_watchdog())
    # nosemgrep -- LOG string, not a live socket. WS_HOST defaults to 127.0.0.1
    # (loopback); "ws://" here is only the advertised local URL for the app UI.
    logger.info("[GW] ready - WS on ws://%s:%d/ws", WS_HOST, WS_PORT)
    yield
    for t in (relay, ann, initk, advw):
        t.cancel()
    try:
        if _provider:
            _provider.stop_advertising()
    except Exception:
        pass


app = FastAPI(title="OracleAI BitChat WinRT Gateway", version="1.0.0", lifespan=lifespan)


@app.get("/api/info")
async def api_info():
    peers = _sage_client.display_peers() if _sage_client else []
    connected = bool(_sage_client and _sage_client.client.is_connected)
    return JSONResponse({
        "name": "OracleAI BitChat WinRT Gateway",
        "websocket_url": f"ws://localhost:{WS_PORT}/ws",
        "peers": peers,
        "status": "ble_failed" if _ble_error else ("ok" if connected else "advertising"),
        "ble_error": _ble_error,
        "bitchat_ready": _ble_error is None})


@app.get("/health")
async def health():
    return JSONResponse({
        "status": "degraded" if _ble_error else "ok",
        "ble_error": _ble_error,
        "ble_peers": len(_sage_client.peers) if _sage_client else 0,
        "advertising": bool(_provider),
        "advertisement_status": (int(_provider.advertisement_status)
                                 if _provider else None),
        "subscribers": _sage_client.client.subscribers if _sage_client else 0})


def _fmt_fp(hex_fp: str) -> str:
    """Group a 64-hex-char SHA-256 fingerprint into readable 4-char blocks --
    the shape humans actually compare out-of-band. v2.12.5: show ALL 64 hex
    (16 blocks). The phone's BitChat app displays the full fingerprint; our
    old 32-hex cut meant the two sides could never be compared block-by-block
    ("16 groups on the phone, 8 in the app")."""
    h = (hex_fp or "").replace(":", "").replace(" ", "").lower()
    return " ".join(h[i:i + 4] for i in range(0, len(h), 4))


@app.get("/api/identity")
async def api_identity():
    """v2.12.3: expose Toga's own identity fingerprint plus each connected
    peer's, so a human can verify (read the block aloud, compare on the phone)
    that the peer they're talking to is who they think -- the standard defense
    against a man-in-the-middle on the mesh. Fingerprints are derived from the
    Noise X25519 static public keys; nothing secret leaves the gateway."""
    if not _sage_client:
        return JSONResponse({"available": False})
    es = _sage_client.encryption_service
    try:
        my_fp = es.get_identity_fingerprint()
    except Exception:
        my_fp = ""
    peers = []
    for pid, peer in list(_sage_client.peers.items()):
        try:
            pfp = es.get_peer_fingerprint(pid)
        except Exception:
            pfp = None
        peers.append({
            "peer_id": pid,
            "nickname": getattr(peer, "nickname", None) or pid,
            "fingerprint": _fmt_fp(pfp) if pfp else None,
            "verified": bool(pfp),   # a fingerprint exists only post-handshake
        })
    return JSONResponse({
        "available": True,
        "nickname": NICKNAME,
        "peer_id": _sage_client.my_peer_id,
        "fingerprint": _fmt_fp(my_fp),
        "peers": peers})


@app.post("/shutdown")
async def shutdown():
    try:
        if _provider:
            _provider.stop_advertising()
    except Exception:
        pass
    asyncio.get_event_loop().call_later(0.3, lambda: os._exit(0))
    return JSONResponse({"status": "stopping"})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    ws_nicknames[ws] = NICKNAME
    logger.info("[WS] client connected - %d total", len(ws_clients))
    try:
        while True:
            data = json.loads(await ws.receive_text())
            mtype = data.get("type")
            if mtype == "register":
                nick = data.get("nickname", NICKNAME)
                ws_nicknames[ws] = nick
                if _sage_client:
                    _sage_client.nickname = nick
                await ws.send_text(json.dumps(
                    {"type": "ack", "status": "registered", "nickname": nick}))
            elif mtype == "message":
                await _handle_outbound(data.get("channel", "general"),
                                       data.get("content", ""))
            elif mtype == "peers":
                peers = _sage_client.display_peers() if _sage_client else []
                await ws.send_text(json.dumps({"type": "peers", "peers": peers}))
    except WebSocketDisconnect:
        logger.info("[WS] client disconnected")
    except Exception as exc:
        logger.error("[WS] error: %s", exc)
    finally:
        ws_clients.discard(ws)
        ws_nicknames.pop(ws, None)


def _resolve_peer_by_nick(nick: str):
    """Find a connected peer's (peer_id_hex, nickname) by nickname, case-insensitive."""
    if not _sage_client:
        return None
    target = nick.strip().lstrip("@").lower()
    for pid, peer in list(_sage_client.peers.items()):
        pnick = getattr(peer, "nickname", None)
        if pnick and pnick.lower() == target:
            return pid, pnick
    return None


def _notice(text: str):
    try:
        inbound_queue.put_nowait({
            "type": "message", "sender": "system", "channel": "general",
            "content": text, "timestamp": time.time(), "peer_id": "system",
            "private": False, "echo": True})
    except asyncio.QueueFull:
        pass


async def _handle_outbound(channel: str, content: str):
    # Slash-command DM support (BitChat-style): "/msg <nick> <text>",
    # "/m <nick> <text>", or "/dm <nick> <text>" sends an ENCRYPTED private
    # message to that peer instead of a public broadcast.
    _stripped = content.strip()
    _low = _stripped.lower()
    for _c in ("/msg ", "/dm ", "/m "):
        if _low.startswith(_c):
            _rest = _stripped[len(_c):].strip()
            parts = _rest.split(None, 1)
            if len(parts) < 2:
                _notice("Usage: /msg <nick> <message>")
                return
            nick_arg, text = parts[0], parts[1]
            if len(text) >= 2 and text[0] == text[-1] == '"':
                text = text[1:-1]
            resolved = _resolve_peer_by_nick(nick_arg)
            if not resolved:
                _notice(f"No connected peer named '{nick_arg}'")
                return
            peer_id, pnick = resolved
            try:
                inbound_queue.put_nowait({
                    "type": "message", "sender": NICKNAME, "channel": pnick,
                    "content": text, "timestamp": time.time(),
                    "peer_id": "self", "private": True, "echo": True})
            except asyncio.QueueFull:
                pass
            if not _sage_client or not _sage_client.client.is_connected:
                logger.warning("[GW] not advertising - DM not sent")
                return
            await _sage_client.send_private_message(text, peer_id, pnick)
            logger.info("[GW] DM -> %s (%s): %s", pnick, peer_id, text)
            return

    if not content.startswith("DM:"):
        try:
            inbound_queue.put_nowait({
                "type": "message", "sender": NICKNAME,
                "channel": channel or "general", "content": content,
                "timestamp": time.time(), "peer_id": "self",
                "private": False, "echo": True})
        except asyncio.QueueFull:
            pass
    if not _sage_client or not _sage_client.client.is_connected:
        logger.warning("[GW] not advertising - message not sent")
        return
    if content.startswith("DM:"):
        parts = content.split(":", 3)
        if len(parts) == 4:
            _, peer_id, nick, dm = parts
            # Echo Sage's outbound DM into the feed (private, self-echo) so the
            # UI shows the reply — consistent with the slash-command DM path,
            # and filtered from auto-reply by echo=True/peer_id='self'.
            try:
                inbound_queue.put_nowait({
                    "type": "message", "sender": NICKNAME, "channel": nick,
                    "content": dm, "timestamp": time.time(),
                    "peer_id": "self", "private": True, "echo": True})
            except asyncio.QueueFull:
                pass
            await _sage_client.send_private_message(dm, peer_id, nick)
        return
    await _sage_client.send_public_message(content)


async def _inbound_relay_loop():
    while True:
        try:
            msg = await asyncio.wait_for(inbound_queue.get(), timeout=1.0)
            payload = json.dumps(msg)
            dead = set()
            for ws in ws_clients:
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.add(ws)
            for ws in dead:
                ws_clients.discard(ws)
                ws_nicknames.pop(ws, None)
        except asyncio.TimeoutError:
            continue
        except Exception as exc:
            logger.error("[relay] %s", exc)


if __name__ == "__main__":
    uvicorn.run("bitchat_winrt_gateway:app", host=WS_HOST, port=WS_PORT, log_level="info")
# --- end of file ---
