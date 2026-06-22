"""
OracleAI / Aether -- skill-share HTTP surface (Layers 4-5 wiring).

An APIRouter exposing the SkillService over HTTP. The whole feature is gated by
config.skill_share_enabled (OFF by default).

  SERVE endpoints  -> GET /api/skills/catalog, GET /api/skills/object/{hash}
      peer-facing: return ONLY promoted, signed skills. main.py adds these two
      paths to the session-gate allowlist so overlay peers can read without a
      login. They reveal nothing and serve nothing when the flag is off.

  MANAGE endpoints -> /api/skills/local, /publish, /browse, /fetch, /promote,
      /reject, /identity, /trusted (+ /trusted/remove)
      owner actions; they stay BEHIND the login session gate (NOT allowlisted).

Promotion enforces the L5 trusted-key store: a fetched skill can only be promoted
once its author's public key has been imported (verified out-of-band by
fingerprint). Signatures are always verified regardless.

Wire in main.py:
    from skill_api import skill_router, set_config
    app.include_router(skill_router); set_config(config)
"""
import json
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request

import skill_keys
from wan_guard import AbuseGuard

skill_router = APIRouter(prefix="/api/skills", tags=["skills"])
_rl = AbuseGuard()  # WAN abuse guard for the exposed serve endpoints

_config = None
_service = None
_service_err = None
_key_dir = None  # tests override sage_data location


def set_config(cfg):
    global _config
    _config = cfg


def set_key_dir(d):
    global _key_dir
    _key_dir = d


def _enabled():
    try:
        return bool(_config.get("skill_share_enabled", False)) if _config else False
    except Exception:
        return False


def _svc():
    global _service, _service_err
    if _service is None and _service_err is None:
        try:
            from skill_service import SkillService
            import skill_gate
            # author trust is enforced now that out-of-band key import exists (L5);
            # signatures are always verified.
            _service = SkillService(key_dir=_key_dir, policy=skill_gate.default_policy(
                require_trusted_author=True))
        except Exception as e:
            _service_err = str(e)
    return _service


def _trusted():
    """Trusted author pubkeys (b64) from the L5 key store."""
    try:
        return skill_keys.trusted_pubkeys(_key_dir)
    except Exception:
        return []


def _self_fingerprint():
    try:
        return skill_keys.self_identity(_key_dir)["fingerprint"]
    except Exception:
        return ""


def _guard():
    if not _enabled():
        raise HTTPException(404, "skill sharing is disabled")
    s = _svc()
    if s is None:
        raise HTTPException(503, "skill service unavailable: %s" % (_service_err or "init failed"))
    return s


def _ratelimit(request):
    ip = (request.client.host if request and request.client else "?")
    rl = _rl.check(ip)
    if not rl["allowed"]:
        raise HTTPException(429, "rate limited; retry in %ss" % rl["retry_after"])


# ---------------- SERVE (peer-facing; allowlisted; flag-gated) ----------------
@skill_router.get("/catalog")
async def skills_catalog(request: Request):
    _ratelimit(request)
    s = _guard()
    return {"skills": s.local_catalog(), "fingerprint": _self_fingerprint()}


@skill_router.get("/object/{hid}")
async def skills_object(hid: str, request: Request):
    _ratelimit(request)
    s = _guard()
    obj = s.get_shareable(hid)
    if obj is None:
        raise HTTPException(404, "not found or not shared")
    return obj


# ---------------- MANAGE (owner; behind session gate; flag-gated) -------------
@skill_router.get("/local")
async def skills_local():
    s = _guard()
    return {"skills": s.store.list(), "stats": s.store.stats(),
            "fingerprint": _self_fingerprint()}


@skill_router.post("/publish")
async def skills_publish(payload: dict):
    s = _guard()
    name = (payload.get("name") or "").strip()
    body = payload.get("body")
    if not name or body is None:
        raise HTTPException(400, "name and body required")
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=True)
    return s.publish(body, name=name, version=payload.get("version", ""),
                     capabilities=payload.get("capabilities") or [],
                     author=payload.get("author", ""))


@skill_router.post("/browse")
async def skills_browse(payload: dict):
    s = _guard()
    relay = (payload.get("relay") or "").strip().rstrip("/")
    target = (payload.get("target") or "").strip()
    if relay and target:
        from relay_client import RelayClient
        res = await RelayClient(relay).request(target, {"path": "catalog"}, timeout=30.0)
        if not res.get("ok"):
            return {"ok": False, "reason": res.get("reason", "relay failed"), "items": []}
        data = res.get("response") or {}
        remote = data.get("skills", []) if isinstance(data, dict) else []
        return s.browse(lambda: remote, trusted_pubkeys=_trusted())
    base = (payload.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise HTTPException(400, "base_url required")
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(base + "/api/skills/catalog")
            if r.status_code != 200:
                raise HTTPException(502, "peer returned %d" % r.status_code)
            data = r.json()
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "reason": "browse error: %s" % e, "items": []}
    remote = data.get("skills", []) if isinstance(data, dict) else []
    return s.browse(lambda: remote, trusted_pubkeys=_trusted())


@skill_router.post("/fetch")
async def skills_fetch(payload: dict):
    s = _guard()
    hid = (payload.get("id") or "").strip()
    relay = (payload.get("relay") or "").strip().rstrip("/")
    target = (payload.get("target") or "").strip()
    if relay and target and hid:
        from relay_client import RelayClient
        res = await RelayClient(relay).request(target, {"path": "object", "id": hid}, timeout=30.0)
        if not res.get("ok"):
            return {"ok": False, "reason": res.get("reason", "relay failed"), "verdict": None}
        obj = res.get("response") or {}
        return s.fetch_object(lambda h: obj, hid, source="relay:" + target, trusted_pubkeys=_trusted())
    base = (payload.get("base_url") or "").strip().rstrip("/")
    if not base or not hid:
        raise HTTPException(400, "base_url and id required")
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.get(base + "/api/skills/object/" + hid)
            if r.status_code != 200:
                raise HTTPException(502, "peer returned %d" % r.status_code)
            obj = r.json()
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "reason": "fetch error: %s" % e, "verdict": None}
    return s.fetch_object(lambda h: obj, hid, source=base, trusted_pubkeys=_trusted())


@skill_router.post("/promote")
async def skills_promote(payload: dict):
    s = _guard()
    hid = (payload.get("id") or "").strip()
    if not hid:
        raise HTTPException(400, "id required")
    ok, verdict = s.promote(hid, trusted_pubkeys=_trusted())
    return {"ok": ok, "verdict": verdict}


@skill_router.post("/reject")
async def skills_reject(payload: dict):
    s = _guard()
    hid = (payload.get("id") or "").strip()
    if not hid:
        raise HTTPException(400, "id required")
    return {"ok": s.store.reject(hid)}


@skill_router.post("/remove")
async def skills_remove(payload: dict):
    s = _guard()
    hid = (payload.get("id") or "").strip()
    if not hid:
        raise HTTPException(400, "id required")
    return {"ok": s.store.remove(hid)}   # reversible: objects move to removed/


# ---------------- IDENTITY + TRUSTED KEYS (owner; behind session gate) ----------
@skill_router.get("/identity")
async def skills_identity():
    _guard()
    return skill_keys.self_identity(_key_dir)


@skill_router.get("/trusted")
async def skills_trusted_list():
    _guard()
    return {"keys": skill_keys.list_keys(_key_dir)}


@skill_router.post("/trusted")
async def skills_trusted_add(payload: dict):
    _guard()
    pub = (payload.get("pubkey") or "").strip()
    if not pub:
        raise HTTPException(400, "pubkey required")
    return skill_keys.add_key(pub, label=payload.get("label", ""), key_dir=_key_dir)


@skill_router.post("/trusted/remove")
async def skills_trusted_remove(payload: dict):
    _guard()
    pub = (payload.get("pubkey") or "").strip()
    if not pub:
        raise HTTPException(400, "pubkey required")
    return skill_keys.remove_key(pub, key_dir=_key_dir)


# ---------------- OFFLINE BUNDLES (owner; behind session gate) -----------------
@skill_router.get("/export/{hid}")
async def skills_export(hid: str):
    s = _guard()
    b = s.export_bundle(hid)
    if b is None:
        raise HTTPException(404, "not found or not exportable")
    return b


@skill_router.post("/import")
async def skills_import(payload: dict):
    s = _guard()
    bundle = payload.get("bundle") if (isinstance(payload, dict) and "bundle" in payload) else payload
    return s.import_bundle(bundle, trusted_pubkeys=_trusted())


async def relay_skill_handler(payload):
    """Source side of the relay: serve a peer's relayed skill request from the
    LOCAL skill service. Dispatched by the relay source-loop. JSON-able dict out;
    serves only when sharing is enabled (catalog/object are signed + promoted)."""
    if not _enabled():
        return {"error": "skill sharing disabled"}
    s = _svc()
    if s is None:
        return {"error": "skill service unavailable"}
    if not isinstance(payload, dict):
        return {"error": "bad request"}
    path = payload.get("path")
    if path == "catalog":
        return {"skills": s.local_catalog(), "fingerprint": _self_fingerprint()}
    if path == "object":
        obj = s.get_shareable(str(payload.get("id", "")))
        return obj if obj is not None else {"error": "not found"}
    return {"error": "unknown path"}
