"""
ipc_monitor.py — live terminal + browser feed of Sage's browser activity.
=========================================================================

Standalone monitor that listens on the OracleAI IPC bridge (port 9999) and
displays every browser event Sage emits in real time:

  * Terminal feed:   one line per event, colour-coded (when a terminal
                     supports ANSI), useful when you're already running
                     OracleAI from a console.
  * Web page:        a small HTML dashboard at http://localhost:9997/
                     that auto-refreshes via /events.json polling, useful
                     when you want to watch from another window.

The monitor is fully self-contained — it does NOT import sage_engine or
browser_tool. It speaks only the IPC bridge protocol from ipc_bridge.py
and HTTP. Run while OracleAI is up:

    cd E:\\OracleAI_v2.1.5\\backend
    py ipc_monitor.py

Stop with Ctrl+C. Safe to start/stop independently of OracleAI.

v2.1.5 — wires up Todd's "watch Sage browse" requirement that the IPC
bridge has been waiting for since the browser_tool.py swap.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

# Reuse the project's bridge — same port, same wire format
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ipc_bridge import start_ipc_server, PORT as IPC_PORT  # noqa: E402
from time_manager import TimeManager  # v2.1.6 unified time source

WEB_PORT = 9997
MAX_EVENTS = 200

_events: "deque[dict]" = deque(maxlen=MAX_EVENTS)
_events_lock = Lock()


# ---------- terminal output ---------------------------------------- #
def _ansi(code: str) -> str:
    return f"\033[{code}m" if sys.stdout.isatty() else ""


_COLOURS = {
    "navigate":         _ansi("36"),   # cyan
    "navigate_done":    _ansi("32"),   # green
    "click":            _ansi("33"),   # yellow
    "fill":             _ansi("33"),   # yellow
    "search":           _ansi("35"),   # magenta
    "search_results":   _ansi("32"),   # green
    "captcha_detected": _ansi("31"),   # red
    "captcha_solved":   _ansi("32"),   # green
    "error":            _ansi("31;1"), # bold red
}
_RESET = _ansi("0")


def _format_event(msg: dict) -> str:
    """Render an IPC message as a one-line terminal entry."""
    payload = msg.get("payload") or msg
    if not isinstance(payload, dict):
        payload = {"raw": payload}
    event = payload.get("event") or msg.get("action") or "event"
    ts = payload.get("ts") or TimeManager.epoch()
    when = TimeManager.local_display(
        when=_dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc),
        fmt="%H:%M:%S",
    )
    colour = _COLOURS.get(event, "")

    # Build a compact summary based on event type
    bits = []
    if event in ("navigate", "navigate_done"):
        bits.append(payload.get("url", ""))
        if payload.get("title"):
            bits.append(f"({payload['title'][:60]})")
    elif event == "click":
        bits.append(payload.get("selector", ""))
    elif event == "fill":
        bits.append(payload.get("selector", ""))
        bits.append(f"len={payload.get('len', 0)}")
    elif event == "search":
        bits.append(repr(payload.get("query", "")))
    elif event == "search_results":
        bits.append(f"count={payload.get('count', 0)}")
        bits.append(f"q={payload.get('query', '')!r}")
    elif event in ("captcha_detected", "captcha_solved"):
        bits.append(payload.get("url", ""))
    elif event == "error":
        bits.append(payload.get("where", ""))
        bits.append(payload.get("message", "")[:120])
    else:
        # Unknown event — dump key=value pairs (skipping noise)
        for k, v in payload.items():
            if k in ("event", "ts"):
                continue
            bits.append(f"{k}={str(v)[:60]}")

    return f"{when} {colour}[{event:<17}]{_RESET} {' '.join(bits)}"


def _on_message(msg: dict) -> None:
    """Handler invoked by ipc_bridge.start_ipc_server for each event."""
    payload = msg.get("payload") or {}
    if isinstance(payload, dict) and "ts" not in payload:
        payload["ts"] = TimeManager.epoch()
    record = {
        "action": msg.get("action"),
        "payload": payload if isinstance(payload, dict) else {"raw": payload},
    }
    with _events_lock:
        _events.append(record)
    # Always print to stdout — the terminal feed is the primary surface.
    line = _format_event(record)
    print(line, flush=True)


# ---------- HTTP handler for the optional web view ----------------- #
_HTML = (
    "<!doctype html><html><head><meta charset=utf-8>"
    "<title>OracleAI — Sage Browser Live Feed</title>"
    "<style>"
    "body{font-family:ui-monospace,Menlo,Consolas,monospace;"
    "background:#0d0d12;color:#d6d6d6;margin:0;padding:1rem}"
    "h1{font-size:1.1rem;color:#9ad}"
    "#feed{font-size:.85rem;line-height:1.4em}"
    ".row{padding:.15rem 0;border-bottom:1px solid #222}"
    ".event{display:inline-block;width:13rem;font-weight:600}"
    ".navigate,.navigate_done{color:#5cf}"
    ".click,.fill{color:#ffcc4d}"
    ".search,.search_results{color:#d49aff}"
    ".captcha_detected,.error{color:#ff5c5c}"
    ".captcha_solved{color:#7afa9b}"
    ".muted{color:#666}"
    "</style></head><body>"
    "<h1>OracleAI — Sage Browser Live Feed "
    "<span class=muted>(IPC :{ipc} | UI :{web})</span></h1>"
    "<div id=feed></div>"
    "<script>"
    "const feed=document.getElementById('feed');"
    "let lastLen=0;"
    "async function poll(){"
    "  try{const r=await fetch('/events.json');"
    "  const d=await r.json();"
    "  if(d.events.length===lastLen)return;"
    "  lastLen=d.events.length;"
    "  feed.innerHTML=d.events.slice().reverse().map(e=>{"
    "    const p=e.payload||{};"
    "    const ev=p.event||e.action||'event';"
    "    const ts=new Date((p.ts||0)*1000)"
    "      .toTimeString().slice(0,8);"
    "    const bits=[];"
    "    if(ev==='navigate'||ev==='navigate_done'){"
    "      bits.push(p.url||'');if(p.title)bits.push('('+p.title+')');"
    "    }else if(ev==='click'||ev==='fill'){"
    "      bits.push(p.selector||'');"
    "      if(ev==='fill')bits.push('len='+(p.len||0));"
    "    }else if(ev==='search'){bits.push('\"'+(p.query||'')+'\"');"
    "    }else if(ev==='search_results'){bits.push('count='+(p.count||0));"
    "      bits.push('q=\"'+(p.query||'')+'\"');"
    "    }else if(ev==='captcha_detected'||ev==='captcha_solved'){"
    "      bits.push(p.url||'');"
    "    }else if(ev==='error'){"
    "      bits.push(p.where||'');bits.push(p.message||'');"
    "    }else{"
    "      Object.keys(p).forEach(k=>{"
    "        if(k==='event'||k==='ts')return;"
    "        bits.push(k+'='+String(p[k]).slice(0,60));"
    "      });"
    "    }"
    "    return '<div class=row>'+ts+' <span class=\"event '+ev+'\">'+"
    "      '['+ev+']</span> '+bits.join(' ')+'</div>';"
    "  }).join('');"
    "  }catch(e){}"
    "}"
    "setInterval(poll,800);poll();"
    "</script></body></html>"
)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003 — silence default
        return  # don't pollute the terminal feed

    def do_GET(self):  # noqa: N802 — http.server convention
        if self.path == "/events.json":
            with _events_lock:
                events = list(_events)
            body = json.dumps({"events": events}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in ("/", "/index.html"):
            html = _HTML.replace("{ipc}", str(IPC_PORT)).replace("{web}", str(WEB_PORT))
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def main():
    # Start IPC listener in background — fires _on_message per event
    print(
        f"[ipc_monitor] listening on IPC port {IPC_PORT} "
        f"(forwarding to web UI on :{WEB_PORT})"
    )
    print(
        "[ipc_monitor] open http://localhost:"
        f"{WEB_PORT}/ to watch in a browser, "
        "or follow this terminal."
    )
    start_ipc_server(_on_message)

    # Start HTTP server — this blocks
    try:
        web = ThreadingHTTPServer(("127.0.0.1", WEB_PORT), _Handler)
    except OSError as e:
        print(f"[ipc_monitor] web server failed to bind {WEB_PORT}: {e}")
        print("[ipc_monitor] falling back to terminal-only mode "
              "(IPC listener still active).")
        # Block forever on a dummy wait — IPC thread is daemon
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            return
        return
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        print("\n[ipc_monitor] shutting down.")
        web.server_close()


if __name__ == "__main__":
    main()
