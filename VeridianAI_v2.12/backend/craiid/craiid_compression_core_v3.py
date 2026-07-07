# craiid_compression_core_v3.py
"""
CRAIID Compression Core v3
Merges v1 (PUA symbols, lossless decompression algorithm) with
v2 (archive integration, ops biasing, CRAIID path awareness).
All known bugs fixed.
"""

import json
import os
import re
import datetime
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any


# --- At-rest encryption helper (lazy, distribution-safe) ---------------------
# The compression key holds the symbol map derived from real conversation
# archives, so it is encrypted at rest via the shared `atrest` Fernet helper.
# Best-effort: if atrest is unavailable (standalone use) we fall back to
# plaintext so the compressor never hard-crashes. Reads use read_file_auto,
# which transparently handles BOTH encrypted and legacy-plaintext files.
_ATREST = None
_ATREST_TRIED = False


def _get_atrest():
    global _ATREST, _ATREST_TRIED
    if _ATREST_TRIED:
        return _ATREST
    _ATREST_TRIED = True
    try:
        import sys as _sys
        _backend = Path(__file__).resolve().parent.parent  # backend/craiid -> backend
        if str(_backend) not in _sys.path:
            _sys.path.insert(0, str(_backend))
        import atrest as _a
        _ATREST = _a
    except Exception:
        _ATREST = None
    return _ATREST


class VLTSCompressor:
    """
    Very Long Term Storage compression for CRAIID archives.

    Analyzes chat archives to identify recurring phrases and replaces
    them with Unicode Private Use Area (PUA) symbols — single characters
    that cannot collide with real text content.

    Granularity evolves naturally with archive depth:
      - Early/short archives  → per-chunk frequency analysis
      - Week-scale archives   → per-session symbol mapping
      - Month+ archives       → cross-session shared dictionary (PLM-informed)
    """

    # Unicode Private Use Area: U+E000 to U+F8FF (6,400 slots)
    PUA_START = 0xE000
    PUA_END   = 0xF8FF

    def __init__(
        self,
        archive_dir: str = None,
        vlts_dir: str = None,
        min_phrase_length: int = 2,
        max_phrase_length: int = 5,
        min_frequency: int = 3,
        ops_lexicon_path: str = None,
    ):
        """
        Args:
            archive_dir:        Path to standard archives (corpus for training).
            vlts_dir:           Path to VLTS archives (targets for compression).
            min_phrase_length:  Minimum word count in phrases to consider.
            max_phrase_length:  Maximum word count in phrases to consider.
            min_frequency:      Minimum occurrences required to earn a symbol.
            ops_lexicon_path:   Optional path to ops lexicon for frequency biasing.
        """
        # --- Path resolution (drive/version agnostic) ---
        root = Path(__file__).parent.parent.parent  # backend/craiid/ → root

        self.archive_dir = Path(archive_dir) if archive_dir else root / "archives"
        self.vlts_dir    = Path(vlts_dir)    if vlts_dir    else Path(__file__).parent / "vlts_archives"

        # --- Compression parameters ---
        self.min_phrase_length = min_phrase_length
        self.max_phrase_length = max_phrase_length
        self.min_frequency     = min_frequency

        # --- Internal state ---
        self.phrase_freq: Counter        = Counter()
        self.symbol_map:  Dict[str, str] = {}   # phrase  → PUA symbol
        self.reverse_map: Dict[str, str] = {}   # symbol  → phrase
        self.next_symbol_code: int       = self.PUA_START
        self.ops_terms: set              = set()

        # --- Ops lexicon (optional biasing) ---
        resolved_lexicon = ops_lexicon_path or str(Path(__file__).parent / "ops_lexicon.txt")
        self._load_ops_lexicon(resolved_lexicon)

    # ------------------------------------------------------------------ #
    #  Ops lexicon                                                         #
    # ------------------------------------------------------------------ #

    def _load_ops_lexicon(self, path: str) -> None:
        """Load journalist ops terms to bias compression toward technical phrases."""
        try:
            if not os.path.exists(path):
                return  # Silent — lexicon is optional
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content.startswith("[") or content.startswith("{"):
                data = json.loads(content)
                self.ops_terms = set(data if isinstance(data, list) else data.get("terms", []))
            else:
                self.ops_terms = {line.strip() for line in content.splitlines() if line.strip()}
            print(f"[INFO] Loaded {len(self.ops_terms)} ops terms from {path}")
        except Exception as e:
            print(f"[WARN] Could not load ops lexicon: {e}")

    # ------------------------------------------------------------------ #
    #  Text extraction                                                     #
    # ------------------------------------------------------------------ #

    def _extract_text_from_archive(self, data: Any) -> str:
        """
        Recursively extract searchable text from various archive JSON shapes.
        Handles: list-of-turns, metadata wrappers, nested dicts.
        """
        parts: List[str] = []

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    for key in ("content", "text", "message", "user_input", "assistant_response"):
                        if key in item and isinstance(item[key], str):
                            parts.append(item[key])
                            break
        elif isinstance(data, dict):
            for key in ("content", "text", "message", "user_input", "assistant_response"):
                if key in data and isinstance(data[key], str):
                    parts.append(data[key])
            for value in data.values():
                if isinstance(value, (dict, list)):
                    parts.append(self._extract_text_from_archive(value))

        return " ".join(filter(None, parts))

    # ------------------------------------------------------------------ #
    #  Corpus analysis                                                     #
    # ------------------------------------------------------------------ #

    def _tokenize(self, text: str) -> List[str]:
        """Whitespace tokenization preserving punctuation attachment."""
        return re.findall(r"\S+", text)

    def _iter_ngrams(self, tokens: List[str]):
        """Yield all n-gram strings within phrase length bounds."""
        n_tokens = len(tokens)
        for start in range(n_tokens):
            for length in range(
                self.min_phrase_length,
                min(self.max_phrase_length + 1, n_tokens - start + 1),
            ):
                yield " ".join(tokens[start : start + length])

    def analyze_corpus(self, texts: List[str]) -> Dict:
        """
        Build compression dictionary from a list of text strings.
        Call this when you have texts in hand (e.g. from chat_memory).
        For archive-directory scanning use build_from_archives().
        """
        self._reset_state()

        for text in texts:
            tokens = self._tokenize(text)
            for phrase in self._iter_ngrams(tokens):
                self.phrase_freq[phrase] += 1

        return self._build_symbol_map()

    def build_from_archives(self) -> Dict:
        """
        Scan archive_dir for JSON files and build compression dictionary.
        This is the primary entry point for CRAIID integration.
        """
        self._reset_state()

        if not self.archive_dir.exists():
            print(f"[WARN] Archive directory not found: {self.archive_dir}")
            return self._build_symbol_map()

        file_count = 0
        for json_file in self.archive_dir.glob("*.json"):
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                text = self._extract_text_from_archive(data)
                if text:
                    tokens = self._tokenize(text)
                    for phrase in self._iter_ngrams(tokens):
                        self.phrase_freq[phrase] += 1
                file_count += 1
            except Exception as e:
                print(f"[WARN] Skipping {json_file.name}: {e}")

        print(f"[INFO] Scanned {file_count} archive files from {self.archive_dir}")
        return self._build_symbol_map()

    def _reset_state(self) -> None:
        self.phrase_freq.clear()
        self.symbol_map.clear()
        self.reverse_map.clear()
        self.next_symbol_code = self.PUA_START

    def _build_symbol_map(self) -> Dict:
        """
        Filter phrase_freq by min_frequency, apply ops biasing,
        assign PUA symbols, return stats dict.
        """
        # Filter
        candidates = [
            (phrase, freq)
            for phrase, freq in self.phrase_freq.items()
            if freq >= self.min_frequency
        ]

        # Ops biasing — boost effective frequency for phrases containing ops terms
        if self.ops_terms:
            def boosted(phrase, freq):
                hits = sum(1 for t in self.ops_terms if t in phrase.lower())
                return freq * (1 + hits * 0.5)
            candidates = [(p, boosted(p, f)) for p, f in candidates]

        # Sort descending by (boosted) frequency
        candidates.sort(key=lambda x: x, reverse=True)

        # Assign PUA symbols
        for phrase, _ in candidates:
            if self.next_symbol_code > self.PUA_END:
                print(f"[WARN] PUA symbol space exhausted at {self.PUA_END:#06x}. "
                      f"Stopping at {len(self.symbol_map)} symbols.")
                break
            symbol = chr(self.next_symbol_code)
            self.symbol_map[phrase] = symbol
            self.reverse_map[symbol] = phrase
            self.next_symbol_code += 1

        stats = {
            "total_phrases_analyzed": len(self.phrase_freq),
            "frequent_phrases_found": len(candidates),
            "symbols_generated":      len(self.symbol_map),
            "min_frequency":          self.min_frequency,
            "phrase_length_range":    f"{self.min_phrase_length}–{self.max_phrase_length}",
            "pua_slots_remaining":    self.PUA_END - self.next_symbol_code + 1,
        }
        print(f"[INFO] Symbol map built: {stats['symbols_generated']} symbols "
              f"({stats['pua_slots_remaining']} PUA slots remaining)")
        return stats

    # ------------------------------------------------------------------ #
    #  Compress / Decompress                                               #
    # ------------------------------------------------------------------ #

    def compress_text(self, text: str) -> Tuple[str, Dict]:
        """
        Compress text using greedy longest-match against the symbol map.
        Returns (compressed_text, stats).
        """
        if not self.symbol_map:
            raise ValueError("No symbol map. Call analyze_corpus() or build_from_archives() first.")

        tokens = self._tokenize(text)
        if not tokens:
            return text, {"original_tokens": 0, "compressed_tokens": 0, "tokens_saved": 0}

        result_tokens: List[str] = []
        replacements = 0
        i = 0

        while i < len(tokens):
            matched = False
            # Try longest phrase first at this position
            for length in range(self.max_phrase_length, self.min_phrase_length - 1, -1):
                if i + length > len(tokens):
                    continue
                phrase = " ".join(tokens[i : i + length])
                if phrase in self.symbol_map:
                    result_tokens.append(self.symbol_map[phrase])
                    i += length
                    replacements += 1
                    matched = True
                    break
            if not matched:
                result_tokens.append(tokens[i])
                i += 1

        compressed = " ".join(result_tokens)
        stats = {
            "original_tokens":        len(tokens),
            "compressed_tokens":      len(result_tokens),
            "tokens_saved":           len(tokens) - len(result_tokens),
            "token_compression_ratio": (len(tokens) - len(result_tokens)) / max(len(tokens), 1),
            "replacements_made":      replacements,
        }
        return compressed, stats

    def decompress_text(self, compressed_text: str) -> str:
        """
        Decompress text via direct single-character PUA symbol replacement.

        Uses str.replace on each PUA symbol — safe because PUA characters
        cannot appear in normal text content, so there is zero collision risk
        and no need for tokenization or space-padding.
        """
        if not self.reverse_map:
            raise ValueError("No symbol map loaded.")
        result = compressed_text
        for symbol, phrase in self.reverse_map.items():
            result = result.replace(symbol, phrase)
        return result

    def verify_round_trip(self, text: str) -> Tuple[bool, Dict]:
        """
        Compress then decompress text and verify bit-for-bit equality.
        Returns (success, stats).
        """
        compressed, stats = self.compress_text(text)
        decompressed = self.decompress_text(compressed)
        success = text == decompressed

        stats.update({
            "original_chars":    len(text),
            "compressed_chars":  len(compressed),
            "char_ratio":        len(compressed) / max(len(text), 1),
            "space_saved_pct":   (1 - len(compressed) / max(len(text), 1)) * 100,
            "lossless":          success,
        })

        if not success:
            # Surface first divergence for debugging
            for i, (a, b) in enumerate(zip(text, decompressed)):
                if a != b:
                    stats["first_diff_pos"]      = i
                    stats["first_diff_original"] = repr(a)
                    stats["first_diff_result"]   = repr(b)
                    break
            else:
                stats["length_mismatch"] = f"{len(text)} vs {len(decompressed)}"

        return success, stats

    # ------------------------------------------------------------------ #
    #  Key serialization                                                   #
    # ------------------------------------------------------------------ #

    def save_key(self, filepath: str = None) -> str:
        """
        Serialize compression dictionary to JSON.
        Called by the Archivist worker via low-urgency OAgentD task.
        Stored alongside VLTS archives for Author reconstruction use.
        """
        if filepath is None:
            self.vlts_dir.mkdir(parents=True, exist_ok=True)
            filepath = str(self.vlts_dir / "compression_key.json")

        key_data = {
            "schema":        "craiid_compression_key",
            "version":       "3.0.0",
            "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "params": {
                "min_phrase_length": self.min_phrase_length,
                "max_phrase_length": self.max_phrase_length,
                "min_frequency":     self.min_frequency,
                "pua_start":         self.PUA_START,
                "pua_end":           self.PUA_END,
            },
            "symbol_map":  self.symbol_map,
            "reverse_map": self.reverse_map,
            "stats": {
                "symbols_generated": len(self.symbol_map),
                "pua_slots_used":    self.next_symbol_code - self.PUA_START,
                "pua_slots_remaining": self.PUA_END - self.next_symbol_code + 1,
            },
        }

        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        _atr = _get_atrest()
        _wrote_encrypted = False
        if _atr is not None:
            try:
                blob = _atr.dump_json_encrypted(key_data)
                _tmp = filepath + ".tmp"
                with open(_tmp, "wb") as f:
                    f.write(blob)
                os.replace(_tmp, filepath)
                _wrote_encrypted = True
            except Exception as _enc_err:
                print(f"[WARN] key at-rest encryption failed ({_enc_err}); writing plaintext")
        if not _wrote_encrypted:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(key_data, f, ensure_ascii=False, indent=2)

        print(f"[INFO] Compression key saved: {filepath} "
              f"({len(self.symbol_map)} symbols)")
        return filepath

    def load_key(self, filepath: str) -> bool:
        """
        Load compression dictionary from JSON key file.
        Called by Author reconstruction when preparing warm instance chunks.
        Returns True on success, False on failure.
        """
        try:
            _atr = _get_atrest()
            if _atr is not None:
                # read_file_auto transparently decrypts, or returns legacy
                # plaintext bytes unchanged, so old keys still load.
                key_data = json.loads(_atr.read_file_auto(filepath))
            else:
                with open(filepath, "r", encoding="utf-8") as f:
                    key_data = json.load(f)

            # Schema validation
            if key_data.get("schema") != "craiid_compression_key":
                print(f"[ERROR] Invalid key schema: {key_data.get('schema')}")
                return False

            self.symbol_map  = key_data.get("symbol_map",  {})
            self.reverse_map = key_data.get("reverse_map", {})

            # Restore parameters
            params = key_data.get("params", {})
            self.min_phrase_length = params.get("min_phrase_length", self.min_phrase_length)
            self.max_phrase_length = params.get("max_phrase_length", self.max_phrase_length)
            self.min_frequency     = params.get("min_frequency",     self.min_frequency)

            # FIX (v1/v2 bug): restore symbol_start so any subsequent
            # analyze_corpus() call continues from the correct offset
            self.PUA_START        = params.get("pua_start", self.PUA_START)
            self.PUA_END          = params.get("pua_end",   self.PUA_END)
            if self.reverse_map:
                self.next_symbol_code = max(ord(s) for s in self.reverse_map) + 1
            else:
                self.next_symbol_code = self.PUA_START

            print(f"[INFO] Compression key loaded: {filepath} "
                  f"({len(self.symbol_map)} symbols, "
                  f"version {key_data.get('version', 'unknown')})")
            return True

        except Exception as e:
            print(f"[ERROR] Failed to load compression key: {e}")
            return False


# ------------------------------------------------------------------ #
#  Factory                                                             #
# ------------------------------------------------------------------ #

def create_default_compressor() -> VLTSCompressor:
    """
    Factory function for easy instantiation with CRAIID defaults.
    Searches common lexicon locations — silent if none found.
    """
    possible_lexicons = [
        Path(__file__).parent / "ops_lexicon.txt",
        Path(__file__).parent.parent / "downloads" / "ops_lexicon.txt",
        Path("E:/OracleAI_v2.3/backend/craiid/ops_lexicon.txt"),
    ]
    ops_lexicon_path = next((str(p) for p in possible_lexicons if p.exists()), None)

    return VLTSCompressor(
        min_phrase_length=2,
        max_phrase_length=5,
        min_frequency=3,
        ops_lexicon_path=ops_lexicon_path,
    )


# ------------------------------------------------------------------ #
#  Self-test                                                           #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    import tempfile

    print("=== CRAIID Compression Core v3 — Self-Test ===\n")

    # Representative ops-mode chat turns
    sample_texts = [
        "Hey Sage, please run verify_file on the latest archive",
        "Let's check daemon status and then consolidate_now",
        "Please run digest now and verify_chain",
        "We need to check daemon status, run digest and verify_file now",
        "Hey Sage, run verify_file on the latest archive",
        "Let's check daemon status please",
        "Please run digest now",
        "We need to verify_chain and consolidate_now",
        "Author reconstruction needs to pull relevant chunks from VLTS archives",
        "Archivist worker should store insights via low priority OAgentD task",
        "Context fatigue is increasing, should trigger warm instance preparation",
        "Author reconstruction needs to pull relevant chunks from VLTS archives",
        "Archivist worker should store insights via low priority OAgentD task",
    ]

    compressor = create_default_compressor()

    # --- Build symbol map ---
    print("Building symbol map from sample corpus...")
    stats = compressor.analyze_corpus(sample_texts)
    print(f"  Phrases analyzed : {stats['total_phrases_analyzed']}")
    print(f"  Symbols generated: {stats['symbols_generated']}")
    print(f"  PUA slots remaining: {stats['pua_slots_remaining']}\n")

    if not compressor.symbol_map:
        print("[INFO] No symbols generated with current thresholds — "
              "expected with small corpus. Lowering min_frequency to 2 for demo.")
        compressor.min_frequency = 2
        stats = compressor.analyze_corpus(sample_texts)
        print(f"  Symbols generated (min_freq=2): {stats['symbols_generated']}\n")

    # --- Show top mappings ---
    if compressor.symbol_map:
        print("Top symbol mappings:")
        top = sorted(compressor.symbol_map.items(), key=lambda x: len(x), reverse=True)[:5]
        for phrase, symbol in top:
            freq = compressor.phrase_freq.get(phrase, "?")
            print(f"  {repr(symbol)} (U+{ord(symbol):04X}) ← '{phrase}' (freq: {freq})")
        print()

    # --- Round-trip verification on every sample ---
    print("Round-trip verification:")
    all_passed = True
    total_orig = total_comp = 0

    for i, text in enumerate(sample_texts):
        success, vstats = compressor.verify_round_trip(text)
        total_orig += vstats["original_chars"]
        total_comp += vstats["compressed_chars"]
        status = "✅" if success else "❌"
        print(f"  [{status}] Text {i+1:02d}: "
              f"{vstats['original_chars']} → {vstats['compressed_chars']} chars "
              f"({vstats['space_saved_pct']:.1f}% saved) "
              f"| {vstats['replacements_made']} replacements")
        if not success:
            all_passed = False
            if "first_diff_pos" in vstats:
                print(f"       First diff @ pos {vstats['first_diff_pos']}: "
                      f"{vstats['first_diff_original']} vs {vstats['first_diff_result']}")

    overall_ratio = total_comp / max(total_orig, 1)
    print(f"\nOverall: {total_orig} → {total_comp} chars "
          f"({(1 - overall_ratio) * 100:.1f}% saved)")
    print(f"All lossless: {'✅ YES' if all_passed else '❌ NO'}\n")

    # --- Key save / load round-trip ---
    print("Key serialization test...")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        key_path = f.name

    try:
        compressor.save_key(key_path)

        compressor2 = VLTSCompressor()
        loaded = compressor2.load_key(key_path)
        assert loaded, "load_key returned False"

        # Verify loaded compressor produces identical output
        test = sample_texts
        c1, _ = compressor.compress_text(test)
        c2, _ = compressor2.compress_text(test)
        assert c1 == c2, f"Compressed output mismatch after key reload:\n  {c1}\n  {c2}"

        d2 = compressor2.decompress_text(c2)
        assert d2 == test, "Decompression failed after key reload"

        print("  ✅ save_key / load_key round-trip verified\n")

    finally:
        os.unlink(key_path)

    print("=== Self-Test Complete ===")
    print(f"Status: {'✅ ALL PASS' if all_passed else '❌ FAILURES — check output above'}")