"""OracleAI / Aether -- relay broker CORE (pure, thread-steady, deterministic). 
A mailbox/queue rendezvous: a CLIENT submits a request addressed to a target peer; the SOURCE peer (dialed outbound to the relay) pulls its pending requests, serves them locally, and posts responses; the CLIENT collects the response by id. The relay holds only what it forwards -- skill content is signed end-to-end, so the broker stays untrusted. TTLs bound memory; nothing here touches the network. 
"""
from collections import defaultdict, deque
import secrets
import time
import asyncio

class RelayHub:
    def __init__(self, request_ttl=120.0, response_ttl=120.0,
                 max_pending_per_peer=256, now_fn=None):
        self.request_ttl = float(request_ttl)
        self.response_ttl = float(response_ttl)
        self.max_pending = int(max_pending_per_peer)
        self._now = now_fn or time.monotonic
        self._inbox = defaultdict(deque)   # O(1) append and popleft
        self._responses = {}
        self._lock = asyncio.Lock()

    async def submit_request(self, target_peer, payload):
        """CLIENT queues a request for target_peer. Returns request_id, or None if that peer's inbox is full."""
        rid = secrets.token_urlsafe(12)
        now = self._now()
        async with self._lock:
            self._prune(now)
            q = self._inbox[target_peer]  # defaultdict creates a new list if needed
            if len(q) >= self.max_pending:
                return None
            q.append({"id": rid, "payload": payload, "expiry": now + self.request_ttl})
        return rid

    async def next_request(self, peer_id):
        """SOURCE pulls its oldest non-expired pending request, or None."""
        now = self._now()
        async with self._lock:
            self._prune(now)
            q = self._inbox.get(peer_id)
            while q:
                req = q.popleft()  # O(1) - correct and clean
                if req["expiry"] >= now:
                    return {"id": req["id"], "payload": req["payload"]}
            return None

    async def submit_response(self, request_id, response):
        """SOURCE posts the response for a request_id."""
        if not request_id:
            return False
        now = self._now()
        async with self._lock:
            self._prune(now)
            self._responses[request_id] = {"response": response,
                                            "expiry": now + self.response_ttl}
        return True

    async def get_response(self, request_id):
        """CLIENT polls; returns {ready, response}. Consumed on first ready read."""
        now = self._now()
        async with self._lock:
            self._prune(now)
            r = self._responses.pop(request_id, None)
            if r is None:
                return {"ready": False, "response": None}
            return {"ready": True, "response": r["response"]}

    def _prune(self, now):
        # Remove expired responses
        for rid in list(self._responses.keys()):
            if self._responses[rid]["expiry"] < now:
                self._responses.pop(rid, None)
"""