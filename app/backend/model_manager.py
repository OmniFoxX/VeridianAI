"""
VeridianAI Model Manager v2.1.6+ Phase 1C — three-tier routing (Updated with TimeManager)
====================================================================================
Replaces the v2.1 GGUF-via-llama-cpp-python path with HTTP routing
to a long-running `llama-server.exe` per tier. Each tier is a
separate process bound to a known port; the launcher (start.bat)
brings them up before the FastAPI backend starts.

Active tiers:
  - Oracle  : Ollama on 11434      (/api/chat, heavy reasoning)
  - Toga    : llama-server on 11435 (/v1/chat/completions, fast chat)
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
        "tier":    "Toga",         # or "Oracle", "Daemon"
        "url":     "http://127.0.0.1:11435",
        "size":    0,
        "loaded":  True,
    }

The frontend can display tier as a badge, group by tier, filter, etc.
"""

from __future__ import annotations
from time_manager import TimeManager

import asyncio
import time as _time
import heapq
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
    MODEL_SAGE,
    MODEL_DAEMON,
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
    (BACKEND_LLAMA_SAGE,    "Toga",   LLAMA_SAGE_URL,    "openai"),
    (BACKEND_LLAMA_DAEMON,  "Daemon", LLAMA_DAEMON_URL,  "openai"),
)

# Turn-terminator strings per tier, read once from each model's own chat
# template. Sent as `stop` alongside the --override-kv applied at tier launch:
# two independent brakes on the same failure, because a server build that
# ignores the override would otherwise put us straight back to a model that
# never stops. Empty for a self-consistent model, which sends no `stop` at all.
_STOP_CACHE: Dict[str, List[str]] = {}


def _tier_stop_strings(tier_label: str) -> List[str]:
    if tier_label in _STOP_CACHE:
        return _STOP_CACHE[tier_label]
    out: List[str] = []
    try:
        from gguf_probe import stop_strings
        path = {"Toga": MODEL_SAGE, "Daemon": MODEL_DAEMON}.get(tier_label)
        if path:
            out = stop_strings(path)
            if out:
                print(f"[ModelManager] {tier_label}: stop sequences {out} "
                      f"(from the model's chat template)")
    except Exception as e:
        print(f"[ModelManager] stop-sequence probe failed for {tier_label}: {e}")
        out = []
    _STOP_CACHE[tier_label] = out
    return out

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

# ---------------------------------------------------------------------------
# v2.15.2 TIMEOUT POLICY
#
# What went wrong (2026-08-20): Ollama wedged -- process alive, port accepting,
# 0.8s of CPU in 40 minutes, no model loaded, answering nothing. A turn hung
# for 21+ minutes showing "thinking", with no error, because every bound that
# could have caught it was set to 56000 seconds (15.5 HOURS): connect, read,
# write, pool, the metadata client, the stall watchdog and the tool watchdog.
# The comments beside those values said "5 min" and "3 min". The numbers were
# cranked during the Arc B580 era, when everything genuinely was that slow, and
# the comments were never updated -- so the system read as protected while
# being, in practice, unbounded.
#
# The policy now separates waits that have genuinely different natures instead
# of sizing one number for the slowest of them:
#
#   connect  -- a TCP handshake to 127.0.0.1. Succeeds in milliseconds or the
#               server is not accepting. NO hardware makes this slow, so a long
#               value buys nothing and costs the fast detection of a dead
#               server. This is the value that turned a wedge into a spinner.
#   read     -- for a STREAM this is the gap between chunks, not the total, so
#               it resets on every token. It must cover the longest legitimate
#               silence, which is the cold load before the first token.
#   write    -- pushing the request body over loopback. Bounded by prompt size.
#   pool     -- waiting for a free connection. We build a fresh client per
#               attempt, so this should never bind at all.
#   metadata -- /api/show and friends. Cheap lookups with a working fallback;
#               they must never be able to hang a turn. This one ran BEFORE
#               generation, so a wedged server hung the turn before the stream
#               was ever opened.
#
# The user-facing budget for a cold load lives in main.py's stall watchdog
# (stall_first_token_timeout_sec), which can explain itself to the user. These
# are backstops sized just above it, not the primary control.
# ---------------------------------------------------------------------------
_CONNECT_TIMEOUT = 10.0     # loopback: fast or broken
_WRITE_TIMEOUT   = 120.0    # generous for very large prompts
_POOL_TIMEOUT    = 30.0     # should never bind; fresh client per attempt
_META_TIMEOUT    = 30.0     # /api/show etc. -- must never hang a turn

# v2.11.13: priority levels for the per-server generation gate.
PRIORITY_LOCAL_URGENT  = 0
PRIORITY_LOCAL_NORMAL  = 1   # default for every request that doesn't say otherwise
PRIORITY_REMOTE_URGENT = 2
PRIORITY_REMOTE_NORMAL = 3


class _PriorityGate:
    """Async admission gate: one holder at a time, waiters admitted by
    (priority, arrival-sequence). Drop-in successor to the plain
    asyncio.Lock that serialized generations per server instance.
    Cancelled waiters (client disconnected while queued) are skipped."""

    def __init__(self):
        self._active = False
        self._waiters = []   # heap of (priority, seq, future)
        self._seq = 0

    async def acquire(self, priority: int = PRIORITY_LOCAL_NORMAL):
        if not self._active and not self._waiters:
            self._active = True
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (int(priority), self._seq, fut))
        self._seq += 1
        try:
            await fut
        except asyncio.CancelledError:
            # If we were already admitted between set_result and this await
            # resuming, pass the slot on; otherwise just leave the heap entry
            # (release() skips cancelled futures).
            if fut.done() and not fut.cancelled():
                self.release()
            raise

    def release(self):
        while self._waiters:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(None)   # gate stays active, handed to waiter
                return
        self._active = False


# ---------------------------------------------------------------------------
# Module-level helpers.
#
# v2.15.2: these live ABOVE `class ModelManager` on purpose. They were briefly
# placed below it, immediately before `    async def _gen_ollama(...)`, and a
# zero-indented `def` there ENDS THE CLASS BODY -- so _gen_ollama and
# _gen_llama_server stopped being methods and became nested functions inside
# _ollama_safe_messages. Still valid Python. Still parsed. Still passed every
# test, because the tests exercised the helpers and read the source as text,
# and never once asked whether ModelManager still had its methods. The app
# failed on the first real generation with:
#
#     'ModelManager' object has no attribute '_gen_ollama'
#
# test_reasoning_capture.py now asserts the class shape, so indentation cannot
# quietly relocate a method again.
# ---------------------------------------------------------------------------

def _turn_stats(options: Dict):
    """The per-turn side-channel, or None.

    v2.15.2. Both backends now have things worth reporting that are not
    tokens-to-display: the model's reasoning trace, and the server's own token
    counts. The generators yield plain strings and every consumer expects that,
    so the reporting cannot ride on the yield.

    It rides on `options` instead -- the caller passes in its OWN dict under
    "_turn_stats" and reads it after the stream ends. Deliberately NOT an
    attribute on the manager: that is a single slot on a process-wide object,
    and two concurrent chats would overwrite each other's stats. (That exact
    shape is what broke the parallel sub-agents in this same release.) A
    caller-owned dict cannot collide with anyone else's.

    The leading underscore marks it private, matching the existing
    options["_ident"] convention.
    """
    s = options.get("_turn_stats")
    return s if isinstance(s, dict) else None


# ---------------------------------------------------------------------------
# v2.15.2 MINIMUM ANSWER GUARANTEE
#
# A reasoning model can spend an entire generation budget in its thinking
# channel and emit zero answer tokens. _no_answer_notice made that legible --
# the user is told what happened instead of being ghosted -- and the tiered
# --reasoning-budget / think level makes it rarer. Neither actually produces an
# ANSWER, which is what the user asked for.
#
# So when a turn is about to end with reasoning and no reply, we ask once more,
# with thinking suppressed and an explicit instruction to answer now.
#
# Modelled on AIQNudge, which already solves "inject a directive mid-run and
# show the user it landed". What is deliberately NOT copied is its HMAC: that
# exists because a nudge arrives from OUTSIDE the process, as a file any local
# program could drop. This directive is composed here, in this function, from a
# constant. There is no trust boundary to cross, and signing our own string
# would be ceremony that implies a guarantee it does not provide.
#
# Sent as a USER turn, not a system one. Ollama rejects any system message that
# is not first -- the whole reason _ollama_safe_messages exists -- so appending
# a system directive here would trade a no-answer turn for a 500.
_ANSWER_NOW_DIRECTIVE = (
    "Your thinking budget for this turn is spent, and you have not yet given "
    "an answer. Do not reason any further. Reply now with your best answer "
    "using what you already worked out. If you are genuinely unsure, say so "
    "plainly and explain what you would need to be sure."
)


def _no_answer_notice(tier_label: str, reasoning_parts: List[str]) -> str:
    """What to say when the model thought and never answered.

    v2.15.2. A reasoning model can spend its entire generation budget inside
    the thinking block and emit zero content tokens. Both stream loops treated
    that as "nothing to yield" and returned in silence -- message sent, no
    reply, instantly the user's turn again, no clue why. Four re-prompts in a
    row for one news briefing, on 2026-08-17.

    That is the same ghosting the llama-server fallback path already has a
    comment about ("Ghosting the user hides real incompatibilities"). It was
    fixed there for the no-tokens-at-all case and missed here, because here
    tokens DID arrive -- they just all went to the reasoning channel.

    Says what happened and what to change, because "no reply" is not a symptom
    anyone can act on.
    """
    _chars = sum(len(p) for p in reasoning_parts)
    return (f"[{tier_label}: the model used its whole generation budget "
            f"thinking and produced no answer. {_chars:,} characters of "
            f"reasoning were captured. Raise max_tokens, or cap the thinking "
            f"with reasoning_budget, and ask again.]")


# ---------------------------------------------------------------------------
# v2.15.2: the Ollama half of the thinking budget.
#
# llama-server takes a NUMBER (--reasoning-budget N, in thinking tokens).
# Ollama takes a LEVEL. Probed against the live server (0.32.13,
# laguna-xs-2.1) rather than assumed, because the field is validated strictly:
#
#     think="banana"  -> HTTP 400  invalid think value: must be
#                                  "high", "medium", "low", "max", true, false
#     think="low" on llama3.2:3b   -> HTTP 400  does not support thinking
#     think=false on llama3.2:3b   -> 200       (accepted by everything)
#     think=false on laguna-xs     -> 200, thinking 0 chars, eval 9 (vs 101)
#
# Two lessons in those four lines. A positive level is NOT safe to send
# blindly -- it hard-fails the turn on any non-thinking model, which is most of
# them. And `false` IS safe everywhere, because suppressing thinking is
# meaningful even for a model that never thinks.
#
# That is the same shape as the mid-array system message that 500'd every
# Ollama turn earlier in this release: a field some models accept and others
# reject outright. So this one is guarded by capability detection rather than a
# hardcoded model list, which would rot the first time a model is added.
_THINK_UNSUPPORTED: set = set()          # model_ids that answered "no thinking"


def _ollama_think_value(budget):
    """Map a thinking-token budget onto Ollama's levels.

    A MAPPING, not an equivalence -- deliberately. Ollama's levels are
    qualitative and it does not accept a token count, so a number cannot be
    honoured exactly here the way llama-server honours it. The numeric budget
    stays authoritative on the llama tiers; this is the nearest faithful
    expression of the same intent on Ollama.

    Returns None to mean "send no field at all", which is how -1 (unrestricted)
    is expressed: Ollama's own default already is unrestricted, and omitting
    the key keeps a turn byte-identical to what it was before this feature.
    """
    try:
        b = int(budget)
    except (TypeError, ValueError):
        return None
    if b < 0:
        return None          # unrestricted -> leave Ollama's default alone
    if b == 0:
        return False         # no thinking at all; accepted by every model
    if b <= 2048:
        return "low"
    if b <= 8192:
        return "medium"
    return "high"


def _ollama_budget_for_tier(cfg, tier_label: str):
    """The configured thinking budget for an Ollama tier.

    Ollama tiers are not spawned by build_llama_server_command, so they cannot
    inherit its per-tier flag. The tier NAMES still line up: Oracle is the
    user's conversation (the sage budget), Daemon is background work.
    """
    try:
        import config as _cfg_mod
        _sage = getattr(_cfg_mod, "REASONING_BUDGET_SAGE", -1)
        _daemon = getattr(_cfg_mod, "REASONING_BUDGET_DAEMON", -1)
    except Exception:
        return None
    # A per-install override wins over the tier default, matching how every
    # other generation option in this file resolves.
    try:
        _override = cfg.get("reasoning_budget") if cfg else None
    except Exception:
        _override = None
    if _override is not None:
        return _override
    return _daemon if str(tier_label).lower() == "daemon" else _sage


def _ollama_safe_messages(messages: List[Dict]) -> List[Dict]:
    """Ollama refuses a system message anywhere except index 0.

    main.py deliberately injects volatile context -- the current date/time
    block, procedural memory, and the CRAIID warm handoff -- as `system`
    messages immediately BEFORE the final user turn, so that the cacheable
    system+history prefix stays byte-stable from turn to turn and only the tail
    is reprocessed. That is a good reason and it should stay.

    llama.cpp's chat templates accept system messages at any position. Ollama's
    renderer does not. It rejects the entire request in routes.go before any
    generation happens:

        msg="chat prompt error" error="system message must be at the beginning"
        POST /api/chat -> 500

    which is exactly why qwen3.8 and laguna-xs-2.1 failed instantly on the
    Ollama tier while the same conversation worked on the llama-server tiers.
    It was never about thinking tokens -- the request never reached the model.

    Verified against the live Ollama on 2026-08-19 with qwen3.8:27b-q4_K_M:
        [system, user]                       -> 200
        [system, user, system, user]         -> 500   (what we were sending)
        [system, user, user,   user]         -> 200   (this function's output)

    THE FIX, and why it is a relabel rather than a move: position is what
    carries the meaning. Hoisting these blocks to the front would put volatile,
    every-turn text back into the cacheable prefix and defeat the whole reason
    they sit at the tail. Dropping them would cost Toga the current date. So
    each non-leading system block is relabelled `user` IN PLACE -- same text,
    same position, a role Ollama accepts anywhere. The blocks are already
    self-delimiting ("=== CURRENT DATE & TIME ... === END DATE & TIME ==="), so
    nothing becomes ambiguous.

    It also slightly strengthens the guarantee main.py already documents for
    the warm handoff: content framed as data-not-instructions now carries user
    authority instead of system authority, so a hostile payload has less standing,
    not more.

    Applied ONLY on the Ollama path. The llama-server tiers keep the system
    role, because they were never the problem.
    """
    out, relabelled = [], 0
    for i, m in enumerate(messages):
        if i > 0 and m.get("role") == "system":
            m = dict(m)
            m["role"] = "user"
            relabelled += 1
        out.append(m)
    if relabelled:
        print(f"[OLLAMA] relabelled {relabelled} tail system block(s) to user "
              f"(Ollama requires system at index 0 only)")
    return out


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
            # v2.15.2: was 56000.0 (15.5h). This is a metadata lookup with a
            # working fallback, and it runs BEFORE the stream is opened -- so
            # on 2026-08-20 a wedged Ollama hung the turn HERE, ahead of any
            # generation timeout. See the timeout policy block above.
            async with httpx.AsyncClient(timeout=_META_TIMEOUT) as c:
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
                if not mid:
                    continue
                if mid in seen_ids:
                    # v2.12.4: same display id served by two tiers (e.g. an
                    # Ollama model and a Lemonade model sharing a name).
                    # Previously the later tier's model was silently DROPPED
                    # from the picker — it looked "not loaded" and could
                    # never be selected. Qualify it with the tier label
                    # instead so both stay pickable and routable.
                    alt = f"{mid} [{tier[1]}]"
                    if alt in seen_ids:
                        continue
                    raw = m.get("raw_id")
                    if raw:
                        # keep the real-id mapping for the qualified name too
                        self._openai_real_ids[alt] = raw
                    m = {**m, "id": alt, "name": alt}
                    mid = alt
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
            # v2.12.4: Path().stem chopped the id at its LAST DOT, which is
            # correct for llama-server (id = model FILE PATH, e.g.
            # ...\Qwen3-8B.Q4_K_M.gguf -> drop ".gguf") but mangles Lemonade
            # model NAMES that merely contain dots — v11 ids like
            # "Qwen3.5-4B-GGUF" displayed as "Qwen3", and two Qwen3.5
            # variants collapsed into one id (the second was dropped by the
            # picker's dedupe, looking "not loaded"/unselectable). Only drop
            # the directory part and a known model-file EXTENSION; a bare
            # model name keeps its dots.
            base = Path(raw_id).name if raw_id else raw_id
            stem = base
            for _ext in (".gguf", ".bin", ".safetensors"):
                if base.lower().endswith(_ext):
                    stem = base[: -len(_ext)]
                    break
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
                            options: Dict,
                            on_token=None) -> str:
        """Collect a full non-streaming response.

        v2.13 (2026-07-17 runaway incident): non-streaming callers (the
        agentic loop above all) were a blind spot — no watchdog feed, no
        console visibility. `on_token` (zero-arg, e.g. watchdog.record_token)
        is called per token so stall/runaway detection sees this path.
        Periodic [GEN SNAPSHOT] lines give raw-stream visibility without
        full verbosity: token count always; content tail only when
        gen_snapshot_content=true (privacy default: counts, not content).
        """
        result = ""
        n = 0
        last_snap = 0
        try:
            snap_every = int(self.config.get("gen_snapshot_every_tokens",
                                             2048))
        except (TypeError, ValueError):
            snap_every = 2048
        snap_content = bool(self.config.get("gen_snapshot_content", False))
        async for token in self.generate(messages, model_id, options):
            result += token
            n += 1
            if on_token is not None:
                try:
                    on_token()
                except Exception:
                    pass
            if snap_every > 0 and (n - last_snap) >= snap_every:
                last_snap = n
                if snap_content:
                    print(f"[GEN SNAPSHOT] n={n} chars={len(result)} "
                          f"tail={result[-120:]!r}", flush=True)
                else:
                    print(f"[GEN SNAPSHOT] n={n} chars={len(result)}",
                          flush=True)
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

    # v2.12.19: was a 1:1 vendor->toggle map, which got AMD badly wrong.
    #
    # It consulted ONLY rocm_enabled for AMD -- but on Windows, ROCm supports
    # discrete Radeon RX/PRO cards ONLY. It does not support Ryzen AI APU
    # integrated graphics at all, at any HSA_OVERRIDE setting. On an APU laptop
    # (e.g. Radeon 840M) Vulkan is the ONLY working GPU path, and there was no
    # way for an AMD user to say so: vulkan_enabled was wired to "intel".
    #
    # Vulkan is vendor-neutral, so it now counts for every vendor. A vendor is
    # considered "GPU-disabled" only when ALL of its usable paths are off.
    _VENDOR_TOGGLES = {
        "nvidia": ("cuda_enabled",   "vulkan_enabled"),
        "amd":    ("rocm_enabled",   "vulkan_enabled"),
        "intel":  ("openvino_enabled", "xe_cores_enabled", "vulkan_enabled"),
    }

    async def _gpu_offload_disabled(self) -> bool:
        """True -> force num_gpu=0 (CPU-only) on Ollama calls."""
        if not self.config.get("gpu_acceleration", True):
            return True
        vendors = await self._detected_gpu_vendors()
        if not vendors:
            return False   # no GPU to gate; Ollama is CPU-bound anyway
        # Disable offload only when EVERY detected vendor has every one of its
        # usable acceleration paths switched off.
        return all(
            all(not self.config.get(t, True) for t in self._VENDOR_TOGGLES[v])
            for v in vendors
        )

    def _gen_lock_for(self, base_url):
        """Per-server (base_url) PRIORITY gate so concurrent requests to the
        SAME server queue instead of evicting each other's loaded model.
        Different tiers (different ports) keep their own gate -> still parallel.

        v2.11.13 (Todd's Dad's question): the old asyncio.Lock was strict
        FIFO — no way to say 'this one matters more'. Now a priority gate:

            0 = LOCAL URGENT   (this machine's user, flagged urgent)
            1 = LOCAL NORMAL   (this machine's user — the default)
            2 = REMOTE URGENT  (Aether peer, urgent flag + quota granted)
            3 = REMOTE NORMAL  (Aether peer)

        FIFO within each level (arrival sequence breaks ties), local always
        outranks remote, and a RUNNING generation is never preempted —
        priority only reorders who goes NEXT."""
        # v2.12.2: aging-fair gate (request_scheduler.AsyncAgingGate) is the
        # default. scheduler_enabled=False falls back to the strict
        # _PriorityGate above (kept as the escape hatch, not deleted).
        cfg = self.config if isinstance(self.config, dict) else {}
        if bool(cfg.get("scheduler_enabled", True)):
            import request_scheduler
            gate = self._gen_locks.get(base_url)
            if gate is None or not isinstance(gate, request_scheduler.AsyncAgingGate):
                try:
                    _rate = max(0.001, float(cfg.get("scheduler_aging_rate", 0.05) or 0.05))
                except (TypeError, ValueError):
                    _rate = 0.05
                try:
                    _limit = max(1, int(cfg.get("scheduler_queue_limit", 24) or 24))
                except (TypeError, ValueError):
                    _limit = 24
                gate = request_scheduler.AsyncAgingGate(aging_rate=_rate,
                                                        queue_limit=_limit)
                self._gen_locks[base_url] = gate
            return gate
        gate = self._gen_locks.get(base_url)
        if gate is None or not isinstance(gate, _PriorityGate):
            gate = _PriorityGate()
            self._gen_locks[base_url] = gate
        return gate

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
        # (Ollama would otherwise evict the in-flight model mid-stream).
        # v2.11.13: FIFO -> priority gate. options["_priority"] is set by the
        # SERVER side only (ws chat: 0/1 local, node endpoints: 2/3 remote
        # after the urgent-quota check) — it is never accepted off the wire.
        try:
            _prio = int(options.get("_priority", PRIORITY_LOCAL_NORMAL))
        except (TypeError, ValueError):
            _prio = PRIORITY_LOCAL_NORMAL
        _prio = max(PRIORITY_LOCAL_URGENT, min(PRIORITY_REMOTE_NORMAL, _prio))
        _gate = self._gen_lock_for(base_url)
        # v2.12.19 GATE DIAGNOSTICS. Symptom being chased: on the Ryzen AI NPU
        # tier the FIRST prompt answers and every later one silently does
        # nothing. Prime suspect is this gate staying held -- release() lives in
        # a `finally` around an async generator (below), and an abandoned
        # generator does not run its finally promptly. Lemonade is known to end
        # turns abnormally (see the non-SSE note in _gen_openai), which is
        # exactly how a stream gets abandoned rather than exhausted.
        #
        # These three log lines make the failure unambiguous: if you see
        # GATE-WAIT with no matching GATE-HELD, the gate is the blocker. If you
        # see GATE-HELD then GATE-FREE every turn, it is NOT the gate and the
        # problem is inside Lemonade or the stream parser.
        _gate_t0 = _time.time()
        _held_by = getattr(ModelManager, "_gate_holder", {}).get(base_url)
        if _held_by:
            print(f"[GATE-WAIT] {tier_label} model={model_id} url={base_url} "
                  f"— gate already held by {_held_by['model']!r} for "
                  f"{_time.time() - _held_by['since']:.1f}s", flush=True)
        else:
            print(f"[GATE-WAIT] {tier_label} model={model_id} url={base_url} "
                  f"— gate free, acquiring", flush=True)
        # v2.12.2: aging-fair scheduling. _priority still authorizes URGENCY
        # (0/2 = urgent, granted server-side only), but ORDER among normal
        # waiters is now the aging score, so peers age up instead of starving
        # behind an endless local stream. Tier comes from the same server-set
        # seam: ws-chat = hyperlocal, node endpoints set _tier explicitly.
        import request_scheduler as _rs
        if isinstance(_gate, _rs.AsyncAgingGate):
            if _prio in (PRIORITY_LOCAL_URGENT, PRIORITY_LOCAL_NORMAL):
                _tier = "hyperlocal"
            else:
                _tier = str(options.get("_tier") or "remote")
            _urgent = _prio in (PRIORITY_LOCAL_URGENT, PRIORITY_REMOTE_URGENT)
            _adm = _gate.should_admit(_tier, urgent=_urgent)
            if not _adm["admit"]:
                # Same bracketed-error convention as the rest of this file:
                # main._is_gen_error() recognizes it, so the node path tries
                # its next candidate model/tier and local users get a clear,
                # non-fatal message instead of an unbounded queue.
                yield ("[Error: busy — " + _adm["reason"] + ". "
                       + ("Try a lighter model or retry shortly."
                          if _adm["suggest_downshift"] else "Please retry shortly.") + "]")
                return
            await _gate.acquire(_tier, urgent=_urgent,
                                ident=str(options.get("_ident") or ""))
        else:
            await _gate.acquire(_prio)
        if not hasattr(ModelManager, "_gate_holder"):
            ModelManager._gate_holder = {}
        ModelManager._gate_holder[base_url] = {"model": model_id,
                                               "tier": tier_label,
                                               "since": _time.time()}
        print(f"[GATE-HELD] {tier_label} model={model_id} "
              f"(waited {_time.time() - _gate_t0:.2f}s)", flush=True)
        _tok_count = 0
        try:
            async for token in gen:
                if self._abort:
                    return
                _tok_count += 1
                yield token
        finally:
            # This finally is the thing under suspicion. If the consumer
            # abandons us mid-stream, Python defers it to generator
            # finalization — which may be much later, or effectively never.
            # The log line proves whether it ran.
            ModelManager._gate_holder.pop(base_url, None)
            print(f"[GATE-FREE] {tier_label} model={model_id} "
                  f"tokens={_tok_count} held={_time.time() - _gate_t0:.2f}s",
                  flush=True)
            _gate.release()

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
            # v2.15.2: see _ollama_safe_messages -- Ollama 500s on any system
            # message that is not first, which is what main.py's tail-injected
            # date/procedural/warm-handoff blocks are.
            "messages": _ollama_safe_messages(messages),
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

        # v2.15.2 thinking budget. See _ollama_think_value for why this is a
        # level rather than a number, and _THINK_UNSUPPORTED for why it is
        # capability-gated instead of model-listed.
        _think = _ollama_think_value(
            _ollama_budget_for_tier(self.config, tier_label))
        # A positive level 400s on a model that cannot think. `false` never
        # does, so it stays even for a model we have already learned about --
        # suppressing thinking is a meaningful instruction to any model.
        if _think is not None and not (
                _think is not False and model_id in _THINK_UNSUPPORTED):
            payload["think"] = _think

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
        # actual generation inside a 300s window.
        #
        # v2.15.2 (2026-08-20): that reasoning was sound and the number that
        # followed it was not. The comment said the default became 1800s; the
        # code said 56000.0 -- 15.5 hours -- and the Arc B580 it was sized for
        # is gone. A comment describing a value the code does not hold is
        # worse than no comment: it is why a wedged Ollama read as "thinking"
        # for 21 minutes instead of erroring.
        #
        # Now 900s (15 min). For a STREAM httpx applies this per chunk, not to
        # the whole response, so it resets on every token -- it bounds the
        # longest legitimate SILENCE, which is the cold load before the first
        # token. It sits just above the user-facing 900s cold-load budget in
        # main.py's stall watchdog, which is the control that can actually
        # explain itself to the user. Raise config.ollama_read_timeout_sec if
        # a genuine cold load ever needs longer.
        _read_timeout = float(
            self.config.get("ollama_read_timeout_sec", 900.0)
        )
        client_timeout = httpx.Timeout(
            connect=_CONNECT_TIMEOUT, read=_read_timeout,
            write=_WRITE_TIMEOUT, pool=_POOL_TIMEOUT,
        )
        max_attempts = 4
        # v2.15.2: one-shot latch for the minimum answer guarantee. A dict, not
        # a bool, because _attempt is a closure that must MUTATE it -- a plain
        # local would need `nonlocal` inside a nested async generator, and this
        # reads more obviously as shared state at the call site.
        _answered_retry = {"done": False}

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
                        # v2.15.2: a `think` rejection is RECOVERABLE, and must
                        # be recovered rather than shown to the user.
                        #
                        # Ollama 400s with "does not support thinking" for any
                        # model without a thinking channel -- which is most of
                        # them. Turning that into a failed turn would mean this
                        # feature broke every non-reasoning model, which is the
                        # exact blast radius the mid-array system message had
                        # before _ollama_safe_messages.
                        #
                        # So: drop the field, remember it for this model, and
                        # retry once. The memo means one model pays this cost
                        # once, not on every turn. Nothing is listed in advance,
                        # so a model added tomorrow is handled the same way.
                        if (resp.status_code == 400
                                and "think" in body.lower()
                                and "think" in payload):
                            _bad = payload.pop("think")
                            _THINK_UNSUPPORTED.add(model_id)
                            print(f"[OLLAMA] {model_id} rejected think="
                                  f"{_bad!r} ({body[:90]!r}); retrying without "
                                  f"it and not sending it to this model again.")
                            async for _tok in _attempt(attempt_idx):
                                yield _tok
                            return
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
                    _stats = _turn_stats(options)
                    _reasoning_parts = []
                    _content_seen = False
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        _msg = chunk.get("message") or {}
                        # v2.15.2: Ollama puts the model's reasoning in
                        # message.thinking, NOT message.content. We were
                        # reading only content, so on a thinking model the
                        # trace was generated, streamed to us, and dropped.
                        _think = _msg.get("thinking")
                        if _think:
                            _reasoning_parts.append(_think)
                        content = _msg.get("content", "")
                        if content:
                            _content_seen = True
                            yield content
                        if chunk.get("done"):
                            # The final chunk carries the server's OWN token
                            # counts. They were being discarded by this return.
                            if _stats is not None:
                                _stats["prompt_tokens"] = chunk.get(
                                    "prompt_eval_count")
                                _stats["completion_tokens"] = chunk.get(
                                    "eval_count")
                                _stats["n_ctx"] = effective_ctx
                                _stats["backend"] = "ollama"
                                _stats["tier"] = tier_label
                                _stats["base_url"] = base_url
                                if _reasoning_parts:
                                    _stats["reasoning"] = "".join(
                                        _reasoning_parts)
                            if not _content_seen and _reasoning_parts:
                                # v2.15.2 minimum answer guarantee. The model
                                # thought and never replied. Ask once more with
                                # thinking OFF and an explicit instruction to
                                # answer, instead of handing back an
                                # explanation of the failure and nothing else.
                                #
                                # think=False is the lever here because it is
                                # the one value Ollama accepts from EVERY model
                                # (verified 2026-08-20: a positive level 400s
                                # on non-thinking models, false never does),
                                # and on a thinking model it genuinely empties
                                # the channel -- laguna-xs went from 383 chars
                                # of thinking to 0, eval 101 to 9.
                                #
                                # ONCE. A model that answers nothing twice is
                                # telling us something, and a loop here would
                                # turn one bad turn into an unbounded one.
                                if not _answered_retry["done"]:
                                    _answered_retry["done"] = True
                                    payload["think"] = False
                                    payload["messages"] = list(
                                        payload["messages"]) + [{
                                            "role": "user",
                                            "content": _ANSWER_NOW_DIRECTIVE,
                                        }]
                                    print(
                                        f"[ANSWER GUARANTEE] {tier_label} "
                                        f"{model_id} produced "
                                        f"{sum(len(p) for p in _reasoning_parts)}"
                                        f" chars of reasoning and no answer; "
                                        f"retrying once with thinking off.")
                                    async for _tok in _attempt(attempt_idx):
                                        yield _tok
                                    return
                                yield _no_answer_notice(
                                    tier_label, _reasoning_parts)
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
        # v2.12.4: strict chat templates (Qwen3.5-era) hard-require exactly
        # ONE system message, at position 0. llama.cpp otherwise aborts with
        # "Unable to generate parser for this template" (HTTP 400) before
        # generating anything. Toga turns can legitimately carry extra
        # system-role entries (session boundary markers, injected context),
        # so merge every system message into a single leading one for
        # OpenAI-protocol tiers. Order is preserved; non-system messages are
        # untouched. No-op for the common one-leading-system case.
        _sys_texts = [str(m.get("content", "")) for m in messages
                      if isinstance(m, dict) and m.get("role") == "system"]
        _first_is_sys = bool(messages) and isinstance(messages[0], dict) \
            and messages[0].get("role") == "system"
        if len(_sys_texts) > 1 or (_sys_texts and not _first_is_sys):
            _rest = [m for m in messages
                     if not (isinstance(m, dict) and m.get("role") == "system")]
            messages = [{
                "role": "system",
                "content": "\n\n".join(t for t in _sys_texts if t),
            }] + _rest
            print(f"[LLAMA-SERVER] merged {len(_sys_texts)} system message(s) "
                  f"into one leading system turn (strict-template compat, "
                  f"tier={tier_label})")
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
        _stops = _tier_stop_strings(tier_label)
        if _stops:
            payload["stop"] = _stops
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
            self.config.get("ollama_read_timeout_sec", 900.0)
        )
        client_timeout = httpx.Timeout(
            connect=_CONNECT_TIMEOUT, read=_read_timeout,
            write=_WRITE_TIMEOUT, pool=_POOL_TIMEOUT,
        )
        max_attempts = 4
        # v2.15.2: one-shot latch for the minimum answer guarantee. See the
        # matching latch in _gen_ollama.
        _answered_retry = {"done": False}

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
                    _sse_tokens = 0          # v2.12.19 diagnostics
                    # v2.15.2 reasoning + usage capture. See _turn_stats.
                    _stats = _turn_stats(options)
                    _reasoning_parts = []
                    _finished = False
                    _finish = None
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
                        # v2.15.2: the server's own token counts, which are
                        # what restores CRAIID's context-fill signal now that
                        # llamacpp:kv_cache_usage_ratio is gone from llama.cpp.
                        #
                        # Read BOTH shapes, because which one you get depends on
                        # the build. An earlier draft of this set
                        # stream_options.include_usage and trusted `usage` --
                        # then a string search of llama-server.exe 8639 came
                        # back with include_usage and stream_options ABSENT.
                        # The flag would have been accepted, ignored, and this
                        # whole feature would have silently done nothing on the
                        # llama-server tiers while looking correct in the diff.
                        # `timings` (prompt_n / predicted_n) IS in that binary.
                        #
                        # Neither present just means no counts this turn. The
                        # consumer already treats that as "no data".
                        if _stats is not None:
                            _usage = chunk.get("usage")
                            if isinstance(_usage, dict):
                                _stats["prompt_tokens"] = _usage.get(
                                    "prompt_tokens")
                                _stats["completion_tokens"] = _usage.get(
                                    "completion_tokens")
                            _tm = chunk.get("timings")
                            if isinstance(_tm, dict):
                                if _tm.get("prompt_n") is not None:
                                    _stats["prompt_tokens"] = _tm.get("prompt_n")
                                if _tm.get("predicted_n") is not None:
                                    _stats["completion_tokens"] = _tm.get(
                                        "predicted_n")
                            if _usage or chunk.get("timings"):
                                _stats["backend"] = "llama-server"
                                _stats["tier"] = tier_label
                                _stats["base_url"] = base_url
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        # v2.15.2: with --reasoning-format auto (the default on
                        # this build) llama-server EXTRACTS the thinking out of
                        # content and puts it here. Reading only `content` meant
                        # the trace was produced, parsed, streamed to us, and
                        # dropped on the floor -- and on a turn where the model
                        # thought its whole budget away, content stayed empty
                        # and the turn ended in silence.
                        _rc = delta.get("reasoning_content")
                        if _rc:
                            _reasoning_parts.append(_rc)
                        # 'content' is standard; 'text' covers legacy /
                        # completion-style deltas some servers emit.
                        content = delta.get("content") or delta.get("text") \
                            or choice.get("text") or ""
                        if content:
                            _yielded = True
                            _sse_tokens += 1
                            yield content
                        if choice.get("finish_reason") is not None:
                            # Do NOT break: the usage chunk is emitted AFTER
                            # finish_reason. Breaking here is what would have
                            # thrown it away even with include_usage set.
                            _finished = True
                            continue
                    # v2.12.19: one line per completed stream, so a hung tier
                    # can be told apart from a tier that answered and then had
                    # its gate leak. Pair this with [GATE-FREE].
                    print(f"[STREAM-END] tier={tier_label} model={model_id} "
                          f"sse_tokens={_sse_tokens} yielded={_yielded} "
                          f"reasoning_chars="
                          f"{sum(len(p) for p in _reasoning_parts)} "
                          f"non_sse_lines={len(_raw_lines)}", flush=True)
                    if _stats is not None and _reasoning_parts:
                        _stats["reasoning"] = "".join(_reasoning_parts)
                    if _yielded:
                        return
                    # v2.15.2: the model thought and never answered. Before
                    # this, _yielded stayed False, _raw_lines was empty (it was
                    # all SSE), and the function returned having yielded
                    # NOTHING -- which is precisely the "empty response, my
                    # turn again" that cost four re-prompts. Tokens did arrive;
                    # they all went to the reasoning channel.
                    if _reasoning_parts:
                        # v2.15.2 minimum answer guarantee, llama-server side.
                        # Same contract as the Ollama path: ask once more, with
                        # thinking suppressed and an explicit instruction to
                        # answer, rather than returning only an explanation.
                        #
                        # The levers differ, and honestly so. Ollama has
                        # think=False, which is VERIFIED to empty the channel.
                        # Here the directive is the load-bearing part:
                        # reasoning_budget=0 is llama.cpp's documented
                        # per-request control and this build ACCEPTS it (200),
                        # but the Toga tier had no thinking model loaded when
                        # this was probed, so its effect could not be proven --
                        # only that it is harmless to send. Sent for that
                        # reason and not claimed as the mechanism.
                        #
                        # ONCE, latched. A model that answers nothing twice is
                        # telling us something a loop would only amplify.
                        if not _answered_retry["done"]:
                            _answered_retry["done"] = True
                            payload["reasoning_budget"] = 0
                            payload["messages"] = list(payload["messages"]) + [{
                                "role": "user",
                                "content": _ANSWER_NOW_DIRECTIVE,
                            }]
                            print(
                                f"[ANSWER GUARANTEE] {tier_label} {model_id} "
                                f"produced "
                                f"{sum(len(p) for p in _reasoning_parts)} "
                                f"chars of reasoning and no answer; retrying "
                                f"once with an answer-now directive.")
                            async for _tok in _attempt(_attempt_idx):
                                yield _tok
                            return
                        yield _no_answer_notice(tier_label, _reasoning_parts)
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
                        _head = " ".join(_raw_lines)[:200]
                        print(
                            f"[LLAMA-SERVER WARN] tier={tier_label} "
                            f"model={model_id} returned no stream tokens; "
                            f"body head: {_head!r}"
                        )
                        # v2.12.4: surface it in the chat too. This path used
                        # to end the turn SILENTLY (message sent, instantly
                        # the user's turn again, no clue why) — e.g. Lemonade
                        # answering 200 with an {"error": ...} body after a
                        # version change. Ghosting the user hides real
                        # incompatibilities; a visible one-liner gets them
                        # (and us) straight to the cause.
                        yield (f"[{tier_label}: server returned no tokens — "
                               f"{_head[:120]}]")
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