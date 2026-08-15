#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
veridian_cli.py — VeridianAI in your terminal
=============================================

A third client for the same backend the Electron UI and browser already
talk to. PURELY ADDITIVE: no backend changes; this file speaks the exact
protocol frontend/js/chat.js speaks.

  * REST  : http://127.0.0.1:8000/api/...   (auth, models, tiers, config,
            archives, downloads, chat-memory, abort, health)
  * Chat  : ws://127.0.0.1:8000/ws/chat     (streamed JSON events)

Tier 1 (2026-07-25): streaming chat REPL, login+MFA, Ctrl+C abort.
Tier 2 (2026-07-26): subcommands over the REST surface + shared history
with the GUI via /api/chat-memory.

Usage:
  veridian-cli                                # chat REPL (default)
  veridian-cli chat --resume                  # continue the GUI conversation
  veridian-cli chat --once "hello Toga"       # one-shot, then exit
  veridian-cli models [load|unload|refresh] [ID]
  veridian-cli tiers [status|restart] [NAME]
  veridian-cli archives [save|load|delete|title] [FILE] [TITLE]
  veridian-cli config get [KEY] | config set KEY VALUE
  veridian-cli downloads [get|save|delete|clear] [NAME] [-o OUT]
  veridian-cli status                         # stack overview
  veridian-cli dash                           # live dashboard (Ctrl+C exits)
  veridian-cli tui                            # chat + pinned dashboard, one
                                              #   terminal (small screens)
  veridian-cli abort                          # kill an in-flight generation

In-chat: /mode battle | /mode symposium switch every send to Build Battle
or Symposium (both stream standard token/done events); /battle <spec> and
/symposium <topic> fire one-shots without switching; /rounds 1-3.

Tier 3 (2026-07-26): `dash` — live tier/model/memory-chain dashboard.
Tier labels shown to the user are display-only ("Veridian" for the
oracle tier, "Agent" for the sage tier — persona names are customizable,
so the CLI stays neutral); functional names and model ids are untouched.

Chat protocol (verified against chat.js + main.py ws_chat, 2026-07-25):
  send    {"action":"chat","messages":[{role,content,ts}],"model_id":...,
           "options":{...}}
  receive {"type": "token"|"done"|"error"|"aborted"|"stall_detected"|
           "agent_step"|"tool_call"|"tool_result"|"aiq_nudge_received"|
           "warm_context_restored"|"image_generated"|"pong", ...}

Auth: when multiuser_enabled, everything requires the "oai_session"
cookie (session.AUTH_COOKIE). We log in via POST /api/auth/login
(+ optional /api/auth/mfa/verify) and carry the cookie everywhere.

Dependencies: requests + websockets — both already installed with the
backend (websockets ships inside uvicorn[standard]).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("veridian-cli: the 'requests' package is required "
          "(pip install requests)")
    sys.exit(1)

# websockets >= 11 ships a synchronous client, which lets this stay a
# simple synchronous program (Ctrl+C behaves predictably on Windows).
try:
    from websockets.sync.client import connect as ws_connect
    from websockets.exceptions import ConnectionClosed
except ImportError:
    ws_connect = None
    ConnectionClosed = Exception


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------
class C:
    """ANSI palette. --plain (or a non-tty stdout) turns everything off."""
    enabled = True
    RESET = "\x1b[0m"; BOLD = "\x1b[1m"; DIM = "\x1b[2m"
    CYAN = "\x1b[36m"; GREEN = "\x1b[32m"; YELLOW = "\x1b[33m"
    RED = "\x1b[31m"; MAGENTA = "\x1b[35m"

    @classmethod
    def c(cls, code: str, text: str) -> str:
        return f"{code}{text}{cls.RESET}" if cls.enabled else text


def _enable_windows_ansi() -> None:
    if os.name == "nt":
        os.system("")  # nudges the console into VT/ANSI mode


# Single shared stdout lock so the TUI's header-refresh thread and the
# chat stream never interleave mid-escape-sequence. Plain REPL/commands pay
# only an uncontended RLock acquire — negligible.
PRINT_LOCK = threading.RLock()


def _print(*args, **kwargs) -> None:
    with PRINT_LOCK:
        print(*args, **kwargs)


def info(msg: str) -> None:
    _print(C.c(C.DIM, f"  · {msg}"))


def warn(msg: str) -> None:
    _print(C.c(C.YELLOW, f"  ! {msg}"))


def err(msg: str) -> None:
    _print(C.c(C.RED, f"  x {msg}"))


# ---------------------------------------------------------------------------
# Display names (v2.12 branding, 2026-07-26). The backend's internal tier
# names (oracle/sage/daemon, labels "Oracle"/"Toga") are load-bearing: they
# appear in routing tables, /api/tiers/{name} paths, and qualified model ids
# like "somemodel [Toga]". We therefore remap ONLY the display layer here —
# "Agent" rather than a persona name because the persona (Toga) is
# user-customizable. Functional names/ids pass through unchanged.
# ---------------------------------------------------------------------------
_TIER_DISPLAY = {
    "oracle": "Veridian", "ollama_oracle": "Veridian",
    "sage": "Agent", "llama_sage": "Agent", "toga": "Agent",
    "daemon": "Daemon", "llama_daemon": "Daemon",
    "npu": "NPU", "npu_lemonade": "NPU",
}


def tier_display(name) -> str:
    """Friendly label for a backend tier tag/label. Unknown -> unchanged."""
    if not name:
        return ""
    return _TIER_DISPLAY.get(str(name).strip().lower(), str(name))


def _fmt_uptime(sec) -> str:
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return "?"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def _fmt_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def _fmt_ts(epoch) -> str:
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


def _die(r: requests.Response) -> None:
    """Print a friendly error from a non-2xx response and exit."""
    detail = ""
    try:
        detail = r.json().get("detail", "")
    except Exception:
        detail = (r.text or "")[:200]
    err(f"HTTP {r.status_code}: {detail or 'request failed'}")
    sys.exit(1)


def _ok(r: requests.Response) -> dict:
    if not r.ok:
        _die(r)
    try:
        return r.json()
    except ValueError:
        return {}


# --------------------------------------------------------------------------
# Backend client
# --------------------------------------------------------------------------
class Veridian:
    AUTH_COOKIE = "oai_session"   # session.AUTH_COOKIE — canonical name

    def __init__(self, host: str, port: int):
        self.base = f"http://{host}:{port}"
        self.ws_url = f"ws://{host}:{port}/ws/chat"
        self.http = requests.Session()

    # -- plumbing -----------------------------------------------------
    def get(self, path: str, **kw) -> requests.Response:
        return self.http.get(f"{self.base}{path}", timeout=kw.pop("timeout", 30), **kw)

    def post(self, path: str, body: dict | None = None, **kw) -> requests.Response:
        return self.http.post(f"{self.base}{path}", json=body,
                              timeout=kw.pop("timeout", 90), **kw)

    def delete(self, path: str, **kw) -> requests.Response:
        return self.http.delete(f"{self.base}{path}",
                                timeout=kw.pop("timeout", 30), **kw)

    # -- health / auth ------------------------------------------------
    def alive(self) -> bool:
        try:
            self.get("/api/health", timeout=4)
            return True
        except requests.RequestException:
            return False

    def auth_status(self) -> dict:
        return _ok(self.get("/api/auth/status", timeout=8))

    def login_interactive(self, username: str | None) -> str:
        """Login (with MFA second step when enrolled). Returns username."""
        while True:
            user = username or input("  username: ").strip()
            username = None  # only prefill the first attempt
            pw = getpass.getpass("  password: ")
            r = self.post("/api/auth/login",
                          {"username": user, "password": pw}, timeout=10)
            if r.status_code == 401:
                err("invalid credentials"); continue
            if r.status_code == 429:
                err(r.json().get("detail", "too many attempts")); continue
            if r.status_code == 403:
                err(r.json().get("detail", "access restricted"))
                sys.exit(2)
            body = _ok(r)
            if body.get("mfa_required"):
                body = self._mfa_step(body)
                if body is None:
                    continue
            if body.get("must_change"):
                warn("this account is flagged must-change-password; "
                     "some actions may be confined until it's updated in the UI.")
            return body.get("username", user)

    def _mfa_step(self, challenge: dict) -> dict | None:
        methods = challenge.get("methods", [])
        info(f"MFA required ({', '.join(methods) or 'totp'})")
        code = input("  code (TOTP, or 'r:<recovery-code>'): ").strip()
        method = "totp"
        if code.lower().startswith("r:"):
            method, code = "recovery", code[2:].strip()
        r = self.post("/api/auth/mfa/verify",
                      {"mfa_token": challenge.get("mfa_token", ""),
                       "method": method, "code": code}, timeout=10)
        if r.status_code == 401:
            err(r.json().get("detail", "code didn't verify"))
            return None
        return _ok(r)

    # -- feature surfaces ---------------------------------------------
    def models(self) -> list:
        return _ok(self.get("/api/models")).get("models", [])

    def default_model(self) -> str | None:
        try:
            cfg = _ok(self.get("/api/config", timeout=8))
            return (cfg.get("default_model")
                    or (cfg.get("inference") or {}).get("default_model"))
        except SystemExit:
            raise
        except requests.RequestException:
            return None

    def abort(self) -> None:
        try:
            self.post("/api/abort", timeout=5)
        except requests.RequestException:
            pass

    # -- WebSocket ----------------------------------------------------
    def ws_open(self):
        if ws_connect is None:
            err("the 'websockets' package (v11+) is required — it ships "
                "with uvicorn[standard]:  pip install websockets")
            sys.exit(1)
        cookie = self.http.cookies.get(self.AUTH_COOKIE)
        headers = {"Cookie": f"{self.AUTH_COOKIE}={cookie}"} if cookie else {}
        try:  # websockets >= 12 name, then the older one
            return ws_connect(self.ws_url, additional_headers=headers,
                              max_size=None)
        except TypeError:
            return ws_connect(self.ws_url, extra_headers=headers,
                              max_size=None)


# --------------------------------------------------------------------------
# One chat turn over the socket
# --------------------------------------------------------------------------
_ACTION_LABELS = {"chat": ("Toga", C.GREEN),
                  "build_battle": ("Battle", C.MAGENTA),
                  "symposium": ("Symposium", C.MAGENTA)}


def run_turn(v: Veridian, ws, messages: list, model_id: str | None,
             options: dict, action: str = "chat",
             rounds: int | None = None) -> str | None:
    """Send one WS action (chat / build_battle / symposium) and stream
    events until done/error/aborted. Both alt modes reuse the standard
    token/done events (see _handle_symposium/_handle_build_battle), so one
    renderer serves all three. Returns the streamed text (None on error).
    Ctrl+C aborts the turn."""
    payload = {"action": action, "messages": messages, "options": options}
    if model_id:
        payload["model_id"] = model_id
    if rounds and action in ("build_battle", "symposium"):
        payload["rounds"] = max(1, min(3, int(rounds)))  # backend re-clamps
    ws.send(json.dumps(payload))

    acc: list = []
    name, color = _ACTION_LABELS.get(action, ("Toga", C.GREEN))
    label = C.c(color + C.BOLD, name)
    _print(f"{label} ", end="", flush=True)
    aborted_by_user = False
    while True:
        try:
            raw = ws.recv()
        except KeyboardInterrupt:
            # Same path as the UI Stop button: out-of-band HTTP abort.
            if not aborted_by_user:
                aborted_by_user = True
                v.abort()
                _print()
                warn("aborting (Ctrl+C again to force-quit the turn)...")
                continue
            _print()
            return "".join(acc) or None
        except ConnectionClosed:
            _print()
            err("connection closed by backend mid-turn")
            return "".join(acc) or None

        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        t = data.get("type")

        if t == "token":
            chunk = data.get("content") or ""
            acc.append(chunk)
            _print(chunk, end="", flush=True)
        elif t == "done":
            final = data.get("content")
            if not acc and final:      # non-streamed reply
                acc.append(final)
                _print(final, end="")
            _print()
            model = data.get("model")
            if model:
                info(f"[{model}]")
            return "".join(acc)
        elif t == "aborted":
            _print()
            warn("generation stopped")
            return "".join(acc) or None
        elif t == "error":
            _print()
            err(str(data.get("content", "unknown backend error")))
            return "".join(acc) or None
        elif t == "stall_detected":
            _print()
            detail = (data.get("content") or data.get("reason")
                      or "run appears wedged")
            warn(f"stall watchdog: {detail}")
        elif t == "agent_step":
            step = data.get("step") or data.get("content") or ""
            _print()
            _print(C.c(C.DIM, f"  [step] {str(step)[:160]}"))
        elif t == "tool_call":
            tool = data.get("tool") or data.get("action") or "?"
            _print()
            _print(C.c(C.MAGENTA, f"  [tool] {tool} "
                       f"{str(data.get('content') or '')[:120]}"))
        elif t == "tool_result":
            preview = str(data.get("content") or "")[:160].replace("\n", " ")
            _print(C.c(C.DIM, f"  [result] {preview}"))
        elif t == "image_generated":
            _print()
            info(f"image generated: "
                 f"{data.get('filename') or data.get('path') or '(see UI)'}")
        elif t == "warm_context_restored":
            info("CRAIID warm context restored into this conversation")
        elif t == "aiq_nudge_received":
            info("AIQ nudge landed")
        # pong / unknown types: ignore gracefully (protocol may grow)


# --------------------------------------------------------------------------
# Chat REPL (default command)
# --------------------------------------------------------------------------
HELP = """\
  /models            list available models (id + tier)
  /model <id>        switch model for subsequent turns
  /mode [chat|battle|symposium]   action used for every send (no arg: show)
  /rounds <1-3>      rounds for battle/symposium modes
  /battle <spec>     one-shot Build Battle (mode stays as-is)
  /symposium <topic> one-shot Symposium debate (mode stays as-is)
  /clear             clear the local conversation history
  /save              push this conversation to the backend (GUI sees it)
  /archive           archive the backend conversation (like the UI button)
  /abort             abort any in-flight generation server-side
  /help              this text
  /quit              exit (Ctrl+D also works; Ctrl+C at the prompt exits too)
  anything else      is sent using the current /mode"""

_MODE_ALIASES = {"chat": "chat", "battle": "build_battle",
                 "build_battle": "build_battle", "symposium": "symposium"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def print_models(models: list, current: str | None) -> None:
    if not models:
        warn("no models reported — are the tiers still warming up?")
        return
    for m in models:
        mid = m.get("id", "?")   # NEVER remapped — ids are functional
        tag = tier_display(m.get("tier") or m.get("backend") or "")
        mark = C.c(C.GREEN, " *") if mid == current else "  "
        print(f"{mark} {mid}" + (C.c(C.DIM, f"  ({tag})") if tag else ""))


def cmd_chat(v: Veridian, args) -> int:
    return _chat_loop(v, args)


def _chat_loop(v: Veridian, args) -> int:
    messages: list = []
    if getattr(args, "resume", False):
        hist = _ok(v.get("/api/chat-memory")).get("history", [])
        if isinstance(hist, list) and hist:
            messages = [m for m in hist
                        if isinstance(m, dict) and m.get("role")]
            info(f"resumed {len(messages)} messages from the shared "
                 f"conversation")
        else:
            info("no shared conversation to resume — starting fresh")

    model_id = args.model or v.default_model()
    options: dict = {}
    if args.temperature is not None:
        options["temperature"] = args.temperature
    if args.max_tokens is not None and args.max_tokens > 0:
        options["max_tokens"] = args.max_tokens

    ws = v.ws_open()
    info("connected — /help for commands, Ctrl+C mid-stream to stop a reply")
    if model_id:
        info(f"model: {model_id}")
    mode = "chat"       # /mode switches; mirrors the UI's toggles
    rounds = 1          # /rounds; backend clamps 1-3
    once = args.once
    try:
        while True:
            if once is not None:
                line = once
            else:
                try:
                    line = input(C.c(C.CYAN + C.BOLD, "You ") + "> ")
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
            line = line.strip()
            if not line:
                continue

            if line.startswith("/"):
                cmd, _, arg = line.partition(" ")
                cmd = cmd.lower()
                if cmd in ("/quit", "/exit", "/q"):
                    break
                elif cmd == "/help":
                    print(HELP)
                elif cmd == "/clear":
                    messages = []
                    info("history cleared")
                elif cmd == "/save":
                    _ok(v.post("/api/chat-memory", {"history": messages}))
                    info(f"saved {len(messages)} messages to the shared "
                         f"conversation")
                elif cmd == "/archive":
                    res = _ok(v.post("/api/archives/save"))
                    info(f"archived: {res.get('filename', res)}")
                elif cmd == "/abort":
                    v.abort()
                    info("abort signal sent")
                elif cmd == "/models":
                    print_models(v.models(), model_id)
                elif cmd == "/model":
                    if arg.strip():
                        model_id = arg.strip()
                        info(f"model -> {model_id}")
                    else:
                        info(f"current model: "
                             f"{model_id or '(backend default)'}")
                elif cmd == "/mode":
                    want = arg.strip().lower()
                    if not want:
                        info(f"mode: {mode} (rounds: {rounds})")
                    elif want in _MODE_ALIASES:
                        mode = _MODE_ALIASES[want]
                        info(f"mode -> {mode}"
                             + (f" (rounds: {rounds})"
                                if mode != "chat" else ""))
                    else:
                        warn("modes: chat, battle, symposium")
                elif cmd == "/rounds":
                    try:
                        rounds = max(1, min(3, int(arg.strip())))
                        info(f"rounds -> {rounds}")
                    except ValueError:
                        warn("usage: /rounds <1-3>")
                elif cmd in ("/battle", "/symposium"):
                    # One-shot: run in that mode without switching /mode.
                    topic = arg.strip()
                    if not topic:
                        warn(f"usage: {cmd} <"
                             + ("coding challenge spec"
                                if cmd == "/battle" else "proposition") + ">")
                        continue
                    one_action = ("build_battle" if cmd == "/battle"
                                  else "symposium")
                    messages.append({"role": "user", "content": topic,
                                     "ts": now_iso()})
                    try:
                        reply = run_turn(v, ws, messages, model_id, options,
                                         action=one_action, rounds=rounds)
                    except ConnectionClosed:
                        warn("socket dropped; reconnecting...")
                        ws = v.ws_open()
                        reply = run_turn(v, ws, messages, model_id, options,
                                         action=one_action, rounds=rounds)
                    if reply:
                        messages.append({"role": "assistant",
                                         "content": reply, "ts": now_iso()})
                    else:
                        messages.pop()
                else:
                    warn(f"unknown command {cmd} — /help")
                continue

            messages.append({"role": "user", "content": line,
                             "ts": now_iso()})
            try:
                reply = run_turn(v, ws, messages, model_id, options,
                                 action=mode, rounds=rounds)
            except ConnectionClosed:
                warn("socket dropped; reconnecting...")
                ws = v.ws_open()
                reply = run_turn(v, ws, messages, model_id, options,
                                 action=mode, rounds=rounds)
            if reply:
                messages.append({"role": "assistant", "content": reply,
                                 "ts": now_iso()})
            else:
                # Empty turn: drop the dangling user message so a retry
                # doesn't double it (the "Umm Toga?" lesson of 2026-07-25).
                messages.pop()

            if once is not None:
                break
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return 0


# --------------------------------------------------------------------------
# Tier-2 subcommands
# --------------------------------------------------------------------------
def cmd_models(v: Veridian, args) -> int:
    act = args.action
    if act == "list":
        print_models(v.models(), None)
    elif act == "load":
        res = _ok(v.post("/api/models/load", {"model_id": args.id},
                         timeout=300))
        print(json.dumps(res, indent=2))
    elif act == "unload":
        res = _ok(v.post("/api/models/unload", {"model_id": args.id}))
        info(f"unloaded {res.get('model_id', args.id)}")
    elif act == "refresh":
        res = _ok(v.post("/api/models/refresh", timeout=300))
        restarted = res.get("restarted_tiers", [])
        info(f"refreshed — restarted tiers: {restarted or 'none'}")
        for w in res.get("warnings", []):
            warn(str(w))
        print_models(res.get("models", []), None)
    return 0


def cmd_tiers(v: Veridian, args) -> int:
    if args.action == "list":
        tiers = _ok(v.get("/api/tiers")).get("tiers", {})
        if not tiers:
            warn("no tier data")
            return 0
        for name, snap in tiers.items():
            snap = snap or {}
            up = snap.get("running", snap.get("up", snap.get("alive")))
            mark = (C.c(C.GREEN, "up  ") if up
                    else C.c(C.RED, "down") if up is not None
                    else C.c(C.DIM, "?   "))
            extra = ", ".join(f"{k}={snap[k]}" for k in
                              ("port", "ctx_size", "model") if snap.get(k))
            # Friendly label first; functional name stays visible because
            # commands (tiers restart <name>) need the real one.
            label = f"{tier_display(name):<8} [{name}]"
            print(f"  {mark} {label}" + (C.c(C.DIM, f"  ({extra})")
                                         if extra else ""))
    elif args.action == "status":
        print(json.dumps(_ok(v.get(f"/api/tiers/{args.name}/status")),
                         indent=2))
    elif args.action == "restart":
        info(f"restarting tier '{args.name}' — the new llama-server may "
             f"take up to ~70s to load its model...")
        res = _ok(v.post(f"/api/tiers/{args.name}/restart", timeout=180))
        print(json.dumps(res, indent=2))
    return 0


def cmd_archives(v: Veridian, args) -> int:
    act = args.action
    if act == "list":
        archives = _ok(v.get("/api/archives")).get("archives", [])
        if not archives:
            info("no archives")
            return 0
        for a in archives:
            if isinstance(a, dict):
                fn = a.get("filename") or a.get("name") or "?"
                title = a.get("title") or a.get("preview") or ""
                print(f"  {fn}" + (C.c(C.DIM, f"  — {str(title)[:70]}")
                                   if title else ""))
            else:
                print(f"  {a}")
    elif act == "save":
        res = _ok(v.post("/api/archives/save"))
        info(f"archived current shared conversation: "
             f"{res.get('filename', res)}")
    elif act == "load":
        res = _ok(v.post("/api/archives/load", {"filename": args.file}))
        info(f"archive loaded into the shared conversation "
             f"({len(res.get('history', [])) or ''} messages) — "
             f"'veridian-cli chat --resume' to continue it")
    elif act == "delete":
        _ok(v.post("/api/archives/delete", {"filename": args.file}))
        info(f"deleted {args.file}")
    elif act == "title":
        _ok(v.post("/api/archives/title",
                   {"filename": args.file, "title": args.title or ""}))
        info(f"title set on {args.file}")
    return 0


def cmd_config(v: Veridian, args) -> int:
    if args.action == "get":
        cfg = _ok(v.get("/api/config"))
        if args.key:
            if args.key not in cfg:
                err(f"no such key: {args.key}")
                return 1
            print(json.dumps(cfg[args.key], indent=2))
        else:
            for k in sorted(cfg):
                val = json.dumps(cfg[k])
                if len(val) > 80:
                    val = val[:77] + "..."
                print(f"  {k} = {val}")
    elif args.action == "set":
        # Coerce: valid JSON (numbers, true/false, null, quoted strings,
        # lists) is parsed; anything else rides as a plain string.
        raw = args.value
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        r = v.post("/api/config", {args.key: value})
        if r.status_code == 400:
            err(r.json().get("detail", "rejected"))  # allowlist miss = typo
            return 1
        if r.status_code == 403:
            err(r.json().get("detail", "owner-managed setting"))
            return 1
        _ok(r)
        info(f"{args.key} = {json.dumps(value)}  (saved)")
        if args.key in ("n_ctx", "max_tokens"):
            warn("tier ctx changes need a tier restart to apply: "
                 "veridian-cli models refresh")
    return 0


def cmd_downloads(v: Veridian, args) -> int:
    act = args.action
    if act == "list":
        files = _ok(v.get("/api/downloads")).get("files", [])
        if not files:
            info("downloads folder is empty")
            return 0
        for f in files:
            print(f"  {f.get('name', '?'):<44} "
                  + C.c(C.DIM, f"{_fmt_size(f.get('size')):>9}  "
                        f"{_fmt_ts(f.get('modified'))}"))
    elif act == "get":
        name = Path(args.name).name
        r = v.get(f"/api/downloads/{name}", params={"dl": 1}, timeout=120)
        if not r.ok:
            _die(r)
        out = Path(args.out or name)
        out.write_bytes(r.content)
        info(f"saved {out} ({_fmt_size(len(r.content))})")
    elif act == "save":
        src = Path(args.name)
        if not src.is_file():
            err(f"no such file: {src}")
            return 1
        res = _ok(v.post("/api/downloads/save",
                         {"filename": src.name,
                          "content": src.read_text(encoding="utf-8",
                                                   errors="replace")}))
        info(f"uploaded as {res.get('filename')} "
             f"({_fmt_size(res.get('size'))})")
    elif act == "delete":
        res = _ok(v.delete(f"/api/downloads/{Path(args.name).name}"))
        info("deleted" if res.get("success") else
             res.get("error", "not found"))
    elif act == "clear":
        confirm = input("  delete ALL files in downloads? [y/N] ").strip()
        if confirm.lower() != "y":
            info("cancelled")
            return 0
        res = _ok(v.delete("/api/downloads"))
        info(f"deleted {res.get('deleted', 0)} file(s)")
    return 0


def cmd_status(v: Veridian, args) -> int:
    st = v.auth_status()
    who = (f"signed in as {st.get('username')} "
           f"({'owner' if st.get('is_owner') else 'user'})"
           if st.get("authenticated")
           else "single-user mode" if not st.get("multiuser")
           else "not signed in")
    print(C.c(C.BOLD, "VeridianAI") + C.c(C.DIM, f"  ·  {v.base}"))
    info(f"backend: up · {who}")
    try:
        tiers = _ok(v.get("/api/tiers")).get("tiers", {})
        up = [n for n, s in tiers.items()
              if (s or {}).get("running") or (s or {}).get("up")
              or (s or {}).get("alive")]
        friendly = [tier_display(n) for n in up]
        info(f"tiers: {len(up)}/{len(tiers)} up "
             f"({', '.join(friendly) if friendly else 'none'})")
    except SystemExit:
        pass
    try:
        models = v.models()
        info(f"models visible: {len(models)}")
    except SystemExit:
        pass
    return 0


def cmd_abort(v: Veridian, args) -> int:
    v.abort()
    info("abort signal sent")
    return 0


# --------------------------------------------------------------------------
# Tier-3: live dashboard
# --------------------------------------------------------------------------
def daemon_status(host: str = "127.0.0.1", port: int = 9998,
                  timeout: float = 3.0) -> dict | None:
    """Query sage_daemon's TCP status directly (8-ASCII-digit length prefix
    + JSON — the sage_daemon_client protocol). Returns None when the daemon
    is unreachable. The socket auth token is best-effort: the daemon only
    requires it when handoff_security.require_socket_auth is enabled."""
    import socket
    req: dict = {"action": "status"}
    try:  # best-effort token from sage_data (sibling of the project folder)
        tok_file = Path(__file__).resolve().parents[2] / "sage_data" / ".socket_token"
        if tok_file.is_file():
            req["auth_token"] = tok_file.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    try:
        body = json.dumps(req, separators=(",", ":")).encode("utf-8")
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(f"{len(body):08d}".encode() + body)
            header = b""
            while len(header) < 8:
                chunk = s.recv(8 - len(header))
                if not chunk:
                    return None
                header += chunk
            length = int(header.decode("utf-8"))
            data = b""
            while len(data) < length:
                chunk = s.recv(min(65536, length - len(data)))
                if not chunk:
                    return None
                data += chunk
        return json.loads(data.decode("utf-8"))
    except (OSError, ValueError):
        return None


def _dash_frame(v: Veridian, daemon_port: int) -> str:
    """Compose one dashboard frame as a string (testable, render-agnostic)."""
    lines: list = []
    add = lines.append
    add(C.c(C.BOLD, "VeridianAI Dashboard")
        + C.c(C.DIM, f"  ·  {v.base}  ·  {datetime.now().strftime('%H:%M:%S')}"))
    add(C.c(C.DIM, "─" * 62))

    # -- tiers ---------------------------------------------------------
    add(C.c(C.BOLD, " Tiers"))
    try:
        tiers = _ok(v.get("/api/tiers", timeout=8)).get("tiers", {})
    except SystemExit:      # _ok exits on HTTP error; stay alive in dash
        tiers = {}
    if tiers:
        for name, snap in tiers.items():
            snap = snap or {}
            up = snap.get("running", snap.get("up", snap.get("alive")))
            mark = (C.c(C.GREEN, "up  ") if up
                    else C.c(C.RED, "down") if up is not None
                    else C.c(C.DIM, "?   "))
            extra = ", ".join(f"{k}={snap[k]}" for k in
                              ("port", "ctx_size", "model") if snap.get(k))
            add(f"   {mark} {tier_display(name):<8} [{name}]"
                + (C.c(C.DIM, f"  {extra}") if extra else ""))
    else:
        add(C.c(C.DIM, "   (no tier data)"))

    # -- models --------------------------------------------------------
    try:
        n_models = len(v.models())
        add(C.c(C.BOLD, " Models ") + C.c(C.DIM, f"{n_models} visible"))
    except SystemExit:
        add(C.c(C.BOLD, " Models ") + C.c(C.RED, "unavailable"))

    # -- socials / Argo-Net mesh (a Socials channel, not a tier) -------
    try:
        soc = _ok(v.get("/api/socials/status", timeout=8))
    except SystemExit:
        soc = {}
    chans = (soc.get("channels") or {}) if soc.get("available") else {}
    argo = chans.get("argonet")
    if argo is not None:
        state = (C.c(C.GREEN, "connected") if argo.get("connected")
                 else C.c(C.DIM, "off (available)") if argo.get("available")
                 else C.c(C.YELLOW, argo.get("note") or "unavailable"))
        listening = " · listening" if argo.get("listening") else ""
        add(C.c(C.BOLD, " Argo-Net mesh ") + state + C.c(C.DIM, listening))
        if argo.get("error"):
            add(C.c(C.YELLOW, f"   last error: {str(argo['error'])[:56]}"))

    # -- mechanics daemon (chain health lives here) --------------------
    add(C.c(C.BOLD, " Mechanics daemon ") + C.c(C.DIM, f":{daemon_port}"))
    ds = daemon_status(port=daemon_port)
    if ds is None:
        add(C.c(C.RED, "   unreachable — is sage_daemon running?"))
    else:
        add(f"   running · uptime {_fmt_uptime(ds.get('uptime_seconds'))}"
            f" · chain {ds.get('entries', '?')} entries"
            f" ({ds.get('chain_head_preview', '?')})")
        pw = ds.get("periodic_worker") or {}
        verify_ok = pw.get("last_verify_ok")
        verify = (C.c(C.GREEN, "verify OK") if verify_ok
                  else C.c(C.RED, "VERIFY FAILED") if verify_ok is False
                  else C.c(C.DIM, "verify pending"))
        anomaly = (C.c(C.RED + C.BOLD,
                       f"ANOMALY ALERT since {pw.get('anomaly_first_ts')}")
                   if pw.get("anomaly_alert")
                   else C.c(C.GREEN, "no anomalies"))
        add(f"   {verify} · {anomaly} · ticks {pw.get('ticks_run', '?')}")
        if pw.get("last_digest_msg"):
            add(C.c(C.DIM, f"   digest: {str(pw['last_digest_msg'])[:56]}"))

    add(C.c(C.DIM, "─" * 62))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# TUI: pinned dashboard header + chat in an ANSI scroll region below.
# Built for small screens (Todd's laptop): one terminal, both views.
# No dependencies — DECSTBM scroll margins + cursor save/restore, which
# Windows Terminal and conhost (with VT enabled) both support.
# --------------------------------------------------------------------------
TUI_HEADER_H = 5   # 4 content lines + separator


def _tui_header_lines(v: Veridian, width: int, daemon_port: int) -> list:
    """Compose the compact header (testable; no cursor movement here)."""
    lines: list = []

    # 1: title + clock
    lines.append(C.c(C.BOLD, "VeridianAI")
                 + C.c(C.DIM, f" · {v.base} · "
                       f"{datetime.now().strftime('%H:%M:%S')}"))

    # 2: tiers, compact
    try:
        tiers = _ok(v.get("/api/tiers", timeout=6)).get("tiers", {})
    except SystemExit:
        tiers = {}
    if tiers:
        bits = []
        for name, snap in tiers.items():
            snap = snap or {}
            up = snap.get("running", snap.get("up", snap.get("alive")))
            dot = C.c(C.GREEN, "●") if up else C.c(C.RED, "●")
            bits.append(f"{dot} {tier_display(name)}")
        lines.append(" " + "   ".join(bits))
    else:
        lines.append(C.c(C.DIM, " tiers: unavailable"))

    # 3: Argo-Net + model count
    argo_txt = ""
    try:
        soc = _ok(v.get("/api/socials/status", timeout=6))
        argo = ((soc.get("channels") or {}).get("argonet")
                if soc.get("available") else None)
        if argo is not None:
            argo_txt = ("Argo-Net "
                        + (C.c(C.GREEN, "connected") if argo.get("connected")
                           else C.c(C.DIM, "off")))
    except SystemExit:
        pass
    try:
        n_models = str(len(v.models()))
    except SystemExit:
        n_models = "?"
    lines.append(" " + (argo_txt + "   " if argo_txt else "")
                 + C.c(C.DIM, f"models: {n_models}"))

    # 4: memory chain (the Thursday panel)
    ds = daemon_status(port=daemon_port, timeout=2.0)
    if ds is None:
        lines.append(C.c(C.DIM, " chain: daemon unreachable"))
    else:
        pw = ds.get("periodic_worker") or {}
        verify_ok = pw.get("last_verify_ok")
        state = (C.c(C.RED + C.BOLD, "ANOMALY ALERT")
                 if pw.get("anomaly_alert")
                 else C.c(C.GREEN, "verified") if verify_ok
                 else C.c(C.RED, "VERIFY FAILED") if verify_ok is False
                 else C.c(C.DIM, "verify pending"))
        lines.append(f" chain: {ds.get('entries', '?')} entries · {state}"
                     + C.c(C.DIM,
                           f" · up {_fmt_uptime(ds.get('uptime_seconds'))}"))

    # 5: separator
    lines.append(C.c(C.DIM, "─" * max(20, width - 1)))
    return lines[:TUI_HEADER_H]


def _tui_paint_header(v: Veridian, daemon_port: int) -> None:
    width = shutil.get_terminal_size((80, 24)).columns
    lines = _tui_header_lines(v, width, daemon_port)
    with PRINT_LOCK:
        out = ["\x1b7"]                              # save cursor
        for i, ln in enumerate(lines, start=1):
            out.append(f"\x1b[{i};1H\x1b[2K{ln}")    # move, clear, draw
        out.append("\x1b8")                          # restore cursor
        print("".join(out), end="", flush=True)


def cmd_tui(v: Veridian, args) -> int:
    if not C.enabled:
        warn("tui needs an ANSI terminal — falling back to plain chat")
        return _chat_loop(v, args)
    rows = shutil.get_terminal_size((80, 24)).lines
    if rows < TUI_HEADER_H + 6:
        warn("terminal too short for the TUI — falling back to plain chat")
        return _chat_loop(v, args)

    interval = max(2.0, float(getattr(args, "interval", 5.0) or 5.0))
    daemon_port = int(getattr(args, "daemon_port", 9998) or 9998)
    stop = threading.Event()

    def _refresher():
        while not stop.is_set():
            try:
                _tui_paint_header(v, daemon_port)
            except Exception:
                pass                      # a paint glitch must never kill chat
            stop.wait(interval)

    # Clear screen, pin the header, confine chat to a scroll region below.
    print(f"\x1b[2J\x1b[{TUI_HEADER_H + 1};{rows}r"
          f"\x1b[{TUI_HEADER_H + 1};1H", end="", flush=True)
    t = threading.Thread(target=_refresher, daemon=True,
                         name="veridian-tui-header")
    t.start()
    try:
        return _chat_loop(v, args)
    finally:
        stop.set()
        # Release the scroll region and park the cursor at the bottom.
        print(f"\x1b[r\x1b[{rows};1H", flush=True)


def cmd_dash(v: Veridian, args) -> int:
    interval = max(1.0, float(getattr(args, "interval", 3.0) or 3.0))
    frames = getattr(args, "frames", None)   # None = run until Ctrl+C
    daemon_port = int(getattr(args, "daemon_port", 9998) or 9998)
    shown = 0
    try:
        while True:
            frame = _dash_frame(v, daemon_port)
            if frames is None and C.enabled:
                # Clear + home, repaint (plain mode just prints frames)
                print("\x1b[2J\x1b[H", end="")
            print(frame)
            if frames is None:
                print(C.c(C.DIM, f" refreshing every {interval:.0f}s — "
                          f"Ctrl+C to exit"))
            shown += 1
            if frames is not None and shown >= frames:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        return 0


# --------------------------------------------------------------------------
# Entry
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="veridian-cli",
        description="VeridianAI terminal client (same backend, no GUI).")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("-u", "--user", default=None, help="username for login")
    ap.add_argument("--plain", action="store_true", help="disable colors")
    # tier-1 compatibility aliases (work without a subcommand)
    ap.add_argument("-m", "--model", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--once", metavar="PROMPT", default=None,
                    help=argparse.SUPPRESS)
    ap.add_argument("--list-models", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--temperature", type=float, default=None,
                    help=argparse.SUPPRESS)
    ap.add_argument("--max-tokens", type=int, default=None,
                    help=argparse.SUPPRESS)
    ap.add_argument("--resume", action="store_true", help=argparse.SUPPRESS)

    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("chat", help="interactive chat (default command)")
    p.add_argument("-m", "--model", default=None)
    p.add_argument("--once", metavar="PROMPT", default=None)
    p.add_argument("--resume", action="store_true",
                   help="start from the conversation the GUI has open")
    p.add_argument("--temperature", type=float, default=None)
    p.add_argument("--max-tokens", type=int, default=None)
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("models", help="list / load / unload / refresh")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "load", "unload", "refresh"])
    p.add_argument("id", nargs="?", default=None, help="model id")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("tiers", help="tier status / restart")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "status", "restart"])
    p.add_argument("name", nargs="?", default=None,
                   help="tier name (e.g. sage, daemon)")
    p.set_defaults(func=cmd_tiers)

    p = sub.add_parser("archives", help="conversation archives")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "save", "load", "delete", "title"])
    p.add_argument("file", nargs="?", default=None, help="archive filename")
    p.add_argument("title", nargs="?", default=None, help="title text")
    p.set_defaults(func=cmd_archives)

    p = sub.add_parser("config", help="read / write settings")
    p.add_argument("action", nargs="?", default="get",
                   choices=["get", "set"])
    p.add_argument("key", nargs="?", default=None)
    p.add_argument("value", nargs="?", default=None)
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("downloads", help="Toga's downloads folder")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "get", "save", "delete", "clear"])
    p.add_argument("name", nargs="?", default=None,
                   help="filename (or local path for 'save')")
    p.add_argument("-o", "--out", default=None,
                   help="output path for 'get'")
    p.set_defaults(func=cmd_downloads)

    p = sub.add_parser("status", help="stack overview")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("tui", help="chat + pinned live dashboard in one "
                                   "terminal (small screens)")
    p.add_argument("-m", "--model", default=None)
    p.add_argument("--resume", action="store_true",
                   help="start from the conversation the GUI has open")
    p.add_argument("--interval", type=float, default=5.0,
                   help="header refresh seconds (default 5)")
    p.add_argument("--temperature", type=float, default=None)
    p.add_argument("--max-tokens", type=int, default=None)
    p.add_argument("--once", default=None, help=argparse.SUPPRESS)
    p.add_argument("--daemon-port", type=int, default=9998,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_tui)

    p = sub.add_parser("dash", help="live dashboard (tiers, models, "
                                    "memory-chain health)")
    p.add_argument("--interval", type=float, default=3.0,
                   help="refresh seconds (default 3)")
    p.add_argument("--frames", type=int, default=None,
                   help=argparse.SUPPRESS)  # render N frames then exit (tests)
    p.add_argument("--daemon-port", type=int, default=9998,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_dash)

    p = sub.add_parser("abort", help="abort an in-flight generation")
    p.set_defaults(func=cmd_abort)

    return ap


def _validate(args) -> str | None:
    """Cross-field checks argparse positionals can't express."""
    if args.cmd == "models" and args.action in ("load", "unload") \
            and not args.id:
        return "models %s requires a model id" % args.action
    if args.cmd == "tiers" and args.action in ("status", "restart") \
            and not args.name:
        return "tiers %s requires a tier name" % args.action
    if args.cmd == "archives" and args.action in ("load", "delete", "title") \
            and not args.file:
        return "archives %s requires a filename" % args.action
    if args.cmd == "config" and args.action == "set" \
            and (not args.key or args.value is None):
        return "config set requires KEY and VALUE"
    if args.cmd == "downloads" and args.action in ("get", "save", "delete") \
            and not args.name:
        return "downloads %s requires a filename" % args.action
    return None


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()

    _enable_windows_ansi()
    if args.plain or not sys.stdout.isatty():
        C.enabled = False

    problem = _validate(args)
    if problem:
        err(problem)
        return 2

    v = Veridian(args.host, args.port)
    if not v.alive():
        err(f"no VeridianAI backend at {v.base} — is the stack running? "
            f"(start.bat, or VeridianAI.exe)")
        return 1

    # Auth (no-op in single-user mode).
    try:
        st = v.auth_status()
    except requests.RequestException as e:
        err(f"auth status check failed: {e}")
        return 1
    if st.get("needs_setup"):
        err("no owner account exists yet — run first-time setup in the UI, "
            "then come back.")
        return 2
    if st.get("multiuser") and not st.get("authenticated"):
        print(C.c(C.BOLD, "VeridianAI sign-in"))
        user = v.login_interactive(args.user)
        info(f"signed in as {user}")

    # tier-1 compatibility: bare flags behave like before
    if args.cmd is None:
        if args.list_models:
            print_models(v.models(), args.model)
            return 0
        args.cmd = "chat"
        args.func = cmd_chat

    if args.cmd == "chat":
        print(C.c(C.BOLD, "VeridianAI") + C.c(C.DIM, f"  ·  {v.base}"))
    return args.func(v, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
