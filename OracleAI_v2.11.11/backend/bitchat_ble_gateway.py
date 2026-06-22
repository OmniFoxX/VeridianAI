#!/usr/bin/env python3
"""
bitchat_ble_gateway.py — OracleAI BitChat BLE Gateway
Fulfills the contract expected by bitchat_bridge.py.

Sits between bleak (BLE mesh) and bitchat_bridge.py (HTTP/WS client):
    bleak (Realtek BLE) ←→ this gateway (FastAPI :8080) ←→ bitchat_bridge.py ←→ Sage

Endpoints:
    GET  /api/info   → { "websocket_url": "ws://localhost:8080/ws" }
    WS   /ws         → full duplex BitChat mesh relay

BitChat BLE protocol: scans for peers advertising the BitChat service UUID,
connects, relays messages in both directions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
import uvicorn
from bleak import BleakScanner, BleakClient
from bleak.backends.device import BLEDevice

# ── BitChat BLE constants ──────────────────────────────────────────────────────
# PROVISIONAL (2026-06-20) — from public BitChat docs Todd found. TREAT AS A
# HYPOTHESIS: web/AI summaries hallucinate UUIDs (note the TX value resembles the
# old placeholder). The AUTHORITY is your phone's actual advertised service UUID,
# which the unfiltered debug scan prints as "[BLE] saw '<name>' (...) adv_uuids=[...]".
# Confirm against that log and correct these if they differ.
BITCHAT_SERVICE_UUID    = "f47b5e2d-4a9e-4c5a-9b3f-8e1d2c3a4b5c"
BITCHAT_CHAR_TX_UUID    = "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"  # write
BITCHAT_CHAR_RX_UUID    = "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"  # notify

LOG_LEVEL = logging.INFO
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [BitChat-GW] %(levelname)s %(message)s")
logger = logging.getLogger("bitchat.gateway")

# ── State ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="BitChat BLE Gateway", version="1.0.0")

# Connected WebSocket clients (bitchat_bridge.py instances)
ws_clients: Set[WebSocket] = set()

# Registered nicknames per websocket
ws_nicknames: dict[WebSocket, str] = {}

# BLE peers currently connected
ble_peers: dict[str, BLEDevice] = {}        # address → device
ble_clients: dict[str, BleakClient] = {}    # address → client

# Inbound BLE message queue → forwarded to WS clients
inbound_queue: asyncio.Queue = asyncio.Queue(maxsize=500)

# Our own peer ID
PEER_ID   = str(uuid.uuid4())[:8]
NICKNAME  = "Sage"


# ── HTTP endpoints ─────────────────────────────────────────────────────────────
@app.get("/api/info")
async def api_info():
    """Entry point — bitchat_bridge.py calls this first."""
    return JSONResponse({
        "name":          "OracleAI BitChat BLE Gateway",
        "peer_id":       PEER_ID,
        "websocket_url": "ws://localhost:8080/ws",
        "peers":         list(ble_peers.keys()),
        "status":        "ok",
    })


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "ble_peers": len(ble_peers)})


# ── WebSocket endpoint ─────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    ws_nicknames[ws] = NICKNAME
    logger.info("[WS] client connected — %d total", len(ws_clients))

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == "register":
                # {"type": "register", "nickname": "Sage"}
                nick = data.get("nickname", NICKNAME)
                ws_nicknames[ws] = nick
                logger.info("[WS] registered as '%s'", nick)
                await ws.send_text(json.dumps({"type": "ack", "status": "registered", "nickname": nick}))

            elif msg_type == "message":
                # {"type": "message", "channel": "general", "content": "..."}
                channel = data.get("channel", "general")
                content = data.get("content", "")
                nick    = ws_nicknames.get(ws, NICKNAME)
                logger.info("[WS→BLE] [%s] %s: %s", channel, nick, content[:80])
                await _ble_broadcast(nick, channel, content)

            elif msg_type == "peers":
                # {"type": "peers"}
                await ws.send_text(json.dumps({
                    "type":  "peers",
                    "peers": list(ble_peers.keys()),
                }))

            else:
                logger.warning("[WS] unknown message type: %s", msg_type)

    except WebSocketDisconnect:
        logger.info("[WS] client disconnected")
    except Exception as exc:
        logger.error("[WS] error: %s", exc)
    finally:
        ws_clients.discard(ws)
        ws_nicknames.pop(ws, None)


# ── BLE broadcast (WS → mesh) ──────────────────────────────────────────────────
async def _ble_broadcast(sender: str, channel: str, content: str) -> None:
    """Send a message out to all connected BLE peers."""
    if not ble_clients:
        logger.warning("[BLE] no peers connected — message dropped")
        return

    payload = json.dumps({
        "sender":    sender,
        "channel":   channel,
        "content":   content,
        "timestamp": time.time(),
        "peer_id":   PEER_ID,
    }).encode()

    for addr, client in list(ble_clients.items()):
        try:
            await client.write_gatt_char(BITCHAT_CHAR_TX_UUID, payload)
            logger.debug("[BLE→] sent to %s", addr)
        except Exception as exc:
            logger.warning("[BLE] send to %s failed: %s", addr, exc)
            ble_peers.pop(addr, None)
            ble_clients.pop(addr, None)


# ── BLE inbound relay (mesh → WS) ─────────────────────────────────────────────
def _on_ble_notify(sender_handle: int, data: bytes) -> None:
    """Callback fired by bleak when a BLE peer sends us data."""
    try:
        msg = json.loads(data.decode())
        inbound_queue.put_nowait(msg)
    except Exception as exc:
        logger.warning("[BLE←] malformed packet: %s", exc)


async def _inbound_relay_loop() -> None:
    """Drain inbound_queue and forward messages to all WS clients."""
    while True:
        try:
            msg = await asyncio.wait_for(inbound_queue.get(), timeout=1.0)
            msg["type"] = "message"     # normalise for bitchat_bridge.py
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


# ── BLE scanner loop ───────────────────────────────────────────────────────────
async def _ble_scan_loop() -> None:
    """Continuously scan for BitChat peers and connect to new ones."""
    logger.info("[BLE] scanner started — looking for BitChat peers...")
    logger.info("[BLE] (debug) configured BitChat service UUID = %s", BITCHAT_SERVICE_UUID)
    while True:
        try:
            # DEBUG: scan UNFILTERED and log every device + the service UUIDs it
            # advertises. Open BitChat on a phone nearby and its line reveals the
            # REAL service UUID to put in BITCHAT_SERVICE_UUID. We still only
            # CONNECT to devices advertising the configured UUID, so once that's
            # corrected this loop works unchanged.
            found = await BleakScanner.discover(timeout=5.0, return_adv=True)
            for addr, (device, adv) in found.items():
                advertised = [str(u).lower() for u in (getattr(adv, "service_uuids", None) or [])]
                logger.info("[BLE] saw '%s' (%s) adv_uuids=%s",
                            (getattr(adv, "local_name", None) or device.name or "?"),
                            addr, advertised or "none")
                if BITCHAT_SERVICE_UUID in advertised and addr not in ble_clients:
                    asyncio.create_task(_connect_peer(device))
        except Exception as exc:
            logger.warning("[BLE] scan error: %s", exc)
        await asyncio.sleep(10)     # re-scan every 10s


async def _connect_peer(device: BLEDevice) -> None:
    """Connect to a discovered BitChat peer and subscribe to notifications."""
    addr = device.address
    logger.info("[BLE] connecting to peer %s (%s)...", device.name or "?", addr)
    try:
        client = BleakClient(device, disconnected_callback=lambda c: _on_disconnect(addr))
        await client.connect(timeout=10.0)
        await client.start_notify(BITCHAT_CHAR_RX_UUID, _on_ble_notify)
        ble_peers[addr]   = device
        ble_clients[addr] = client
        logger.info("[BLE] ✓ connected to %s", addr)

        # Notify WS clients a new peer joined
        await _broadcast_ws({
            "type":    "peer_joined",
            "peer_id": addr,
            "name":    device.name or addr,
        })
    except Exception as exc:
        logger.warning("[BLE] failed to connect %s: %s", addr, exc)


def _on_disconnect(addr: str) -> None:
    logger.info("[BLE] peer disconnected: %s", addr)
    ble_peers.pop(addr, None)
    ble_clients.pop(addr, None)


async def _broadcast_ws(msg: dict) -> None:
    """Send a dict to all connected WS clients."""
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


# ── Startup ────────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    logger.info("[GW] OracleAI BitChat BLE Gateway starting — peer_id=%s", PEER_ID)
    asyncio.create_task(_ble_scan_loop())
    asyncio.create_task(_inbound_relay_loop())


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "bitchat_ble_gateway:app",
        host="127.0.0.1",
        port=8080,
        log_level="info",
    )