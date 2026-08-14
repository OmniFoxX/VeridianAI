"""
VeridianAI / Aether -- relay client + source.

RelayClient: submit a request for a target peer, then await the brokered response.
RelaySource: poll the relay for requests addressed to me, dispatch each to a local
async handler, and post the response.

httpx clients are INJECTED per call (submit/await_response/serve_once take a
client), so the logic is transport-agnostic and unit-testable against an ASGI
transport; the convenience wrappers (.request/.run) create a real client.

SSRF note -- CodeQL py/full-ssrf, formerly flagged at lines 22 and 32. The relay URL
reaches RelayClient from an owner-gated request body (skill_api browse/fetch). The
ADDRESS POLICY deliberately does not live in this module: the same classes also serve
LAN and loopback relays from owner config (main.py's source-loop) and an ASGI stub in
the tests, where a public-address-only rule would be wrong. Instead the CALLER
validates and PINS. net_guard.pinned_base() returns a base URL addressing the
already-validated IP, plus the Host header and SNI hostname needed to keep virtual
hosting and certificate verification intact; skill_api passes those in via
host_header/sni_hostname. The name is therefore never re-resolved between the check
and the request, which closes the DNS-rebinding TOCTOU that the direct-peer path had
already closed via _pinned_get. A caller that passes a bare URL and no pin behaves
exactly as it did before.

Path segments that originate OUTSIDE this process -- request_id, which the relay
itself returns, and peer_id from config -- are percent-encoded, so a hostile or
compromised relay cannot steer the request path with a slash, query or fragment.
"""
import asyncio
from urllib.parse import quote

import httpx


class _RelayEndpoint:
    """Shared URL construction: optional IP pin, with Host/SNI preserved."""

    def __init__(self, relay_url, host_header=None, sni_hostname=None):
        self.relay = relay_url.rstrip("/")
        self.host_header = host_header      # original netloc when relay is an IP pin
        self.sni_hostname = sni_hostname    # https only; None for http

    def _send(self, client, method, path, **kw):
        headers = {"Host": self.host_header} if self.host_header else None
        req = client.build_request(method, self.relay + path, headers=headers, **kw)
        if self.sni_hostname:
            ext = dict(req.extensions)
            ext["sni_hostname"] = self.sni_hostname
            req.extensions = ext
        return client.send(req)


class RelayClient(_RelayEndpoint):
    async def submit(self, client, target_peer, payload):
        r = await self._send(client, "POST", "/api/relay/request",
                             json={"target": target_peer, "payload": payload})
        if r.status_code != 200:
            return None
        return r.json().get("request_id")

    async def await_response(self, client, request_id, timeout=30.0, poll_interval=0.5):
        rid = quote(str(request_id), safe="")
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            r = await self._send(client, "GET", "/api/relay/response/" + rid)
            d = r.json() if r.status_code == 200 else {}
            if d.get("ready"):
                return {"ok": True, "response": d.get("response")}
            await asyncio.sleep(poll_interval)
        return {"ok": False, "reason": "relay timeout"}

    async def request(self, target_peer, payload, timeout=30.0, poll_interval=0.5):
        async with httpx.AsyncClient(timeout=15.0) as client:
            rid = await self.submit(client, target_peer, payload)
            if not rid:
                return {"ok": False, "reason": "relay submit failed"}
            return await self.await_response(client, rid, timeout, poll_interval)


class RelaySource(_RelayEndpoint):
    def __init__(self, relay_url, peer_id, handler, host_header=None, sni_hostname=None):
        super().__init__(relay_url, host_header, sni_hostname)
        self.peer_id = peer_id
        self.handler = handler          # async fn(payload) -> response
        self._running = False

    async def serve_once(self, client):
        r = await self._send(client, "GET",
                             "/api/relay/poll/" + quote(str(self.peer_id), safe=""))
        req = r.json() if r.status_code == 200 else {}
        if not req or "id" not in req:
            return False
        try:
            resp = await self.handler(req.get("payload"))
        except Exception as e:
            resp = {"error": str(e)}
        await self._send(client, "POST", "/api/relay/respond",
                         json={"request_id": req["id"], "response": resp})
        return True

    async def run(self, poll_interval=0.5):
        self._running = True
        async with httpx.AsyncClient(timeout=15.0) as client:
            while self._running:
                try:
                    got = await self.serve_once(client)
                except Exception:
                    got = False
                if not got:
                    await asyncio.sleep(poll_interval)

    def stop(self):
        self._running = False
