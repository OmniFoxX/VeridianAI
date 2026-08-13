#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gguf_probe.py — read a GGUF file's metadata header and catch the one
inconsistency that silently ruins a chat model.

THE BUG THIS EXISTS FOR
-----------------------
A GGUF carries both a chat template and a declared end-of-sequence token, and
NOTHING checks that they agree. Qwen2.5-Coder-1.5B (base) ships:

    tokenizer.chat_template   ... ends every assistant turn with <|im_end|>
    tokenizer.ggml.eos_token_id = 151643                      (<|endoftext|>)
    <|im_end|>                  = 151645

So the model does exactly the right thing -- it emits <|im_end|> to close its
turn -- and llama-server, which stops on 151643, does not recognise it. Decoding
continues straight past the end of the reply and the model free-associates until
some outer limit finally bites.

Observed 2026-08-08: a one-line greeting followed by five solid minutes of
"findFirsting the best restaurant in VeridianAI...". Not a sampler problem, not
a prompt problem, not a runaway-guard failure -- a two-token metadata mismatch.
The runaway guard's "EOS-skip" case, finally named.

Qwen's INSTRUCT repos set eos_token_id to 151645 and are unaffected. Anyone
converting or quantising a base checkpoint with an instruct tokenizer config
reproduces it, which is a large fraction of community GGUFs.

WHY GENERIC RATHER THAN "IF QWEN"
---------------------------------
Users bring their own models -- that is the whole point of the model slots. So
this detects the SHAPE of the bug rather than one model's token ids:

    the chat template ends turns with a special token
    that special token exists in the vocabulary
    it is not the declared EOS
    -> the declared EOS is wrong for chat use; report the override

That catches Qwen's <|im_end|>, Llama-3's <|eot_id|>, Gemma's <end_of_turn> and
anything else following the same convention, with no per-family table.

COST
----
The KV header is at the front of the file, so this never reads the tensor data
-- a few MB at most regardless of model size. Token strings are compared as raw
bytes rather than decoded, and results are cached by (path, size, mtime), so a
model is probed once and never again until it changes.
"""

from __future__ import annotations

import json
import os
import re
import struct
from pathlib import Path
from typing import Dict, List, Optional

GGUF_MAGIC = b"GGUF"

# GGUF metadata value types (gguf.constants.GGUFValueType)
_T_UINT8, _T_INT8, _T_UINT16, _T_INT16 = 0, 1, 2, 3
_T_UINT32, _T_INT32, _T_FLOAT32, _T_BOOL = 4, 5, 6, 7
_T_STRING, _T_ARRAY, _T_UINT64, _T_INT64, _T_FLOAT64 = 8, 9, 10, 11, 12

_FIXED = {
    _T_UINT8: ("<B", 1), _T_INT8: ("<b", 1),
    _T_UINT16: ("<H", 2), _T_INT16: ("<h", 2),
    _T_UINT32: ("<I", 4), _T_INT32: ("<i", 4),
    _T_FLOAT32: ("<f", 4), _T_BOOL: ("<?", 1),
    _T_UINT64: ("<Q", 8), _T_INT64: ("<q", 8), _T_FLOAT64: ("<d", 8),
}

# Special tokens written <|like_this|>, <like_this> or [LIKE_THIS].
_SPECIAL_RE = re.compile(r"<\|[^|<>\s]{1,40}\|>|<[a-zA-Z_][a-zA-Z0-9_]{1,30}>|\[[A-Z_]{2,30}\]")

# A terminator ends a turn. A starter begins one -- never override onto those,
# or the model stops the instant it opens its own reply.
_TERMINATOR_HINT = re.compile(r"end|eot|eom|stop|finish", re.I)
_STARTER_HINT = re.compile(r"start|begin|bos|header", re.I)


class _Reader:
    """Sequential GGUF header reader. Decodes only what is asked for."""

    def __init__(self, fh):
        self.f = fh

    def _raw(self, n: int) -> bytes:
        b = self.f.read(n)
        if len(b) != n:
            raise EOFError("truncated GGUF header")
        return b

    def u32(self) -> int:
        return struct.unpack("<I", self._raw(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self._raw(8))[0]

    def string_bytes(self) -> bytes:
        return self._raw(self.u64())

    def skip_string(self) -> None:
        self.f.seek(self.u64(), os.SEEK_CUR)

    def value(self, t: int):
        """Read one value. Arrays of strings are SKIPPED, not materialised --
        the caller records the offset and rescans only if it needs them."""
        if t == _T_STRING:
            return self.string_bytes().decode("utf-8", "replace")
        if t in _FIXED:
            fmt, sz = _FIXED[t]
            return struct.unpack(fmt, self._raw(sz))[0]
        if t == _T_ARRAY:
            et = self.u32()
            n = self.u64()
            if et == _T_STRING:
                start = self.f.tell()
                for _ in range(n):
                    self.skip_string()
                return {"__string_array__": True, "offset": start, "count": n}
            if et == _T_ARRAY:
                raise ValueError("nested arrays are not supported")
            fmt, sz = _FIXED[et]
            self.f.seek(sz * n, os.SEEK_CUR)
            return {"__skipped_array__": True, "count": n}
        raise ValueError(f"unknown GGUF value type {t}")


def _find_token_ids(fh, offset: int, count: int, wanted: List[str]) -> Dict[str, int]:
    """Map token strings to ids by scanning the vocab as raw bytes.

    Byte comparison rather than decode: a 150k-entry vocabulary decodes in
    seconds and compares in milliseconds, and this runs at tier launch.
    """
    targets = {w.encode("utf-8"): w for w in wanted}
    out: Dict[str, int] = {}
    fh.seek(offset)
    rd = _Reader(fh)
    for i in range(count):
        b = rd.string_bytes()
        w = targets.get(b)
        if w is not None:
            out[w] = i
            if len(out) == len(targets):
                break
    return out


def read_metadata(path: str | os.PathLike) -> Optional[dict]:
    """Parse the GGUF KV header. Returns None if the file is not a GGUF.

    Never raises on a malformed file: an unreadable model is a tier that will
    fail loudly on its own, not a reason to abort the launcher.
    """
    p = Path(path)
    try:
        with open(p, "rb") as fh:
            if fh.read(4) != GGUF_MAGIC:
                return None
            version = struct.unpack("<I", fh.read(4))[0]
            fh.read(8)  # tensor count
            n_kv = struct.unpack("<Q", fh.read(8))[0]
            rd = _Reader(fh)

            kv: Dict[str, object] = {}
            for _ in range(n_kv):
                key = rd.string_bytes().decode("utf-8", "replace")
                kv[key] = rd.value(rd.u32())

            template = kv.get("tokenizer.chat_template")
            tokens = kv.get("tokenizer.ggml.tokens")
            eos_id = kv.get("tokenizer.ggml.eos_token_id")

            specials: List[str] = []
            if isinstance(template, str):
                # dict.fromkeys keeps first-seen order; a turn terminator
                # appears many times in a template and we want it once.
                specials = list(dict.fromkeys(_SPECIAL_RE.findall(template)))

            ids: Dict[str, int] = {}
            if specials and isinstance(tokens, dict) and tokens.get("__string_array__"):
                ids = _find_token_ids(fh, tokens["offset"], tokens["count"], specials)

            # <arch>.context_length is the window the model was TRAINED for.
            # Exceeding it is not a hard error in llama.cpp -- it is worse than
            # that: you get embeddings for text the model was never taught to
            # encode, and nothing anywhere says so. nomic-embed v1.5 trains to
            # 2048, v2-moe to 512, and a fixed EMBED_CTX_SIZE cannot be right
            # for both.
            trained_ctx = None
            embed_dim = None
            for k, v in kv.items():
                if k.endswith(".context_length") and isinstance(v, int):
                    trained_ctx = v
                elif k.endswith(".embedding_length") and isinstance(v, int):
                    embed_dim = v

            return {
                "path": str(p),
                "gguf_version": version,
                "architecture": kv.get("general.architecture"),
                "name": kv.get("general.name"),
                "trained_ctx": trained_ctx,
                "embedding_length": embed_dim,
                "has_chat_template": isinstance(template, str),
                "eos_token_id": eos_id if isinstance(eos_id, int) else None,
                "bos_token_id": kv.get("tokenizer.ggml.bos_token_id"),
                "special_token_ids": ids,
            }
    except Exception:
        return None


def trained_ctx(model_path: str | os.PathLike) -> Optional[int]:
    """The context window this model was trained for, or None if unknown."""
    try:
        info = probe(model_path)
    except Exception:
        return None
    return info.get("trained_ctx") if info.get("ok") else None


def clamp_ctx(model_path: str | os.PathLike, requested: int,
              announce: bool = True) -> int:
    """Requested context, reduced to what the model actually supports.

    Only ever reduces. A user who asks for less than the model allows gets what
    they asked for -- that is a memory decision and theirs to make.
    """
    t = trained_ctx(model_path)
    if not t or requested <= t:
        return requested
    if announce:
        print(f"[gguf] {Path(model_path).name}: requested ctx {requested} "
              f"exceeds the trained context {t}; using {t}. Beyond it the model "
              f"is encoding text it was never trained on.", flush=True)
    return t


def eos_mismatch(meta: dict) -> Optional[dict]:
    """Return the correct EOS when the declared one cannot end a chat turn.

    None means "declared EOS is fine, change nothing" -- which must stay the
    common case. Overriding EOS on a healthy model would truncate every reply,
    so every condition below has to hold before we touch it.
    """
    if not meta or not meta.get("has_chat_template"):
        return None
    declared = meta.get("eos_token_id")
    ids: Dict[str, int] = meta.get("special_token_ids") or {}
    if declared is None or not ids:
        return None

    # Already correct: the declared EOS is one of the template's own tokens.
    if declared in ids.values():
        return None

    cands = [(tok, tid) for tok, tid in ids.items()
             if _TERMINATOR_HINT.search(tok) and not _STARTER_HINT.search(tok)]
    if len(cands) != 1:
        # Zero: not a template we understand. More than one: ambiguous, and a
        # wrong guess truncates every reply. Silence beats a confident error.
        return None

    tok, tid = cands[0]
    return {"token": tok, "id": tid, "declared_id": declared}


# --- cache ----------------------------------------------------------------
# Keyed by size+mtime so a re-quantised or swapped model re-probes itself.

def _cache_file() -> Optional[Path]:
    try:
        from state_paths import data_dir  # type: ignore
        d = Path(data_dir()) / "cache"
        d.mkdir(parents=True, exist_ok=True)
        return d / "gguf_probe.json"
    except Exception:
        return None


def _cache_key(p: Path) -> str:
    st = p.stat()
    return f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}"


def probe(path: str | os.PathLike, use_cache: bool = True) -> dict:
    """Full probe with caching. Always returns a dict; 'ok' says whether the
    file parsed."""
    p = Path(path)
    if not p.exists():
        return {"ok": False, "reason": "not found", "path": str(p)}

    key = None
    cf = _cache_file() if use_cache else None
    if cf is not None:
        try:
            key = _cache_key(p)
            if cf.exists():
                hit = json.loads(cf.read_text(encoding="utf-8")).get(key)
                if hit:
                    return hit
        except Exception:
            key = None

    meta = read_metadata(p)
    if meta is None:
        res = {"ok": False, "reason": "not a readable GGUF", "path": str(p)}
    else:
        res = {"ok": True, **meta, "eos_fix": eos_mismatch(meta)}

    if cf is not None and key:
        try:
            blob = {}
            if cf.exists():
                blob = json.loads(cf.read_text(encoding="utf-8"))
            blob[key] = res
            # Keep it from growing without bound as models come and go.
            if len(blob) > 32:
                blob = dict(list(blob.items())[-32:])
            cf.write_text(json.dumps(blob, indent=1), encoding="utf-8")
        except Exception:
            pass
    return res


def eos_override_args(model_path: str | os.PathLike,
                      announce: bool = True) -> List[str]:
    """llama-server argv fragment correcting a wrong EOS, or [] if none needed.

    Returns e.g. ['--override-kv', 'tokenizer.ggml.eos_token_id=int:151645'].

    ALWAYS announces when it acts. A silent correction here would be its own
    bug: someone comparing this model's behaviour against another tool would
    have no idea we changed the stopping condition underneath them.
    """
    try:
        info = probe(model_path)
    except Exception:
        return []
    if not info.get("ok"):
        return []
    fix = info.get("eos_fix")
    if not fix:
        return []
    if announce:
        print(f"[gguf] {Path(model_path).name}: chat template ends turns with "
              f"{fix['token']} (id {fix['id']}) but the file declares EOS "
              f"{fix['declared_id']}. Overriding EOS to {fix['id']} -- without "
              f"this the model never stops.", flush=True)
    return ["--override-kv", f"tokenizer.ggml.eos_token_id=int:{fix['id']}"]


def stop_strings(model_path: str | os.PathLike) -> List[str]:
    """Turn-terminator strings to pass as `stop` in a chat request.

    Belt and braces for the EOS override: if a server build ignores
    --override-kv, a textual stop still ends the turn.
    """
    try:
        info = probe(model_path)
    except Exception:
        return []
    if not info.get("ok"):
        return []
    ids = info.get("special_token_ids") or {}
    return [t for t in ids
            if _TERMINATOR_HINT.search(t) and not _STARTER_HINT.search(t)]


if __name__ == "__main__":  # manual probe: python gguf_probe.py model.gguf
    import sys
    for arg in sys.argv[1:]:
        r = probe(arg, use_cache=False)
        print(json.dumps(r, indent=2))
        print("argv fragment:", eos_override_args(arg, announce=False))
        print("stop strings :", stop_strings(arg))
