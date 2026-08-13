"""
VeridianAI / Aether -- skill-share HTTP surface (Layers 4-5 wiring).

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

import ipaddress
import socket
from urllib.parse import urlparse, quote

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
        raise HTTPException(503, "skill service unavailable")  # detail in _service_err (server-side), not leaked to client
    return s


def _safe_detail(exc, where=""):
    """Log the real exception server-side; return a generic client-safe message with a
    correlation ref (CodeQL py/stack-trace-exposure). Full error is in the server log."""
    import logging as _logging, uuid as _uuid
    _ref = _uuid.uuid4().hex[:8]
    try:
        _logging.getLogger("veridian").warning("[err %s] %s: %r", _ref, where or "op", exc)
    except Exception:
        pass
    return "internal error (ref %s)" % _ref


def _resolve_validated(url: str):
    """Parse + validate a URL and return (scheme, host, port, ip, netloc).

    Validates EVERY address `host` resolves to (getaddrinfo, not just gethostbyname's
    first record -- closes the multi-A-record bypass) and returns the exact IP so the
    caller can PIN it: the connection then uses the same address we validated, which
    closes the DNS-rebinding TOCTOU (the name can't re-resolve to an internal IP
    between the check and the request). Raises on any private/loopback/etc. address
    or a resolution failure."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "url must be http or https")
    host = parsed.hostname
    if not host:
        raise HTTPException(400, "url missing host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise HTTPException(400, "could not resolve host")
    ip_pin = None
    for info in infos:
        ip_s = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_s)
        except ValueError:
            continue
        # is_unspecified blocks 0.0.0.0 / ::; is_multicast blocks 224.0.0.0/4.
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise HTTPException(400, "target address not allowed")
        if ip_pin is None:
            ip_pin = ip_s
    if ip_pin is None:
        raise HTTPException(400, "could not resolve host")
    return parsed.scheme, host, port, ip_pin, parsed.netloc


def _validate_external_url(url: str) -> None:
    """SSRF guard for the relay path (which routes through RelayClient rather than the
    pinned GET below). Validates scheme + every resolved address."""
    _resolve_validated(url)


async def _pinned_get(client, base_url: str, path: str):
    """GET base_url+path but CONNECT to the pre-validated IP, preserving the Host
    header and (for https) SNI + certificate verification against the original
    hostname. httpx is handed an IP literal, so it never re-resolves the name -- the
    address we validated is exactly the address we talk to (DNS-rebinding safe).
    Verified against httpx 0.28 for http and https (self-signed SNI round-trip)."""
    scheme, host, port, ip, netloc = _resolve_validated(base_url)
    ip_host = ("[%s]" % ip) if ":" in ip else ip   # bracket IPv6 literals
    pinned_url = "%s://%s:%d%s" % (scheme, ip_host, port, path)
    req = client.build_request("GET", pinned_url, headers={"Host": netloc})
    if scheme == "https":
        ext = dict(req.extensions)
        ext["sni_hostname"] = host     # TLS SNI + cert check use the hostname, not the IP
        req.extensions = ext
    return await client.send(req)


def _ratelimit(request):
    ip = (request.client.host if request and request.client else "?")
    rl = _rl.check(ip)
    if not rl["allowed"]:
        raise HTTPException(429, "rate limited; retry in %ss" % rl["retry_after"])


def _owner_guard(request: Request):
    """v2.12.8 semgrep hardening: the MANAGE / IDENTITY+TRUSTED / BUNDLE
    endpoints are owner actions (this module's docstring always said so);
    now it's enforced, not just documented. Single-user mode (multiuser
    off) = owner by definition, so solo installs see zero change. In
    multi-user mode main.py's _session_gate middleware has already
    attached request.state.user for every non-allowlisted path, so a
    missing or non-owner session gets the uniform 404 cloak here --
    matching main.py's _require_owner / WAN-guard convention. The
    peer-facing SERVE endpoints (/catalog, /object) are intentionally
    NOT gated: they are allowlisted, rate-limited, and expose only
    promoted signed skills."""
    try:
        mu = bool(_config.get("multiuser_enabled", False)) if _config else False
    except Exception:
        mu = False
    if not mu:
        return
    import session as _session
    # v2.12.9 delegated admin: owner, or a profile the owner granted the
    # "skills" capability via Access Controls. owner_or_granted reads the
    # cookie directly (no middleware dependency) and is fail-closed -- a
    # broken policy store never mints admin powers.
    # v2.12.10 REGRESSION FIX: this guard referenced an undefined
    # _OWNER_COOKIE (NameError -> HTTP 500 on every manage/identity call
    # in multi-user mode). skills.js reads a failed /api/skills/identity
    # as "feature off", so the Aether toggle appeared to reset OFF after
    # every restart even though config.json had skill_share_enabled=true.
    if _session.owner_or_granted(request, _session.AUTH_COOKIE, "skills"):
        return
    raise HTTPException(404)


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
async def skills_local(request: Request):
    _owner_guard(request)  # v2.12.8 owner-only (semgrep)
    s = _guard()
    return {"skills": s.store.list(), "stats": s.store.stats(),
            "fingerprint": _self_fingerprint()}


@skill_router.post("/publish")
async def skills_publish(payload: dict, request: Request):
    _owner_guard(request)  # v2.12.8 owner-only (semgrep)
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
async def skills_browse(payload: dict, request: Request):
    _owner_guard(request)  # v2.12.8 owner-only (semgrep)
    s = _guard()
    relay = (payload.get("relay") or "").strip().rstrip("/")
    target = (payload.get("target") or "").strip()
    if relay and target:
        _validate_external_url(relay)
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
            # base validated AND the connection pinned to the validated IP by
            # _pinned_get -- one resolution for both, so DNS-rebinding safe. nosemgrep
            r = await _pinned_get(c, base, "/api/skills/catalog")
            if r.status_code != 200:
                raise HTTPException(502, "peer returned %d" % r.status_code)
            data = r.json()
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "reason": _safe_detail(e, "browse"), "items": []}
    remote = data.get("skills", []) if isinstance(data, dict) else []
    return s.browse(lambda: remote, trusted_pubkeys=_trusted())


@skill_router.post("/fetch")
async def skills_fetch(payload: dict, request: Request):
    _owner_guard(request)  # v2.12.8 owner-only (semgrep)
    s = _guard()
    hid = (payload.get("id") or "").strip()
    relay = (payload.get("relay") or "").strip().rstrip("/")
    target = (payload.get("target") or "").strip()
    if relay and target and hid:
        _validate_external_url(relay)
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
            # base validated + connection pinned to the validated IP by _pinned_get
            # (one resolution -> DNS-rebinding safe); hid URL-encoded; owner-only.
            # codeql[py/full-ssrf]
            r = await _pinned_get(c, base, "/api/skills/object/" + quote(hid, safe=""))  # nosemgrep
            if r.status_code != 200:
                raise HTTPException(502, "peer returned %d" % r.status_code)
            obj = r.json()
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "reason": _safe_detail(e, "fetch"), "verdict": None}
    return s.fetch_object(lambda h: obj, hid, source=base, trusted_pubkeys=_trusted())


@skill_router.post("/promote")
async def skills_promote(payload: dict, request: Request):
    _owner_guard(request)  # v2.12.8 owner-only (semgrep)
    s = _guard()
    hid = (payload.get("id") or "").strip()
    if not hid:
        raise HTTPException(400, "id required")
    ok, verdict = s.promote(hid, trusted_pubkeys=_trusted())
    return {"ok": ok, "verdict": verdict}


@skill_router.post("/reject")
async def skills_reject(payload: dict, request: Request):
    _owner_guard(request)  # v2.12.8 owner-only (semgrep)
    s = _guard()
    hid = (payload.get("id") or "").strip()
    if not hid:
        raise HTTPException(400, "id required")
    return {"ok": s.store.reject(hid)}


@skill_router.post("/remove")
async def skills_remove(payload: dict, request: Request):
    _owner_guard(request)  # v2.12.8 owner-only (semgrep)
    s = _guard()
    hid = (payload.get("id") or "").strip()
    if not hid:
        raise HTTPException(400, "id required")
    return {"ok": s.store.remove(hid)}   # reversible: objects move to removed/


# ---------------- IDENTITY + TRUSTED KEYS (owner; behind session gate) ----------
@skill_router.get("/identity")
async def skills_identity(request: Request):
    _owner_guard(request)  # v2.12.8 owner-only (semgrep)
    _guard()
    return skill_keys.self_identity(_key_dir)


@skill_router.get("/trusted")
async def skills_trusted_list(request: Request):
    _owner_guard(request)  # v2.12.8 owner-only (semgrep)
    _guard()
    return {"keys": skill_keys.list_keys(_key_dir)}


@skill_router.post("/trusted")
async def skills_trusted_add(payload: dict, request: Request):
    _owner_guard(request)  # v2.12.8 owner-only (semgrep)
    _guard()
    pub = (payload.get("pubkey") or "").strip()
    if not pub:
        raise HTTPException(400, "pubkey required")
    return skill_keys.add_key(pub, label=payload.get("label", ""), key_dir=_key_dir)


@skill_router.post("/trusted/remove")
async def skills_trusted_remove(payload: dict, request: Request):
    _owner_guard(request)  # v2.12.8 owner-only (semgrep)
    _guard()
    pub = (payload.get("pubkey") or "").strip()
    if not pub:
        raise HTTPException(400, "pubkey required")
    return skill_keys.remove_key(pub, key_dir=_key_dir)


# ---------------- OFFLINE BUNDLES (owner; behind session gate) -----------------
@skill_router.get("/export/{hid}")
async def skills_export(hid: str, request: Request):
    _owner_guard(request)  # v2.12.8 owner-only (semgrep)
    s = _guard()
    b = s.export_bundle(hid)
    if b is None:
        raise HTTPException(404, "not found or not exportable")
    return b


@skill_router.post("/import")
async def skills_import(payload: dict, request: Request):
    _owner_guard(request)  # v2.12.8 owner-only (semgrep)
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
   