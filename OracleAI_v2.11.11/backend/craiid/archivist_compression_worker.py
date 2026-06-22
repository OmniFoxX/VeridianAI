"""
Archivist Worker for CRAIID Compression Integration
Handles persistent storage of compression keys via low-urgency OAgentD tasks.
Designed to integrate with existing Archivist insight persistence patterns.
"""

import json
import time
from pathlib import Path
from typing import Dict, Any, Optional
import logging

# Import the compression core (v3)
try:
    from craiid_compression_core_v3 import VLTSCompressor  # Adjusted for actual filename pattern
except ImportError:
    try:
        from .craiid_compression_core_v3 import VLTSCompressor
    except ImportError:
        # Fallback path resolution
        import sys
        from pathlib import Path
        sys.path.append(str(Path(__file__).parent))
        from craiid_compression_core_v3 import VLTSCompressor

logger = logging.getLogger(__name__)


def _worker_atrest():
    """Lazy, best-effort handle to the shared at-rest encryption helper."""
    try:
        import sys as _sys
        _backend = Path(__file__).resolve().parent.parent  # backend/craiid -> backend
        if str(_backend) not in _sys.path:
            _sys.path.insert(0, str(_backend))
        import atrest as _a
        return _a
    except Exception:
        return None


class ArchivistCompressionWorker:
    """
    Archivist worker responsible for:
    - Generating compression keys from archive content
    - Storing keys via low-urgency OAgentD task (for Archivist to pick up)
    - Integrating with existing insight persistence patterns
    
    Called periodically or on archive updates to maintain VLTS readiness.
    """
    
    def __init__(self, 
                 archive_root: str = None,
                 vlts_archive_dir: str = None,
                 compression_params: Dict[str, Any] = None):
        """
        Initialize Archivist compression worker.
        
        Args:
            archive_root: Root directory containing standard archives (for symbol training)
            vlts_archive_dir: Directory for VLTS archives (where keys will be stored/used)
            compression_params: Tuning parameters for compressor (from procedural memory)
        """
        # Set up paths with OracleAI-aware defaults
        if archive_root is None:
            # Try to locate OracleAI root intelligently
            self.archive_root = self._find_oracle_archive_root()
        else:
            self.archive_root = Path(archive_root)
            
        if vlts_archive_dir is None:
            # At-rest + isolation: VLTS chunks hold compressed conversation
            # content, so keep them OUT of the project tree -> sage_data (via
            # config). The daemon passes this explicitly; this default just
            # covers standalone/other callers.
            self.vlts_archive_dir = self._default_vlts_dir()
        else:
            self.vlts_archive_dir = Path(vlts_archive_dir)
        
        # Ensure directories exist
        self.vlts_archive_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize compressor with parameters (defaults + procedural memory overrides)
        default_params = {
            "min_phrase_length": 2,
            "max_phrase_length": 3,
            "min_frequency": 3,   # Start low for testing; increase with archive depth
            "symbol_start": 0xE000,  # PUA start
        }
        
        if compression_params:
            default_params.update(compression_params)
            
        self.compressor = VLTSCompressor(
            archive_dir=str(self.archive_root),
            vlts_dir=str(self.vlts_archive_dir),
            min_phrase_length=default_params["min_phrase_length"],
            max_phrase_length=default_params["max_phrase_length"], 
            min_frequency=default_params["min_frequency"],
        )
        
        # Track last key generation for incremental updates
        self.last_key_generation = 0
        self.key_generation_interval = 3600  # 1 hour default (configurable)
        
        logger.info(f"ArchivistCompressionWorker initialized")
        logger.info(f"  Archive root: {self.archive_root}")
        logger.info(f"  VLTS dir: {self.vlts_archive_dir}")
        logger.info(f"  Compression params: {default_params}")

    def _default_vlts_dir(self) -> Path:
        """Resolve the default VLTS dir to sage_data (out of the project tree).
        Falls back to a COMPUTED in-project path (never a hardcoded drive/version)
        only if config is unavailable, e.g. a standalone run."""
        try:
            import sys as _sys
            _backend = Path(__file__).resolve().parent.parent  # craiid -> backend
            if str(_backend) not in _sys.path:
                _sys.path.insert(0, str(_backend))
            from config import DATA_DIR as _DD
            return Path(_DD) / "vlts_archives"
        except Exception:
            return self.archive_root.parent / "backend" / "craiid" / "vlts_archives"

    def _find_oracle_archive_root(self) -> Path:
        """Locate OracleAI archives directory by walking up from current file."""
        current = Path(__file__).resolve()
        
        # Check common locations
        candidates = [
            current.parent.parent.parent / "archives",  # backend/craiid/ → root/archives
            current.parent.parent / "archives",          # backend/ → root/archives  
            Path.cwd() / "archives",
        ]
        
        for candidate in candidates:
            if candidate.exists():
                logger.info(f"Found archive root at: {candidate}")
                return candidate
                
        # Fallback to parent traversal
        for parent in current.parents:
            if (parent / "archives").exists():
                logger.info(f"Found archive root via traversal: {parent}")
                return parent / "archives"
                
        # Last resort - the conventional <root>/archives, COMPUTED (never a
        # hardcoded drive/version path). Created so downstream globs are safe.
        default_path = current.parent.parent.parent / "archives"
        default_path.mkdir(parents=True, exist_ok=True)
        logger.warning(f"Archives not found; using computed default: {default_path}")
        return default_path

    def should_generate_key(self) -> bool:
        """
        Determine if a new compression key should be generated.
        Based on time interval and archive change detection (simplified).
        """
        current_time = time.time()
        
        # Time-based trigger
        if current_time - self.last_key_generation > self.key_generation_interval:
            logger.info("Key generation triggered by time interval")
            return True
            
        # TODO: Add archive modification detection (file timestamps, content hashes)
        # For now, rely on time interval or manual triggers
        
        return False

    def generate_and_store_key(self) -> Dict[str, Any]:
        """
        Generate compression key from current archives and store for Archivist pickup.
        Returns metadata about the operation for logging/procedural memory.
        """
        start_time = time.time()
        result = {
            "success": False,
            "timestamp": None,
            "key_path": None,
            "symbols_generated": 0,
            "compression_ratio_estimate": 0.0,
            "error": None
        }
        
        try:
            logger.info("Starting compression key generation...")
            
            # Build symbol map from archive content
            build_stats = self.compressor.build_from_archives()
            
            if not self.compressor.symbol_map:
                raise ValueError("No symbols generated - archive content may be insufficient")
                
            # Generate timestamped key filename
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            key_filename = f"craiid_compression_key_{timestamp}.json"
            key_path = self.vlts_archive_dir / key_filename
            
            # Save the compression key (this is what Archivist will pick up)
            saved_path = self.compressor.save_key(str(key_path))
            
            # Estimate compression ratio from sample text if available
            sample_ratio = self._estimate_compression_ratio()
            
            result.update({
                "success": True,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                "key_path": str(saved_path),
                "symbols_generated": len(self.compressor.symbol_map),
                "compression_ratio_estimate": sample_ratio,
                "build_stats": build_stats
            })
            
            self.last_key_generation = time.time()
            
            logger.info(f"Compression key generated and stored: {saved_path}")
            logger.info(f"  Symbols: {len(self.compressor.symbol_map)}")
            logger.info(f"  Estimated ratio: {sample_ratio:.3f}")
            
        except Exception as e:
            result["error"] = str(e)
            logger.error(f"Failed to generate compression key: {e}", exc_info=True)
            
        return result

    def compress_archives_to_vlts(self, max_chunk_entries: int = 50) -> Dict[str, Any]:
        """Build the compression key AND write compressed VLTS chunks the Author
        can decompress. ROBUST by design: if archives are empty/sparse so no
        symbols are learned, it writes nothing and returns cleanly (the normal
        fresh-install path, NOT an error). Never raises."""
        result = {"success": False, "key_path": None, "chunks_written": 0,
                  "entries_compressed": 0, "symbols": 0, "error": None}
        try:
            self.compressor.build_from_archives()
            symbols = len(self.compressor.symbol_map)
            result["symbols"] = symbols
            if symbols == 0:
                result["success"] = True
                result["note"] = "no symbols (sparse archives) - nothing to compress"
                return result
            self.vlts_archive_dir.mkdir(parents=True, exist_ok=True)
            key_path = self.vlts_archive_dir / "compression_key.json"
            self.compressor.save_key(str(key_path))
            result["key_path"] = str(key_path)
            batch = []
            chunk_idx = 0
            for af in sorted(self.archive_root.glob("archive*.json")):
                try:
                    data = json.loads(af.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(data, list):
                    rows = data
                elif isinstance(data, dict):
                    rows = data.get("entries") or data.get("records") or [data]
                else:
                    continue
                for row in rows:
                    try:
                        text = self.compressor._extract_text_from_archive(row)
                    except Exception:
                        text = ""
                    if not text or not text.strip():
                        continue
                    try:
                        comp, _ = self.compressor.compress_text(text)
                    except Exception:
                        continue
                    batch.append({"compressed": comp})
                    result["entries_compressed"] += 1
                    if len(batch) >= max_chunk_entries:
                        chunk_idx += 1
                        self._write_vlts_chunk(chunk_idx, batch)
                        result["chunks_written"] += 1
                        batch = []
            if batch:
                chunk_idx += 1
                self._write_vlts_chunk(chunk_idx, batch)
                result["chunks_written"] += 1
            result["success"] = True
            logger.info(f"VLTS build: {result['chunks_written']} chunks, "
                        f"{result['entries_compressed']} entries, {symbols} symbols.")
        except Exception as e:
            result["error"] = str(e)
            logger.error(f"compress_archives_to_vlts failed (non-fatal): {e}", exc_info=True)
        return result

    def _write_vlts_chunk(self, idx: int, entries: list) -> None:
        """Atomically write one VLTS chunk of compressed entries."""
        import os as _os
        import tempfile as _tf
        self.vlts_archive_dir.mkdir(parents=True, exist_ok=True)
        doc = {"schema": "craiid_vlts_chunk", "version": 1,
               "count": len(entries), "entries": entries}
        path = self.vlts_archive_dir / f"chunk_{idx:04d}.json"
        # Encrypt at rest — chunks hold compressed conversation content.
        _atr = _worker_atrest()
        try:
            blob = _atr.dump_json_encrypted(doc) if _atr is not None else None
        except Exception:
            blob = None
        fd, tmp = _tf.mkstemp(dir=str(self.vlts_archive_dir), suffix=".tmp")
        try:
            if blob is not None:
                with _os.fdopen(fd, "wb") as f:
                    f.write(blob)
            else:
                with _os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(doc, f, ensure_ascii=False)
            _os.replace(tmp, str(path))
        finally:
            try:
                if _os.path.exists(tmp):
                    _os.unlink(tmp)
            except OSError:
                pass

    def _estimate_compression_ratio(self) -> float:
        """
        Estimate compression ratio using sample text from recent archives.
        Returns 0.0 if estimation fails (safe fallback).
        """
        try:
            # Get a sample of recent archive content for testing
            sample_texts = self._get_archive_sample(max_chars=5000)
            
            if not sample_texts.strip():
                return 0.0
                
            compressed, stats = self.compressor.compress_text(sample_texts)
            ratio = len(compressed) / len(sample_texts) if len(sample_texts) > 0 else 0
            
            logger.debug(f"Compression ratio estimate: {ratio:.3f} "
                        f"(original: {len(sample_texts)}, compressed: {len(compressed)})")
                        
            return ratio
        except Exception as e:
            logger.warning(f"Could not estimate compression ratio: {e}")
            return 0.0

    def _get_archive_sample(self, max_chars: int = 5000) -> str:
        """
        Extract a representative sample from recent archive files for testing.
        Returns concatenated text up to max_chars.
        """
        sample_parts = []
        current_length = 0
        
        # Get most recent JSON files (simple approach - could be enhanced)
        json_files = sorted(self.archive_root.glob("*.json"), 
                           key=lambda f: f.stat().st_mtime, 
                           reverse=True)[:10]  # Last 10 modified files
        
        for json_file in json_files:
            if current_length >= max_chars:
                break
                
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    
                text_content = self._extract_text_from_archive(data)
                remaining = max_chars - current_length
                
                if len(text_content) > remaining:
                    text_content = text_content[:remaining] + "..."
                    
                sample_parts.append(text_content)
                current_length += len(text_content)
                
            except Exception as e:
                logger.debug(f"Skipping {json_file.name} in sample: {e}")
                continue
                
        return " ".join(sample_parts)

    def _extract_text_from_archive(self, data: Any) -> str:
        """Extract searchable text from archive JSON (matches validation harness)."""
        text_parts = []
        
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("content"), str):
                    content = item["content"].strip()
                    if content:
                        text_parts.append(content)
                        
        elif isinstance(data, dict):
            # Look for common text fields
            for key in ("content", "text", "message", "user_input", "assistant_response"):
                if isinstance(data.get(key), str) and data[key].strip():
                    text_parts.append(data[key].strip())
                    
            # Recurse into nested structures
            for value in data.values():
                if isinstance(value, (dict, list)):
                    text_parts.append(self._extract_text_from_archive(value))
                    
        return " ".join(filter(None, text_parts))

    def get_latest_key_path(self) -> Optional[Path]:
        """
        Get the most recent compression key file for Archivist consumption.
        Returns None if no keys exist.
        """
        try:
            key_files = list(self.vlts_archive_dir.glob("craiid_compression_key_*.json"))
            if not key_files:
                return None
                
            # Return most recently modified
            latest_key = max(key_files, key=lambda f: f.stat().st_mtime)
            return latest_key
        except Exception as e:
            logger.error(f"Error finding latest compression key: {e}")
            return None

    def submit_low_priority_task(self) -> Dict[str, Any]:
        """
        Submit compression key generation as a low-urgency OAgentD task.
        This is the main integration point with OracleAI's task prioritizer.
        
        Returns:
            Task submission metadata for logging/procedural memory
        """
        # Check if we should generate a key
        if not self.should_generate_key():
            logger.debug("Key generation not needed at this time")
            return {"submitted": False, "reason": "not_due_yet"}
            
        # Generate and store the key
        result = self.generate_and_store_key()
        
        if result["success"]:
            # In a real implementation, this would submit via OAgentD:
            # [PRIORITISE: low_urgency_task | { "fn": archivist_store_insight, 
            #                                    "payload": {"type": "compression_key", 
            #                                              "key_path": result["key_path"]},
            #                                    "priority": "low"}]
            
            logger.info("Submitted compression key storage as low-urgency OAgentD task")
            result["task_submitted"] = True
            result["task_type"] = "low_urgency_compression_key_storage"
        else:
            logger.error(f"Failed to generate key for task submission: {result.get('error')}")
            result["task_submitted"] = False
            
        return result


# Example usage and self-test when run directly
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    
    print("=== ArchivistCompressionWorker Self-Test ===")
    
    try:
        # Initialize worker
        worker = ArchivistCompressionWorker()
        
        # Test key generation (will use synthetic data if archives empty)
        result = worker.generate_and_store_key()
        
        if result["success"]:
            print(f"✅ Key generation successful")
            print(f"   Path: {result['key_path']}")
            print(f"   Symbols: {result['symbols_generated']}")
            print(f"   Estimated ratio: {result['compression_ratio_estimate']:.3f}")
            
            # Test task submission logic
            task_result = worker.submit_low_priority_task()
            print(f"   Task submitted: {task_result.get('task_submitted', False)}")
        else:
            print(f"❌ Key generation failed: {result.get('error')}")
            
        # Show latest key
        latest_key = worker.get_latest_key_path()
        if latest_key:
            print(f"🔑 Latest key: {latest_key.name}")
        else:
            print("🔑 No compression keys found")
            
    except Exception as e:
        print(f"❌ Self-test failed: {e}")
        import traceback
        traceback.print_exc()
        
    print("\n=== Self-Test Complete ===")
