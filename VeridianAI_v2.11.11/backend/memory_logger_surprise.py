"""
memory_logger_surprise.py — Tamper-evident memory logger with surprise-scoring
-------------------------------------------------------------------------------
Foundation for the JournalistAgent feature. Each log entry is hash-chained to
the previous one, making tampering detectable. The "surprise score" combines
token probability and temperature deviation so that high-significance moments
can be prioritized for long-term retention while routine moments can be
compressed away.

v2.1.2 FIXES (April 9, 2026):
  * CRITICAL: Replaced "atomic replace" write pattern with true append.
    The previous version wrote each new entry to a temp file and then
    os.replace()d it over the log file, which atomically REPLACED the
    entire log with just the new entry. Only one entry ever survived
    on disk, regardless of how many log() calls were made. Now uses
    binary append mode with flush+fsync.
  * Test suite moved to a tempfile.mkdtemp() directory so running the
    script directly no longer pollutes the production log location.
  * Added reset() method for a clean way to wipe the log and start
    over from the genesis hash without manually hunting down files.
  * Added is_genesis() helper so callers can tell if they're about to
    write the first-ever entry.
  * Added count_entries() helper for quick diagnostics.

v2.1.4 CHANGES (April 21, 2026):
  * Fernet encryption wired in. The 'content' field of each entry is now
    symmetrically encrypted with a key loaded from config.FERNET_KEY_FILE
    (generated automatically on first boot). Everything else — timestamp,
    role, surprise score, metadata, hashes — stays plaintext so
    verify_chain(), get_recent() ordering, and surprise-sorted retrieval
    still work without the key. Hashes are computed over the ciphertext,
    so chain verification does NOT require the key either.
  * New 'role' parameter on log() (default "assistant") captures both
    sides of the conversation in a single chain. Also used with
    role="procedure" to log successful procedure commits for provenance
    (see procedural_memory.py).
  * Backward compatible: entries written pre-v2.1.4 have no 'role' field
    and a plaintext 'content' field. get_recent() and verify_chain()
    handle both formats. Old entries are treated as role="assistant"
    on read.
"""

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime  # noqa: F401 — kept for backward compat
from time_manager import TimeManager  # v2.1.6 unified time source
from cryptography.fernet import Fernet, InvalidToken


GENESIS_HASH = "0" * 64  # Standard "before time began" hash for chain start

# Fernet ciphertext is URL-safe base64 and always starts with "gAAAAA" for the
# v1 format we use. This prefix lets us detect encrypted vs plaintext content
# on read, so old plaintext entries continue to load without conversion.
_FERNET_PREFIX = "gAAAAA"

# Roles recognized by log(). "assistant" preserves back-compat (default).
VALID_ROLES = ("user", "assistant", "system", "procedure")


# -----------------------------------------------------------------------------
#  Fernet key loader (v2.1.4)
# -----------------------------------------------------------------------------
# The key lives at config.FERNET_KEY_FILE (backend/.fernet_key). On first call
# we either read the existing key or generate and persist a fresh one. We cache
# the Fernet instance at module scope so repeated log() calls are cheap.
_fernet_instance = None


def _get_or_create_fernet():
    """Return a cached Fernet instance, creating the key file if missing.

    Key location comes from config.FERNET_KEY_FILE so main.py and sage_daemon.py
    (and any other process that imports MemoryLogger) agree on the same key.
    """
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance

    # Import here to avoid a circular import at module-load time if config
    # ever needs to import from this module.
    from config import FERNET_KEY_FILE

    key_path = FERNET_KEY_FILE
    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            key = f.read().strip()
    else:
        key = Fernet.generate_key()
        # Write atomically so a crash mid-write can't produce a truncated key
        tmp = str(key_path) + ".tmp"
        with open(tmp, "wb") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, key_path)
        # Best-effort permission tighten on POSIX; no-op on Windows
        try:
            os.chmod(key_path, 0o600)
        except Exception:
            pass

    _fernet_instance = Fernet(key)
    return _fernet_instance


def _encrypt_content(plaintext):
    """Encrypt a string, returning a Fernet token string."""
    if plaintext is None:
        return None
    if not isinstance(plaintext, str):
        plaintext = json.dumps(plaintext, separators=(',', ':'), ensure_ascii=False)
    fernet = _get_or_create_fernet()
    return fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def _decrypt_content(ciphertext):
    """Decrypt a Fernet token string back to plaintext. Returns the input
    unchanged if it doesn't look encrypted (for pre-v2.1.4 entries).
    Raises InvalidToken if it looks encrypted but can't be decrypted,
    which signals either a missing/wrong key or tampered ciphertext.
    """
    if ciphertext is None:
        return None
    if not isinstance(ciphertext, str) or not ciphertext.startswith(_FERNET_PREFIX):
        # Pre-v2.1.4 plaintext entry — return as-is
        return ciphertext
    fernet = _get_or_create_fernet()
    return fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


class MemoryLogger:
    def __init__(self, storage_dir="./memory_log", password=None, baseline_temp=0.5):
        """
        Tamper-evident memory logger with surprise-scoring for meaningful retention.

        Args:
            storage_dir: Directory for log files (created if missing)
            password: Optional password for encryption (reserved for future use)
            baseline_temp: Personal temperature baseline for surprise calculation
        """
        self.storage_dir = storage_dir
        self.password = password
        self.baseline_temp = baseline_temp
        self.log_file = os.path.join(storage_dir, "memory_chain.log")
        os.makedirs(storage_dir, exist_ok=True)
        self.chain_head = self._load_chain_head()

    # --- Chain state ------------------------------
    def _load_chain_head(self):
        """Load the hash of the most recent entry, or GENESIS_HASH if no log."""
        if not os.path.exists(self.log_file):
            return GENESIS_HASH
        try:
            with open(self.log_file, "rb") as f:
                lines = f.readlines()
            if not lines:
                return GENESIS_HASH
            # Walk backwards to find the last valid JSON line
            # (in case a partial write left a junk trailing line)
            for raw in reversed(lines):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                    h = entry.get("hash")
                    if h:
                        return h
                except json.JSONDecodeError:
                    continue
            return GENESIS_HASH
        except (OSError, IndexError):
            return GENESIS_HASH

    def is_genesis(self):
        """Return True if the next log() call will be the first entry in the chain."""
        return self.chain_head == GENESIS_HASH

    # --- Hashing -------------------
    def _hash_entry(self, entry):
        """Hash everything in the entry EXCEPT the 'hash' field itself."""
        entry_copy = {k: v for k, v in entry.items() if k != "hash"}
        entry_json = json.dumps(entry_copy, sort_keys=True, separators=(',', ':'))
        return hashlib.sha3_256(entry_json.encode()).hexdigest()

    # --- Write --------------------
    def log(self, content, temperature=0.7, token_prob=None, metadata=None,
            role="assistant"):
        """Append a new entry to the hash-chained log.

        Args:
            content:     The payload to log. Encrypted before hashing.
            temperature: Sampling temperature at generation time.
            token_prob:  Mean token probability (0.0-1.0) for surprise
                         scoring. Pass None for non-generated entries
                         (user messages, procedure commits) — treated as
                         0.5 (neutral surprise).
            metadata:    Free-form dict attached to the entry.
            role:        One of VALID_ROLES. "assistant" is the historical
                         default; "user" captures the user side of the
                         conversation; "procedure" links a procedural
                         memory commit into the chain for provenance;
                         "system" for system-prompt / boot markers.

        Returns the chain hash of the newly-appended entry (str). Callers
        that need to record the entry elsewhere (procedural memory
        provenance) use this return value as the authoritative link.
        """
        if role not in VALID_ROLES:
            raise ValueError(f"Invalid role: {role!r}. Expected one of {VALID_ROLES}")

        # v2.1.7 pre-write guard (Leo audit, Bug 4): the chain is meant
        # to be a tamper-evident record of CONVERSATIONAL exchanges,
        # not a dumping ground for empty strings and operational error
        # messages. Without this guard, the chain accumulates entries
        # like "[Oracle Ollama error 500]" and assistant turns with no
        # content — both of which pollute /api/daemon/digest, confuse
        # [SEARCH_MEMORY:], and waste chain entries on non-information.
        #
        # We only filter assistant-side writes. User messages are
        # captured verbatim regardless (a short user turn like "hi"
        # is legitimate). Procedure and system entries pass through
        # because they're internally generated and shaped.
        if role == "assistant":
            content_str = str(content) if content is not None else ""
            if not content_str.strip():
                print(f"[MEMORY LOGGER] skipping empty assistant content "
                      f"(would have written {len(content_str)} chars)")
                return None
            # Error-shaped strings from the inference layer — these are
            # operational, not memory. They go to stdout for debugging
            # but NOT into the chain. Add new prefixes here as new
            # error shapes get discovered.
            _ERROR_PREFIXES = (
                "[Oracle Ollama error",
                "[Sage Ollama error",
                "[Daemon Ollama error",
                "[Sage llama-server error",
                "[Daemon llama-server error",
                "[Oracle llama-server error",
                "[Error: Cannot connect",
                "[Error: ",
            )
            stripped = content_str.strip()
            if any(stripped.startswith(p) for p in _ERROR_PREFIXES):
                print(f"[MEMORY LOGGER] skipping error-shaped content "
                      f"from chain: {stripped[:80]}")
                return None

        # Calculate surprise score: (1 - token_prob) weighted + temp deviation
        token_surprise = 1.0 - (token_prob if token_prob is not None else 0.5)
        temp_deviation = abs(temperature - self.baseline_temp)
        surprise_score = min(
            1.0, max(0.0, (token_surprise * 0.7) + (temp_deviation * 0.3))
        )

        # v2.1.4: encrypt the content field. Hashes are computed over the
        # ciphertext so verify_chain() works without the Fernet key and the
        # chain remains tamper-evident independent of encryption state.
        encrypted_content = _encrypt_content(content)

        entry = {
            # v2.1.6: TimeManager.iso_z() produces the same Z-suffixed
            # ISO format the chain log was already using, so old and
            # new entries co-mingle without breaking verify_chain().
            "timestamp": TimeManager.iso_z(),
            "role": role,
            "content": encrypted_content,
            "temperature": temperature,
            "token_prob": token_prob,
            "surprise_score": round(surprise_score, 4),
            "metadata": metadata or {},
            "prev_hash": self.chain_head,
        }

        entry_hash = self._hash_entry(entry)
        entry["hash"] = entry_hash
        entry_line = json.dumps(entry, separators=(',', ':')) + "\n"
        entry_bytes = entry_line.encode("utf-8")

        # --- TRUE ATOMIC APPEND (v2.1.2 fix) ------------------
        # Previous version used os.replace() which REPLACED the entire log
        # with a single-entry temp file. Now we append correctly:
        #   1. Open in binary append mode (seeks to end automatically)
        #   2. Write the line
        #   3. Flush Python's buffer to the OS
        #   4. fsync to force the OS to commit to disk
        # This is atomic for small writes (<4KB) on both POSIX and Windows
        # since we have a single writer. Larger entries are written as one
        # contiguous write() call, which is safe for single-process use.
        with open(self.log_file, "ab") as f:
            f.write(entry_bytes)
            f.flush()
            os.fsync(f.fileno())

        self.chain_head = entry_hash
        return entry_hash

    # --- Verify --------------
    def verify_chain(self):
        """Walk the entire log and verify each entry's hash + chain linkage.

        Returns (is_valid: bool, message: str, valid_count: int).
        """
        if not os.path.exists(self.log_file):
            return True, "No log file yet", 0
        try:
            with open(self.log_file, "rb") as f:
                lines = f.readlines()
            if not lines:
                return True, "Empty log file", 0

            expected_prev = GENESIS_HASH
            valid_count = 0

            for i, line_bytes in enumerate(lines):
                line_bytes = line_bytes.strip()
                if not line_bytes:
                    continue
                try:
                    entry = json.loads(line_bytes.decode("utf-8"))
                except json.JSONDecodeError as e:
                    return False, f"Invalid JSON at line {i+1}: {e}", valid_count

                # Verify entry integrity
                if entry.get("hash") != self._hash_entry(entry):
                    return False, f"Hash mismatch at line {i+1}", valid_count

                # Verify chain linkage
                if entry.get("prev_hash") != expected_prev:
                    return (
                        False,
                        f"Chain break at line {i+1} "
                        f"(expected {expected_prev[:16]}..., got {(entry.get('prev_hash') or '')[:16]}...)",
                        valid_count,
                    )

                expected_prev = entry["hash"]
                valid_count += 1

            return True, f"Chain verified ({valid_count} entries)", valid_count
        except Exception as e:
            return False, f"Read error: {e}", 0

    # --- Read -----------------------
    def get_recent(self, n=10, sort_by_surprise=False, decrypt=True):
        """Return the N most recent entries (or top N by surprise score).

        Args:
            n:                Number of entries to return.
            sort_by_surprise: If True, return top-N by surprise_score
                              (descending) instead of most recent.
            decrypt:          If True (default), decrypt the 'content'
                              field of each entry before returning.
                              Set to False for raw inspection (e.g.
                              re-hashing during verify_chain()).

        If decryption fails for an entry (missing key / tampered
        ciphertext), that entry's 'content' is replaced with the sentinel
        string "[DECRYPT_FAILED]" rather than raising, so a single bad
        line does not poison the whole read.
        """
        if not os.path.exists(self.log_file):
            return []
        try:
            with open(self.log_file, "rb") as f:
                lines = f.readlines()
            entries = []
            for line_bytes in lines:
                line_bytes = line_bytes.strip()
                if not line_bytes:
                    continue
                try:
                    entry = json.loads(line_bytes.decode("utf-8"))
                except json.JSONDecodeError:
                    continue  # Skip any corrupted lines

                if decrypt and "content" in entry:
                    try:
                        entry["content"] = _decrypt_content(entry["content"])
                    except InvalidToken:
                        entry["content"] = "[DECRYPT_FAILED]"
                # Back-compat: entries without a role default to assistant
                if "role" not in entry:
                    entry["role"] = "assistant"
                entries.append(entry)

            if sort_by_surprise:
                entries.sort(
                    key=lambda x: x.get("surprise_score", 0), reverse=True
                )
                return entries[:n]
            return entries[-n:]
        except Exception:
            return []

    def count_entries(self):
        """Return the total number of entries in the log."""
        if not os.path.exists(self.log_file):
            return 0
        try:
            with open(self.log_file, "rb") as f:
                return sum(1 for line in f if line.strip())
        except Exception:
            return 0


    # --- Reset ----------------------
    def reset(self):
        """Delete the log file and reset the chain head to genesis.

        Clean way to start over — new users will see their own genesis hash,
        not someone else's inherited chain.
        """
        if os.path.exists(self.log_file):
            os.unlink(self.log_file)
        self.chain_head = GENESIS_HASH
        return True


# -----------------------------------------------------------------------------
#  TEST SUITE — uses a temporary directory so it NEVER touches production logs
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    print("=== Memory Logger (v2.1.2) — Test Suite ===\n")

    # Use a throwaway tempdir so running this script never pollutes
    # backend/memory_log/ or any other production location
    test_dir = tempfile.mkdtemp(prefix="memlog_test_")
    print(f"Test directory: {test_dir}\n")

    try:
        # --- Test 1: Basic logging and chain verification -------
        print("1. Basic logging and chain verification...")
        logger = MemoryLogger(storage_dir=test_dir, baseline_temp=0.5)
        assert logger.is_genesis(), "Fresh logger should be at genesis"

        logger.log(
            content="Focused coding session: debugging vibe-coded parser",
            temperature=0.2,
            token_prob=0.85,
            metadata={"activity": "coding", "focus": "high"},
        )
        logger.log(
            content="Brainstorming surprise-scoring idea for memory prioritization",
            temperature=0.8,
            token_prob=0.3,
            metadata={"activity": "ideation", "novelty": "high"},
        )
        logger.log(
            content="Reflecting on accessibility needs for memory-impaired users",
            temperature=0.5,
            token_prob=0.5,
            metadata={"activity": "reflection", "empathy": "high"},
        )

        is_valid, msg, count = logger.verify_chain()
        assert is_valid, f"Chain verification failed: {msg}"
        assert count == 3, f"Expected 3 entries, got {count}"
        print(f"   PASS: {msg}")
        print(f"   PASS: All 3 entries persisted (the overwrite bug is fixed)\n")

        # --- Test 2: Surprise score calculations ---------
        print("2. Surprise score calculations...")
        recent = logger.get_recent(3)
        assert len(recent) == 3, f"Expected 3 recent, got {len(recent)}"
        assert recent[0]["surprise_score"] < 0.3, \
            f"Expected low surprise for coding, got {recent[0]['surprise_score']}"
        assert recent[1]["surprise_score"] > 0.6, \
            f"Expected high surprise for ideation, got {recent[1]['surprise_score']}"
        assert 0.4 <= recent[2]["surprise_score"] <= 0.6, \
            f"Expected medium for reflection, got {recent[2]['surprise_score']}"
        print("   PASS: Surprise scores align with expectations")
        print(f"   Coding: {recent[0]['surprise_score']}, "
              f"Ideation: {recent[1]['surprise_score']}, "
              f"Reflection: {recent[2]['surprise_score']}\n")

        # --- Test 3: Tamper detection --------------
        print("3. Tamper detection...")
        with open(logger.log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # Corrupt the middle entry
        lines[1] = lines[1].replace('"content":"', '"content":"TAMPERED: "', 1)
        with open(logger.log_file, "w", encoding="utf-8") as f:
            f.writelines(lines)

        is_valid, msg, _ = logger.verify_chain()
        assert not is_valid, "Tampering should break chain verification"
        print(f"   PASS: Tamper detected - {msg}\n")

        # --- Test 4: Reset and re-chain --------------
        print("4. Reset and fresh start...")
        logger.reset()
        assert logger.is_genesis(), "After reset, should be at genesis"
        assert logger.count_entries() == 0, "After reset, log should be empty"
        logger.log(
            content="Fresh start after reset",
            temperature=0.5,
            token_prob=0.6,
        )
        is_valid, msg, count = logger.verify_chain()
        assert is_valid, f"Post-reset chain should verify: {msg}"
        assert count == 1, f"Expected 1 entry after reset+log, got {count}"
        print(f"   PASS: Reset works cleanly - {msg}\n")

        # --- Test 5: Persistence across instances -----------
        print("5. Persistence across MemoryLogger instances...")
        # Write a few more entries
        logger.log(content="Second entry", temperature=0.4, token_prob=0.7)
        logger.log(content="Third entry", temperature=0.6, token_prob=0.5)

        # Create a brand new instance pointing at the same directory
        logger2 = MemoryLogger(storage_dir=test_dir, baseline_temp=0.5)
        assert not logger2.is_genesis(), \
            "New instance with existing log should not be at genesis"
        assert logger2.count_entries() == 3, \
            f"Expected 3 entries, got {logger2.count_entries()}"

        # Add one more through the new instance
        logger2.log(content="Added via new instance", temperature=0.5, token_prob=0.5)
        is_valid, msg, count = logger2.verify_chain()
        assert is_valid and count == 4, \
            f"Chain should still verify with 4 entries: {msg}"
        print(f"   PASS: Log persists correctly across instances - {msg}\n")

        # --- Test 6: Surprise-sorted retrieval ----------
        print("6. Surprise-sorted retrieval...")
        top = logger2.get_recent(n=3, sort_by_surprise=True)
        assert len(top) > 0, "Should return some entries"
        for i in range(len(top) - 1):
            assert top[i]["surprise_score"] >= top[i + 1]["surprise_score"], \
                "Scores should be descending"
        print(f"   PASS: Top surprise score = {top[0]['surprise_score']}\n")

        print("=== ALL TESTS PASSED ===")
        print("Memory logger is ready for use in OracleAI.")
        print(f"\nNote: this test ran in a throwaway directory ({test_dir})")
        print("and did NOT touch your real memory log.")

    finally:
        # Always clean up the test directory
        try:
            shutil.rmtree(test_dir)
            print(f"Cleaned up: {test_dir}")
        except Exception as e:
            print(f"Could not clean up test dir: {e}")
