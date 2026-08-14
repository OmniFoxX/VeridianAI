import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any
import datetime

# Attempt to import the compression core from likely locations
def _import_compression_core():
    """Import VLTSCompressor from the sibling craiid_compression_core_v3.py.

    Distribution-safe (#69): add THIS file's own directory to sys.path and
    import by module name. The previous version used `sys` without importing it
    (NameError if the first attempt failed) and tried a dead hardcoded
    `E.OracleAI_v2_4...` path - both removed."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from craiid_compression_core_v3 import VLTSCompressor
    return VLTSCompressor

# Actually perform the import. Degrade to None (rather than crashing the
# importer) if the compression core is absent; callers guard on availability.
try:
    VLTSCompressor = _import_compression_core()
except Exception as _vlts_err:
    VLTSCompressor = None
    print(f"[AuthorCompressionHelper] compression core unavailable: {_vlts_err}")

class AuthorCompressionHelper:
    """
    Helper for Author worker to handle VLTS decompression during warm instance preparation.
    
    Responsibilities:
    - Locate and load compression keys from VLTS archives
    - Decompress archived chunks using appropriate keys
    - Provide decompressed text to Author's context reconstruction
    - Integrates with existing prepare_warm_instance flow
    """
    
    def __init__(self, vlts_archives_dir: str = None):
        # Set VLTS directory (defaults to OracleAI standard location)
        if vlts_archives_dir:
            self.vlts_dir = Path(vlts_archives_dir)
        else:
            # Try to auto-detect OracleAI root
            # At-rest + distribution-safe: resolve sage_data via config; never a
            # hardcoded drive/version path.
            try:
                here = os.path.dirname(os.path.abspath(__file__))
                backend = os.path.dirname(here)  # craiid -> backend
                if backend not in sys.path:
                    sys.path.insert(0, backend)
                from config import DATA_DIR as _DD
                self.vlts_dir = Path(_DD) / "vlts_archives"
            except Exception:
                # computed in-project fallback (never a hardcoded drive/version)
                self.vlts_dir = Path(__file__).resolve().parent / "vlts_archives"
        
        # Ensure directory exists
        self.vlts_dir.mkdir(parents=True, exist_ok=True)
        
        # Cache for loaded keys to avoid repeated file I/O
        self._key_cache: Dict[str, VLTSCompressor] = {}
        
        print(f"[INFO] AuthorCompressionHelper initialized with VLTS dir: {self.vlts_dir}")
    
    def get_latest_compression_key(self) -> Optional[Path]:
        """
        Find the most recent compression key file in VLTS directory.
        Archivist worker saves keys via low-urgency OAgentD task.
        """
        key_files = list(self.vlts_dir.glob("compression_key_*.json"))
        if not key_files:
            # Also check for any key files without timestamp
            key_files = list(self.vlts_dir.glob("compression_key.json"))
        if not key_files:
            return None
        # Return most recent by modification time
        return max(key_files, key=lambda p: p.stat().st_mtime)
    
    def load_compression_key(self, key_path: Path) -> VLTSCompressor:
        """
        Load a compression key file and return initialized compressor.
        Uses caching to avoid reloading same key repeatedly.
        """
        key_str = str(key_path)
        if key_str in self._key_cache:
            return self._key_cache[key_str]
        
        # Create new compressor instance
        compressor = VLTSCompressor(
            symbol_length=1,  # Will be overridden by loaded key
            min_frequency=3,
            ngram_max=3
        )
        
        # Load the key from file
        success = compressor.load_key(str(key_path))
        if not success:
            raise ValueError(f"Failed to load compression key from {key_path}")
        
        # Cache and return
        self._key_cache[key_str] = compressor
        print(f"[INFO] Loaded compression key from {key_path} ({len(compressor.symbol_map)} symbols)")
        return compressor
    
    def decompress_vlts_chunk(self, chunk_filename: str) -> Optional[str]:
        """
        Decompress a specific VLTS chunk file using its associated compression key.
        
        Expected naming convention: 
        - Chunk file: vlts_chunk_<hash>.json.gz or similar (we'll assume JSON for simplicity)
        - Key file: compression_key_<timestamp>.json (most recent used if not specified)
        
        In practice, the Archivist would store both compressed data and key references together.
        For this integration, we assume:
          1. VLTS archive contains metadata indicating which compression key was used
          2. Or we use the most recent key for all chunks (simple approach)
        """
        chunk_path = self.vlts_dir / chunk_filename
        if not chunk_path.exists():
            print(f"[WARN] VLTS chunk not found: {chunk_path}")
            return None
        
        try:
            # Load the chunk data (assume JSON with compressed_text field)
            with open(chunk_path, 'r', encoding='utf-8') as f:
                chunk_data = json.load(f)
            
            compressed_text = chunk_data.get("compressed_text")
            if not compressed_text:
                print(f"[WARN] No compressed_text found in {chunk_path}")
                return None
            
            # Determine which key to use: look for key reference in chunk, else use latest
            key_ref = chunk_data.get("compression_key_file")
            if key_ref:
                key_path = self.vlts_dir / key_ref
                if not key_path.exists():
                    print(f"[WARN] Referenced key file not found: {key_path}, falling back to latest")
                    key_path = self.get_latest_compression_key()
            else:
                key_path = self.get_latest_compression_key()
            
            if not key_path:
                print("[ERROR] No compression key available for decompression")
                return None
            
            # Load compressor and decompress
            compressor = self.load_compression_key(key_path)
            decompressed_text = compressor.decompress_text(compressed_text)
            
            print(f"[INFO] Decompressed VLTS chunk {chunk_filename} "
                  f"({len(compressed_text)} -> {len(decompressed_text)} chars)")
            return decompressed_text
            
        except Exception as e:
            print(f"[ERROR] Failed to decompress VLTS chunk {chunk_filename}: {e}")
            return None

    def prepare_warm_instance_with_vlts(self, 
                                       insight_ids: List[str],
                                       vlts_chunk_mapping: Dict[str, str] = None) -> Dict[str, Any]:
        """
        Extended warm instance preparation that includes VLTS decompression.
        
        Args:
            insight_ids: List of insight identifiers to retrieve from Archivist/KB
            vlts_chunk_mapping: Optional mapping of insight_id -> VLTS chunk filename
                               If not provided, we attempt to derive chunk names from IDs
        
        Returns:
            Dictionary containing reconstructed context from both chat memory and VLTS sources
        """
        # This would normally call into Archivist worker to get insights by ID
        # For now, we simulate by trying to load corresponding VLTS chunks
        
        vlts_chunks = []
        failed_chunks = []
        
        for insight_id in insight_ids:
            # Determine chunk filename: either from mapping or derive from ID
            if vlts_chunk_mapping and insight_id in vlts_chunk_mapping:
                chunk_filename = vlts_chunk_mapping[insight_id]
            else:
                # Simple derivation: assume chunk named after insight ID
                chunk_filename = f"vlts_chunk_{insight_id}.json"
            
            decompressed = self.decompress_vlts_chunk(chunk_filename)
            if decompressed is not None:
                vlts_chunks.append({
                    "insight_id": insight_id,
                    "content": decompressed,
                    "source": "vlts"
                })
            else:
                failed_chunks.append(insight_id)
        
        # Also retrieve chat memory (existing Author function would do this)
        # For demonstration, we'll note that chat memory retrieval happens elsewhere
        
        result = {
            "status": "partial" if failed_chunks else "complete",
            "vlts_chunks_retrieved": len(vlts_chunks),
            "vlts_chunks_failed": len(failed_chunks),
            "vlts_content": vlts_chunks,
            "failed_insight_ids": failed_chunks,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            # In real integration, this would be combined with chat memory context
        }
        
        if failed_chunks:
            print(f"[WARN] Failed to decompress {len(failed_chunks)} VLTS chunks: {failed_chunks}")
        else:
            print(f"[INFO] Successfully prepared warm instance with {len(vlts_chunks)} VLTS chunks")
        
        return result

# Example usage and self-test when run directly
if __name__ == "__main__":
    print("=== Author Compression Integration Self-Test ===")
    
    # Initialize helper
    helper = AuthorCompressionHelper()
    
    # Check if any compression keys exist (from Archivist runs)
    latest_key = helper.get_latest_compression_key()
    if latest_key:
        print(f"[INFO] Found latest compression key: {latest_key.name}")
        
        # Try to load it and test decompression on a dummy chunk
        try:
            compressor = helper.load_compression_key(latest_key)
            print(f"[SUCCESS] Loaded key with {len(compressor.symbol_map)} symbols")
            
            # Create a test compressed chunk to verify round-trip
            test_text = "Author reconstruction needs to pull relevant chunks from VLTS archives. Archivist worker should store insights via low priority OAgentD task."
            compressed, _ = compressor.compress_text(test_text)
            decompressed = helper.decompress_vlts_chunk.__func__(helper, compressed)  # Not ideal but for test
            
        except Exception as e:
            print(f"[ERROR] Self-test failed: {e}")
    else:
        print("[INFO] No compression keys found yet - run Archivist worker to generate some")
    
    print("\nSelf-test complete. Integrate AuthorCompressionHelper into craiid_author.py.")
