"""
VeridianAI / Aether -- relay client + source.

RelayClient: submit a request for a target peer, then await the brokered response.
RelaySource: poll the relay for requests addressed to me, dispatch each to a local
async handler, and post the response.

httpx clients are INJECTED per call (submit/await_response/serve_once take a
client), so the logic is transport-agnostic and unit-testable against an ASGI
transport; the convenience wrappers (.request/.run) create a real client.
"""
import asyncio

import httpx


class RelayClient:
    def __init__(self, relay_url):
        self.relay = relay_url.rstrip("/")

    async def submit(self, client, target_peer, payload):
        r = await client.post(self.relay + "/api/relay/request",
                              json={"target": target_peer, "payload": payload})
        if r.status_code != 200:
            return None
        return r.json().get("request_id")

    async def await_response(self, client, request_id, timeout=30.0, poll_interval=0.5):
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            r = await client.get(self.relay + "/api/relay/response/" + request_id)
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


class RelaySource:
    def __init__(self, relay_url, peer_id, handler):
        self.relay = relay_url.rstrip("/")
        self.peer_id = peer_id
        self.handler = handler          # async fn(payload) -> response
        self._running = False

    async def serve_once(self, client):
        r = await client.get(self.relay + "/api/relay/poll/" + self.peer_id)
        req = r.json() if r.status_code == 200 else {}
        if not req or "id" not in req:
            return False
        try:
            resp = await self.handler(req.get("payload"))
        except Exception as e:
            resp = {"error": str(e)}
        await client.post(self.relay + "/api/relay/respond",
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
