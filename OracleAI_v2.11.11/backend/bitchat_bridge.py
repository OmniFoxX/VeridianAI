#!/usr/bin/env python3
"""OracleAI BitChat Bridge — bitchat_bridge.py  (Build Battle #1 winner, adapted)

EXPERIMENTAL. This talks to a *local BitChat ↔ HTTP/WebSocket bridge* on
host:port. Note that BitChat itself is a Bluetooth-mesh native app and does NOT
ship an official localhost HTTP/WS API of this shape — so this adapter stays
inert until such a bridge is running (e.g. a community gateway, the BitChat web
build, or a wrapper around the terminal client). It is wired and ready; it just
won't move messages until that gateway exists. Graceful either way.

Adapted from the uploaded build: imports the LOCAL framework (no fictional
`oracleai.integrations` package), takes a plain config dict (host/port/nickname)
like the other adapters, and imports aiohttp lazily so the app boots without it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from sage_messaging_adapter import (
    ChannelMessage,
    ChannelProfile,
    SageMessagingAdapter,
)

try:
    import aiohttp
except Exception:                       # aiohttp optional — adapter degrades cleanly
    aiohttp = None

logger = logging.getLogger("sage.bitchat")


class BitChatConnectionError(Exception):
    pass


class BitChatBridge(SageMessagingAdapter):
    """Connect OracleAI to a BitChat mesh via a local HTTP/WebSocket bridge."""

    PROFILE = ChannelProfile(
        name="bitchat", max_chars=500, strip_markdown=True,
        # No content prefix: BitChat already shows "Sage" as the sender, so a
        # "Sage: " prefix just doubles up (bitchat/Sage: Sage: ...).
        split_long=True, sage_prefix="",
    )
    EXPERIMENTAL = False
    _MAX_RECV = 50

    def __init__(self, config: dict):
        super().__init__(config)
        self._host      = self.config.get("host", "localhost")
        self._port      = int(self.config.get("port", 8080))
        self._nickname  = self.config.get("nickname", "Sage")
        self._session   = None
        self._ws        = None
        self._connected = False
        self._ws_lock   = asyncio.Lock()

    def update_config(self, cfg: dict) -> None:
        if not cfg:
            return
        self.config.update(cfg)
        self._host     = cfg.get("host", self._host)
        self._port     = int(cfg.get("port", self._port))
        self._nickname = cfg.get("nickname", self._nickname)

    # --- availability ---
    def available(self) -> bool:
        return aiohttp is not None

    def unavailable_reason(self) -> str:
        if aiohttp is None:
            return "pip install aiohttp"
        # Not an error — a standing hardware requirement shown next to the
        # channel. Sage joins the mesh by advertising as a real BLE peer, which
        # needs a Bluetooth LE (4.0+) adapter that supports the peripheral /
        # advertising role (a USB BLE dongle; most built-in Windows radios can't
        # advertise a GATT service). The gateway now auto-starts on Connect.
        return "needs a Bluetooth LE (4.0+) dongle (peripheral-capable)"

    def connected(self) -> bool:
        return bool(self._connected)

    @property
    def _base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    async def _ensure_session(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def _reconnect_if_needed(self) -> bool:
        if self._ws and not self._ws.closed:
            return True
        self._connected = False
        try:
            return await self.connect()
        except Exception as exc:
            logger.error("[BitChat] reconnect failed: %s", exc)
            return False

    async def connect(self) -> bool:
        if aiohttp is None:
            logger.error("[BitChat] aiohttp not installed.")
            return False
        try:
            await self._ensure_session()
            async with self._session.get(
                f"{self._base_url}/api/info",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    raise BitChatConnectionError(
                        f"/api/info returned {resp.status}"
                    )
                data = await resp.json()
            ws_url = data.get("websocket_url")
            if not ws_url:
                raise BitChatConnectionError("no websocket_url in /api/info")
            self._ws = await self._session.ws_connect(ws_url)
            await self._ws.send_str(json.dumps(
                {"type": "register", "nickname": self._nickname}
            ))
            try:
                ack = await asyncio.wait_for(self._ws.receive(), timeout=3.0)
                if ack.type in (
                    aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR
                ):
                    raise BitChatConnectionError(
                        "WS closed before registration ack."
                    )
            except asyncio.TimeoutError:
                logger.debug("[BitChat] no ack — assuming OK.")
            self._connected = True
            self._ready     = True
            logger.info(
                "[BitChat] connected as '%s' on %s",
                self._nickname, self._base_url
            )
            return True
        except BitChatConnectionError as exc:
            logger.warning("[BitChat] %s", exc)
            return False
        except Exception as exc:
            logger.warning(
                "[BitChat] connection failed (no bridge at %s?): %s",
                self._base_url, exc
            )
            return False

    async def disconnect(self) -> None:
        try:
            if self._ws and not self._ws.closed:
                await self._ws.close()
            if self._session and not self._session.closed:
                await self._session.close()
        except Exception as exc:
            logger.warning("[BitChat] disconnect error: %s", exc)
        finally:
            self._connected = False
            self._ready     = False

    async def send(self, message: str, channel: str = "general") -> bool:
        if not await self._reconnect_if_needed():
            return False
        try:
            async with self._ws_lock:
                await self._ws.send_str(json.dumps(
                    {"type": "message", "channel": channel, "content": message}
                ))
            return True
        except Exception as exc:
            logger.error("[BitChat] send error: %s", exc)
            return False

    async def sage_respond(self, message: ChannelMessage) -> bool:
        """Route Sage's reply the way the incoming message arrived.

        A reply to a *private DM* goes back to that same peer as an encrypted
        DM — never leaked into the public mesh. A reply to public chat is
        broadcast as before. This override lives here (not in the generic base)
        because only the BitChat bridge knows the gateway's DM convention:
        the gateway routes a message whose content is "DM:<peer_id>:<nick>:<text>"
        as an encrypted private message (content colons survive its split(':',3)).
        """
        if not self._reply_fn:
            logger.warning("[BitChat] no reply_fn injected — cannot auto-respond.")
            return False
        try:
            result = await self._reply_fn(message.content)
        except Exception as exc:
            logger.exception("[BitChat] reply generation error: %s", exc)
            return False
        if not result:
            return False

        raw       = getattr(message, "raw", None) or {}
        is_private = bool(raw.get("private"))
        peer_id    = (raw.get("peer_id") or "").strip()
        nick       = (message.sender or raw.get("sender") or "").strip()
        # Only route as a DM when we have a real peer to reply to — never echo
        # a self/system pseudo-peer back out.
        route_dm   = is_private and bool(peer_id) and peer_id not in ("self", "system")

        ok_all = True
        for chunk in self._format_for_channel(result):
            if route_dm:
                payload = f"DM:{peer_id}:{nick}:{chunk}"
                ok = await self.send(payload, channel=message.channel)
            else:
                ok = await self.send(chunk, channel=message.channel)
            if not ok:
                ok_all = False
        return ok_all

    async def receive(self, timeout: float = 5.0) -> list:
        if not await self._reconnect_if_needed():
            return []
        messages, deadline = [], asyncio.get_event_loop().time() + timeout
        try:
            while len(messages) < self._MAX_RECV:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    async with self._ws_lock:
                        raw = await asyncio.wait_for(
                            self._ws.receive(),
                            timeout=min(remaining, 1.0)
                        )
                except asyncio.TimeoutError:
                    break
                if raw.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(raw.data)
                    if data.get("type") == "message":
                        messages.append(ChannelMessage(
                            sender    = data.get("sender", "unknown"),
                            channel   = data.get("channel", "general"),
                            content   = data.get("content", ""),
                            timestamp = data.get("timestamp", time.time()),
                            platform  = "bitchat",
                            raw       = data,
                        ))
                elif raw.type in (
                    aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR
                ):
                    self._connected = False
                    break
        except Exception as exc:
            logger.error("[BitChat] receive error: %s", exc)
        return messages

    async def peers(self) -> list:
        if not await self._reconnect_if_needed():
            return []
        try:
            async with self._ws_lock:
                await self._ws.send_str(json.dumps({"type": "peers"}))
                resp = await asyncio.wait_for(
                    self._ws.receive(), timeout=2.0
                )
            if resp.type == aiohttp.WSMsgType.TEXT:
                _d = json.loads(resp.data)
                logger.info("[BitChat] peers response: %s", str(_d)[:300])
                # Tolerant of however the bridge shapes its peer list: a bare
                # list, or a dict under any of these common keys; dict entries
                # are reduced to a display name.
                _lst = _d if isinstance(_d, list) else (
                    _d.get("peers")   or _d.get("nicknames") or
                    _d.get("clients") or _d.get("members")   or
                    _d.get("devices") or []
                )
                out = []
                for x in (_lst or []):
                    if isinstance(x, dict):
                        out.append(
                            x.get("nickname") or x.get("name")
                            or x.get("id") or str(x)
                        )
                    else:
                        out.append(str(x))
                return out
        except asyncio.TimeoutError:
            logger.warning("[BitChat] peer list timed out.")
        except Exception as exc:
            logger.error("[BitChat] peers() error: %s", exc)
        return []