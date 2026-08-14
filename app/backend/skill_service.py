"""
OracleAI / Aether -- skill SERVICE: serve + fetch (Layer 4).

Ties the trust core (L1), store (L2), and gate (L3) into the share workflow, in a
TRANSPORT-AGNOSTIC way. The serving side produces a small catalog + per-hash
objects; the fetching side pulls them through an injected `fetcher` callable
(real HTTP in main.py, a stub in tests). Pool vs person-to-person is just which
base URL / fetcher you point at -- the mechanism is identical.

Serve = only PROMOTED local skills (signed; you chose to share them; re-verified
on the way out). Fetch = lands QUARANTINED (untrusted by arrival) and returns the
L3 verdict; promotion stays a separate, gated, human step.
"""
import base64

import skill_trust
import skill_gate
from skill_store import SkillStore, STATE_PROMOTED, STATE_QUARANTINED

_PUBLIC_FIELDS = ("id", "name", "version", "author", "author_pub", "author_fp",
                  "capabilities", "size", "created")
_BUNDLE_SCHEMA = "oracleai.skill-bundle/1"


class SkillService:
    def __init__(self, store=None, key_dir=None, policy=None, base_dir=None):
        self.store = store or SkillStore(base_dir=base_dir)
        self.key_dir = key_dir
        self.policy = policy or skill_gate.default_policy()

    # ---- author / serve side ----
    def publish(self, body, name, version="", capabilities=None, author=""):
        """Sign a local skill with this Sage's key and store it PROMOTED (served)."""
        if isinstance(body, str):
            body = body.encode("utf-8")
        env = skill_trust.sign_artifact(body, name=name, version=version,
                                        capabilities=capabilities or [],
                                        author=author, key_dir=self.key_dir)
        return self.store.put(body, env, source="local", state=STATE_PROMOTED)

    def local_catalog(self):
        """Public metadata for PROMOTED skills -- what a peer/pool browses."""
        return [{k: row.get(k) for k in _PUBLIC_FIELDS}
                for row in self.store.list(state=STATE_PROMOTED)]

    def get_shareable(self, hid):
        """Body (b64) + envelope for a PROMOTED skill, else None. Re-verified on
        the way out so tampered objects are never served."""
        row = self.store.get(hid)
        if not row or row.get("state") != STATE_PROMOTED:
            return None
        if not self.store.verify(hid)["ok"]:
            return None
        body = self.store.get_body(hid)
        env = self.store.get_envelope(hid)
        if body is None or env is None:
            return None
        return {"id": hid,
                "body_b64": base64.b64encode(body).decode("ascii"),
                "envelope": env}

    # ---- fetch / client side ----
    def ingest(self, body, envelope, source="peer", trusted_pubkeys=None):
        """Verify + store QUARANTINED, then evaluate against policy. Returns
        {ok, id, reason, verdict}. Never promotes."""
        if isinstance(body, str):
            body = body.encode("utf-8")
        r = self.store.put(body, envelope, source=source,
                           trusted_pubkeys=trusted_pubkeys, state=STATE_QUARANTINED)
        if not r["ok"]:
            return {"ok": False, "id": r.get("id", ""), "reason": r["reason"], "verdict": None}
        tset = set(trusted_pubkeys or [])
        trusted = envelope["payload"].get("author_pub") in tset
        verdict = skill_gate.evaluate(envelope, self.policy, trusted=trusted, body=body)
        return {"ok": True, "id": r["id"], "reason": "quarantined",
                "deduped": r.get("deduped", False), "verdict": verdict}

    def fetch_object(self, fetcher, hid, source="peer", trusted_pubkeys=None):
        """fetcher(hid) -> {body_b64, envelope}. Confirms the returned bytes hash
        to the REQUESTED id before trusting them, then ingests (verify +
        quarantine + evaluate)."""
        try:
            obj = fetcher(hid)
        except Exception as e:
            import logging; logging.getLogger("veridian").warning("skill fetch_object error: %r", e)
            return {"ok": False, "id": hid, "reason": "fetch failed", "verdict": None}
        if not isinstance(obj, dict) or "body_b64" not in obj or "envelope" not in obj:
            return {"ok": False, "id": hid, "reason": "malformed object", "verdict": None}
        try:
            body = base64.b64decode(obj["body_b64"])
        except Exception:
            return {"ok": False, "id": hid, "reason": "bad body encoding", "verdict": None}
        if skill_trust.content_hash(body) != hid:
            return {"ok": False, "id": hid, "reason": "hash mismatch (wrong object)", "verdict": None}
        return self.ingest(body, obj["envelope"], source=source,
                           trusted_pubkeys=trusted_pubkeys)

    def browse(self, catalog_fetcher, trusted_pubkeys=None):
        """catalog_fetcher() -> [metadata]. Annotates each entry with whether we
        already have it locally (+ its state) and whether the author is trusted."""
        try:
            remote = catalog_fetcher() or []
        except Exception as e:
            import logging; logging.getLogger("veridian").warning("skill browse error: %r", e)
            return {"ok": False, "reason": "browse failed", "items": []}
        tset = set(trusted_pubkeys or [])
        items = []
        for m in remote:
            if not isinstance(m, dict):
                continue
            local = self.store.get(m.get("id", ""))
            items.append({
                "meta": {k: m.get(k) for k in _PUBLIC_FIELDS},
                "have": local is not None,
                "local_state": (local or {}).get("state"),
                "author_trusted": m.get("author_pub") in tset,
            })
        return {"ok": True, "items": items}

    def promote(self, hid, trusted_pubkeys=None):
        """Re-verify on disk, gate (L3), then promote (L2). Returns (ok, verdict)."""
        env = self.store.get_envelope(hid)
        body = self.store.get_body(hid)
        if env is None or body is None:
            return (False, {"reason": "not found"})
        v = skill_trust.verify_artifact(env, body, trusted_pubkeys=trusted_pubkeys)
        if not v["ok"]:
            return (False, {"reason": "verify failed: " + v["reason"]})
        allowed, verdict = skill_gate.gate_for_promotion(
            env, self.policy, trusted=v.get("trusted", False), body=body)
        if not allowed:
            return (False, verdict)
        self.store.promote(hid)
        return (True, verdict)

    # ---- offline bundles (the transport-agnostic floor) ----
    def export_bundle(self, hid):
        """Pack a stored skill into a self-contained, signed bundle dict that can
        travel as a FILE (email/USB/BitChat) and verify anywhere. Any locally
        verifiable skill can be exported; None if missing or tampered."""
        body = self.store.get_body(hid)
        env = self.store.get_envelope(hid)
        if body is None or env is None:
            return None
        if not self.store.verify(hid)["ok"]:
            return None
        return {"schema": _BUNDLE_SCHEMA, "envelope": env,
                "body_b64": base64.b64encode(body).decode("ascii")}

    def import_bundle(self, bundle, source="bundle", trusted_pubkeys=None):
        """Import a .skill bundle: verify hash + signature, land QUARANTINED, return
        the L3 verdict. No network involved. Never promotes."""
        if not isinstance(bundle, dict):
            return {"ok": False, "id": "", "reason": "malformed bundle", "verdict": None}
        if bundle.get("schema") != _BUNDLE_SCHEMA:
            return {"ok": False, "id": "", "reason": "unknown bundle schema", "verdict": None}
        env = bundle.get("envelope")
        if not isinstance(env, dict):
            return {"ok": False, "id": "", "reason": "missing envelope", "verdict": None}
        hid = (env.get("payload") or {}).get("id", "")
        obj = {"body_b64": bundle.get("body_b64", ""), "envelope": env}
        return self.fetch_object(lambda h: obj, hid, source=source, trusted_pubkeys=trusted_pubkeys)
