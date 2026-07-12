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
    sage_prefix:     str  = "Toga: "   # v2.12.0 rebrand (field name is internal)


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
        # v2.12.3: outbound-echo hook. The router injects this so that when
        # Toga replies (auto-reply OR manual send) her message also lands in
        # the feed buffer the UI reads. Before this, only INBOUND messages
        # were buffered, so the BitChat sub-UI showed the phone's half of the
        # conversation but never Toga's. Signature: fn(text, channel).
        self._echo_fn  = None
        self._ready    = False
        # v2.11.15: adapters record their most recent failure here so the
        # UI can SHOW it. Before this, connect errors only reached the
        # backend console (hidden unless Developer Mode) — "it just won't
        # connect" with no visible reason. Cleared on successful connect.
        self.last_error = None

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

    def _inject_echo(self, echo_fn) -> None:
        """Router hands us a callable(text, channel) that records Toga's own
        outbound message into the shared feed buffer."""
        self._echo_fn = echo_fn

    def _echo_outbound(self, text: str, channel: str) -> None:
        """Best-effort local echo of Toga's reply to the feed. Never raises
        into the send path — a feed hiccup must not fail a real send."""
        if self._echo_fn and text:
            try:
                self._echo_fn(text, channel)
            except Exception:
                pass

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
        self._echo_outbound(result, message.channel)   # show Toga's half in the feed
        ok_all = True
        for chunk in self._format_for_channel(result):
            if not await self.send(chunk, channel=message.channel):
                ok_all = False
        return ok_all


# ---------------------------------------------------------------------------
# Router — registry, per-adapter poll loops, recent buffer, auto-reply flag
# ---------------------------------------------------------------------------
class SageChannelRouter:
    # Reserved store key for router-level settings (not a real channel).
    _ROUTER_KEY = "__router__"

    def __init__(self, reply_fn=None, wake_word: str = "toga", recent_max: int = 60,
                 store=None, assistant_name: str = None):
        self._reply_fn  = reply_fn
        self.wake_word  = (wake_word or "toga").strip().lower()
        # v2.12.3: display name for Toga's own outbound messages in the feed.
        # Defaults to the wake word title-cased when not supplied.
        self.assistant_name = (assistant_name or self.wake_word.title() or "Toga").strip()
        self.auto_reply = False                      # opt-in; OFF by default
        self._adapters  = {}                         # name -> adapter
        self._tasks     = {}                         # name -> asyncio.Task
        self._recent    = deque(maxlen=recent_max)
        self._store     = store                       # SocialsConfig (secret-aware) or None
        # v2.12.3: per-peer auto-reply rate limit (mesh flood / abuse guard).
        try:
            from bitchat_guard import RateLimiter
            self._reply_limiter = RateLimiter()
        except Exception:
            self._reply_limiter = None
        # v2.12.4: auto_reply is now PERSISTED (per-profile, in the socials
        # store), so it survives a backend restart. Before this it was an
        # in-memory flag that silently reverted to OFF on every relaunch --
        # you'd enable "Toga auto-reply", restart for any reason, and Toga
        # would go quiet again with no clue why.
        self._restored_from_notice = 0.0   # throttle for the wake-word hint
        if store is not None:
            try:
                saved = store.get(self._ROUTER_KEY)
                if isinstance(saved, dict) and "auto_reply" in saved:
                    self.auto_reply = bool(saved["auto_reply"])
            except Exception:
                pass

    def register(self, adapter: SageMessagingAdapter) -> None:
        adapter._inject_reply(self._reply_fn)
        # v2.12.3: wire outbound echo so Toga's own replies show in the feed.
        _plat = adapter.PROFILE.name
        adapter._inject_echo(
            lambda text, channel, _p=_plat: self._remember_outbound(text, channel, _p))
        self._adapters[_plat] = adapter
        logger.info("[Router] registered: %s", _plat)

    def _remember_outbound(self, text: str, channel: str, platform: str) -> None:
        """Append one of Toga's own messages to the feed buffer, so the UI
        shows both halves of the conversation. sender = the assistant's name
        so the frontend can style it as 'ours'."""
        self._recent.append(ChannelMessage(
            sender=self.assistant_name, channel=channel or "general",
            content=text or "", timestamp=time.time(),
            platform=platform, raw={"echo": True, "peer_id": "self"}))

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
        if not a:
            return False
        ok = await a.send(text, channel=channel)
        if ok:
            # v2.12.3: a manual send from the UI is still Toga's voice — echo
            # it to the feed too, so the composer's message appears in-thread.
            self._remember_outbound(text, channel, name)
        return bool(ok)

    async def peers(self, name: str) -> list:
        a = self._adapters.get(name)
        if not a:
            return []
        try:
            return await a.peers()
        except Exception:
            return []

    async def identity(self, name: str) -> dict:
        """v2.12.3: identity fingerprints for a channel that supports them
        (currently BitChat). {} for channels without an identity() method."""
        a = self._adapters.get(name)
        if not a or not hasattr(a, "identity"):
            return {}
        try:
            return await a.identity()
        except Exception:
            return {}

    def set_auto_reply(self, on: bool) -> None:
        self.auto_reply = bool(on)
        # v2.12.4: persist so it survives a restart.
        if self._store is not None:
            try:
                self._store.set(self._ROUTER_KEY, {"auto_reply": bool(on)})
            except Exception:
                pass

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
                "error":        getattr(a, "last_error", None),
                "listening":    name in self._tasks,
            }
        return {
            "auto_reply": self.auto_reply, "wake_word": self.wake_word,
            "channels": chans, "config": self.config_snapshot(),
            "python": sys.executable, "python_version": sys.version.split()[0],
        }

    async def _dispatch_reply(self, adapter, m) -> None:
        """Run the auto-reply and make FAILURES VISIBLE. Before v2.12.4 a
        reply that came back empty (e.g. the configured model isn't loaded)
        or a send that failed just vanished -- the phone got silence and the
        UI showed nothing, indistinguishable from 'feature off'. Now a failed
        reply drops a plain notice into the feed so you can see WHY."""
        try:
            ok = await adapter.sage_respond(m)
        except Exception as exc:
            logger.exception("[Router] reply dispatch error on %s: %s",
                             adapter.PROFILE.name, exc)
            self._notice(adapter.PROFILE.name,
                         f"{self.assistant_name} hit an error replying: "
                         f"{type(exc).__name__}. See the backend console.")
            return
        if not ok:
            self._notice(adapter.PROFILE.name,
                         f"{self.assistant_name} couldn't generate a reply — "
                         "the model returned nothing or the send failed. Check "
                         "that a model is loaded/selected and try again.")

    def _notice(self, platform: str, text: str) -> None:
        """Drop a local system notice into the feed (never sent to the mesh)."""
        self._recent.append(ChannelMessage(
            sender="system", channel="general", content=text,
            timestamp=time.time(), platform=platform,
            raw={"echo": True, "peer_id": "system", "notice": True}))

    def _maybe_wake_hint(self, platform: str) -> None:
        """Drop a throttled local notice when the wake word is heard but
        auto-reply is off. At most once per 5 minutes so it never spams."""
        now = time.time()
        if now - self._restored_from_notice < 300:
            return
        self._restored_from_notice = now
        self._recent.append(ChannelMessage(
            sender="system", channel="general",
            content=(f"Heard the wake word “{self.wake_word}”, but "
                     f"auto-reply is OFF. Turn on “{self.assistant_name} "
                     "auto-reply” in Socials settings to have "
                     f"{self.assistant_name} respond on the mesh."),
            timestamp=now, platform=platform,
            raw={"echo": True, "peer_id": "system", "notice": True}))

    async def _poll(self, adapter: SageMessagingAdapter) -> None:
        """Buffer every inbound message; only auto-respond when opted in AND the
        wake word is present. Never dies on a transient error."""
        while True:
            try:
                for m in await adapter.receive(timeout=3.0):
                    self._recent.append(m)
                    # Never auto-reply to our OWN messages (echoes of what Sage
                    # posted) or system notices — that feedback loop is Sage
                    # "talking to herself". BitChat echoes carry echo=True /
                    # peer_id='self', and the sender name equals our own nickname.
                    _raw = getattr(m, "raw", None) or {}
                    _is_self = (
                        _raw.get("echo") is True
                        or _raw.get("peer_id") in ("self", "system")
                        or (m.sender or "").strip().lower()
                            in ("system", (self.wake_word or "").strip().lower())
                    )
                    _has_wake = (not _is_self
                                 and self.wake_word in (m.content or "").lower())
                    if self.auto_reply and self._reply_fn and _has_wake:
                        # v2.12.3: throttle per sender so one peer can't flood
                        # the reply path (and the GPU). Over-limit = silent
                        # drop; we don't announce the throttle to the sender.
                        _key = (_raw.get("peer_id") or m.sender or "anon")
                        if (self._reply_limiter is None
                                or self._reply_limiter.allow(str(_key))):
                            logger.info("[Router] wake word matched on %s from %s "
                                        "-> dispatching reply",
                                        adapter.PROFILE.name, m.sender)
                            asyncio.create_task(self._dispatch_reply(adapter, m))
                        else:
                            logger.info("[Router] auto-reply rate-limited for %s", _key)
                    elif _has_wake and not self.auto_reply:
                        # v2.12.4: the wake word was heard but auto-reply is
                        # OFF. Silence here is the #1 "why won't Toga answer"
                        # confusion, so drop ONE throttled hint into the feed
                        # (local notice, never sent to the mesh).
                        self._maybe_wake_hint(adapter.PROFILE.name)
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
        split_long=True, sage_prefix="🔮 **Toga:** ",
    )

    def __init__(self, config: dict):
        super().__init__(config)
        self._client   = None
        self._task     = None      # v2.11.15: gateway task, observed for errors
        self._token    = self.config.get("token", "")
        self._watched  = self.config.get("watched_channels", [])
        self._nickname = self.config.get("nickname", "Toga")
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
        """v2.11.15 rework — the old version had two lies in it:
          1. client.start() ran as a fire-and-forget task whose EXCEPTIONS
             VANISHED — a bad token (LoginFailure) or the Message Content
             Intent being disabled in the Discord Developer Portal
             (PrivilegedIntentsRequired) killed the connection silently.
          2. When on_ready hadn't fired after the wait, it returned True
             anyway ("assume connecting") — so the UI said connected while
             nothing would ever communicate. Exactly the no-handshake
             symptom Todd reported.
        Now: the task's outcome is observed, failures land in last_error
        with an actionable hint, and connect() only returns True when the
        gateway is actually READY."""
        if not self.available():
            self.last_error = "discord.py not installed (pip install discord.py)"
            logger.error("[Discord] discord.py not installed.")
            return False
        if not self._token:
            self.last_error = "no bot token configured"
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
                self.last_error = None
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

            self._task = asyncio.create_task(self._client.start(self._token))

            def _observe(t):
                exc = None
                try:
                    exc = t.exception()
                except (asyncio.CancelledError, asyncio.InvalidStateError):
                    return
                if exc is None:
                    return
                name = type(exc).__name__
                if name == "PrivilegedIntentsRequired":
                    self.last_error = ("Message Content Intent is OFF for this bot — "
                                       "enable it: discord.com/developers > your app > "
                                       "Bot > Privileged Gateway Intents")
                elif name == "LoginFailure":
                    self.last_error = "login failed — the bot token is wrong or was reset"
                else:
                    self.last_error = f"{name}: {exc}"
                self._ready = False
                logger.error("[Discord] gateway task died: %s", self.last_error)
            self._task.add_done_callback(_observe)

            # Wait up to 15s for a REAL ready (or an early task death).
            for _ in range(30):
                if self._ready:
                    return True
                if self._task.done():
                    return False          # _observe filled last_error
                await asyncio.sleep(0.5)
            self.last_error = ("no response from Discord after 15s — check your "
                               "network, the token, and the bot's server invite")
            return False
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
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
                             split_long=True, sage_prefix="🔮 Toga: ")

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
            self.last_error = self.unavailable_reason() or "not configured"
            return False
        import aiohttp
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(self._instance + "/api/v1/accounts/verify_credentials",
                                 headers=self._hdr(),
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        # v2.11.15: say WHY in the UI, not just the console.
                        if r.status == 401:
                            self.last_error = ("token rejected (401) — regenerate it: "
                                               "your instance > Preferences > Development")
                        elif r.status == 404:
                            self.last_error = ("instance URL looks wrong (404) — expected "
                                               "e.g. https://mastodon.social")
                        else:
                            self.last_error = f"verify_credentials -> HTTP {r.status}"
                        logger.error("[Mastodon] %s", self.last_error)
                        return False
                    me = await r.json()
            self._ready = True
            self.last_error = None
            logger.info("[Mastodon] connected as @%s", me.get("username"))
            return True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
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
                             split_long=True, sage_prefix="🔮 Toga: ")

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
            self.last_error = self.unavailable_reason() or "not configured"
            return False
        import aiohttp
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(self._service + "/xrpc/com.atproto.server.createSession",
                                  json={"identifier": self._handle, "password": self._app_password},
                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        # v2.11.15: say WHY in the UI, not just the console.
                        if r.status == 401:
                            self.last_error = ("sign-in rejected (401) — check the handle "
                                               "(e.g. you.bsky.social) and use an App "
                                               "Password (Settings > App Passwords), not "
                                               "your account password")
                        else:
                            self.last_error = f"createSession -> HTTP {r.status}"
                        logger.error("[BlueSky] %s", self.last_error)
                        return False
                    d = await r.json()
            self._jwt = d.get("accessJwt")
            self._did = d.get("did")
            self._ready = bool(self._jwt and self._did)
            if self._ready:
                self.last_error = None
                logger.info("[BlueSky] connected as %s", self._handle)
            else:
                self.last_error = "session created but no token returned — try again"
            return self._ready
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
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
