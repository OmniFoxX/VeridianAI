#!/usr/bin/env python3
"""OracleAI Messaging Adapter Framework — sage_messaging_adapter.py

Adapted for in-app use from the Build Battle #1 design. The original was sound
but assumed a `Sage().step()` API that does not exist here — Sage generation runs
through model_manager. So the base no longer imports/calls Sage; instead the
router INJECTS an async reply-callable (main.py wires it to model_manager), and
the base only ever awaits that. Self-contained: standard library only at import
time; per-channel client libs (discord.py, aiohttp) are imported lazily so the
app boots even with none of them installed.

Layout:
    inbound message ──► SageChannelRouter (registry + poll loops + recent buffer)
                            │ injects reply_fn, owns auto_reply flag
        ┌───────────────┬───┴───────────────┐
   BitChatBridge   DiscordAdapter     (BlueSky / Mastodon / … later)
        └── all implement SageMessagingAdapter ──┘
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger("sage.messaging")


def _installed(mod: str) -> bool:
    try:
        importlib.invalidate_caches()   # so a freshly pip-installed package is seen
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Per-platform output shaping
# ---------------------------------------------------------------------------
@dataclass
class ChannelProfile:
    name:            str
    max_chars:       int  = 2000     # hard cap on an outbound message
    strip_markdown:  bool = False
    split_long:      bool = True     # chunk long replies instead of truncating
    chunk_separator: str  = "\n---\n"
    sage_prefix:     str  = "Sage: "


@dataclass
class ChannelMessage:
    sender:    str
    channel:   str
    content:   str
    timestamp: float = field(default_factory=time.time)
    platform:  str   = "unknown"
    raw:       dict  = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Base adapter — every channel implements this contract
# ---------------------------------------------------------------------------
class SageMessagingAdapter(ABC):
    PROFILE: ChannelProfile = ChannelProfile(name="base")
    EXPERIMENTAL: bool = False

    def __init__(self, config: dict):
        self.config   = dict(config or {})
        self._reply_fn = None          # async fn(text)->str, injected by the router
        self._ready    = False

    # --- contract ---
    @abstractmethod
    async def connect(self) -> bool: ...
    @abstractmethod
    async def disconnect(self) -> None: ...
    @abstractmethod
    async def send(self, message: str, channel: str = "general") -> bool: ...
    @abstractmethod
    async def receive(self, timeout: float = 5.0) -> list: ...
    @abstractmethod
    async def peers(self) -> list: ...

    # --- availability (for the UI; never raises) ---
    def available(self) -> bool:
        return True

    def unavailable_reason(self):
        return None

    def connected(self) -> bool:
        return bool(self._ready)

    # --- reply injection + formatting (provided by base) ---
    def _inject_reply(self, reply_fn) -> None:
        self._reply_fn = reply_fn

    def update_config(self, cfg: dict) -> None:
        """Merge fresh settings (e.g. a token entered in the UI) before connect.
        Subclasses override to map keys onto their own fields."""
        if cfg:
            self.config.update(cfg)

    def _format_for_channel(self, text: str) -> list:
        p = self.PROFILE
        if p.strip_markdown:
            import re
            text = re.sub(r"```[\s\S]*?```", "[code]", text)
            text = re.sub(r"`([^`]+)`", r"\1", text)
            text = re.sub(r"[*_]{1,2}([^*_]+)[*_]{1,2}", r"\1", text)
            text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        prefixed = f"{p.sage_prefix}{text}"
        if len(prefixed) <= p.max_chars:
            return [prefixed]
        if not p.split_long:
            return [prefixed[: p.max_chars - 3] + "..."]
        chunks, remaining = [], prefixed
        while remaining:
            if len(remaining) <= p.max_chars:
                chunks.append(remaining)
                break
            cut = remaining.rfind(" ", 0, p.max_chars)
            if cut == -1:
                cut = p.max_chars
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip()
        return chunks

    async def sage_respond(self, message: ChannelMessage) -> bool:
        """Generate a Sage reply (via the injected callable) and send it back."""
        if not self._reply_fn:
            logger.warning("[%s] no reply_fn injected — cannot auto-respond.", self.PROFILE.name)
            return False
        try:
            result = await self._reply_fn(message.content)
        except Exception as exc:
            logger.exception("[%s] reply generation error: %s", self.PROFILE.name, exc)
            return False
        if not result:
            return False
        ok_all = True
        for chunk in self._format_for_channel(result):
            if not await self.send(chunk, channel=message.channel):
                ok_all = False
        return ok_all


# ---------------------------------------------------------------------------
# Router — registry, per-adapter poll loops, recent buffer, auto-reply flag
# ---------------------------------------------------------------------------
class SageChannelRouter:
    def __init__(self, reply_fn=None, wake_word: str = "sage", recent_max: int = 60, store=None):
        self._reply_fn  = reply_fn
        self.wake_word  = (wake_word or "sage").strip().lower()
        self.auto_reply = False                      # opt-in; OFF by default
        self._adapters  = {}                         # name -> adapter
        self._tasks     = {}                         # name -> asyncio.Task
        self._recent    = deque(maxlen=recent_max)
        self._store     = store                       # SocialsConfig (secret-aware) or None

    def register(self, adapter: SageMessagingAdapter) -> None:
        adapter._inject_reply(self._reply_fn)
        self._adapters[adapter.PROFILE.name] = adapter
        logger.info("[Router] registered: %s", adapter.PROFILE.name)

    def names(self) -> list:
        return list(self._adapters.keys())

    async def connect(self, name: str) -> bool:
        a = self._adapters.get(name)
        if not a:
            return False
        if self._store is not None:
            try:
                a.update_config(self._store.get(name))   # pick up a token saved in the UI
            except Exception:
                pass
        ok = await a.connect()
        if ok and name not in self._tasks:
            self._tasks[name] = asyncio.create_task(self._poll(a))
        return ok

    async def disconnect(self, name: str) -> bool:
        t = self._tasks.pop(name, None)
        if t:
            t.cancel()
        a = self._adapters.get(name)
        if a:
            try:
                await a.disconnect()
            except Exception:
                pass
        return True

    async def send(self, name: str, text: str, channel: str = "general") -> bool:
        a = self._adapters.get(name)
        return bool(a and await a.send(text, channel=channel))

    async def peers(self, name: str) -> list:
        a = self._adapters.get(name)
        if not a:
            return []
        try:
            return await a.peers()
        except Exception:
            return []

    def set_auto_reply(self, on: bool) -> None:
        self.auto_reply = bool(on)

    def set_config(self, name: str, settings: dict) -> None:
        if self._store is not None:
            self._store.set(name, settings or {})
        a = self._adapters.get(name)
        if a and self._store is not None:
            try:
                a.update_config(self._store.get(name))
            except Exception:
                pass

    def clear_config(self, name: str, keys=None) -> None:
        if self._store is not None:
            self._store.clear(name, keys)
        a = self._adapters.get(name)
        if a and self._store is not None:
            try:
                a.update_config(self._store.get(name))
            except Exception:
                pass

    def config_snapshot(self) -> dict:
        return self._store.masked() if self._store is not None else {}

    def recent(self, limit: int = 30) -> list:
        out = []
        for m in list(self._recent)[-limit:]:
            out.append({
                "sender": m.sender, "channel": m.channel, "content": m.content,
                "timestamp": m.timestamp, "platform": m.platform,
            })
        return out

    def clear_recent(self, platform: str = None) -> int:
        """Drop buffered messages from the recent feed and return how many were
        removed. platform=None clears every channel; a platform name (e.g.
        "discord") clears only that channel's thread. The buffer is an in-memory
        deque -- nothing is persisted to disk and nothing is shared across user
        profiles -- so this fully clears what the Socials feed can show."""
        plat = (platform or "").strip().lower()
        if not plat:
            n = len(self._recent)
            self._recent.clear()
            return n
        kept = [m for m in self._recent if (m.platform or "").lower() != plat]
        removed = len(self._recent) - len(kept)
        self._recent.clear()
        self._recent.extend(kept)   # deque keeps maxlen; order preserved
        return removed

    def status(self) -> dict:
        import sys
        chans = {}
        for name, a in self._adapters.items():
            chans[name] = {
                "available":    a.available(),
                "connected":    a.connected(),
                "experimental": getattr(a, "EXPERIMENTAL", False),
                "note":         a.unavailable_reason(),
                "listening":    name in self._tasks,
            }
        return {
            "auto_reply": self.auto_reply, "wake_word": self.wake_word,
            "channels": chans, "config": self.config_snapshot(),
            "python": sys.executable, "python_version": sys.version.split()[0],
        }

    async def _poll(self, adapter: SageMessagingAdapter) -> None:
        """Buffer every inbound message; only auto-respond when opted in AND the
        wake word is present. Never dies on a transient error."""
        while True:
            try:
                for m in await adapter.receive(timeout=3.0):
                    self._recent.append(m)
                    if (self.auto_reply and self._reply_fn
                            and self.wake_word in (m.content or "").lower()):
                        asyncio.create_task(adapter.sage_respond(m))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("[Router] poll error on %s: %s", adapter.PROFILE.name, exc)
                await asyncio.sleep(5.0)


# ---------------------------------------------------------------------------
# Discord adapter (real — pip install discord.py + a bot token)
# ---------------------------------------------------------------------------
class DiscordAdapter(SageMessagingAdapter):
    PROFILE = ChannelProfile(
        name="discord", max_chars=2000, strip_markdown=False,
        split_long=True, sage_prefix="🔮 **Sage:** ",
    )

    def __init__(self, config: dict):
        super().__init__(config)
        self._client   = None
        self._token    = self.config.get("token", "")
        self._watched  = self.config.get("watched_channels", [])
        self._nickname = self.config.get("nickname", "Sage")
        self._inbox    = asyncio.Queue()

    def update_config(self, cfg: dict) -> None:
        if not cfg:
            return
        self.config.update(cfg)
        self._token    = cfg.get("token", self._token)
        self._watched  = cfg.get("watched_channels", self._watched)
        self._nickname = cfg.get("nickname", self._nickname)

    def available(self) -> bool:
        return _installed("discord")

    def unavailable_reason(self):
        if not self.available():
            return "pip install discord.py"
        if not self._token:
            return "add a bot token (config 'discord.token')"
        return None

    async def connect(self) -> bool:
        if not self.available():
            logger.error("[Discord] discord.py not installed.")
            return False
        if not self._token:
            logger.error("[Discord] no bot token configured.")
            return False
        try:
            import discord
            intents = discord.Intents.default()
            intents.message_content = True
            self._client = discord.Client(intents=intents)

            @self._client.event
            async def on_ready():
                logger.info("[Discord] connected as %s", self._client.user)
                self._ready = True

            @self._client.event
            async def on_message(message):
                if message.author == self._client.user:
                    return
                if self._watched and message.channel.name not in self._watched:
                    return
                await self._inbox.put(ChannelMessage(
                    sender=str(message.author.display_name),
                    channel=message.channel.name,
                    content=message.content,
                    platform="discord",
                    raw={"message_id": message.id, "channel_id": message.channel.id},
                ))

            asyncio.create_task(self._client.start(self._token))
            for _ in range(20):
                if self._ready:
                    return True
                await asyncio.sleep(0.5)
            return True            # slow to fire on_ready; assume connecting
        except Exception as exc:
            logger.error("[Discord] connect failed: %s", exc)
            return False

    async def disconnect(self) -> None:
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
        self._ready = False

    async def send(self, message: str, channel: str = "general") -> bool:
        if not self._client:
            return False
        try:
            import discord
            target = discord.utils.get(self._client.get_all_channels(), name=channel)
            if not target:
                logger.warning("[Discord] channel '%s' not found.", channel)
                return False
            await target.send(message)
            return True
        except Exception as exc:
            logger.error("[Discord] send error: %s", exc)
            return False

    async def receive(self, timeout: float = 5.0) -> list:
        msgs, deadline = [], time.time() + timeout
        while time.time() < deadline:
            try:
                msgs.append(self._inbox.get_nowait())
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.1)
        return msgs

    async def peers(self) -> list:
        if not self._client:
            return []
        try:
            import discord
            members = set()
            for cname in (self._watched or []):
                ch = discord.utils.get(self._client.get_all_channels(), name=cname)
                if ch and hasattr(ch, "members"):
                    for m in ch.members:
                        if not getattr(m, "bot", False):
                            members.add(m.display_name)
            return list(members)
        except Exception:
            return []


def _strip_html(s: str) -> str:
    import re
    s = re.sub(r"<br\s*/?>", "\n", s or "")
    s = re.sub(r"</p>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    import html as _html
    return _html.unescape(s).strip()


# ---------------------------------------------------------------------------
# Mastodon adapter (real — REST API + an access token)
# ---------------------------------------------------------------------------
class MastodonAdapter(SageMessagingAdapter):
    PROFILE = ChannelProfile(name="mastodon", max_chars=500, strip_markdown=True,
                             split_long=True, sage_prefix="🔮 Sage: ")

    def __init__(self, config: dict):
        super().__init__(config)
        self._instance = (self.config.get("instance") or "").rstrip("/")
        self._token = self.config.get("token", "")
        self._since_id = None
        self._last_fetch = 0.0

    def available(self) -> bool:
        return _installed("aiohttp")

    def unavailable_reason(self):
        if not self.available():
            return "pip install aiohttp"
        if not self._instance:
            return "set your instance URL (e.g. https://mastodon.social)"
        if not self._token:
            return "add an access token (your instance: Preferences > Development)"
        return None

    def update_config(self, cfg: dict) -> None:
        if not cfg:
            return
        self.config.update(cfg)
        self._instance = (cfg.get("instance") or self._instance or "").rstrip("/")
        self._token = cfg.get("token", self._token)

    def _hdr(self):
        return {"Authorization": "Bearer " + self._token}

    async def connect(self) -> bool:
        if not self.available() or not self._instance or not self._token:
            return False
        import aiohttp
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(self._instance + "/api/v1/accounts/verify_credentials",
                                 headers=self._hdr(),
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        logger.error("[Mastodon] verify_credentials -> %s", r.status)
                        return False
                    me = await r.json()
            self._ready = True
            logger.info("[Mastodon] connected as @%s", me.get("username"))
            return True
        except Exception as exc:
            logger.error("[Mastodon] connect failed: %s", exc)
            return False

    async def disconnect(self) -> None:
        self._ready = False

    async def send(self, message: str, channel: str = "general") -> bool:
        if not self._ready:
            return False
        import aiohttp
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(self._instance + "/api/v1/statuses", headers=self._hdr(),
                                  json={"status": message, "visibility": "public"},
                                  timeout=aiohttp.ClientTimeout(total=15)) as r:
                    return r.status in (200, 201)
        except Exception as exc:
            logger.error("[Mastodon] send error: %s", exc)
            return False

    async def receive(self, timeout: float = 5.0) -> list:
        if not self._ready:
            return []
        if time.time() - self._last_fetch < 15:      # be gentle on the API
            return []
        self._last_fetch = time.time()
        import aiohttp
        try:
            params = {"types[]": "mention", "limit": "20"}
            if self._since_id:
                params["since_id"] = self._since_id
            async with aiohttp.ClientSession() as s:
                async with s.get(self._instance + "/api/v1/notifications", headers=self._hdr(),
                                 params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        return []
                    notifs = await r.json()
            if notifs:
                self._since_id = notifs[0].get("id")  # newest-first -> remember the top
            out = []
            for n in notifs:
                if n.get("type") != "mention":
                    continue
                st = n.get("status") or {}
                out.append(ChannelMessage(
                    sender=(n.get("account") or {}).get("acct", "?"),
                    channel="mention", content=_strip_html(st.get("content") or ""),
                    platform="mastodon", raw={"id": n.get("id"), "status_id": st.get("id")}))
            return out
        except Exception as exc:
            logger.error("[Mastodon] receive error: %s", exc)
            return []

    async def peers(self) -> list:
        return []


# ---------------------------------------------------------------------------
# BlueSky adapter (real — AT Protocol + an app password)
# ---------------------------------------------------------------------------
class BlueSkyAdapter(SageMessagingAdapter):
    PROFILE = ChannelProfile(name="bluesky", max_chars=300, strip_markdown=True,
                             split_long=True, sage_prefix="🔮 Sage: ")

    def __init__(self, config: dict):
        super().__init__(config)
        self._service = (self.config.get("service") or "https://bsky.social").rstrip("/")
        self._handle = self.config.get("handle", "")
        self._app_password = self.config.get("app_password", "")
        self._jwt = None
        self._did = None
        self._last_fetch = 0.0

    def available(self) -> bool:
        return _installed("aiohttp")

    def unavailable_reason(self):
        if not self.available():
            return "pip install aiohttp"
        if not self._handle:
            return "set your handle (e.g. you.bsky.social)"
        if not self._app_password:
            return "add an App Password (BlueSky: Settings > App Passwords)"
        return None

    def update_config(self, cfg: dict) -> None:
        if not cfg:
            return
        self.config.update(cfg)
        self._service = (cfg.get("service") or self._service or "https://bsky.social").rstrip("/")
        self._handle = cfg.get("handle", self._handle)
        self._app_password = cfg.get("app_password", self._app_password)

    async def connect(self) -> bool:
        if not self.available() or not self._handle or not self._app_password:
            return False
        import aiohttp
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(self._service + "/xrpc/com.atproto.server.createSession",
                                  json={"identifier": self._handle, "password": self._app_password},
                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        logger.error("[BlueSky] createSession -> %s", r.status)
                        return False
                    d = await r.json()
            self._jwt = d.get("accessJwt")
            self._did = d.get("did")
            self._ready = bool(self._jwt and self._did)
            if self._ready:
                logger.info("[BlueSky] connected as %s", self._handle)
            return self._ready
        except Exception as exc:
            logger.error("[BlueSky] connect failed: %s", exc)
            return False

    async def disconnect(self) -> None:
        self._ready = False
        self._jwt = None

    def _auth(self):
        return {"Authorization": "Bearer " + (self._jwt or "")}

    async def send(self, message: str, channel: str = "general") -> bool:
        if not self._ready:
            return False
        import aiohttp
        import datetime
        rec = {"$type": "app.bsky.feed.post", "text": message,
               "createdAt": datetime.datetime.now(datetime.timezone.utc)
               .isoformat().replace("+00:00", "Z")}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(self._service + "/xrpc/com.atproto.repo.createRecord",
                                  headers=self._auth(),
                                  json={"repo": self._did, "collection": "app.bsky.feed.post",
                                        "record": rec},
                                  timeout=aiohttp.ClientTimeout(total=15)) as r:
                    return r.status in (200, 201)
        except Exception as exc:
            logger.error("[BlueSky] send error: %s", exc)
            return False

    async def receive(self, timeout: float = 5.0) -> list:
        if not self._ready:
            return []
        if time.time() - self._last_fetch < 15:
            return []
        self._last_fetch = time.time()
        import aiohttp
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(self._service + "/xrpc/app.bsky.notification.listNotifications",
                                 headers=self._auth(), params={"limit": "20"},
                                 timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 401:                  # JWT expired -> re-auth next pass
                        self._ready = False
                        return []
                    if r.status != 200:
                        return []
                    d = await r.json()
            out = []
            for n in (d.get("notifications") or []):
                if n.get("reason") not in ("mention", "reply"):
                    continue
                rec = n.get("record") or {}
                out.append(ChannelMessage(
                    sender=(n.get("author") or {}).get("handle", "?"),
                    channel=n.get("reason", "mention"), content=rec.get("text", ""),
                    platform="bluesky", raw={"uri": n.get("uri")}))
            return out
        except Exception as exc:
            logger.error("[BlueSky] receive error: %s", exc)
            return []

    async def peers(self) -> list:
        return []
