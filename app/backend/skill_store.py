"""
OracleAI / Aether -- skill STORE + CATALOG (Layer 2).

Content-addressed storage for signed skills + a SQLite catalog. Bodies are
hash-named files (the name proves the bytes -> free dedupe + tamper-evidence);
the catalog is a single SQLite index tracking author, declared capabilities,
size, provenance, and trust/quarantine STATE.

Every put() re-verifies the signed envelope against the body via skill_trust,
so nothing enters the store unverified. State defaults to 'quarantined' -- being
in the store grants an artifact NO authority (promotion is L3's gate). Removal
is reversible (objects move to removed/, never hard-deleted).
"""
import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path

import skill_trust

_DIRNAME = "aether_skills"
_OBJECTS = "objects"
_CATALOG = "catalog.sqlite"

STATE_QUARANTINED = "quarantined"
STATE_PROMOTED = "promoted"
STATE_REJECTED = "rejected"
_STATES = {STATE_QUARANTINED, STATE_PROMOTED, STATE_REJECTED}


def _base_dir(explicit=None):
    if explicit is not None:
        return Path(explicit)
    try:
        from config import DATA_DIR
        return Path(DATA_DIR) / _DIRNAME
    except Exception:
        return Path(os.path.expanduser("~")) / ".oracleai_sage_data" / _DIRNAME


class SkillStore:
    def __init__(self, base_dir=None):
        self.base = _base_dir(base_dir)
        self.objects = self.base / _OBJECTS
        self.catalog = self.base / _CATALOG
        self.objects.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ---- db plumbing ----
    def _connect(self):
        conn = sqlite3.connect(str(self.catalog), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextlib.contextmanager
    def _db(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._db() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS skills ("
                " id TEXT PRIMARY KEY,"
                " name TEXT NOT NULL DEFAULT '',"
                " version TEXT NOT NULL DEFAULT '',"
                " author TEXT NOT NULL DEFAULT '',"
                " author_pub TEXT NOT NULL DEFAULT '',"
                " author_fp TEXT NOT NULL DEFAULT '',"
                " capabilities TEXT NOT NULL DEFAULT '[]',"
                " size INTEGER NOT NULL DEFAULT 0,"
                " created INTEGER NOT NULL DEFAULT 0,"
                " added INTEGER NOT NULL DEFAULT 0,"
                " state TEXT NOT NULL DEFAULT 'quarantined',"
                " source TEXT NOT NULL DEFAULT '',"
                " verified INTEGER NOT NULL DEFAULT 0)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_skills_state ON skills(state)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_skills_pub ON skills(author_pub)")

    # ---- object paths ----
    @staticmethod
    def _safe_hid(hid):
        """Content-addresses are hashes/tokens, never paths. Reject separators and
        traversal so a fetched id can't escape the objects/ dir (path injection)."""
        h = str(hid or "").strip()
        if (not h or len(h) > 160 or "/" in h or "\\" in h or ".." in h
                or "\x00" in h or h in (".", "..")):
            raise ValueError("invalid skill id")
        return h

    def _body_path(self, hid):
        return self.objects / (self._safe_hid(hid) + ".bin")

    def _env_path(self, hid):
        return self.objects / (self._safe_hid(hid) + ".skill.json")

    def _atomic_write(self, path, data):
        tmp = Path(str(path) + ".tmp")
        tmp.write_bytes(data)
        os.replace(str(tmp), str(path))

    # ---- put / get ----
    def put(self, body, envelope, source="local", trusted_pubkeys=None,
            state=STATE_QUARANTINED, max_body_bytes=None):
        """Verify the envelope against the body, then store both content-addressed
        and upsert the catalog row. Idempotent on the content hash; an existing
        row's state/added/source are PRESERVED (a re-put never re-quarantines a
        promoted skill). Returns {ok, id, reason, deduped}."""
        if not isinstance(body, (bytes, bytearray)):
            return {"ok": False, "id": "", "reason": "body must be bytes", "deduped": False}
        if max_body_bytes is not None and len(body) > max_body_bytes:
            return {"ok": False, "id": "", "reason": "body exceeds size cap", "deduped": False}
        v = skill_trust.verify_artifact(envelope, body, trusted_pubkeys=trusted_pubkeys)
        if not v["ok"]:
            return {"ok": False, "id": v.get("id", ""),
                    "reason": "verify failed: " + v["reason"], "deduped": False}
        hid = v["id"]
        payload = envelope["payload"]
        if state not in _STATES:
            state = STATE_QUARANTINED
        deduped = self._body_path(hid).exists()
        self._atomic_write(self._body_path(hid), bytes(body))
        self._atomic_write(self._env_path(hid),
                           json.dumps(envelope, indent=2, ensure_ascii=True).encode("ascii"))
        now = int(time.time())
        with self._db() as c:
            c.execute(
                "INSERT INTO skills (id,name,version,author,author_pub,author_fp,"
                "capabilities,size,created,added,state,source,verified)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)"
                " ON CONFLICT(id) DO UPDATE SET"
                "  name=excluded.name, version=excluded.version, author=excluded.author,"
                "  author_pub=excluded.author_pub, author_fp=excluded.author_fp,"
                "  capabilities=excluded.capabilities, size=excluded.size,"
                "  created=excluded.created, verified=1",
                (hid, payload.get("name", ""), payload.get("version", ""),
                 payload.get("author", ""), payload.get("author_pub", ""),
                 v.get("author_fingerprint", ""),
                 json.dumps(payload.get("capabilities", [])),
                 len(body), int(payload.get("created", 0)), now, state, source))
        return {"ok": True, "id": hid, "reason": "stored", "deduped": deduped}

    def get_body(self, hid):
        try:
            p = self._body_path(hid)
        except ValueError:
            return None
        return p.read_bytes() if p.exists() else None

    def get_envelope(self, hid):
        try:
            p = self._env_path(hid)
        except ValueError:
            return None
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def get(self, hid):
        with self._db() as c:
            r = c.execute("SELECT * FROM skills WHERE id=?", (hid,)).fetchone()
        return dict(r) if r else None

    def list(self, state=None, author_pub=None, limit=1000):
        q = "SELECT * FROM skills"
        conds, args = [], []
        if state:
            conds.append("state=?"); args.append(state)
        if author_pub:
            conds.append("author_pub=?"); args.append(author_pub)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY added DESC LIMIT ?"
        args.append(int(limit))
        with self._db() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    def set_state(self, hid, state):
        if state not in _STATES:
            return False
        with self._db() as c:
            cur = c.execute("UPDATE skills SET state=? WHERE id=?", (state, hid))
            return cur.rowcount > 0

    def promote(self, hid):
        return self.set_state(hid, STATE_PROMOTED)

    def reject(self, hid):
        return self.set_state(hid, STATE_REJECTED)

    def verify(self, hid, trusted_pubkeys=None):
        """Re-verify a stored skill against its on-disk body (catches tampering)."""
        body = self.get_body(hid)
        env = self.get_envelope(hid)
        if body is None or env is None:
            return {"ok": False, "reason": "missing"}
        return skill_trust.verify_artifact(env, body, trusted_pubkeys=trusted_pubkeys)

    def remove(self, hid):
        """Reversible removal: move objects to removed/ and drop the catalog row.
        Never hard-deletes."""
        try:
            bp, ep = self._body_path(hid), self._env_path(hid)
        except ValueError:
            return False
        row = self.get(hid)
        if not row and not bp.exists():
            return False
        rem = self.base / "removed"
        rem.mkdir(parents=True, exist_ok=True)
        for p in (bp, ep):
            if p.exists():
                os.replace(str(p), str(rem / p.name))
        with self._db() as c:
            c.execute("DELETE FROM skills WHERE id=?", (hid,))
        return True

    def stats(self):
        with self._db() as c:
            rows = c.execute(
                "SELECT state, COUNT(*) n, COALESCE(SUM(size),0) b"
                " FROM skills GROUP BY state").fetchall()
        by_state = {r["state"]: r["n"] for r in rows}
        total = sum(r["b"] for r in rows)
        return {"count": sum(by_state.values()), "by_state": by_state,
                "total_bytes": total}
