"""
procedural_memory.py -- Procedural knowledge base for OracleAI
----------------------------------------------------------------
Stores operational knowledge (working commands, file paths, known bugs,
verified recipes) separately from episodic/surprise memory so the
human-facing recall stream stays uncluttered.

Structure on disk:
    {
        "successful":   { "<key>": { "value": ..., "metadata": {...},
                                     "timestamp": "...",
                                     "chain_hash": "<sha3_256 hex>" } },
        "unsuccessful": { "<key>": { ...same shape minus chain_hash... } }
    }

Successful procedures are additionally witnessed by the memory_logger hash
chain: at add time, a role="procedure" entry is written to the main chain
containing a SHA-256 of the procedure's value. The returned chain hash is
stored back on the procedure record as `chain_hash`, creating a
verifiable provenance link. `verify_procedure_provenance()` walks the
chain to confirm the witness entry is still present and intact.

Unsuccessful procedures are NOT chained (they are dead-ends, tracked locally
to avoid re-trying; promoting them to the provenance log would add noise).

v2.1.4 CHANGES (April 21, 2026):
  * Multiple CRUD methods were operating on self._knowledge_base[key]
    as if it were flat, but the structure splits into
    "successful"/"unsuccessful". Those methods silently returned False
    or raised on every call. Now they correctly traverse both buckets.
  * update_procedure / delete_procedure / list_procedures / clear now
    take a `category` argument ("successful", "unsuccessful", or "any").
  * Added optional `memory_logger` injection so successful procedures
    get chain-witnessed for verifiable provenance.
  * Added verify_procedure_provenance() and verify_integrity() now
    actually walks the chain when a logger is attached.
"""

import json
import os
import hashlib
from datetime import datetime  # noqa: F401 — kept for backward compat
from time_manager import TimeManager  # v2.1.6 unified time source
from typing import Any, Dict, List, Optional


class ProceduralMemory:
    """Persistent key-value store for operational knowledge with optional
    hash-chain provenance for successful procedures.
    """

    def __init__(self,
                 storage_dir: str = "./procedural_memory",
                 filename: str = "procedural.json",
                 memory_logger: Optional[Any] = None):
        """
        Args:
            storage_dir:   Directory for the JSON store (created if missing)
            filename:      Name of the JSON file
            memory_logger: Optional MemoryLogger instance. When provided,
                           successful procedures are additionally committed
                           to its hash chain with role="procedure" and the
                           returned chain hash is stored as provenance.
        """
        self.storage_dir = storage_dir
        self.filename = filename
        self.file_path = os.path.join(storage_dir, filename)
        self.memory_logger = memory_logger
        os.makedirs(storage_dir, exist_ok=True)
        self._knowledge_base: Dict[str, Dict[str, Any]] = self._load()

    # --- Persistence ------------------------------------------------
    def _load(self) -> Dict[str, Dict[str, Any]]:
        """Load the knowledge base; return empty split structure if missing."""
        if not os.path.exists(self.file_path):
            return {"successful": {}, "unsuccessful": {}}
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"successful": {}, "unsuccessful": {}}
            # Migrate a flat legacy structure
            if "successful" not in data and "unsuccessful" not in data:
                return {"successful": data, "unsuccessful": {}}
            # Ensure both buckets exist even if only one was persisted
            data.setdefault("successful", {})
            data.setdefault("unsuccessful", {})
            return data
        except (json.JSONDecodeError, OSError):
            return {"successful": {}, "unsuccessful": {}}

    def _save(self) -> None:
        """Atomically write the knowledge base to disk."""
        temp_path = self.file_path + ".tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self._knowledge_base, f,
                          indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.file_path)  # atomic on POSIX/Windows
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    # --- Internal helpers -------------------------------------------
    @staticmethod
    def _hash_value(value: Any) -> str:
        """Canonical SHA-256 over a procedure value (for chain witness)."""
        if isinstance(value, (dict, list)):
            payload = json.dumps(value, sort_keys=True,
                                 separators=(',', ':'), ensure_ascii=False)
        else:
            payload = str(value)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _witness_to_chain(self, key: str, value: Any,
                          metadata: Optional[Dict]) -> Optional[str]:
        """Commit a procedure witness to the attached memory_logger, if any.
        Returns the chain hash on success, None if no logger is attached.
        """
        if self.memory_logger is None:
            return None
        value_hash = self._hash_value(value)
        meta = dict(metadata or {})
        meta.update({
            "procedure_key": key,
            "procedure_value_sha256": value_hash,
        })
        # MemoryLogger.log() returns the chain hash (v2.1.4). The 'content'
        # we commit is the value hash, not the value itself -- the
        # procedural JSON remains the authoritative copy of the value; the
        # chain is only the tamper-evident witness.
        chain_hash = self.memory_logger.log(
            content=f"procedure:{key}:{value_hash}",
            temperature=0.0,
            token_prob=None,
            metadata=meta,
            role="procedure",
        )
        return chain_hash

    # --- CRUD -------------------------------------------------------
    def add_procedure(self,
                      key: str,
                      value: Any,
                      success: bool = True,
                      metadata: Optional[Dict] = None) -> bool:
        """Store procedural knowledge.

        Returns True if newly added, False if overwriting existing.
        """
        category = "successful" if success else "unsuccessful"
        entry: Dict[str, Any] = {
            "value": value,
            "metadata": metadata or {},
            "timestamp": TimeManager.iso_z(),  # v2.1.6 unified
        }
        # Only successful procedures are chain-witnessed. Unsuccessful ones
        # are dead-ends tracked locally to avoid re-trying; witnessing them
        # would just add noise to the conversation chain.
        if success:
            chain_hash = self._witness_to_chain(key, value, metadata)
            if chain_hash is not None:
                entry["chain_hash"] = chain_hash

        is_new = key not in self._knowledge_base[category]
        self._knowledge_base[category][key] = entry
        self._save()
        return is_new

    def get_procedure(self,
                      key: str,
                      category: str = "successful") -> Optional[Any]:
        """Retrieve a procedure value. category: 'successful' | 'unsuccessful' | 'any'"""
        entry = self._get_entry(key, category)
        return entry["value"] if entry else None

    def get_procedure_with_metadata(self,
                                    key: str,
                                    category: str = "any") -> Optional[Dict]:
        """Retrieve the full entry (value + metadata + timestamp + chain_hash)."""
        return self._get_entry(key, category)

    def _get_entry(self, key: str, category: str) -> Optional[Dict]:
        if category == "any":
            return (self._knowledge_base["successful"].get(key)
                    or self._knowledge_base["unsuccessful"].get(key))
        return self._knowledge_base.get(category, {}).get(key)

    def update_procedure(self,
                         key: str,
                         value: Any,
                         metadata: Optional[Dict] = None,
                         category: str = "successful") -> bool:
        """Update an existing procedure in the given category.

        If the procedure is in the "successful" bucket and a memory_logger
        is attached, an updated chain witness is written and stored.
        Returns True if the key existed and was updated, False otherwise.
        """
        if category not in ("successful", "unsuccessful"):
            return False
        bucket = self._knowledge_base[category]
        if key not in bucket:
            return False
        entry: Dict[str, Any] = {
            "value": value,
            "metadata": metadata or {},
            "timestamp": TimeManager.iso_z(),  # v2.1.6 unified
        }
        if category == "successful":
            chain_hash = self._witness_to_chain(key, value, metadata)
            if chain_hash is not None:
                entry["chain_hash"] = chain_hash
        bucket[key] = entry
        self._save()
        return True

    def delete_procedure(self,
                         key: str,
                         category: str = "any") -> bool:
        """Remove a procedure; returns True if deleted, False if not found."""
        if category == "any":
            for cat in ("successful", "unsuccessful"):
                if key in self._knowledge_base[cat]:
                    del self._knowledge_base[cat][key]
                    self._save()
                    return True
            return False
        if category not in ("successful", "unsuccessful"):
            return False
        if key not in self._knowledge_base[category]:
            return False
        del self._knowledge_base[category][key]
        self._save()
        return True

    def list_procedures(self, category: str = "successful") -> List[str]:
        """Return procedure keys in the given category ('any' returns both)."""
        if category == "any":
            return (list(self._knowledge_base["successful"].keys())
                    + list(self._knowledge_base["unsuccessful"].keys()))
        return list(self._knowledge_base.get(category, {}).keys())

    def get_all(self) -> Dict[str, Any]:
        """Deep copy of the entire knowledge base."""
        return json.loads(json.dumps(self._knowledge_base))

    def clear(self, category: str = "any") -> None:
        """Delete procedural knowledge in the given category.
        Default 'any' clears both buckets; the top-level structure is preserved.
        """
        if category == "any":
            self._knowledge_base["successful"].clear()
            self._knowledge_base["unsuccessful"].clear()
        elif category in ("successful", "unsuccessful"):
            self._knowledge_base[category].clear()
        else:
            return
        self._save()

    # --- Integrity / Provenance -------------------------------------
    def verify_procedure_provenance(self, key: str) -> Dict[str, Any]:
        """Confirm a successful procedure's chain witness is still present
        and un-tampered.

        Returns a dict:
            {
              "key": <key>,
              "found": True/False,
              "witnessed": True/False,
              "chain_hash": "<hash>" or None,
              "chain_intact": True/False,
              "value_hash_matches": True/False,
              "message": "<human-readable>"
            }
        """
        entry = self._knowledge_base["successful"].get(key)
        if entry is None:
            return {
                "key": key, "found": False, "witnessed": False,
                "chain_hash": None, "chain_intact": False,
                "value_hash_matches": False,
                "message": f"Procedure {key!r} not found in 'successful'",
            }

        recorded_hash = entry.get("chain_hash")
        if not recorded_hash:
            return {
                "key": key, "found": True, "witnessed": False,
                "chain_hash": None, "chain_intact": False,
                "value_hash_matches": False,
                "message": f"Procedure {key!r} has no chain_hash "
                           f"(added without a memory_logger attached)",
            }

        if self.memory_logger is None:
            return {
                "key": key, "found": True, "witnessed": True,
                "chain_hash": recorded_hash, "chain_intact": False,
                "value_hash_matches": False,
                "message": "Cannot verify: no memory_logger attached",
            }

        # Verify the chain itself first
        is_valid, msg, _count = self.memory_logger.verify_chain()
        if not is_valid:
            return {
                "key": key, "found": True, "witnessed": True,
                "chain_hash": recorded_hash, "chain_intact": False,
                "value_hash_matches": False,
                "message": f"Chain tampered: {msg}",
            }

        # Walk entries to find the witness
        # get_recent with a very large n returns the whole log
        entries = self.memory_logger.get_recent(n=10**9, sort_by_surprise=False)
        witness = next((e for e in entries if e.get("hash") == recorded_hash),
                       None)
        if witness is None:
            return {
                "key": key, "found": True, "witnessed": True,
                "chain_hash": recorded_hash, "chain_intact": True,
                "value_hash_matches": False,
                "message": f"Chain intact but witness entry "
                           f"{recorded_hash[:16]}... not found",
            }

        # Confirm the value hash recorded in the witness still matches the
        # procedure's current value (detects in-place value edits that
        # bypass update_procedure)
        current_value_hash = self._hash_value(entry["value"])
        recorded_value_hash = witness.get("metadata", {}).get(
            "procedure_value_sha256")
        matches = (current_value_hash == recorded_value_hash)

        return {
            "key": key, "found": True, "witnessed": True,
            "chain_hash": recorded_hash, "chain_intact": True,
            "value_hash_matches": matches,
            "message": ("Provenance verified" if matches else
                        "Chain intact but procedure value has drifted "
                        "from the witnessed hash"),
        }

    def verify_integrity(self) -> bool:
        """Quick integrity check. Without a logger: JSON parseability only.
        With a logger: full chain walk PLUS provenance check on every
        successful procedure that has a chain_hash.
        """
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                json.load(f)
        except Exception:
            return False

        if self.memory_logger is None:
            return True

        is_valid, _msg, _n = self.memory_logger.verify_chain()
        if not is_valid:
            return False

        for key, entry in self._knowledge_base["successful"].items():
            if "chain_hash" not in entry:
                continue
            result = self.verify_procedure_provenance(key)
            if not result["value_hash_matches"]:
                return False
        return True
