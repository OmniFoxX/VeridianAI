"""
OracleAI / Aether -- relay broker HTTP surface. Rendezvous for peers that can't
reach each other directly (CGNAT-to-CGNAT, or the reverse direction). Brokers
opaque request/response between a CLIENT and a registered SOURCE. OFF by default
behind relay_server_enabled; rate-limited (internet-exposed). The relay is
untrusted -- skill payloads are signed end-to-end.

Wire in main.py:
    from relay_api import relay_router, set_config as set_relay_config
    app.include_router(relay_router); set_relay_config(config)
"""
from fastapi import APIRouter, HTTPException, Request

from relay_core import RelayHub
from wan_guard import AbuseGuard

relay_router = APIRouter(prefix="/api/relay", tags=["relay"])
_hub = RelayHub()
_rl = AbuseGuard(max_requests=600, window_sec=60)  # headroom for source-loop polling
_config = None


def set_config(cfg):
    global _config
    _config = cfg


def _enabled():
    try:
        return bool(_config.get("relay_server_enabled", False)) if _config else False
    except Exception:
        return False


def _gate(request):
    if not _enabled():
        raise HTTPException(404, "relay disabled")
    ip = request.client.host if request and request.client else "?"
    if not _rl.check(ip)["allowed"]:
        raise HTTPException(429, "rate limited")


@relay_router.post("/request")
async def relay_request(payload: dict, request: Request):
    _gate(request)
    rid = _hub.submit_request(str(payload.get("target", "")), payload.get("payload"))
    if rid is None:
        raise HTTPException(429, "relay queue full")
    return {"request_id": rid}


@relay_router.get("/poll/{peer_id}")
async def relay_poll(peer_id: str, request: Request):
    _gate(request)
    return _hub.next_request(peer_id) or {}


@relay_router.post("/respond")
async def relay_respond(payload: dict, request: Request):
    _gate(request)
    _hub.submit_response(str(payload.get("request_id", "")), payload.get("response"))
    return {"ok": True}


@relay_router.get("/response/{request_id}")
async def relay_response(request_id: str, request: Request):
    _gate(request)
    return _hub.get_response(request_id)
