#!/usr/bin/env python3
"""
bless_ble_daemon.py - BitChat BLE *peripheral* daemon (Linux/BlueZ via bless).

Why this exists
---------------
Windows can't advertise (the radio rejects the peripheral command), so the phones
can never discover Sage there. This daemon runs on a host whose Bluetooth stack
CAN advertise (Linux/BlueZ: a Raspberry Pi, a spare Linux box, or WSL2 with a
custom kernel + a USB dongle via usbipd) and gives Sage a true peripheral role.

Architecture
------------
    OracleAI (Windows)
      |  WebSocket  (unchanged contract)
    bitchat_ble_gateway.py (Windows :8080)   <-- optional forwarder
      |  WebSocket
    bless_ble_daemon.py  (THIS FILE, Linux)
      |  bless peripheral: advertise + GATT characteristic (write + notify)
    BlueZ radio  ->  iPhone / Android BitChat

It reuses the EXISTING, already-fixed protocol code (bitchat.py + encryption.py):
all packet parsing, the TLV announce, and the Noise prologue/transport/handshake
fixes run unchanged. Only the BLE transport is swapped, via _BlessTransport:
  - OUTBOUND  send_packet -> self.client.write_gatt_char(...) -> bless notify
  - INBOUND   bless write  -> client.notification_handler(None, data) -> handle_packet

This daemon exposes the SAME WS contract as bitchat_ble_gateway.py, so OracleAI's
bitchat_bridge.py can point straight at it (host/port) with no changes.

Setup (on the Linux host)
-------------------------
  1) Copy the whole `bitchat-python/` tree next to this file (or set
     BITCHAT_PYTHON_ROOT to its parent).
  2) pip install bless fastapi uvicorn cryptography bleak aioconsole pybloom-live
  3) Make sure bluetoothd is running and the adapter is up (`bluetoothctl show`).
  4) python bless_ble_daemon.py        # advertises + serves WS on :8080

NOTE: written against our verified protocol code but the bless event API
(write/subscribe callbacks, notify) is exercised live on first run -- expect to
tweak a couple of bless-specific lines once a real radio is attached.
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

# ---------------------------------------------------------------------------
# Locate and force-import the local bitchat-python protocol stack
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
_ROOT = Path(os.environ.get("BITCHAT_PYTHON_ROOT", _THIS_DIR.parent))
_BITCHAT_PYTHON = _ROOT / "bitchat-python" / "bitchat"


def _force_local_import(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Match the gateway's import order so relative module names resolve.
_compress = _THIS_DIR / "bitchat_compression.py"
if _compress.exists():
    _force_local_import("bitchat_compression", _compress)
_force_local_import("fragmentation", _BITCHAT_PYTHON / "fragmentation.py")
_force_local_import("persistence", _BITCHAT_PYTHON / "persistence.py")
_force_local_import("encryption", _BITCHAT_PYTHON / "encryption.py")
_force_local_import("terminal_ux", _BITCHAT_PYTHON / "terminal_ux.py")
_force_local_import("bitchat", _BITCHAT_PYTHON / "bitchat.py")

from bitchat import (  # noqa: E402
    BitchatClient,
    BitchatMessage,
    BitchatPacket,
    MessageType,
    create_bitchat_packet,
    BITCHAT_SERVICE_UUID,
    BITCHAT_CHARACTERISTIC_UUID,
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
    print(f"[BitChat-Daemon] protocol constants from JSON "
          f"({_pc.get('active_network')}): svc={BITCHAT_SERVICE_UUID}")
except Exception as _pc_err:
    print(f"[BitChat-Daemon] constants JSON unavailable, using built-ins: {_pc_err}")

from bless import (  # noqa: E402
    BlessServer,
    GATTCharacteristicProperties,
    GATTAttributePermissions,
)
from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
import uvicorn  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [BitChat-Daemon] %(levelname)s %(message)s")
logger = logging.getLogger("bitchat.daemon")

def _configured_nickname() -> str:
    """v2.12.2: BLE announce name follows the owner's assistant_name (the
    v2.12.1 socials-reply rename never reached the BLE announce path)."""
    try:
        with open(Path(__file__).resolve().parent.parent / "config.json",
                  encoding="utf-8") as f:
            return (json.load(f).get("sage", {}).get("assistant_name")
                    or "Sage").strip() or "Sage"
    except Exception:
        return "Sage"


NICKNAME = _configured_nickname()
WS_HOST = os.environ.get("BITCHAT_WS_HOST", "0.0.0.0")
WS_PORT = int(os.environ.get("BITCHAT_WS_PORT", "8080"))

ws_clients: Set[WebSocket] = set()
ws_nicknames: dict = {}
inbound_queue: asyncio.Queue = asyncio.Queue(maxsize=500)

_server: Optional[BlessServer] = None
_sage_client: Optional["SagePeripheralClient"] = None


# ---------------------------------------------------------------------------
# Transport shim: makes a bless peripheral look like bleak's client/char so the
# UNCHANGED BitchatClient.send_packet / fragmentation paths "just write".
# ---------------------------------------------------------------------------
class _BlessTransport:
    """Quacks like a BleakClient for the parts bitchat.py touches on send."""

    def __init__(self):
        self.server: Optional[BlessServer] = None
        self.subscribers = 0          # updated by the subscribe callback

    @property
    def is_connected(self) -> bool:
        # Once we're advertising we can always *attempt* a notify; if no central
        # is subscribed it's a harmless no-op. (bless doesn't expose a reliable
        # cross-platform subscriber count, so we don't gate sends on it.)
        return self.server is not None

    async def write_gatt_char(self, characteristic, data, response=False):
        """bitchat.py calls this to 'send'. We notify subscribed centrals."""
        if not self.server:
            return
        try:
            char = self.server.get_characteristic(BITCHAT_CHARACTERISTIC_UUID)
            char.value = bytearray(data)
            # update_value notifies all subscribed centrals on this char.
            self.server.update_value(BITCHAT_SERVICE_UUID, BITCHAT_CHARACTERISTIC_UUID)
        except Exception as exc:
            logger.warning("[TX] notify failed: %s", exc)

    async def disconnect(self):
        return None


# A non-None sentinel so bitchat.py's `if self.client and self.characteristic`
# guards pass in peripheral mode (we never dereference it).
_CHAR_SENTINEL = object()


class SagePeripheralClient(BitchatClient):
    """BitchatClient whose BLE transport is a bless peripheral instead of bleak.

    Reuses ALL protocol logic; only routes display_message -> WS and send -> notify.
    """

    def __init__(self, transport: _BlessTransport):
        super().__init__()
        self.nickname = NICKNAME
        self.client = transport          # the shim (has is_connected/write_gatt_char)
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
                "private": is_private,
            })
        except asyncio.QueueFull:
            logger.warning("[GW] inbound_queue full - dropping from %s", sender_nick)
        if is_private:
            self.chat_context.last_private_sender = (packet.sender_id_str, sender_nick)
            self.chat_context.add_dm(sender_nick, packet.sender_id_str)

    def feed_incoming(self, data: bytes):
        """A central wrote to our characteristic -> run it through the parser."""
        asyncio.create_task(self.notification_handler(None, bytes(data)))

    async def announce_self(self):
        """Send identity announce + TLV announce so a freshly-connected central
        learns who we are. Reuses handshake() which now works because the shim
        makes self.client/self.characteristic truthy."""
        try:
            await self.handshake()
        except Exception as exc:
            logger.warning("announce_self failed: %s", exc)


# ---------------------------------------------------------------------------
# bless peripheral setup
# ---------------------------------------------------------------------------
def _read_request(characteristic, **kwargs) -> bytearray:
    return characteristic.value or bytearray()


def _write_request(characteristic, value, **kwargs):
    characteristic.value = value
    if _sage_client:
        _sage_client.feed_incoming(bytes(value))


async def _start_peripheral(transport: _BlessTransport):
    global _server
    server = BlessServer(name=NICKNAME)
    server.read_request_func = _read_request
    server.write_request_func = _write_request
    await server.add_new_service(BITCHAT_SERVICE_UUID)
    flags = (
        GATTCharacteristicProperties.read
        | GATTCharacteristicProperties.write
        | GATTCharacteristicProperties.write_without_response
        | GATTCharacteristicProperties.notify
    )
    perms = GATTAttributePermissions.readable | GATTAttributePermissions.writeable
    await server.add_new_characteristic(
        BITCHAT_SERVICE_UUID, BITCHAT_CHARACTERISTIC_UUID, flags, None, perms)
    await server.start()
    transport.server = server
    _server = server
    logger.info("[BLE] advertising '%s' service %s", NICKNAME, BITCHAT_SERVICE_UUID)
    return server


async def _periodic_announce():
    """Re-announce periodically (and detect subscribers) so phones that connect
    learn Sage. bless's subscriber count drives transport.is_connected."""
    last = 0
    while True:
        await asyncio.sleep(3)
        if not (_sage_client and _server):
            continue
        try:
            # Best-effort subscriber count (bless exposes the char's subscribers).
            char = _server.get_characteristic(BITCHAT_CHARACTERISTIC_UUID)
            subs = len(getattr(char, "subscribed_centrals", []) or [])
        except Exception:
            subs = _sage_client.client.subscribers
        _sage_client.client.subscribers = subs
        now = time.time()
        if now - last > 10:        # re-announce every ~10s while advertising
            last = now
            await _sage_client.announce_self()


# ---------------------------------------------------------------------------
# WebSocket contract (identical to bitchat_ble_gateway.py)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sage_client
    transport = _BlessTransport()
    _sage_client = SagePeripheralClient(transport)
    try:
        await _start_peripheral(transport)
    except Exception as exc:
        logger.error("[BLE] could not start peripheral: %s", exc)
    relay_task = asyncio.create_task(_inbound_relay_loop())
    ann_task = asyncio.create_task(_periodic_announce())
    logger.info("[Daemon] ready - WS on ws://%s:%d/ws", WS_HOST, WS_PORT)
    yield
    for t in (relay_task, ann_task):
        t.cancel()
    if _server:
        try:
            await _server.stop()
        except Exception:
            pass


app = FastAPI(title="OracleAI BitChat BLE Daemon", version="1.0.0", lifespan=lifespan)


@app.get("/api/info")
async def api_info():
    peers = _sage_client.display_peers() if _sage_client else []
    connected = bool(_sage_client and _sage_client.client.is_connected)
    return JSONResponse({
        "name": "OracleAI BitChat BLE Daemon",
        "websocket_url": f"ws://localhost:{WS_PORT}/ws",
        "peers": peers,
        "status": "ok" if connected else "advertising",
        "bitchat_ready": True,
    })


@app.get("/health")
async def health():
    return JSONResponse({
        "status": "ok",
        "ble_peers": len(_sage_client.peers) if _sage_client else 0,
        "advertising": _server is not None,
        "subscribers": _sage_client.client.subscribers if _sage_client else 0,
    })


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
                channel = data.get("channel", "general")
                content = data.get("content", "")
                await _handle_outbound(channel, content)
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


async def _handle_outbound(channel: str, content: str):
    if not content.startswith("DM:"):
        try:
            inbound_queue.put_nowait({
                "type": "message", "sender": NICKNAME,
                "channel": channel or "general", "content": content,
                "timestamp": time.time(), "peer_id": "self",
                "private": False, "echo": True,
            })
        except asyncio.QueueFull:
            pass
    if not _sage_client or not _sage_client.client.is_connected:
        logger.warning("[Daemon] no subscribed central - message not sent")
        return
    if content.startswith("DM:"):
        parts = content.split(":", 3)
        if len(parts) == 4:
            _, peer_id, nick, dm = parts
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
    uvicorn.run("bless_ble_daemon:app", host=WS_HOST, port=WS_PORT, log_level="info")
# --- end of file ---
