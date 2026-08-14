#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
embeddings.py — one front door for text embeddings.

WHY
---
Two subsystems wanted embeddings and each grew its own client against a
different server:

  * craiid/journalist.py  -> llama-server /v1/embeddings on PORT_LLAMA_EMBED
  * sage_rag.py           -> Ollama       /api/embeddings on the Ollama port

Nothing ever launched a server on PORT_LLAMA_EMBED (config.py could not even
BUILD the command until v2.13), so the Journalist silently fell back to lexical
matching. And sage_rag only worked if the user had installed Ollama *and*
pulled an embedding model — so semantic search was unavailable out of the box.

This module speaks BOTH dialects and returns one shape, so callers stop caring
which engine answered. That is the compatibility principle applied: more
engines qualify, none is required.

DIALECTS
--------
llama-server (OpenAI-compatible), batched:
    POST /v1/embeddings   {"input": ["a","b"], "model": "..."}
    ->   {"data": [{"embedding": [...]}, {"embedding": [...]}]}

Ollama, one text per call:
    POST /api/embeddings  {"model": "...", "prompt": "a"}
    ->   {"embedding": [...]}

ORDER
-----
Local embed tier first: it ships with the app and needs no third-party
install. Ollama second, for users who prefer it or who have a different
embedding model pulled. Neither is mandatory.

NOT SILENT
----------
Every previous failure here returned None/[] and let the caller quietly do
something dumber. Degrading gracefully is right; degrading *silently* is how
CRAIID ran in lexical mode for months without anyone noticing. This module
warns ONCE per process when it wanted a backend and found none.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from typing import List, Optional

# Emitted at most once per process, so a genuinely absent backend is visible
# in the log without spamming it on every call.
_warned = False
_warn_lock = threading.Lock()

# Identifies which engine produced a vector. Persisted alongside vector
# indexes: cosine similarity between vectors from DIFFERENT embedding models is
# meaningless, so an index built by one backend must not be queried with
# another. See sage_rag.semantic_search.
SOURCE_LLAMA = "llama-embed"
SOURCE_OLLAMA = "ollama"

DEFAULT_MODEL = "nomic-embed-text"
_TIMEOUT = 8.0

# Nomic's models are trained WITH task instruction prefixes and expect them at
# inference. Omitting them does not fail -- it quietly costs retrieval quality,
# because a query and a document are supposed to land in different regions of
# the space and without the prefixes they do not.
#
# The asymmetry is the point: you embed what you STORE as a document and what
# you SEARCH WITH as a query, and the model was trained to map that pair
# together. Prefixing both sides the same way throws that away.
#
# Applied only when the active model is a nomic one. A prefix is literal text
# to any other model -- it would be embedded as content and pollute the vector,
# so this must never be applied blindly.
TASK_DOCUMENT = "search_document"
TASK_QUERY = "search_query"
TASK_CLASSIFICATION = "classification"
TASK_CLUSTERING = "clustering"

_PREFIX_MODELS = ("nomic-embed", "nomic_embed")


def _wants_prefix(model_name: str) -> bool:
    n = (model_name or "").lower()
    return any(k in n for k in _PREFIX_MODELS)


def _apply_task(texts: List[str], task: Optional[str], model: str) -> List[str]:
    if not task or not _wants_prefix(model):
        return list(texts)
    return [f"{task}: {t}" for t in texts]


def _warn_once(msg: str) -> None:
    global _warned
    with _warn_lock:
        if _warned:
            return
        _warned = True
    print(f"[embeddings] {msg}", flush=True)


def _post(url: str, payload: dict, timeout: float = _TIMEOUT) -> Optional[dict]:
    """POST JSON, return parsed JSON, or None on any failure.

    Uses net_guard.safe_urlopen when available (SSRF/DNS-rebind protection),
    falling back to urlopen so this module stays importable standalone.
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    try:
        try:
            from net_guard import safe_urlopen  # type: ignore
            opener = safe_urlopen
        except Exception:
            opener = urllib.request.urlopen
        with opener(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _embed_llama(texts: List[str], model: str) -> Optional[List[List[float]]]:
    """Local embed tier. One request for the whole batch."""
    try:
        from config import LLAMA_EMBED_URL as base
    except Exception:
        base = "http://127.0.0.1:11437"
    data = _post(base.rstrip("/") + "/v1/embeddings",
                 {"input": list(texts), "model": model})
    if not data:
        return None
    rows = data.get("data")
    if not isinstance(rows, list) or len(rows) != len(texts):
        return None
    vecs = [r.get("embedding") for r in rows]
    if all(isinstance(v, list) and v for v in vecs):
        return vecs
    return None


def _embed_ollama(texts: List[str], model: str) -> Optional[List[List[float]]]:
    """Ollama. No batch endpoint, so one request per text."""
    try:
        from config import OLLAMA_URL as base  # type: ignore
    except Exception:
        base = "http://127.0.0.1:11434"
    url = base.rstrip("/") + "/api/embeddings"
    out: List[List[float]] = []
    for t in texts:
        data = _post(url, {"model": model, "prompt": t})
        if not data:
            return None
        v = data.get("embedding")
        if not (isinstance(v, list) and v):
            return None
        out.append([float(x) for x in v])
    return out or None


def embed(texts: List[str],
          model: str = DEFAULT_MODEL,
          prefer: Optional[str] = None,
          task: Optional[str] = None) -> Optional[List[List[float]]]:
    """Return one vector per input text, or None if no backend could serve.

    prefer: SOURCE_LLAMA or SOURCE_OLLAMA to try that engine first. Default
            order is local tier, then Ollama.

    None means "no embeddings available" — callers should fall back to a
    non-semantic method, and this module will have logged why once.
    """
    if not texts:
        return []

    order = [_embed_llama, _embed_ollama]
    if prefer == SOURCE_OLLAMA:
        order.reverse()

    payload = _apply_task(texts, task, model)

    for fn in order:
        vecs = fn(payload, model)
        if vecs:
            return vecs

    _warn_once(
        "no embedding backend reachable (tried the local embed tier and "
        "Ollama). Semantic features will fall back to lexical matching. "
        "Check that the embed tier launched — see inference.embed_enabled "
        "and the Llama-Embed tier in the launcher log.")
    return None


def embed_one(text: str, model: str = DEFAULT_MODEL,
              task: Optional[str] = None) -> List[float]:
    """Single-text convenience. Returns [] on failure, matching the historical
    sage_rag.get_embedding contract so existing callers are unaffected."""
    vecs = embed([text], model=model, task=task)
    return vecs[0] if vecs else []


def index_tag(model: str = DEFAULT_MODEL) -> Optional[str]:
    """A tag identifying WHICH embedding produced a set of vectors.

    active_source() alone is not enough to protect a stored index. It answers
    "llama-embed or ollama", and swapping one llama-served model for another
    (nomic v1.5 -> nomic v2-moe, say) leaves that answer unchanged. Both are
    768-dimensional, so no shape check catches it either -- the stale index
    loads happily and every similarity score it produces is meaningless.
    Cosine distance between vectors from different models is not a worse
    measurement, it is not a measurement.

    So the tag carries source + model + dimension:

        llama-embed:nomic_embed_text_latest.gguf:768

    Any of the three changing invalidates the index, which sage_rag then
    discards and rebuilds. Rebuilding is cheap; silently wrong retrieval is not,
    and it is the kind of wrong that never announces itself.
    """
    for fn, src in ((_embed_llama, SOURCE_LLAMA), (_embed_ollama, SOURCE_OLLAMA)):
        vecs = fn(["ping"], model)
        if vecs:
            name = _served_model_name(src, model) or model
            # "+task" records that documents/queries were prefixed. Turning
            # prefixes on or off changes what the vectors MEAN while leaving the
            # model, source and dimension identical -- so it has to be part of
            # the identity or an index would survive a change it cannot survive.
            policy = "+task" if _wants_prefix(name) else "-task"
            return f"{src}:{name}:{len(vecs[0])}:{policy}"
    return None


def _served_model_name(src: str, fallback: str) -> Optional[str]:
    """What the server calls the model it is actually serving.

    Asked of the server rather than assumed from config, because config says
    what we REQUESTED and the tag has to describe what we GOT.
    """
    try:
        if src == SOURCE_LLAMA:
            try:
                from config import LLAMA_EMBED_URL as base  # type: ignore
            except Exception:
                base = "http://127.0.0.1:11437"
            data = _post(base.rstrip("/") + "/v1/embeddings",
                         {"input": ["ping"], "model": fallback})
            if data and isinstance(data.get("model"), str):
                return data["model"]
    except Exception:
        pass
    return None


def active_source(model: str = DEFAULT_MODEL) -> Optional[str]:
    """Which backend currently answers, or None. For diagnostics and for
    tagging a vector index with the engine that built it."""
    if _embed_llama(["ping"], model):
        return SOURCE_LLAMA
    if _embed_ollama(["ping"], model):
        return SOURCE_OLLAMA
    return None
