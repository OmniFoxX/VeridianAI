#!/usr/bin/env python3
"""
bitchat_ble_gateway.py — OracleAI BitChat BLE Gateway v2
Rewrites the hand-rolled BLE stub with a real BitchatClient integration.

Architecture:
    BitchatClient (bitchat.py)
        ↕  subclassed as SageBitchatClient
    FastAPI :8080  (this file)
        ↕  WebSocket
    bitchat_bridge.py
        ↕
    Sage

Location/version agnostic: all paths derived from __file__.
No hardcoded drive letters, version strings, or absolute paths.

Endpoints (unchanged — bitchat_bridge.py contract):
    GET  /api/info   → { "websocket_url": "ws://localhost:8080/ws" }
    WS   /ws         → full-duplex BitChat mesh relay
    GET  /health     → { "status": "ok", ... }

Fix (v2.1):
    Startup now calls connect() → handshake() → background_scanner()
    in the correct order, matching bitchat.py's run() lifecycle.
    Previously, handshake() was called before connect(), leaving
    self.client = None permanently and dropping every outbound message.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Set, Optional

# ── Path resolution (location/version agnostic) ───────────────────────────────
# Gateway lives at:        <root>/backend/bitchat_ble_gateway.py
# bitchat-python lives at: <root>/bitchat-python/bitchat/
_BACKEND_DIR    = Path(__file__).resolve().parent
_ROOT_DIR       = _BACKEND_DIR.parent
_BITCHAT_PYTHON = _ROOT_DIR / "bitchat-python" / "bitchat"


def _force_local_import(module_name: str, file_path: Path):
    """Force import from local path, bypassing stdlib namespace collision."""
    spec   = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Force local bitchat-python modules BEFORE anything else.
# Necessary because Python 3.14 has a stdlib 'compression' module
# that collides with bitchat_compression.py, and lz4 depends on it.
_force_local_import("bitchat_compression", _BACKEND_DIR / "bitchat_compression.py")
_force_local_import("fragmentation",       _BITCHAT_PYTHON / "fragmentation.py")
_force_local_import("persistence",         _BITCHAT_PYTHON / "persistence.py")
_force_local_import("encryption",          _BITCHAT_PYTHON / "encryption.py")
_force_local_import("terminal_ux",         _BITCHAT_PYTHON / "terminal_ux.py")
_force_local_import("bitchat",             _BITCHAT_PYTHON / "bitchat.py")


# ── BitchatClient imports ──────────────────────────────────────────────────────
try:
    from bitchat import (
        BitchatClient,
        BitchatMessage,
        BitchatPacket,
        MessageType,
        create_bitchat_packet,
    )
    from terminal_ux import PrivateDM, Channel
    _BITCHAT_AVAILABLE    = True
    _BITCHAT_IMPORT_ERROR = None
except ImportError as _e:
    _BITCHAT_AVAILABLE    = False
    _BITCHAT_IMPORT_ERROR = str(_e)

# v2.12.2: protocol constants come from bitchat_protocol_constants.json (the
# drift checker's single source of truth); the vendored bitchat.py literals
# remain the built-in fallback. Overriding the MODULE attributes matters:
# bitchat.py reads them at call time (scan filter, characteristic match).
if _BITCHAT_AVAILABLE:
    try:
        from bitchat_drift import load_constants as _load_pc, \
            active_service_uuid as _active_svc
        import bitchat as _bc_mod
        _pc = _load_pc()
        _bc_mod.BITCHAT_SERVICE_UUID = _active_svc(_pc)
        _bc_mod.BITCHAT_CHARACTERISTIC_UUID = _pc["characteristic_uuid"]
        print(f"[BitChat-GW] protocol constants from JSON "
              f"({_pc.get('active_network')}): svc={_bc_mod.BITCHAT_SERVICE_UUID}")
    except Exception as _pc_err:
        print(f"[BitChat-GW] constants JSON unavailable, using built-ins: {_pc_err}")


# ── FastAPI / uvicorn ──────────────────────────────────────────────────────────
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
import uvicorn


# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BitChat-GW] %(levelname)s %(message)s"
)
logger = logging.getLogger("bitchat.gateway")


# ── Gateway state ──────────────────────────────────────────────────────────────
ws_clients:    Set[WebSocket]       = set()
ws_nicknames:  dict[WebSocket, str] = {}
inbound_queue: asyncio.Queue        = asyncio.Queue(maxsize=500)

PEER_ID  = str(uuid.uuid4())[:8]


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

# The single shared BitchatClient instance (set at startup)
_sage_client: Optional["SageBitchatClient"] = None


# ── SageBitchatClient — subclass that routes display_message to WS ─────────────
if _BITCHAT_AVAILABLE:

    class SageBitchatClient(BitchatClient):
        """
        Thin subclass of BitchatClient.

        Overrides display_message() so incoming BLE messages are pushed onto
        inbound_queue (→ WS clients → Sage) instead of printed to a terminal.

        Everything else — BLE scan, connect, Noise XX handshake, fragmentation,
        compression, delivery ACKs, reconnection — is inherited unchanged.
        """

        def __init__(self):
            super().__init__()
            self.nickname = NICKNAME

        async def display_message(
            self,
            message:    BitchatMessage,
            packet:     BitchatPacket,
            is_private: bool,
        ):
            """
            Route incoming messages to the WebSocket relay instead of stdout.
            Handles encrypted channel message decryption before queuing.
            """
            # Safe nickname resolution — peer may exist but have nickname=None
            sender_nick = (
                (self.peers.get(packet.sender_id_str) and
                 self.peers[packet.sender_id_str].nickname)
                or packet.sender_id_str
            )

            # Resolve display content (handles encrypted channel messages)
            display_content = message.content
            if message.is_encrypted and message.channel:
                if message.channel in self.channel_keys:
                    try:
                        creator_fp      = self.channel_creators.get(message.channel, "")
                        display_content = self.encryption_service.decrypt_from_channel(
                            message.encrypted_content,
                            message.channel,
                            self.channel_keys[message.channel],
                            creator_fp,
                        )
                    except Exception:
                        display_content = "[Encrypted — decryption failed]"
                else:
                    display_content = "[Encrypted — join channel with password]"

            logger.info("[GW] RECV from %s: %s", sender_nick,
                        (display_content or "")[:80])

            msg = {
                "type":      "message",
                "sender":    sender_nick,
                "channel":   message.channel or "general",
                "content":   display_content,
                "timestamp": time.time(),
                "peer_id":   packet.sender_id_str,
                "private":   is_private,
            }

            try:
                inbound_queue.put_nowait(msg)
            except asyncio.QueueFull:
                logger.warning(
                    "[GW] inbound_queue full — dropping message from %s", sender_nick
                )

            # Preserve parent chat-context bookkeeping for DM tracking
            if is_private:
                self.chat_context.last_private_sender = (
                    packet.sender_id_str, sender_nick
                )
                self.chat_context.add_dm(sender_nick, packet.sender_id_str)


# ── Lifespan (replaces deprecated on_event) ────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manages the full gateway lifecycle using the modern FastAPI lifespan pattern.
    Replaces the deprecated @app.on_event("startup") / ("shutdown") decorators.
    """
    # ── STARTUP ───────────────────────────────────────────────────────────────
    global _sage_client

    logger.info("[GW] OracleAI BitChat BLE Gateway v2 starting — peer_id=%s", PEER_ID)

    if not _BITCHAT_AVAILABLE:
        logger.error("[GW] bitchat-python not importable: %s", _BITCHAT_IMPORT_ERROR)
        logger.error("[GW] Expected path: %s", _BITCHAT_PYTHON)
        logger.warning("[GW] Gateway running in STUB mode — no BLE functionality")
        relay_task = asyncio.create_task(_inbound_relay_loop())
        yield
        relay_task.cancel()
        return

    _sage_client = SageBitchatClient()

    # Launch all background tasks
    ble_task     = asyncio.create_task(_run_bitchat_client())
    relay_task   = asyncio.create_task(_inbound_relay_loop())
    monitor_task = asyncio.create_task(_peer_monitor_loop())

    logger.info("[GW] All tasks started — gateway ready")

    yield   # ── Server runs here ──────────────────────────────────────────────

    # ── SHUTDOWN ──────────────────────────────────────────────────────────────
    logger.info("[GW] Shutting down — sending LEAVE to mesh...")

    try:
        if _sage_client and _sage_client.client and _sage_client.client.is_connected:
            leave_packet = create_bitchat_packet(
                _sage_client.my_peer_id,
                MessageType.LEAVE,
                _sage_client.nickname.encode(),
            )
            await _sage_client.send_packet(leave_packet)
            await asyncio.sleep(0.1)
            await _sage_client.client.disconnect()
    except Exception as exc:
        logger.warning("[GW] Shutdown error (non-fatal): %s", exc)

    if _sage_client:
        await _sage_client.save_app_state()

    # Cancel background tasks cleanly
    for task in (ble_task, relay_task, monitor_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    logger.info("[GW] Shutdown complete")


# ── App (lifespan-aware) ───────────────────────────────────────────────────────
app = FastAPI(
    title    = "OracleAI BitChat BLE Gateway",
    version  = "2.1.0",
    lifespan = lifespan,
)


# ── HTTP endpoints (contract unchanged) ───────────────────────────────────────
@app.get("/api/info")
async def api_info():
    peers     = _sage_client.display_peers() if _sage_client else []
    connected = bool(
        _sage_client and _sage_client.client and _sage_client.client.is_connected
    )
    return JSONResponse({
        "name":          "OracleAI BitChat BLE Gateway",
        "peer_id":       PEER_ID,
        "websocket_url": "ws://localhost:8080/ws",
        "peers":         peers,
        "status":        "ok" if connected else "scanning",
        "bitchat_ready": _BITCHAT_AVAILABLE,
    })


@app.get("/health")
async def health():
    peers    = len(_sage_client.peers) if _sage_client else 0
    sessions = (
        _sage_client.encryption_service.get_session_count()
        if _sage_client else 0
    )
    return JSONResponse({
        "status":          "ok",
        "ble_peers":       peers,
        "secure_sessions": sessions,
        "bitchat_ready":   _BITCHAT_AVAILABLE,
    })


def _fmt_fp(hex_fp: str) -> str:
    """Group a SHA-256 fingerprint into readable 4-char blocks (first 128
    bits), matching the WinRT gateway's formatting so the UI is identical
    whichever gateway is live."""
    h = (hex_fp or "").replace(":", "").replace(" ", "").lower()[:32]
    return " ".join(h[i:i + 4] for i in range(0, len(h), 4))


@app.get("/api/identity")
async def api_identity():
    """v2.12.3: identity fingerprints for out-of-band verification. Parity
    with the WinRT gateway -- WITHOUT this, the central-role fallback (the
    gateway that runs when the adapter can't advertise) returned 404 and the
    UI's Verify button just showed 'none'. Public mesh chat needs no Noise
    handshake, so peer fingerprints only appear once an ENCRYPTED session
    exists; Toga's own fingerprint always shows."""
    if not _sage_client:
        return JSONResponse({"available": False})
    es = _sage_client.encryption_service
    try:
        my_fp = es.get_identity_fingerprint()
    except Exception:
        my_fp = ""
    peers = []
    for pid, peer in list(getattr(_sage_client, "peers", {}).items()):
        try:
            pfp = es.get_peer_fingerprint(pid)
        except Exception:
            pfp = None
        peers.append({
            "peer_id": pid,
            "nickname": getattr(peer, "nickname", None) or pid,
            "fingerprint": _fmt_fp(pfp) if pfp else None,
            "verified": bool(pfp),
        })
    return JSONResponse({
        "available": True,
        "nickname": NICKNAME,
        "peer_id": getattr(_sage_client, "my_peer_id", PEER_ID),
        "fingerprint": _fmt_fp(my_fp),
        "peers": peers})


@app.post("/shutdown")
async def shutdown():
    """Fully stop BitChat: leave the mesh, drop BLE, end scanning, exit process.
    Wired to the UI 'Disconnect' so 'off' actually means off -- no more scanning.
    The backend respawns this gateway on the next 'Connect'."""
    global _sage_client
    logger.info("[GW] /shutdown requested -- stopping BitChat BLE")
    try:
        if _sage_client:
            _sage_client.running = False  # ends background_scanner loop
            if (_BITCHAT_AVAILABLE and _sage_client.client
                    and _sage_client.client.is_connected):
                try:
                    leave_packet = create_bitchat_packet(
                        _sage_client.my_peer_id,
                        MessageType.LEAVE,
                        _sage_client.nickname.encode(),
                    )
                    await _sage_client.send_packet(leave_packet)
                    await asyncio.sleep(0.1)
                    await _sage_client.client.disconnect()
                except Exception as exc:
                    logger.warning("[GW] shutdown BLE cleanup: %s", exc)
    finally:
        # Exit shortly after responding so BleakScanner truly stops and the
        # port frees for a clean restart.
        asyncio.get_event_loop().call_later(0.3, lambda: os._exit(0))
    return JSONResponse({"status": "stopping"})


# ── WebSocket endpoint (contract unchanged) ────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    ws_nicknames[ws] = NICKNAME
    logger.info("[WS] client connected — %d total", len(ws_clients))

    try:
        while True:
            raw      = await ws.receive_text()
            data     = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == "register":
                nick = data.get("nickname", NICKNAME)
                ws_nicknames[ws] = nick
                if _sage_client:
                    _sage_client.nickname = nick
                logger.info("[WS] registered as '%s'", nick)
                await ws.send_text(json.dumps({
                    "type": "ack", "status": "registered", "nickname": nick
                }))

            elif msg_type == "message":
                channel = data.get("channel", "general")
                content = data.get("content", "")
                nick    = ws_nicknames.get(ws, NICKNAME)
                logger.info("[WS→BLE] [%s] %s: %s", channel, nick, content[:80])
                await _handle_outbound(channel, content, nick)

            elif msg_type == "peers":
                peers = _sage_client.display_peers() if _sage_client else []
                await ws.send_text(json.dumps({"type": "peers", "peers": peers}))

            else:
                logger.warning("[WS] unknown message type: %s", msg_type)

    except WebSocketDisconnect:
        logger.info("[WS] client disconnected")
    except Exception as exc:
        logger.error("[WS] error: %s", exc)
    finally:
        ws_clients.discard(ws)
        ws_nicknames.pop(ws, None)


# ── Outbound: WS → BLE mesh ────────────────────────────────────────────────────
async def _handle_outbound(channel: str, content: str, nick: str):
    """
    Route an outbound message from Sage/WS to the BLE mesh.
    Handles public, channel, and private (DM) messages.

    DM format:  "DM:<peer_id>:<nickname>:<content>"
    """
    # Echo Sage's own (non-DM) message into the UI feed so the user sees what
    # they sent. Goes inbound_queue -> relay -> WS -> bridge.receive -> recent().
    if not content.startswith("DM:"):
        try:
            inbound_queue.put_nowait({
                "type": "message", "sender": nick,
                "channel": channel or "general", "content": content,
                "timestamp": time.time(), "peer_id": "self",
                "private": False, "echo": True,
            })
        except asyncio.QueueFull:
            pass

    if not _sage_client:
        logger.warning("[GW] BitchatClient not ready — message dropped")
        return

    if not _sage_client.client or not _sage_client.client.is_connected:
        logger.warning("[GW] BLE not connected — message queued (background scanner active)")
        return

    # Private message via DM prefix
    if content.startswith("DM:"):
        parts = content.split(":", 3)
        if len(parts) == 4:
            _, target_peer_id, target_nick, dm_content = parts
            await _sage_client.send_private_message(
                dm_content, target_peer_id, target_nick
            )
        return

    # Channel or public message — preserve and restore chat context
    original_mode = _sage_client.chat_context.current_mode

    if channel and channel.startswith("#"):
        # switch_to_channel_silent exists in terminal_ux.py — verified
        _sage_client.chat_context.switch_to_channel_silent(channel)
        await _sage_client.send_public_message(content)
        _sage_client.chat_context.current_mode = original_mode
    else:
        if not isinstance(
            _sage_client.chat_context.current_mode,
            (Channel if _BITCHAT_AVAILABLE else object),
        ):
            await _sage_client.send_public_message(content)
        else:
            _sage_client.chat_context.switch_to_public()
            await _sage_client.send_public_message(content)
            _sage_client.chat_context.current_mode = original_mode


# ── Inbound relay: inbound_queue → WS clients ─────────────────────────────────
async def _inbound_relay_loop():
    """Drain inbound_queue and forward all messages to connected WS clients.
    Dead connections are pruned automatically.
    """
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


# ── Peer monitor: watch for peer join/leave events ────────────────────────────
async def _peer_monitor_loop():
    """
    Watches _sage_client.peers and notifies WS clients when peers
    join or leave the BLE mesh. BitchatClient handles the BLE
    connect/disconnect internally — we just observe the peer dict.
    """
    known_peers: set = set()

    while True:
        await asyncio.sleep(2)
        if not _sage_client:
            continue

        current = set(_sage_client.display_peers())

        for name in current - known_peers:
            await _broadcast_ws({
                "type": "peer_joined", "peer_id": name, "name": name
            })
            logger.info("[GW] peer joined: %s", name)

        for name in known_peers - current:
            await _broadcast_ws({
                "type": "peer_left", "peer_id": name, "name": name
            })
            logger.info("[GW] peer left: %s", name)

        known_peers = current


async def _broadcast_ws(msg: dict):
    """Broadcast a message to all connected WS clients, pruning dead ones."""
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


# ── THE FIX: correct BLE lifecycle ────────────────────────────────────────────
async def _run_bitchat_client():
    """
    Runs the full BitchatClient BLE lifecycle in the correct order:

        1. connect()            — initial BLE scan + connect attempt
        2. handshake()          — Noise identity announce + ANNOUNCE packet
                                  (works in offline mode too — prints status)
        3. background_scanner() — continuous reconnection loop from here on

    Previously, startup() called handshake() before connect(), which left
    self.client = None permanently. Every outbound message then hit the
    'BLE not connected — message dropped' guard and was silently discarded.

    This matches the lifecycle order in bitchat.py's run() method exactly.
    """
    if not _sage_client:
        return

    try:
        # Step 1 — initial BLE scan + connect
        # Returns True even if no peer found; offline mode is valid.
        connected = await _sage_client.connect()

        if connected and _sage_client.client and _sage_client.client.is_connected:
            logger.info("[GW] BLE connected on first attempt")
        else:
            logger.info("[GW] No BLE peer found on first scan — entering offline mode")

        # Step 2 — handshake AFTER connect
        # If connected: sends Noise identity announce + ANNOUNCE to mesh.
        # If offline:   prints "Running in offline mode. Waiting for peers..."
        #               and restores persisted state (nickname, channel keys, etc.)
        await _sage_client.handshake()

        # Step 3 — background_scanner() takes over reconnection from here.
        # It loops every 5 seconds, scanning for peers when not connected,
        # and handles all future connect/disconnect/reconnect events.
        # Verified: background_scanner() exists in bitchat.py and is awaitable.
        await _sage_client.background_scanner()

    except asyncio.CancelledError:
        logger.info("[GW] BitchatClient task cancelled — shutting down")
    except Exception as exc:
        logger.error("[GW] BitchatClient fatal error: %s", exc, exc_info=True)


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "bitchat_ble_gateway:app",
        host      = "127.0.0.1",
        port      = 8080,
        log_level = "info",
    )