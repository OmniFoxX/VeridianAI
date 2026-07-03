"""
OracleAI Model Manager v2.1.6+ Phase 1C — three-tier routing (Updated with TimeManager)
====================================================================================
Replaces the v2.1 GGUF-via-llama-cpp-python path with HTTP routing
to a long-running `llama-server.exe` per tier. Each tier is a
separate process bound to a known port; the launcher (start.bat)
brings them up before the FastAPI backend starts.

Active tiers:
  - Oracle  : Ollama on 11434      (/api/chat, heavy reasoning)
  - Sage    : llama-server on 11435 (/v1/chat/completions, fast chat)
  - Daemon  : llama-server on 11436 (/v1/chat/completions, small)

There is NO in-process inference any more. The `from llama_cpp import
Llama` path is gone — it was the source of the misleading
"llama-cpp-python not installed" error for users who already had the
llama.cpp binaries via `llama-server.exe`.

Public surface (preserved from v2.1):
    ModelManager(config)
    .config             (mutable dict, live-updated by /api/config)
    .abort()
    ._abort             (read/written directly by ws_chat)
    await .list_models()
    await .load_model(model_id)     -> status dict
    await .unload_model(model_id)   -> no-op (kept for API compat)
    await .generate_full(messages, model_id, options) -> str
    async .generate(messages, model_id, options) -> AsyncGenerator[str]

Each model dict returned by list_models() carries (Option C, max flex):
    {
        "id":      "openhands-lm-7b-v0.1",
        "name":    "openhands-lm-7b-v0.1",
        "backend": "llama_sage",   # or "ollama_oracle", "llama_daemon"
        "tier":    "Sage",         # or "Oracle", "Daemon"
        "url":     "http://127.0.0.1:11435",
        "size":    0,
        "loaded":  True,
    }

The frontend can display tier as a badge, group by tier, filter, etc.
"""

from __future__ import annotations
from time_manager import TimeManager

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx

# --- config.py lives alongside this file --------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (
    OLLAMA_ORACLE_URL,
    LLAMA_SAGE_URL,
    LLAMA_DAEMON_URL,
    NPU_LLM_URL,
)

# --- Backend tag constants (single source of truth) --------------------------
BACKEND_OLLAMA_ORACLE = "ollama_oracle"
BACKEND_LLAMA_SAGE    = "llama_sage"
BACKEND_LLAMA_DAEMON  = "llama_daemon"
BACKEND_NPU           = "npu_lemonade"   # v2.11.12: Ryzen AI NPU tier

# Tier descriptors: (backend_tag, tier_label, base_url, protocol)
#   protocol = "ollama" -> /api/chat (Ollama native streaming)
#   protocol = "openai" -> /v1/chat/completions (OpenAI-compatible SSE)
TIERS: Tuple[Tuple[str, str, str, str], ...] = (
    (BACKEND_OLLAMA_ORACLE, "Oracle", OLLAMA_ORACLE_URL, "ollama"),
    (BACKEND_LLAMA_SAGE,    "Sage",   LLAMA_SAGE_URL,    "openai"),
    (BACKEND_LLAMA_DAEMON,  "Daemon", LLAMA_DAEMON_URL,  "openai"),
)

# v2.11.12: NPU tier (AMD Lemonade Server — OpenAI-compatible, serves models
# on the Ryzen AI XDNA NPU). Kept out of the static TIERS tuple because its
# inclusion is LIVE-toggleable: ModelManager._active_tiers() appends it only
# while inference.npu_enabled is on. Toggle off in the Hardware panel ->
# the tier's models vanish from the picker and nothing routes to it;
# toggle on -> next list/generate sees it again. (Whether the Lemonade
# server process itself runs is decided at boot by tier_launcher.py.)
NPU_TIER: Tuple[str, str, str, str] = (BACKEND_NPU, "NPU", NPU_LLM_URL, "openai")

# Per-tier listing timeout. Intentionally short so a dead tier does not
# stall the UI's model picker.
_LIST_TIMEOUT = 5.0


class ModelManager:
    # ---------------------------------------------------------------
    # v2.1.7 adaptive context sizing — fallback table for known model
    # families when /api/show doesn't expose context_length. Matched
    # by case-insensitive prefix/substring on the model_id. Add new
    # entries here as you adopt new models, OR rely on /api/show
    # auto-detection (preferred — this table is a safety net).
    #
    # Future-proofing note: million-token models are arriving (Gemini
    # 1.5 Pro, Claude 3 200k, GPT-4 Turbo 128k, Qwen 2.5 1M).
    # Hard-cap toggle (hard_cap_ctx config) defaults True for safety
    # but can be disabled when running such models so the user isn't
    # locked out of capabilities they paid for.
    # ---------------------------------------------------------------
    _KNOWN_TRAINED_CTX = {
        # OpenHands / Mistral-derived
        "openhands":    32768,
        "mistral":      32768,
        "mixtral":      32768,
        # Llama family
        "llama4:scout": 128000,
        "llama3.2":     131072,
        "llama3.1":     131072,
        "llama3":         8192,
        "llama2":         4096,
        # Gemma family
        "gemma4":       256000,
        "gemma3":        32768,
        "gemma2":         8192,
        "gemma":          8192,
        # Qwen family
        "qwen3":        128000,
        "qwen2.5":      128000,
        "qwen2":         32768,
        # Nemotron
        "nemotron":     256000,
        # Phi
        "phi3":         131072,
        "phi":            4096,
        # Coder/embed
        "qwen2.5_coder": 32768,
        "qwen2.5-coder": 32768,
        "nomic-embed":    2048,
    }

    def __init__(self, config: dict):
        self.config = config
        self._abort = False
        self._gen_locks = {}  # base_url -> asyncio.Lock: serialize same-instance gens
        # Routing table: model_id -> (backend_tag, tier_label, base_url, protocol)
        # Populated lazily by list_models(). generate() consults this to
        # route each request without re-querying every tier.
        self._routing: Dict[str, Tuple[str, str, str, str]] = {}
        # v2.11.12d: display_id -> raw server id for OpenAI-protocol tiers.
        # llama-server ids are file PATHS (we display the stem; the server
        # ignores the model field, so the stem was harmless). Lemonade/NPU
        # ids are often 'org/model' — the stem chops the org and the server
        # 404s "model not found". Generation must send the RAW id.
        self._openai_real_ids: Dict[str, str] = {}
        # v2.1.7: per-model trained-context cache. Populated on first
        # call to _get_trained_ctx for a model, so /api/show is queried
        # once and reused for the lifetime of the process.
        self._trained_ctx_cache: Dict[str, int] = {}

    def abort(self) -> None:
        self._abort = True

    # ---------------------------------------------------------------
    #  v2.1.7 ADAPTIVE CONTEXT SIZING
    # ---------------------------------------------------------------
    # Three-layer system:
    #   1. Detect the model's trained context window via /api/show
    #      (cached after first call). Fall back to _KNOWN_TRAINED_CTX
    #      table by name match. Last resort: 32768.
    #   2. Compute effective num_ctx as max(prompt_tokens * pad,
    #      ctx_min) — gives each request exactly the headroom it needs
    #      plus a safety margin, without allocating 255k of KV cache
    #      for a 2k-char prompt.
    #   3. Optionally clamp to trained_max (hard_cap_ctx config).
    #      Default True for safety; users running million-token models
    #      can disable to push beyond the table's known limits.
    #
    # Power-user override: if options.num_ctx or config.num_ctx is
    # set explicitly, that value wins (still subject to hard_cap).
    # If config has the old 255480 default lingering, this code
    # treats it as an explicit choice — you can clear it from config
    # to opt into adaptive sizing.

    def _resolve_trained_ctx_from_name(self, model_id: str) -> Optional[int]:
        """Heuristic fallback when /api/show doesn't reveal context_length.
        Matches model_id against the known-models table by case-insensitive
        prefix or substring. Longest match wins so 'gemma4' beats 'gemma'.
        Returns None if no entry matches."""
        m_lower = model_id.lower()
        candidates = [
            (prefix, ctx) for prefix, ctx in self._KNOWN_TRAINED_CTX.items()
            if prefix in m_lower
        ]
        if not candidates:
            return None
        # Pick longest prefix so 'gemma4:31b' matches 'gemma4' not 'gemma'
        candidates.sort(key=lambda kv: len(kv[0]), reverse=True)
        return candidates[0][1]

    async def _get_trained_ctx(self, model_id: str, base_url: str) -> int:
        """Detect the model's trained context window. Cached per-model.

        Tries Ollama's /api/show endpoint first (canonical answer when
        the model file exposes it). Falls back to the known-models name
        table. Final fallback: 32768 — conservative but not punitive.
        """
        if model_id in self._trained_ctx_cache:
            return self._trained_ctx_cache[model_id]

        trained: Optional[int] = None
        try:
            async with httpx.AsyncClient(timeout=56000.0) as c:
                r = await c.post(
                    f"{base_url}/api/show",
                    json={"name": model_id},
                )
                if r.status_code == 200:
                    data = r.json()
                    # Path 1: model_info dict often has *.context_length
                    model_info = data.get("model_info") or {}
                    if isinstance(model_info, dict):
                        for k, v in model_info.items():
                            if ("context_length" in k.lower()
                                    and isinstance(v, int)
                                    and v > 0):
                                trained = v
                                break
                    # Path 2: parameters string with "num_ctx N"
                    if trained is None:
                        params = data.get("parameters", "")
                        if isinstance(params, str):
                            for line in params.split("\n"):
                                if "num_ctx" in line:
                                    parts = line.split()
                                    if (len(parts) >= 2
                                            and parts[-1].isdigit()):
                                        trained = int(parts[-1])
                                        break
        except Exception as e:
            # Detection must never break inference — fall through to
            # the heuristic. Worst case the user gets a slightly off
            # default; they can override via config.num_ctx.
            print(f"[CTX DETECT] /api/show failed for {model_id}: {e}")

        if trained is None:
            trained = self._resolve_trained_ctx_from_name(model_id)
        if trained is None:
            trained = 32768   # conservative safety floor

        self._trained_ctx_cache[model_id] = trained
        print(
            f"[CTX DETECT] {model_id} trained_ctx={trained} "
            f"(source={'api/show' if trained != 32768 else 'fallback'})"
        )
        return trained

    # v2.1.8 bucket rounding — see _round_to_bucket. Power-of-two
    # ladder covers everything from tiny chats (4k) through the
    # million-token frontier. New buckets can be appended later
    # without needing code changes elsewhere.
    _CTX_BUCKETS = [
        4096, 8192, 16384, 32768, 65536,
        131072, 262144, 524288, 1048576,
    ]

    def _round_to_bucket(self, n: int, max_bucket: int) -> int:
        """Round `n` UP to the next power-of-two bucket, capped at
        max_bucket. The bucket ladder is fixed [4k..1M] so small
        variations in needed context land on the same value — which
        means Ollama doesn't trigger a model reload between requests
        for trivial prompt-size differences.

        Without this, adaptive sizing would feed Ollama 8192 on one
        turn and 8398 on the next, and Ollama would treat that as
        'different model context' and reload the model — a 100+ second
        operation on the user's CPU-bound 120B setup, which then
        races the client's 300-second read timeout. Bucket rounding
        keeps consecutive requests on the SAME ctx value as long as
        the prompt growth stays inside a bucket band.
        """
        for b in self._CTX_BUCKETS:
            if b >= n and b <= max_bucket:
                return b
        # n exceeds even our biggest bucket — fall back to max_bucket.
        # (Caller's hard_cap logic still applies on top of this.)
        return max_bucket

    def _compute_adaptive_ctx(
        self,
        total_chars: int,
        trained_max: int,
        options: Dict,
    ) -> Tuple[int, str]:
        """Decide the effective num_ctx for a single request.

        Returns (effective_ctx, decision_note). The note is a short
        string suitable for logging that explains how the value was
        chosen — useful for postmortems.

        Priority order:
          1. options['num_ctx'] is an explicit per-request override.
          2. config['num_ctx'] or config['n_ctx'] is an explicit
             install-wide override.
          3. Otherwise, adaptive: pad estimated prompt tokens by
             ctx_padding_factor (default 1.5) and floor at ctx_min
             (default 8192).

        Hard-cap behaviour:
          - If hard_cap_ctx config is True (default) and effective
            > trained_max, the value is silently clamped.
          - If hard_cap_ctx is False, the effective value is honored
            even when it exceeds trained_max. A warning is emitted by
            the caller (this function returns the requested value so
            the caller can log the WARN).
        """
        ctx_min = int(self.config.get("ctx_min", 8192))
        pad = float(self.config.get("ctx_padding_factor", 1.5))
        hard_cap = bool(self.config.get("hard_cap_ctx", True))

        # Power-user explicit override (per-request or install-wide)
        explicit = (
            options.get("num_ctx")
            or self.config.get("n_ctx")
            or self.config.get("num_ctx")
        )

        if explicit:
            explicit = int(explicit)
            if hard_cap and explicit > trained_max:
                return (
                    trained_max,
                    f"explicit {explicit} capped to trained_max "
                    f"{trained_max}",
                )
            if explicit > trained_max:
                return (
                    explicit,
                    f"explicit {explicit} EXCEEDS trained_max "
                    f"{trained_max} (hard_cap=False, allowed)",
                )
            return explicit, f"explicit {explicit}"

        # v2.1.8 (bug from morning of 2026-05-12): multiplicative
        # padding (`ctx_padding_factor`, default 1.5x) was pushing
        # mid-size prompts just past a bucket boundary, triggering
        # Ollama to reload the 120B model on consecutive turns. Switch
        # to ADDITIVE response headroom — a fixed number of tokens
        # reserved for the model's reply, regardless of prompt size.
        # This is more semantically correct: response length doesn't
        # scale with prompt length, so reserving a fixed budget makes
        # sense. ctx_padding_factor is kept as a deprecated knob; it
        # only applies if explicitly set above 1.0 in config.
        est_tokens = max(1, total_chars // 4)
        headroom = int(self.config.get("ctx_response_headroom", 1500))
        raw_needed_add = est_tokens + headroom

        # Legacy multiplicative path (back-compat only). pad defaults to
        # 1.0 in DEFAULT_CONFIG now; values above 1.0 are a deliberate
        # power-user override.
        raw_needed_mul = int(est_tokens * pad)

        raw_needed = max(raw_needed_add, raw_needed_mul, ctx_min)

        # v2.1.8 bucket rounding: snap raw_needed UP to a power-of-two
        # bucket so consecutive requests with slightly different prompt
        # sizes land on the SAME num_ctx — preventing Ollama from
        # reloading the model between turns. See _round_to_bucket
        # docstring for the full failure analysis.
        needed = self._round_to_bucket(raw_needed, trained_max)

        if hard_cap and needed > trained_max:
            return (
                trained_max,
                f"adaptive bucket {needed} "
                f"(~{est_tokens} tokens + {headroom} headroom "
                f"→ {raw_needed}) capped to trained_max {trained_max}",
            )
        if needed > trained_max:
            return (
                needed,
                f"adaptive bucket {needed} EXCEEDS trained_max "
                f"{trained_max} (hard_cap=False, allowed)",
            )
        return (
            needed,
            f"adaptive bucket {needed} "
            f"(~{est_tokens} tokens + {headroom} headroom "
            f"→ {raw_needed}, floor={ctx_min})",
        )

    # =======================================================================
    #  LISTING — parallel across all tiers
    # =======================================================================
    def _active_tiers(self) -> Tuple[Tuple[str, str, str, str], ...]:
        """v2.11.12: the static tiers plus the NPU tier when its toggle is
        on. Reads self.config LIVE (main.py refreshes it on every
        /api/config POST), so flipping the Hardware-panel switch takes
        effect on the very next list/generate — no restart."""
        if self.config.get("npu_enabled", True):
            return TIERS + (NPU_TIER,)
        return TIERS

    async def list_models(self) -> List[Dict]:
        """Query all active tiers concurrently, merge results, tag each model
        with its backend/tier, and populate the routing table. A dead tier
        is silently skipped (not a fatal error for the whole call)."""
        active = self._active_tiers()
        results = await asyncio.gather(
            *(self._list_tier(*t) for t in active),
            return_exceptions=True,
        )

        merged: List[Dict] = []
        seen_ids: set = set()
        new_routing: Dict[str, Tuple[str, str, str, str]] = {}

        for tier, res in zip(active, results):
            tier_label = tier
            if isinstance(res, Exception):
                print(f"[ModelManager] Tier {tier_label} unreachable: {res}")
                continue
            for m in res:
                mid = m.get("id")
                if not mid or mid in seen_ids:
                    continue
                seen_ids.add(mid)
                new_routing[mid] = tier
                merged.append(m)

        self._routing = new_routing
        return merged

    async def _list_tier(self, backend_tag: str, tier_label: str,
                         base_url: str, protocol: str) -> List[Dict]:
        """List models from a single tier. Returns a list of model dicts.
        Returns [] on any error (caller logs via gather's return_exceptions)."""
        try:
            async with httpx.AsyncClient(timeout=_LIST_TIMEOUT) as c:
                if protocol == "ollama":
                    return await self._list_ollama_tier(c, backend_tag, tier_label, base_url)
                else:
                    return await self._list_openai_tier(c, backend_tag, tier_label, base_url)
        except Exception as e:
            print(f"[ModelManager] {tier_label} list failed: {e}")
            return []

    async def _list_ollama_tier(self, c: httpx.AsyncClient, backend_tag: str,
                                 tier_label: str, base_url: str) -> List[Dict]:
        r = await c.get(f"{base_url}/api/tags")
        if r.status_code != 200:
            return []
        return [
            {
                "id":      m.get("name"),
                "name":    m.get("name"),
                "size":    m.get("size", 0),
                "backend": backend_tag,
                "tier":    tier_label,
                "url":     base_url,
                "loaded":  True,
            }
            for m in r.json().get("models", [])
            if m.get("name")
        ]

    async def _list_openai_tier(self, c: httpx.AsyncClient, backend_tag: str,
                                 tier_label: str, base_url: str) -> List[Dict]:
        """llama-server exposes its loaded model at /v1/models with the full
        file path as the id. We extract a clean stem for display."""
        r = await c.get(f"{base_url}/v1/models")
        if r.status_code != 200:
            return []
        data = r.json().get("data", [])
        out: List[Dict] = []
        for m in data:
            raw_id = m.get("id", "")
            if not raw_id:
                continue
            stem = Path(raw_id).stem if raw_id else raw_id
            display_id = stem or raw_id
            # v2.11.12d: remember the raw id so _gen_llama_server can send
            # what the server actually calls the model (Lemonade needs it).
            self._openai_real_ids[display_id] = raw_id
            out.append({
                "id":      display_id,
                "name":    display_id,
                "size":    0,
                "backend": backend_tag,
                "tier":    tier_label,
                "url":     base_url,
                "loaded":  True,
                # Keep the raw path around in case the caller needs it.
                "raw_id":  raw_id,
            })
        return out

    # =======================================================================
    #  LOAD / UNLOAD — no-op for llama-server, compat shim for Ollama
    # =======================================================================
    async def load_model(self, model_id: str) -> Dict:
        """Both Ollama (lazy) and llama-server (pinned at process start)
        manage their own model lifecycles. This method is a status check,
        not an action. Kept for API compatibility with /api/models/load."""
        if not self._routing:
            await self.list_models()
        tier = self._routing.get(model_id)
        if tier is None:
            return {
                "status":  "error",
                "message": (f"Model '{model_id}' not found on any running tier. "
                            f"Check that the corresponding server is up and "
                            f"has this model loaded."),
            }
        backend_tag, tier_label, base_url, _ = tier
        return {
            "status":   "ready",
            "model_id": model_id,
            "backend":  backend_tag,
            "tier":     tier_label,
            "url":      base_url,
        }

    async def unload_model(self, model_id: str) -> None:
        """No-op. llama-server keeps its model pinned for the lifetime of
        the server process; Ollama manages its own unloads via
        OLLAMA_KEEP_ALIVE. Kept for API compatibility with /api/models/unload."""
        return

    # =======================================================================
    #  GENERATION — route per model_id
    # =======================================================================
    async def generate_full(self, messages: List[Dict],
                            model_id: Optional[str],
                            options: Dict) -> str:
        result = ""
        async for token in self.generate(messages, model_id, options):
            result += token
        return result

    # -------------------------------------------------------------------
    # v2.11.12: GPU-offload gating. Before this, gpu_acceleration and the
    # brand toggles (cuda/rocm/vulkan_enabled) were never consulted by the
    # inference path — the switches did nothing. Now: if global GPU
    # acceleration is off, OR every detected GPU vendor's brand toggle is
    # off, Ollama requests get options.num_gpu = 0 (all layers on CPU).
    # Hardware vendor detection is expensive (PowerShell probes), so it
    # runs once off the event loop and is cached for the process lifetime.
    # -------------------------------------------------------------------
    _gpu_vendors_cache: Optional[Tuple[str, ...]] = None

    async def _detected_gpu_vendors(self) -> Tuple[str, ...]:
        if ModelManager._gpu_vendors_cache is not None:
            return ModelManager._gpu_vendors_cache
        def _probe():
            try:
                from hw_utils import detect_hardware
                hw = detect_hardware()
                vendors = tuple(v for v in ("nvidia", "amd", "intel")
                                if hw.get(v, {}).get("available"))
            except Exception:
                vendors = ()
            return vendors
        try:
            vendors = await asyncio.to_thread(_probe)
        except Exception:
            vendors = ()
        ModelManager._gpu_vendors_cache = vendors
        return vendors

    _VENDOR_TOGGLE = {"nvidia": "cuda_enabled",
                      "amd":    "rocm_enabled",
                      "intel":  "vulkan_enabled"}

    async def _gpu_offload_disabled(self) -> bool:
        """True -> force num_gpu=0 (CPU-only) on Ollama calls."""
        if not self.config.get("gpu_acceleration", True):
            return True
        vendors = await self._detected_gpu_vendors()
        if not vendors:
            return False   # no GPU to gate; Ollama is CPU-bound anyway
        return all(
            not self.config.get(self._VENDOR_TOGGLE[v], True)
            for v in vendors
        )

    def _gen_lock_for(self, base_url):
        """Per-Ollama-instance (base_url) async lock so concurrent requests to
        the SAME server queue instead of evicting each other's loaded model.
        Different tiers (different ports) keep their own lock -> still parallel."""
        lock = self._gen_locks.get(base_url)
        if lock is None:
            lock = asyncio.Lock()
            self._gen_locks[base_url] = lock
        return lock

    async def generate(self, messages: List[Dict],
                       model_id: Optional[str],
                       options: Dict) -> AsyncGenerator[str, None]:
        # NOTE: do NOT reset self._abort here. An abort request from the
        # user can arrive at any point during a multi-step agentic turn
        # (tool calls, recursive generate() invocations, etc.). Resetting
        # here silently clobbers the user's stop intent. The flag is reset
        # exactly once per ws_chat turn, in main.py, right after we receive
        # the next user message. (v2.1.4 stop-button fix)

        if not model_id:
            yield "[Error: No model selected]"
            return

        # Refresh routing table if this model is unknown.
        if model_id not in self._routing:
            await self.list_models()

        tier = self._routing.get(model_id)
        if tier is None:
            yield (f"[Error: Model '{model_id}' not found on any running tier. "
                   f"Check start.bat output to see which tiers came up.]")
            return

        backend_tag, tier_label, base_url, protocol = tier

        # v2.11.12: NPU toggle enforcement at the routing boundary. The
        # routing table may hold a stale NPU entry from before the switch
        # was flipped off; honor the CURRENT toggle, not the cached route.
        if backend_tag == BACKEND_NPU and not self.config.get("npu_enabled", True):
            await self.list_models()          # rebuild without the NPU tier
            tier = self._routing.get(model_id)
            if tier is None or tier[0] == BACKEND_NPU:
                yield ("[Error: NPU acceleration is toggled OFF and "
                       f"'{model_id}' is only served by the NPU tier. "
                       "Re-enable the NPU toggle in Settings → Hardware, "
                       "or pick a model from another tier.]")
                return
            backend_tag, tier_label, base_url, protocol = tier

        if protocol == "ollama":
            gen = self._gen_ollama(messages, model_id, options, base_url, tier_label)
        else:
            gen = self._gen_llama_server(messages, model_id, options, base_url, tier_label)

        # v2.9: serialize generations per Ollama instance so a local query and
        # an OFFLOADED request from another node don't collide on the one GPU
        # (Ollama would otherwise evict the in-flight model mid-stream). FIFO
        # queue; different tiers (ports) still run in parallel.
        async with self._gen_lock_for(base_url):
            async for token in gen:
                if self._abort:
                    return
                yield token

    # --- Ollama streaming (/api/chat) ------------------------------------
    async def _gen_ollama(self, messages: List[Dict], model_id: str,
                          options: Dict, base_url: str,
                          tier_label: str) -> AsyncGenerator[str, None]:
        # max_tokens defaults to -1 (unlimited) per project policy: local
        # system, hardware-limited, no arbitrary response caps. Ollama's
        # num_predict=-1 means "generate until EOS or context full."

        # v2.1.7 adaptive context sizing: was a hardcoded 255480 default
        # which forced 18+ GiB of KV cache on every call regardless of
        # prompt size, driving the cluster of ReadTimeouts on 40k-char
        # prompts. Now we detect the model's trained context window
        # (via /api/show, cached) and allocate just enough headroom for
        # prompt + response. See _get_trained_ctx and
        # _compute_adaptive_ctx for the policy.
        _total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        trained_max = await self._get_trained_ctx(model_id, base_url)
        effective_ctx, decision = self._compute_adaptive_ctx(
            _total_chars, trained_max, options,
        )

        # If the user disabled hard_cap_ctx AND we're exceeding trained,
        # emit a warning. The request still goes through — power-user
        # mode trusts the user — but they get a clear console signal.
        if (not self.config.get("hard_cap_ctx", True)
                and effective_ctx > trained_max):
            print(
                f"[CTX WARN] {model_id} effective_ctx={effective_ctx} "
                f"EXCEEDS trained_max={trained_max}. hard_cap_ctx is "
                f"OFF so honoring user intent. If results degrade or "
                f"the request times out, this is likely why."
            )

        # v2.1.8 max_tokens=-1 trap fix — last-line defensive coercion.
        # Frontend sanitizes; /api/config POST sanitizes; load_config
        # sanitizes on boot. But this runs at the exact moment we hand
        # the value to Ollama, so any future code path that bypasses
        # those (a plugin, a scripted ws call, a stale options dict)
        # still gets canonicalized. Cheap insurance against the exact
        # symptom Todd reported.
        _raw_max = options.get("max_tokens",
                                self.config.get("max_tokens", -1))
        try:
            _max_int = int(_raw_max)
        except (TypeError, ValueError):
            _max_int = -1
        if _max_int == 0 or (_max_int < 0 and _max_int != -1):
            print(
                f"[MAX_TOKENS GUARD] received invalid value {_raw_max!r} "
                f"for {model_id}, coercing to -1 (unlimited). If you see "
                f"this in normal use, something upstream is sending a bad "
                f"max_tokens — check UI settings or per-request options."
            )
            _max_int = -1

        # v2.11.12: honor the Hardware-panel toggles. num_gpu=0 tells
        # Ollama to keep every layer on CPU; omitting the key leaves
        # Ollama's own GPU auto-detection in charge (previous behavior).
        _cpu_only = await self._gpu_offload_disabled()
        if _cpu_only:
            print(f"[GPU GATE] GPU acceleration toggled OFF — "
                  f"{model_id} running CPU-only (num_gpu=0)")

        payload = {
            "model":    model_id,
            "messages": messages,
            "stream":   True,
            "options": {
                "temperature": options.get("temperature",
                                            self.config.get("temperature", 0.5)),
                "num_predict": _max_int,
                "num_ctx":     effective_ctx,
                **({"num_gpu": 0} if _cpu_only else {}),
                **({"top_p": float(options["top_p"])}
                   if options.get("top_p") is not None else {}),
                **({"top_k": int(options["top_k"])}
                   if options.get("top_k") is not None else {}),
                **({"repeat_penalty": float(options["repeat_penalty"])}
                   if options.get("repeat_penalty") is not None else {}),
            },
        }

        # v2.1.7 diagnostic logging: capture model + prompt size + ctx
        # decision on every call. The [CTX SIZE] line tells postmortems
        # exactly what was allocated and why.
        print(
            f"[OLLAMA CALL] tier={tier_label} model={model_id} "
            f"turns={len(messages)} chars={_total_chars} "
            f"num_ctx={effective_ctx}"
        )
        print(
            f"[CTX SIZE] model={model_id} chars={_total_chars} "
            f"~tokens={_total_chars // 4} effective={effective_ctx} "
            f"trained_max={trained_max} "
            f"hard_cap={self.config.get('hard_cap_ctx', True)} "
            f"({decision})"
        )

        # v2.1.7 Bug 5 timeout + retry: previously timeout=None meant we
        # would wait forever for a stuck Ollama (common during long Arc
        # autonomous runs when VRAM pressure causes the server to drop
        # the connection mid-generation). Now: 300s overall read window
        # plus 30s connect timeout. On disconnect/timeout, retry ONCE
        # after a 5s pause. If the retry also fails, yield a clean error
        # string — memory_logger's pre-write guard (Bug 4) will keep it
        # out of the chain.
        #
        # v2.1.8 (2026-05-12): read timeout is now config-driven. The
        # original v2.1.7 hardcode of 300s was killing legitimate big-
        # model + cold-load + long-prompt workflows. On Todd's Arc B580
        # running nemotron-3-super:120b with 6/89 layers on GPU and the
        # rest on CPU, the cold-load alone takes ~150s, then prompt
        # processing eats another 60-180s, leaving zero budget for
        # actual generation inside a 300s window. Default raised to
        # 1800s (30 min) which comfortably covers cold-load + heavy
        # prompts + multi-minute generation. Users on faster hardware
        # can lower it via config.ollama_read_timeout_sec.
        _read_timeout = float(
            self.config.get("ollama_read_timeout_sec", 56000.0)
        )
        client_timeout = httpx.Timeout(
            connect=56000.0, read=_read_timeout, write=56000.0, pool=56000.0,
        )
        max_attempts = 4

        async def _attempt(attempt_idx: int):
            """Single try. Yields tokens or an error string."""
            async with httpx.AsyncClient(timeout=client_timeout) as c:
                async with c.stream("POST", f"{base_url}/api/chat",
                                    json=payload) as resp:
                    if resp.status_code != 200:
                        # Capture body for diagnosis — without this the
                        # user just saw "[Ollama error 500]" with no
                        # clue what Ollama actually said.
                        try:
                            body_bytes = await resp.aread()
                            body = body_bytes.decode(
                                "utf-8", "replace")[:300]
                        except Exception:
                            body = ""
                        print(
                            f"[OLLAMA ERROR {resp.status_code}] "
                            f"tier={tier_label} model={model_id} "
                            f"chars={_total_chars} body={body!r}"
                        )
                        yield (
                            f"[{tier_label} Ollama error "
                            f"{resp.status_code}: {body[:120]}]"
                        )
                        return
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        content = chunk.get("message", {}).get("content", "")
                        if content:
                            yield content
                        if chunk.get("done"):
                            return

        for attempt in range(max_attempts):
            try:
                async for tok in _attempt(attempt):
                    yield tok
                return  # successful completion
            except httpx.ConnectError:
                if attempt == max_attempts - 1:
                    yield (
                        f"[Error: Cannot connect to {tier_label} "
                        f"Ollama at {base_url} -- is it running?]"
                    )
                    return
                print(
                    f"[OLLAMA RETRY] connect failed for {tier_label}, "
                    f"sleeping 5s before retry {attempt + 2}/{max_attempts}"
                )
                await asyncio.sleep(5.0)
            except (httpx.ReadTimeout, httpx.RemoteProtocolError,
                    httpx.ReadError) as e:
                if attempt == max_attempts - 1:
                    print(
                        f"[OLLAMA GAVE UP] tier={tier_label} "
                        f"model={model_id} after {max_attempts} "
                        f"attempts: {type(e).__name__}: {e}"
                    )
                    yield (
                        f"[{tier_label} Ollama error: server disconnected "
                        f"({type(e).__name__}) after {max_attempts} "
                        f"attempt(s)]"
                    )
                    return
                print(
                    f"[OLLAMA RETRY] {type(e).__name__} on {tier_label}, "
                    f"sleeping 5s before retry "
                    f"{attempt + 2}/{max_attempts}: {e}"
                )
                await asyncio.sleep(5.0)
            except Exception as e:
                # Anything else: fail fast, don't retry on unknown error
                yield f"[{tier_label} Ollama error: {e}]"
                return

    # --- llama-server streaming (/v1/chat/completions, SSE) -------------
    async def _gen_llama_server(self, messages: List[Dict], model_id: str,
                                 options: Dict, base_url: str,
                                 tier_label: str) -> AsyncGenerator[str, None]:
        """Stream from an OpenAI-compatible llama-server instance.

        Request: OpenAI chat.completions shape with stream=true.
        Response: Server-Sent Events. Each event is a line of the form
            data: {"choices":[{"delta":{"content":"token"},"finish_reason":null}]}
        terminated by the literal line
            data: [DONE]
        """
        # max_tokens defaults to -1 (unlimited) per project policy: local
        # system, hardware-limited, no arbitrary response caps. For the
        # OpenAI-compatible llama-server API, we omit max_tokens entirely
        # when unlimited is requested so the server uses its own default
        # (ctx-window limited), rather than passing a negative number that
        # strict validators might reject.
        #
        # v2.1.8 max_tokens=-1 trap fix: coerce the same way _gen_ollama
        # does, so a stale or buggy upstream can't sneak 0 or -5 through
        # and either trigger a 400 from a strict OpenAI-compat server or
        # produce a zero-length response.
        _raw_max = options.get("max_tokens",
                                self.config.get("max_tokens", -1))
        try:
            _req_max = int(_raw_max)
        except (TypeError, ValueError):
            _req_max = -1
        if _req_max == 0 or (_req_max < 0 and _req_max != -1):
            print(
                f"[MAX_TOKENS GUARD] llama-server received invalid value "
                f"{_raw_max!r} for {model_id}, treating as unlimited."
            )
            _req_max = -1

        # Vision: the per-message `images` field is Ollama's format; the
        # llama-server OpenAI endpoint does not accept it, so strip it here so a
        # llama-tier turn that happens to carry an image still runs (text-only)
        # rather than erroring. (Ollama vision goes through _gen_ollama, which
        # forwards `images` unchanged.)
        messages = [
            {k: v for k, v in m.items() if k != "images"}
            if isinstance(m, dict) else m
            for m in messages
        ]
        # v2.11.12d: send the server's REAL model id, not the display stem.
        # Critical for the Lemonade/NPU tier ('org/model' ids); a no-op for
        # llama-server, which ignores the model field.
        _real_id = self._openai_real_ids.get(model_id, model_id)
        payload = {
            "model":       _real_id,
            "messages":    messages,
            "stream":      True,
            "temperature": options.get("temperature",
                                        self.config.get("temperature", 0.5)),
        }
        if options.get("top_p") is not None:
            payload["top_p"] = float(options["top_p"])
        if options.get("top_k") is not None:
            payload["top_k"] = int(options["top_k"])
        if options.get("repeat_penalty") is not None:
            payload["repeat_penalty"] = float(options["repeat_penalty"])
        if _req_max > 0:
            payload["max_tokens"] = _req_max

        # v2.1.7 Bug 1 diagnostic logging — see _gen_ollama for rationale.
        _total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        print(
            f"[LLAMA-SERVER CALL] tier={tier_label} model={model_id} "
            f"turns={len(messages)} chars={_total_chars}"
        )

        # v2.1.7 Bug 5 timeout + retry — same pattern as _gen_ollama.
        # v2.1.8: read timeout now config-driven, default 1800s. See
        # _gen_ollama comment block for the full rationale.
        _read_timeout = float(
            self.config.get("ollama_read_timeout_sec", 56000.0)
        )
        client_timeout = httpx.Timeout(
            connect=56000.0, read=_read_timeout, write=56000.0, pool=56000.0,
        )
        max_attempts = 4

        async def _attempt(_attempt_idx: int):
            async with httpx.AsyncClient(timeout=client_timeout) as c:
                async with c.stream("POST",
                                    f"{base_url}/v1/chat/completions",
                                    json=payload) as resp:
                    if resp.status_code != 200:
                        body = ""
                        try:
                            body_bytes = await resp.aread()
                            body = body_bytes.decode(
                                "utf-8", "replace")[:300]
                        except Exception:
                            pass
                        print(
                            f"[LLAMA-SERVER ERROR {resp.status_code}] "
                            f"tier={tier_label} model={model_id} "
                            f"chars={_total_chars} body={body!r}"
                        )
                        yield (f"[{tier_label} llama-server error "
                               f"{resp.status_code}: {body[:120]}]")
                        return
                    # v2.11.12d: track whether the SSE stream produced any
                    # tokens, and buffer non-SSE lines. Some OpenAI-compat
                    # servers (observed with Lemonade/NPU) answer certain
                    # requests with ONE plain JSON body instead of SSE —
                    # the old parser skipped every non-"data:" line and the
                    # turn ended instantly with an empty reply.
                    _yielded = False
                    _raw_lines = []
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        if not line.startswith("data:"):
                            _raw_lines.append(line)
                            continue
                        data_str = line[5:].lstrip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        # v2.1.6 fix: choices is a LIST per OpenAI shape
                        # (see docstring above: {"choices":[{"delta":...}]}),
                        # not a dict. Use choices[0] for delta + finish.
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        # 'content' is standard; 'text' covers legacy /
                        # completion-style deltas some servers emit.
                        content = delta.get("content") or delta.get("text") \
                            or choice.get("text") or ""
                        if content:
                            _yielded = True
                            yield content
                        if choice.get("finish_reason") is not None:
                            break
                    if _yielded:
                        return
                    # Fallback: non-streamed OpenAI JSON body.
                    if _raw_lines:
                        try:
                            doc = json.loads("\n".join(_raw_lines))
                            choices = doc.get("choices") or []
                            if choices:
                                msg = choices[0].get("message") or {}
                                text = (msg.get("content")
                                        or choices[0].get("text") or "")
                                if text:
                                    yield text
                                    return
                        except Exception:
                            pass
                        print(
                            f"[LLAMA-SERVER WARN] tier={tier_label} "
                            f"model={model_id} returned no stream tokens; "
                            f"body head: {' '.join(_raw_lines)[:200]!r}"
                        )
                    return

        for attempt in range(max_attempts):
            try:
                async for tok in _attempt(attempt):
                    yield tok
                return
            except httpx.ConnectError:
                if attempt == max_attempts - 1:
                    yield (f"[Error: Cannot connect to {tier_label} "
                           f"llama-server at {base_url} -- is it running?]")
                    return
                print(
                    f"[LLAMA-SERVER RETRY] connect failed for "
                    f"{tier_label}, sleeping 5s before retry "
                    f"{attempt + 2}/{max_attempts}"
                )
                await asyncio.sleep(5.0)
            except (httpx.ReadTimeout, httpx.RemoteProtocolError,
                    httpx.ReadError) as e:
                if attempt == max_attempts - 1:
                    print(
                        f"[LLAMA-SERVER GAVE UP] tier={tier_label} "
                        f"model={model_id} after {max_attempts} "
                        f"attempts: {type(e).__name__}: {e}"
                    )
                    yield (
                        f"[{tier_label} llama-server error: server "
                        f"disconnected ({type(e).__name__}) after "
                        f"{max_attempts} attempt(s)]"
                    )
                    return
                print(
                    f"[LLAMA-SERVER RETRY] {type(e).__name__} on "
                    f"{tier_label}, sleeping 5s before retry "
                    f"{attempt + 2}/{max_attempts}: {e}"
                )
                await asyncio.sleep(5.0)
            except Exception as e:
                yield f"[{tier_label} llama-server error: {e}]"
                return