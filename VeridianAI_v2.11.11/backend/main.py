"""
VeridianAI v2.12 Backend — FastAPI Server v2
Handles inference, hardware, plugins, Sage engine, archives, and WebSocket chat.
"""

import sys
sys.stdout.reconfigure(encoding='utf-8')
import contextvars  # v2.1.8 #56: ambient stall-watchdog handle
import sage_engine
from memory_logger_surprise import MemoryLogger
from procedural_memory import ProceduralMemory
from aiq_nudge import AIQNudge, NudgeError  # v2.1.10 #44: HMAC-signed mid-run side-channel
from plugin_manager import PluginManager
from model_manager import ModelManager
from hw_utils import detect_hardware
import asyncio
import json
import os                          # needed early: boot-time ComfyUI check (below) uses os.environ
import sys
import re
import shutil
import threading                # v2.1.5: needed by [PRIORITISE:] handler
from time_manager import TimeManager  # v2.1.6 unified time source
import time                     # v2.1.5: also fixes a latent bug at line ~330
from pathlib import Path
from typing import Dict

import uvicorn
from fastapi import FastAPI, UploadFile, File, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent))

# --- Paths --------------------------------------------------------------------
# v2.1.4 Phase 0: paths now come from config.py (auto-creates sage_data/).
# Memory and logs live in sage_data; uploads/downloads/archives/plugins/
# frontend/models still live inside the project folder for now and will
# migrate in later phases as those concerns are touched.
from config import (
    PROJECT_DIR,
    DATA_DIR,
    MEMORY_DIR,
    LOG_DIR,
    PROCEDURAL_DIR,
)

# Phase 1D Step 4: tier lifecycle management
import tier_lifecycle

BASE_DIR = PROJECT_DIR  # alias kept so existing route code keeps working
FRONTEND_DIR = BASE_DIR / "frontend"
PLUGINS_DIR = BASE_DIR / "plugins"
MODELS_DIR = BASE_DIR / "models"
CONFIG_FILE = BASE_DIR / "config.json"
DOWNLOADS_DIR = BASE_DIR / "downloads"


# --------------------------------------------------------------------------- #
#  ComfyUI setup check (non-blocking, runs once at boot)
# --------------------------------------------------------------------------- #
def _check_comfyui_setup():
    """Silently check if ComfyUI is present at boot. If not, flag it so the
    UI can offer the setup wizard when the user first tries to generate an image.
    Never raises -- this is a background advisory check only."""
    try:
        import comfyui_setup
        existing = comfyui_setup.detect_existing()
        if existing:
            # Already installed -- make sure COMFYUI_HOME is live in this process.
            os.environ.setdefault("COMFYUI_HOME", existing)
            print(f"[ComfyUI] Found at {existing}", flush=True)
        else:
            # Not installed -- set a flag the UI can query via an endpoint.
            os.environ["COMFYUI_SETUP_REQUIRED"] = "1"
            print("[ComfyUI] Not found -- setup wizard will be offered on first image generation.", flush=True)
    except Exception as e:
        print(f"[ComfyUI] Setup check failed (non-fatal): {e}", flush=True)

_check_comfyui_setup()


# ---------------------------------------------------------------------------
# v2.1.8 Phase 2 — TaskP routing helper (Todd + Leo integration brief).
#
# What this does
# --------------
# Wraps the existing run_in_executor pattern that every SAFE tool dispatch
# uses (search, weather, browse, web_search, search_memory, recall) and
# adds one decision at the top:
#
#   * FEATURES_ENABLED["task_prioritiser"] is OFF -> run fn() exactly the
#     way main.py did pre-Phase-2: loop.run_in_executor(None, fn). Same
#     behavior, same return value, same exception semantics. Toggle-off
#     is byte-identical to v2.1.7.
#
#   * FEATURES_ENABLED["task_prioritiser"] is ON -> submit fn() to the
#     Oracle dispatcher (sage_engine.oracle_d). The dispatcher applies
#     urgency-based scheduling, per-task timeout, retry, and routes to
#     the best-performing subagent for the task type. We block this
#     coroutine until the work is done (or our async-friendly timeout
#     fires) and surface fn's return value or exception unchanged.
#
# What it does NOT do
# -------------------
# Memory WRITES, hash chain appends, Fernet encryption, and existing
# memory modifications are NEVER routed through here. The brief is
# explicit and the codebase agrees: those paths sit in memory_logger,
# procedural_memory, and the REMEMBER / REMEMBER_FAIL handlers, and
# they call their writers directly. The helper is only invoked from
# the SAFE-list dispatch sites in the agentic loop.
#
# Why a helper and not inline checks at each site
# -----------------------------------------------
# Six dispatch sites with the same toggle/submit/wait pattern means
# six copies of the same defensive code, six places to keep in sync,
# and six chances to drift. One helper, six one-line call-site
# changes. Easy to read, easy to revert (one delete + six edits).
# ---------------------------------------------------------------------------
async def _taskp_run_or_direct(
    tool_name: str,
    fn,
    *,
    importance: float = 0.5,
    timeout_seconds: float = 56000.0,
):
    """Dispatch fn() through TaskP when the toggle is on, else direct.

    Args:
        tool_name: Short label for the task, surfaced in the prioritiser's
            stats so per-task-type performance tracking is meaningful.
        fn: Zero-arg callable to run. Build with a lambda when the real
            handler needs arguments, e.g.
              lambda: sage_engine.web_search(content, search_type="news")
        importance: 0.0 - 1.0, fed to OAgentP.compute_urgency. Default
            0.5 matches the brief's "adjust per task type" intent.
        timeout_seconds: Outer-async timeout. The prioritiser also has
            its own per-task TASK_TIMEOUT (30s as of v2.1.8); this is a
            second-layer wait so a wedged dispatcher can't hang the
            agentic loop forever.

    Returns: whatever fn() returns.
    Raises:  whatever fn() raised, or TimeoutError if neither the inner
             dispatcher nor fn completed in timeout_seconds.
    """
    # v2.1.8 #56: record tool dispatch start for the stall watchdog.
    # The watchdog sits in a ContextVar — None when running outside the
    # WS chat handler (e.g. unit tests) so we no-op in that case.
    _wd = _current_watchdog.get()
    if _wd is not None:
        _wd.record_tool_call(tool_name)

    # OFF path: identical to pre-Phase-2 behavior. No TaskP, no overhead.
    if not sage_engine.is_feature_enabled("task_prioritiser"):
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, fn)
        finally:
            if _wd is not None:
                _wd.record_tool_result()

    # ON path: submit to oracle_d, wait async-style, surface result.
    # We don't rely on OAgentD.get_results() because that list is
    # cumulative and we only care about THIS submission. A threading
    # Event + result box gives us a private completion signal that
    # doesn't get tangled up with other in-flight tasks.
    done_event = threading.Event()
    result_box = {"value": None, "error": None}

    def runner():
        try:
            result_box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - we re-raise below
            result_box["error"] = e
        finally:
            done_event.set()

    sage_engine.oracle_d.submit_raw_task({
        "type":       tool_name,
        "fn":         runner,
        "importance": importance,
    })

    # Cooperative wait. asyncio.sleep(0.05) yields the event loop so
    # the rest of the server (ping, abort, WS sends) stays responsive
    # while the worker thread does its thing.
    waited = 0.0
    while not done_event.is_set() and waited < timeout_seconds:
        await asyncio.sleep(0.05)
        waited += 0.05

    if not done_event.is_set():
        # v2.1.8 #56: clear the watchdog's pending-tool slot even on
        # TaskP-internal timeout so we don't double-fire a stall later.
        if _wd is not None:
            _wd.record_tool_result()
        raise TimeoutError(
            f"[TaskP] {tool_name} did not complete within "
            f"{timeout_seconds:.0f}s. The task may still finish in the "
            f"background, but this dispatch is being abandoned."
        )

    if _wd is not None:
        _wd.record_tool_result()

    if result_box["error"] is not None:
        # Re-raise so the dispatch site's existing try/except surfaces
        # the same error string it would have without TaskP in front.
        raise result_box["error"]
    return result_box["value"]


# ---------------------------------------------------------------------------
# v2.1.8 #56 — Stall detection for long autonomous runs.
#
# Today's pain (2026-05-15): Sage processed REF.md for 90 min successfully
# AFTER Todd bumped ollama_read_timeout_sec from 1800 to 10800. But during
# the diagnosis, two 500s fired at exactly 30 min (the old read timeout)
# and the overseer auto-restarted the server. Sage's run was orphaned —
# the user had to send a fresh prompt to recover.
#
# What this is
# ------------
# Two parallel-running watchdogs that detect when the agentic loop has
# gone silent (no progress for too long) and abort the run cleanly:
#
#   * Token watchdog: fires if no LLM token has arrived in
#     stall_token_timeout_sec. Catches Ollama/llama-server hangs,
#     httpx ReadTimeouts that get swallowed, model crashes mid-stream.
#     Default 300s — well past any reasonable cold-load on a sensible
#     model. The httpx Ollama timeout (default 1800s, Todd's 10800s)
#     guards the time-to-first-byte. This guards time-between-tokens.
#     Currently intentionally set very high during development by Todd.
#
#   * Tool watchdog: fires if a tool_call WS message was sent but the
#     matching tool_result hasn't arrived in stall_tool_timeout_sec.
#     Default 180s. TaskP's per-task timeout (60-120s for most tools)
#     handles the common case; this is the belt for TaskP's
#     suspenders — catches the rare case where TaskP itself wedges or
#     the dispatch try/except hangs on something weird.
#     Currently intentionally set very high during development by Todd.
#
# What it is NOT
# --------------
# - It does NOT re-prompt Sage. Recovery action = abort cleanly + UI
#   banner. The user decides whether to retry. Re-prompting would
#   re-engage the AIQNudge self-prompt-injection surface that #44 is
#   queued to audit — strictly off-limits until that audit is done.
# - It does NOT touch Fernet, the hash chain, or chat_memory writes.
#   On stall, we set model_manager._abort and let the existing teardown
#   code paths run normally.
# - It does NOT replace existing timeouts. ollama_read_timeout_sec
#   still applies to httpx; TaskP's per-task timeout still applies to
#   each tool. The stall watchdogs are a higher-level safety net.
# ---------------------------------------------------------------------------
class _StallWatchdog:
    """Tracks last-token + pending-tool timestamps. Set abort + WS signal
    on stall. One instance per WS chat handler invocation; lives for
    the duration of one Sage run.
    """

    def __init__(self, token_timeout_sec: float, tool_timeout_sec: float):
        self.token_timeout    = token_timeout_sec
        self.tool_timeout     = tool_timeout_sec
        # Initialize last_token_ts to "now" so the time-to-first-token
        # is part of the budget. Cold-load is the user's responsibility
        # to size correctly via stall_token_timeout_sec and the
        # underlying ollama_read_timeout_sec.
        self.last_token_ts    = time.time()
        self.pending_tool_ts  = None
        self.pending_tool     = None
        self.stalled          = False
        self.stall_reason     = None
        self._stop            = False

    def record_token(self):
        self.last_token_ts = time.time()

    def record_tool_call(self, tool_name: str):
        self.pending_tool_ts = time.time()
        self.pending_tool    = tool_name

    def record_tool_result(self):
        self.pending_tool_ts = None
        self.pending_tool    = None

    def stop(self):
        self._stop = True

    async def watch(self, on_stall):
        """Run until stop()'d or stall detected. Calls on_stall(reason)
        exactly once if a stall is detected, then returns. The caller
        is responsible for cancelling/awaiting the task on shutdown.
        """
        while not self._stop:
            await asyncio.sleep(2.0)
            if self._stop:
                return
            now = time.time()

            tok_gap = now - self.last_token_ts
            if tok_gap > self.token_timeout:
                self.stalled = True
                self.stall_reason = (
                    f"No tokens received in {tok_gap:.0f}s "
                    f"(limit {self.token_timeout:.0f}s). The model may "
                    f"have hung mid-generation."
                )
                try:
                    await on_stall(self.stall_reason)
                except Exception:
                    pass
                return

            if self.pending_tool_ts is not None:
                tool_gap = now - self.pending_tool_ts
                if tool_gap > self.tool_timeout:
                    self.stalled = True
                    self.stall_reason = (
                        f"Tool '{self.pending_tool}' has not produced a "
                        f"result in {tool_gap:.0f}s (limit "
                        f"{self.tool_timeout:.0f}s). The tool may be wedged."
                    )
                    try:
                        await on_stall(self.stall_reason)
                    except Exception:
                        pass
                    return


async def _node_stream_tokens(messages, options):
    """Async-generator: stream DECRYPTED tokens from the remote node's
    /api/node/infer-stream (Sage Network streaming offload). Each remote line is
    one Fernet message. Yields nothing on any failure (caller falls back)."""
    import uuid
    import httpx
    import node_trust
    remote = (config.get("remote_node_url") or "").strip()
    token = node_trust.load_or_create_home_token(str(DATA_DIR))
    env = {"v": 1, "user": "owner", "session": uuid.uuid4().hex,
           "kind": "infer_stream",
           "body": {"model_id": None, "messages": messages, "options": options or {},
                    # v2.11.13: a local-urgent request stays urgent when
                    # offloaded — the remote applies its quota + trust gate
                    # before honoring it (see /api/node/infer-stream).
                    "urgent": int((options or {}).get("_priority", 1)) == 0}}
    blob = node_trust.encrypt_payload(env, token)
    try:
        _to = httpx.Timeout(connect=15.0, read=600.0, write=15.0, pool=15.0)
        async with httpx.AsyncClient(timeout=_to) as c:
            async with c.stream("POST", remote.rstrip("/") + "/api/node/infer-stream",
                                content=blob,
                                headers={"Content-Type": "application/octet-stream"}) as resp:
                if resp.status_code != 200:
                    return
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    ok, obj = node_trust.decrypt_payload(line.encode("utf-8"), token)
                    if not ok or not isinstance(obj, dict):
                        continue
                    if obj.get("end") or "e" in obj:
                        return
                    if "t" in obj:
                        yield obj["t"]
    except Exception:
        return


async def _watched_generate(messages, model_id, options, watchdog):
    """Wrap model_manager.generate() so every yielded token bumps the
    watchdog's last-token timestamp. Lets us add stall detection at all
    5 existing token-yield sites with a one-line replacement at each
    (use _watched_generate instead of model_manager.generate)."""
    # Sage Network offload: when enabled + a remote node is set, STREAM the
    # inference from the remote (the desktop's model) token-by-token. Falls back
    # to a single-block offload if streaming is unavailable, then to LOCAL if the
    # node is unreachable. model_id=None -> the desktop picks (a vision model for
    # an image-bearing turn).
    try:
        if (bool(config.get("offload_enabled", False))
                and (config.get("remote_node_url") or "").strip()):
            _got = False
            async for _tok in _node_stream_tokens(messages, options):
                _got = True
                watchdog.record_token()
                yield _tok
            if _got:
                _current_offload.set((config.get("remote_node_url") or "").strip())
                return
            try:
                import node_trust
                import node_client
                _remote = config.get("remote_node_url").strip()
                _token = node_trust.load_or_create_home_token(str(DATA_DIR))
                _ok, _res = await asyncio.to_thread(
                    node_client.node_infer, _remote, _token, None, messages, options,
                    "owner", 300,
                    int((options or {}).get("_priority", 1)) == 0)  # urgent carries over
                if (_ok and isinstance(_res, dict) and _res.get("success")
                        and _res.get("content") is not None):
                    watchdog.record_token()
                    _current_offload.set(_remote)
                    yield _res["content"]
                    return
            except Exception:
                pass
            # both offload paths failed -> fall through to local
    except Exception:
        pass
    # LOCAL generation with graceful fallback across capable slots so a
    # standalone rig "just works" too: an unloadable model (e.g. mllama) -> the
    # next capable slot. Single-model setups behave exactly as before.
    _nv_local = any(isinstance(_m, dict) and _m.get("images") for _m in messages)
    _local_cands = _node_model_candidates(messages, model_id, _nv_local) or (
        [model_id] if model_id else [])
    _local_err = ""
    for _lc in _local_cands:
        _first = True
        _failed = False
        _yielded = False
        try:
            async for token in model_manager.generate(messages, _lc, options):
                if _first:
                    _first = False
                    if _is_gen_error(token):
                        _local_err = token
                        _failed = True
                        break
                watchdog.record_token()
                yield token
                _yielded = True
        except Exception as _e:
            _local_err = str(_e)
            _failed = True
        if not _failed:
            return
        if _yielded:
            return
    if _local_err:
        yield _local_err


# v2.1.8 #56: ambient handle to the per-turn watchdog. Set at the top
# of each WS chat turn; consumed inside _taskp_run_or_direct so the
# tool-stall side gets recorded without having to thread the watchdog
# through every call site. ContextVar is async-safe and per-task, so
# concurrent turns can't accidentally share state.
_current_watchdog: contextvars.ContextVar = contextvars.ContextVar(
    "_current_stall_watchdog", default=None,
)

# v2.9: per-turn flag -> set to the remote node URL when an inference for THIS
# turn actually ran on a Sage Network node, so the UI shows a "ran on [node]"
# badge only when offload truly happened (not merely when the toggle is on).
_current_offload: contextvars.ContextVar = contextvars.ContextVar(
    "_current_offload_node", default=None,
)


# ---------------------------------------------------------------------------
# v2.1.8 #55 — model-aware prompt tier selection.
#
# Maps an Ollama-style model id (e.g. "llama3.2:3b", "qwen2.5:7b",
# "gemma4:31b") to a prompt tier so the composition step in the WS
# chat handler can swap in a SMALL-model-friendly prompt for tiny
# models that can't follow the full SAGE_SYSTEM_PROMPT reliably.
#
# Heuristic: pull the first "<n>b" token out of the name and compare
# against a threshold. ≤4B → "small", anything else → "full". Unknown
# (no number-b in name) defaults to "full" — conservative, never
# downgrades a model that might actually handle the full prompt.
#
# The threshold (4B) was picked empirically from #55 testing — the
# format-fidelity cliff sits between 3B and 7B. 3B models hallucinate
# tags; 7B models follow the full prompt fine.
# ---------------------------------------------------------------------------
import re as _re_size

_MODEL_SIZE_PATTERN = _re_size.compile(r"(\d+(?:\.\d+)?)\s*b\b", _re_size.IGNORECASE)
_SMALL_TIER_THRESHOLD_B = 4.0


def _model_size_hint(model_id) -> str:
    """Return 'small' or 'full' based on a heuristic of the model name.

    Examples:
        _model_size_hint("llama3.2:3b")             -> "small"
        _model_size_hint("qwen2.5-coder:1.5b-base") -> "small"
        _model_size_hint("qwen2.5:7b")              -> "full"
        _model_size_hint("gemma4:31b")              -> "full"
        _model_size_hint("nemotron-3-super:120b")   -> "full"
        _model_size_hint("")                        -> "full"
        _model_size_hint(None)                      -> "full"
        _model_size_hint("rnj-1:latest")            -> "full"  (no size hint)
    """
    if not model_id:
        return "full"
    name = str(model_id).lower()
    m = _MODEL_SIZE_PATTERN.search(name)
    if not m:
        return "full"
    try:
        size_b = float(m.group(1))
    except ValueError:
        return "full"
    return "small" if size_b <= _SMALL_TIER_THRESHOLD_B else "full"


DEFAULT_CONFIG = {
    "theme": "dark", "haptic": True,
    "backend": "ollama", "ollama_url": "http://localhost:11434",
    "models_dir": str(MODELS_DIR), "default_model": None,
    # v2.1.8 fix: n_ctx removed from DEFAULT_CONFIG. The hardcoded 8190
    # was being merged into the runtime config when user's config.json
    # didn't have the key, and my adaptive ctx code (model_manager._
    # compute_adaptive_ctx) couldn't distinguish "user-explicit 8190"
    # from "DEFAULT_CONFIG default 8190." Result: adaptive was always
    # bypassed, num_ctx was always sent as 8190, prompts > 8190 tokens
    # got brutally truncated by Ollama (keep=4 from beginning, dropping
    # the actual user request), and nemotron either confused-replied or
    # timed out. Removing the default lets `config.get("n_ctx")` return
    # None for users who don't explicitly set it, and adaptive engages.
    # Users who DO want an explicit ctx put `"n_ctx": <N>` in config.json.
    "gpu_acceleration": True, "n_gpu_layers": -1,
    # v2.11.12 hardware-acceleration toggles — persisted via config_store
    # (InferenceSection), consumed by model_manager (GPU offload gating +
    # NPU tier routing) and tier_launcher (NPU tier spawn at boot).
    "cuda_enabled": True, "rocm_enabled": True, "vulkan_enabled": True,
    "openvino_enabled": True, "xe_cores_enabled": True, "npu_enabled": True,
    "temperature": 0.5, "max_tokens": -1,
    # v2.1.8 ctx-sizing knobs surfaced as defaults so they're discoverable
    # via /api/config without forcing a config.json edit.
    # ctx_response_headroom = additive tokens reserved for the model's
    # reply (default 1500). Replaces ctx_padding_factor (multiplicative,
    # default now 1.0 = no effect; only set above 1.0 as a power-user
    # override). The additive model is more semantically correct: a
    # response budget doesn't scale with prompt size, so reserving a
    # fixed number of tokens for the reply makes more sense than
    # multiplying the prompt length.
    "hard_cap_ctx": True, "ctx_min": 8192,
    "ctx_response_headroom": 1500, "ctx_padding_factor": 1.0,
    # v2.1.8: Ollama read timeout in seconds. Was hardcoded at 300s in
    # v2.1.7 which killed legit big-model + cold-load workflows. 1800s
    # (30 min) covers cold-load + heavy prompts + multi-minute
    # generation on slow hardware. Lower it on fast rigs if you want
    # quicker failure detection.
    "ollama_read_timeout_sec": 56010,
    # v2.1.8 #56 stall-detection knobs. Defaults are conservative so the
    # watchdog only fires on real silence, never on legitimate slow
    # generation. Bump these in config.json on slower hardware, lower
    # them on fast rigs if you want quicker failure detection.
    "stall_token_timeout_sec": 56000,   # 5 min between tokens = stall
    "stall_tool_timeout_sec":  56000,   # 3 min for a tool result = stall
    # v2.1.8 #55 model-aware prompt tier override. Default null = auto-
    # detect via _model_size_hint(model_id). Set to "small" or "full" to
    # force a specific tier regardless of model size. Useful if you want
    # to test the SMALL prompt against a 30B model, or if you have a
    # tag-confident 3B model that handles the full prompt fine.
    "force_prompt_tier": None,
    # v2.1.10 #44 AIQNudge — HMAC-signed mid-run side-channel for guiding
    # Sage on long runs without aborting. Off by default for distribution
    # safety; user opts in when they need it. When True, the agentic
    # loop scans sage_data/nudges/ between steps for nudge_*.txt files,
    # verifies their HMAC against backend/.aiq_nudge_key, and injects
    # verified content as a system-role priority directive. Tampered or
    # unsigned files are quarantined (renamed to .rejected_<unix_ts>).
    "aiq_nudge_enabled":       True,
    "aiq_nudge_watch_pattern": "nudge_*.txt",
    # #68 Phase E Step 6: system_prompt moved out of DEFAULT_CONFIG. It now
    # lives in a real file (prompts/system.txt by default) and is read by
    # GET /api/prompts/system and the inference path. Distribution-safe:
    # a fresh install gets an empty prompt file and Sage uses its built-in
    # SAGE_SYSTEM_PROMPT, no Todd-specific text baked into the codebase.
    "sage_mode": True, "agentic_mode": True,
    "web_search_enabled": True, "code_exec_enabled": True,
    "privacy_mode": False,
    "n_ctx": 256000,
}


def _sanitize_max_tokens(v) -> int:
    """v2.1.8 max_tokens=-1 trap fix.

    Backend canonical sentinel for "unlimited" is -1. Any other non-positive
    value (0, negative ints other than -1, NaN, None, non-numeric strings,
    floats) is invalid and would either silently cap responses to zero
    or cause the Ollama / llama-server call to behave inconsistently.
    This coerces every weird value to -1 so the downstream generation paths
    only ever see either a positive integer or the unlimited sentinel.

    Called from load_config (so on-disk config gets cleaned up at boot)
    and from api_update_config (so /api/config POSTs can't introduce
    invalid values at runtime). Defense in depth — model_manager also
    re-validates at the moment of the Ollama call.
    """
    try:
        n = int(v)
    except (TypeError, ValueError):
        return -1
    if n == -1:
        return -1
    if n <= 0:
        return -1
    return n


# --- #68 Phase E: unified config layer integration ---------------------------
# Imports the new config_store + migrate_config modules. The boot-time
# migrator call below ensures v1 (flat) config.json files self-upgrade to
# v2 (sectioned) on the next boot — no user action required. Once v2, the
# migrator is a clean no-op.
#
# load_config() and save_config() keep their original names and flat-dict
# I/O signature so every existing call site (config.get('temperature'),
# model_manager.config = config, etc.) keeps working unchanged. Underneath,
# they now go through OracleConfig.load / .save which give us:
#   - atomic writes (tmp + os.replace; crash-safe)
#   - schema validation at /api/config POST (validate_flat_payload)
#   - typed accessors for code that wants to migrate off .get() later
#   - system_prompt compat shim (prompt text stays in flat dict by being
#     read from prompts/system.txt at load time — Phase F removes this
#     when settings.js gets a dedicated /api/prompts endpoint)
from config_store import (
    OracleConfig,
    save_config as _save_oracle_config,
    validate_flat_payload,
)
from migrate_config import migrate as _migrate_config


def _run_boot_time_migration() -> None:
    """Boot-time #68 v1→v2 migrator. Idempotent — clean no-op on v2 files.
    Never raises: a migration failure must not block the backend from
    starting. If migrate fails, the unchanged v1 config.json remains on
    disk and load_config() handles it via OracleConfig.load's v1 fallback.
    """
    try:
        _migrate_config(dry_run=False)
    except Exception as e:
        print(f"[main] WARN: config migration raised {e!r}; continuing with "
              f"existing config.json (load_config will fall back to v1 handling).",
              flush=True)


_run_boot_time_migration()


def load_config() -> dict:
    """Return the runtime config as a flat dict (back-compat shape).
    Now backed by OracleConfig under the hood — see config_store.py.

    Reads the on-disk config.json (v2 nested) via OracleConfig.load, then
    converts to the flat shape that every existing call site expects.
    Missing files / parse errors fall through to dataclass defaults
    (handled inside OracleConfig.load).
    """
    cfg = OracleConfig.load(CONFIG_FILE)
    flat = cfg.to_flat_dict()
    # max_tokens sanitizer: defense-in-depth. config_store already runs
    # _sanitize_max_tokens during from_flat_dict / _from_nested_dict, but
    # if a user hand-edited config.json mid-session this catches it.
    flat["max_tokens"] = _sanitize_max_tokens(flat.get("max_tokens", -1))
    return flat


def save_config(cfg: dict):
    """Persist the runtime flat-dict config. Now backed by OracleConfig
    atomic write (tmp + os.replace) — a crash mid-write can't corrupt
    config.json.

    The system_prompt key (if present in cfg) is written to the prompt
    file by OracleConfig.from_flat_dict's compat shim — not stored
    inline in config.json.
    """
    full = OracleConfig.from_flat_dict(cfg)
    _save_oracle_config(full, CONFIG_FILE)


config = load_config()

app = FastAPI(title="OracleAI", version="2.11.11", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

model_manager = ModelManager(config)
plugin_manager = PluginManager(PLUGINS_DIR)

# Aether skill-share HTTP surface (L4). Gated by config.skill_share_enabled (OFF
# by default); the two serve paths are allowlisted in the session gate below.
try:
    from skill_api import skill_router as _skill_router, set_config as _set_skill_config
    app.include_router(_skill_router)
    _set_skill_config(config)
except Exception as _skill_e:
    print("[skills] skill-share surface unavailable:", _skill_e)

# Aether relay broker (rendezvous for peers that can't reach each other directly).
# OFF by default behind relay_server_enabled.
try:
    from relay_api import relay_router as _relay_router, set_config as _set_relay_config
    app.include_router(_relay_router)
    _set_relay_config(config)
except Exception as _relay_e:
    print("[relay] relay surface unavailable:", _relay_e)


@app.on_event("startup")
async def _start_relay_source():
    """If configured, run the relay source-loop so this node serves its skills over
    a relay (lets peers reach it even behind CGNAT). OFF by default."""
    try:
        if not bool(config.get("relay_source_enabled", False)):
            return
        relay_url = (config.get("relay_url") or "").strip()
        peer_id = (config.get("relay_peer_id") or config.get("node_name") or "").strip()
        if not relay_url or not peer_id:
            print("[relay] source enabled but relay_url/relay_peer_id missing")
            return
        import asyncio
        from relay_client import RelaySource
        import skill_api as _skill_api
        _src = RelaySource(relay_url, peer_id, _skill_api.relay_skill_handler)
        asyncio.create_task(_src.run())
        print("[relay] source-loop started: serving skills as '%s' via %s" % (peer_id, relay_url))
    except Exception as _e:
        print("[relay] source-loop not started:", _e)


@app.on_event("startup")
async def _start_comfyui():
    """Optionally spawn ComfyUI as an OracleAI-OWNED process (OFF by default), so
    prompt-driven image generation works without launching ComfyUI by hand, and so
    closing OracleAI reaps it -- destroying the ComfyUI job-queue box (privacy).
    Health-gated (won't double-launch) and non-blocking (ComfyUI warms in parallel).

    v2.11.12 CRITICAL FIX (2026-07-02, the "backend won't start" morning):
    this previously did `res = await asyncio.to_thread(comfyui_launcher.start,...)`.
    The await meant the startup event did NOT finish until the launcher
    returned — and uvicorn does not bind its port until ALL startup events
    complete. On a cold boot, the launcher's DirectML probe (importing
    torch inside ComfyUI's embedded Python, up to 40s) plus install
    detection + readiness waiting can run past Electron's 90s health
    timeout, so port 8000 never opened while every tier terminal sat
    there looking healthy. The docstring above always SAID non-blocking;
    now it's true: fire-and-forget task, uvicorn binds immediately,
    ComfyUI warms in the background and logs when done."""
    try:
        if not bool(config.get("comfyui_autostart_enabled", False)):
            return
        import asyncio
        import comfyui_launcher

        async def _bg():
            try:
                res = await asyncio.to_thread(comfyui_launcher.start, config)
                print("[comfyui] autostart (background):", res)
            except Exception as _e:
                print("[comfyui] autostart skipped:", _e)

        asyncio.create_task(_bg())
        print("[comfyui] autostart: launching in background (boot not blocked)")
    except Exception as _e:
        print("[comfyui] autostart skipped:", _e)


@app.on_event("shutdown")
async def _stop_comfyui():
    """Reap the ComfyUI process we own on graceful shutdown, wiping its queue box."""
    try:
        import comfyui_launcher
        if comfyui_launcher.owns_process():
            print("[comfyui] reaping owned process:", comfyui_launcher.stop())
    except Exception as _e:
        print("[comfyui] shutdown reap skipped:", _e)


# Sync plugin states to sage_engine on startup
feature_map = {
    "weather-tool": "weather",
    "semantic-search": "semantic_search",
    "exercise-tracker": "exercise_tracker",
    "complexity-detector": "complexity_detector",
    "task-prioritiser": "task_prioritiser",
    "browser-plugin": "browser",
}
for plugin_id, feature_name in feature_map.items():
    plugin = plugin_manager._plugins.get(plugin_id)
    if plugin:
        sage_engine.set_feature(feature_name, plugin.get("enabled", False))
        print(
            f"[PLUGIN SYNC] {plugin_id} → {feature_name} = {plugin.get('enabled', False)}")

DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)


# --- Tier lifecycle startup (Phase 1D Step 4) -----------------------------
@app.on_event("startup")
async def _init_tier_cache_on_startup():
    """Seed the tier ctx cache so subsequent UI refreshes know what
    ctx size each llama-server process is actually running with."""
    tier_lifecycle.init_cache(config)
    # BitChat is OPT-IN: do NOT scan at boot. The BLE gateway starts only when
    # the user clicks Connect (POST /api/socials/connect). Power users can force
    # boot autostart by setting "bitchat_autostart": true in config.json.
    if config.get("bitchat_autostart", False):
        await tier_lifecycle.ensure_bitchat_gateway()
    else:
        print("[tier] bitchat: opt-in -- gateway NOT auto-started (no BLE scan at boot)")

# --- Frontend cache-busting (2026-06-09) --------------------------------------
# Electron/browser were serving STALE frontend code: the asset URLs carry a fixed
# ?v= query, so editing a .js/.css did NOT change its URL and the renderer kept
# the cached copy (the "restart 3-6 times for the UI to update" problem). For a
# LOCAL app the assets are on disk, so always-fresh has zero cost. This forces the
# frontend (index.html + everything under /static + any .js/.css/.html) to never
# be cached, so a NORMAL reload always picks up new code. API/data is untouched.
# --- LAN exposure guard (2026-06-09) ------------------------------------------
# When the node server is enabled and bound to the LAN, ONLY the encrypted,
# token-gated node surface may be reached from another machine. EVERY other
# endpoint - the UI, /api/config, token reveal, all of it - stays localhost-only,
# so binding to the LAN never exposes your settings or the home token. A remote
# request to anything but the node surface gets a flat 403.
_NODE_SURFACE_PATHS = frozenset((
    "/api/node/info", "/api/node/infer", "/api/node/infer-stream",
    "/api/node/generate-image",
))
# Aether peer-facing surfaces ALSO reachable from the LAN/WAN: signed + flag-gated
# + rate-limited, and they expose no settings/secrets. Prefix-matched because some
# carry path params (object/<hash>, relay/poll/<peer>, relay/response/<id>).
# /api/health is included so peers can do a plain reachability check.
_REMOTE_OK_PREFIXES = ("/api/health", "/api/skills/catalog",
                       "/api/skills/object/", "/api/relay/")


def _is_local_client(request: Request) -> bool:
    """True only for loopback callers (the desktop UI on this machine)."""
    client = (request.client.host if request.client else "") or ""
    return (client in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")
            or client.startswith("127."))


def _cloak_not_found() -> JSONResponse:
    """The single uniform 404 every cloak path returns. Byte-identical to the
    framework's default 'not found' so a remote prober cannot distinguish a
    blocked/protected route from one that simply does not exist."""
    return JSONResponse({"detail": "Not Found"}, status_code=404)


# Manual, persistent IP access control (denylist + lockdown allowlist), stored in
# sage_data so it survives restarts and never rides a copied/synced project
# folder. Complements wan_guard.AbuseGuard (automatic + in-memory + temporary).
# Non-fatal: on any error the channel is simply absent and the guard skips it.
try:
    from ip_access import IPAccess
    ip_access = IPAccess(DATA_DIR / "ip_access.json")
    print(f"[IP_ACCESS] Loaded: {ip_access.summary()}")
except Exception as _ipa_err:           # pragma: no cover
    print(f"[IP_ACCESS] disabled due to error: {_ipa_err}")
    ip_access = None


# Auto-ban for repeat REMOTE probers. Every cloaked remote hit is a "probe";
# after a GENEROUS number of them (with no legitimate peer-surface hit resetting
# the streak) the IP is dropped for a cooldown. DELIBERATELY temporary/expiring
# and NOT written to the persistent denylist, so a real peer who merely fumbles
# recovers on its own -- nobody gets permanently banned by accident. Generous,
# config-tunable defaults; set wan_autoban_enabled=false to turn it off.
try:
    from wan_guard import AbuseGuard as _AbuseGuard
    _probe_guard = _AbuseGuard(
        max_requests=1_000_000, window_sec=60,   # rate-limit part intentionally unused
        fail_threshold=int(config.get("wan_autoban_probe_threshold", 50)),
        ban_sec=float(config.get("wan_autoban_ban_sec", 3600)),
    )
    _PROBE_AUTOBAN = bool(config.get("wan_autoban_enabled", True))
    print(f"[WAN_AUTOBAN] enabled={_PROBE_AUTOBAN} "
          f"threshold={config.get('wan_autoban_probe_threshold', 50)} probes -> "
          f"{config.get('wan_autoban_ban_sec', 3600)}s cooldown")
except Exception as _pg_err:            # pragma: no cover
    print(f"[WAN_AUTOBAN] disabled: {_pg_err}")
    _probe_guard = None
    _PROBE_AUTOBAN = False


# Voice input service (server-side STT -> the NORMAL chat). Heavy deps are lazy,
# so this loads even with nothing installed; status() reports what's available
# and the endpoints return clear install hints. Non-fatal on any error.
from starlette.concurrency import run_in_threadpool as _run_in_threadpool
try:
    from voice_service import VoiceService
    voice_service = VoiceService({
        "wake_word":          config.get("voice_wake_word", "Toga"),
        "stt_engine":         config.get("voice_stt_engine", "whisper"),
        "language":           config.get("voice_language", "en"),
        "record_seconds":     config.get("voice_record_seconds", 6),
        "wake_chunk_seconds": config.get("voice_wake_chunk_seconds", 3),
        "model":              config.get("voice_whisper_model", "base"),
    })
    print(f"[VOICE] ready; capabilities={voice_service.status().get('capabilities')}")
except Exception as _v_err:              # pragma: no cover
    print(f"[VOICE] disabled: {_v_err}")
    voice_service = None


# --- Socials: messaging channels (BitChat experimental + Discord) -------------
# An adapter framework that feeds the NORMAL Sage via an injected reply callable.
# Opt-in auto-reply is OFF by default. Per-channel client libs (discord.py,
# aiohttp) are lazy, so this loads even with them absent; status() reports what
# each channel needs. Non-fatal on any error.
try:
    from sage_messaging_adapter import (SageChannelRouter, DiscordAdapter,
                                        MastodonAdapter, BlueSkyAdapter)

    async def _socials_reply(_text: str) -> str:
        """One-shot Sage reply for channel auto-reply, via the real model_manager."""
        _msgs = [
            {"role": "system", "content": "You are Toga replying on a Bluetooth "
             "mesh chat. Keep replies conversational and reasonably brief — a few "
             "short sentences is ideal (long messages are auto-fragmented, so "
             "you're not hard-limited). Friendly, plain-text. No preamble, no "
             "sign-off, and do NOT prefix your reply with 'Sage:'."},
            {"role": "user", "content": (_text or "")[:2000]},
        ]
        # Pick the active model from the configured slots (default -> secondary
        # -> tertiary), routing by content — the same selection the rest of the
        # app uses. Passing None here is what produced "[Error: No model
        # selected]" even with the slots filled.
        try:
            _slots = [s for s in (config.get("default_model"),
                                  config.get("secondary_model"),
                                  config.get("tertiary_model")) if s]
            if len(_slots) >= 2:
                _model = sage_engine.route_query(_text or "", candidates=_slots,
                                                 needs_vision=False)
            else:
                _model = _slots[0] if _slots else config.get("default_model")
        except Exception:
            _model = config.get("default_model")
        try:
            _out = (await model_manager.generate_full(_msgs, _model, {}) or "").strip()
            # Strip any stray "Sage:" the model prepended (the channel adds its
            # own prefix), and cap length so the reply fits one BLE notification.
            if _out[:5].lower() == "sage:":
                _out = _out[5:].strip()
            # Outbound fragmentation handles long messages now, so allow a
            # generous cap (a safety bound against runaway generations).
            return _out[:800]
        except Exception as _gen_err:
            print(f"[SOCIALS] reply generation error: {_gen_err}")
            return ""

    try:
        from socials_config import SocialsConfig
    except Exception as _scfg_err:
        print(f"[SOCIALS] config store unavailable: {_scfg_err}")
        SocialsConfig = None

    # --- v2.11.14: PER-PROFILE socials -------------------------------------
    # Each profile gets its OWN router: own connector instances, own
    # credential store (owner: sage_data/socials_config.json — unchanged;
    # child: sage_data/users/<ns>/socials_config.json), and own in-memory
    # message feed — so person A can never read person B's socials, and the
    # per-channel threads stay separated per profile exactly as they already
    # are per platform. Child routers are built lazily on first use.
    #
    # BitChat is the one exception: it drives THIS machine's BLE radio, and
    # one radio = one bridge, so it registers on the OWNER router only.
    def _socials_store_path(ns=None):
        return (DATA_DIR / "users" / str(ns) / "socials_config.json") if ns \
            else (DATA_DIR / "socials_config.json")

    def _build_socials_router(ns=None):
        store = None
        if SocialsConfig is not None:
            try:
                store = SocialsConfig(_socials_store_path(ns))
            except Exception as _e:
                print(f"[SOCIALS] store unavailable (ns={ns}): {_e}")
        router = SageChannelRouter(
            reply_fn=_socials_reply,
            wake_word=config.get("voice_wake_word", "Toga"),
            store=store,
        )

        def _seed(_name):
            try:
                return (store.get(_name) if store else {}) or {}
            except Exception:
                return {}

        if ns is None:
            try:
                from bitchat_bridge import BitChatBridge
                router.register(BitChatBridge(_seed("bitchat")))
            except Exception as _bc_err:
                print(f"[SOCIALS] BitChat adapter not registered: {_bc_err}")
        for _cls, _name in ((DiscordAdapter, "discord"),
                            (MastodonAdapter, "mastodon"),
                            (BlueSkyAdapter, "bluesky")):
            try:
                router.register(_cls(_seed(_name)))
            except Exception as _a_err:
                print(f"[SOCIALS] {_name} adapter not registered (ns={ns}): {_a_err}")
        return router

    socials_router = _build_socials_router(None)   # owner / single-user
    _socials_routers = {}                          # ns -> child router (lazy)
    print(f"[SOCIALS] channels: {socials_router.names()}")
except Exception as _soc_err:            # pragma: no cover
    print(f"[SOCIALS] disabled: {_soc_err}")
    socials_router = None
    _socials_routers = {}


@app.middleware("http")
async def _lan_exposure_guard(request: Request, call_next):
    try:
        if not _is_local_client(request):
            _ip = request.client.host if request.client else ""
            _autoban = _PROBE_AUTOBAN and _probe_guard is not None

            # 0) Already auto-banned this cooldown? Drop cheaply, before any work.
            if _autoban and _probe_guard.is_banned(_ip):
                return _cloak_not_found()

            # 1) Manual denylist / lockdown -> 404 cloak (counts as a probe).
            #    Applies even to the peer surface: a denylisted IP (or, under
            #    lockdown, any non-allowlisted IP) gets nothing.
            if ip_access is not None and ip_access.remote_blocked(_ip):
                if _autoban:
                    _probe_guard.record_failure(_ip)
                return _cloak_not_found()

            # 2) Off the peer surface -> 404 cloak (counts as a probe). Default-
            #    deny + pre-routing => every current/future endpoint is invisible
            #    to outsiders unless explicitly on the peer surface.
            _p = request.url.path
            _remote_ok = (_p in _NODE_SURFACE_PATHS
                          or any(_p.startswith(pre) for pre in _REMOTE_OK_PREFIXES))
            if not _remote_ok:
                if _autoban:
                    _probe_guard.record_failure(_ip)
                return _cloak_not_found()

            # 3) Reached the peer surface -> legitimate traffic; forgive any prior
            #    probe streak so honest peers never accumulate toward a ban.
            if _autoban:
                _probe_guard.record_success(_ip)
    except Exception:
        pass
    return await call_next(request)


# Render not-found / not-allowed / auth failures as a uniform 404 for REMOTE
# callers, so per-route rejections (e.g. an unauthorized hit on the token-gated
# node surface) and unmatched routes are all indistinguishable from "nothing
# here". Localhost keeps real status codes — the UI and login flow depend on
# them. Global => new endpoints inherit the cloak with no extra work. The peer
# surface still serves real 200s to authenticated peers.
from starlette.exceptions import HTTPException as _StarletteHTTPException


@app.exception_handler(_StarletteHTTPException)
async def _cloaking_exception_handler(request: Request, exc: _StarletteHTTPException):
    if not _is_local_client(request) and exc.status_code in (401, 403, 404, 405):
        return _cloak_not_found()
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


@app.middleware("http")
async def _no_cache_frontend(request: Request, call_next):
    response = await call_next(request)
    try:
        p = request.url.path
        if (p == "/" or p.startswith("/static")
                or p.endswith((".js", ".css", ".html", ".mjs"))):
            response.headers["Cache-Control"] = (
                "no-store, no-cache, must-revalidate, max-age=0")
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
    except Exception:
        pass
    return response


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")


# --- Core Routes --------------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


# PWA: serve the service worker from the ROOT scope so it can control the whole
# app (a SW only governs paths at/below where it's served). Manifest is served
# at root too for a clean install; icons + offline page live under /static.
@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        str(FRONTEND_DIR / "sw.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/manifest.webmanifest")
async def web_manifest():
    return FileResponse(
        str(FRONTEND_DIR / "manifest.webmanifest"),
        media_type="application/manifest+json",
    )


@app.get("/favicon.ico")
async def favicon():
    _ico = FRONTEND_DIR / "icons" / "icon-192.png"
    if _ico.exists():
        return FileResponse(str(_ico), media_type="image/png")
    return JSONResponse({"ok": True})


@app.get("/api/hardware")
async def api_hardware():
    return detect_hardware()


# --- IP access control (denylist / lockdown allowlist) -- localhost-only mgmt --
@app.get("/api/ip-access")
async def api_ip_access_get(request: Request):
    if not _is_local_client(request):
        raise HTTPException(404)
    if ip_access is None:
        raise HTTPException(503, "ip access store unavailable")
    return ip_access.snapshot()


@app.post("/api/ip-access")
async def api_ip_access_post(request: Request, payload: dict):
    """Manage the manual denylist / allowlist / lockdown. Localhost-only.

    Body:
      {"action":"add","list":"deny|allow","ip":"<ip-or-cidr>"}
      {"action":"remove","list":"deny|allow","ip":"<ip-or-cidr>"}
      {"action":"lockdown","enabled":true|false}
    Returns the new snapshot {denylist, allowlist, lockdown}.
    """
    if not _is_local_client(request):
        raise HTTPException(404)
    if ip_access is None:
        raise HTTPException(503, "ip access store unavailable")
    action = (payload.get("action") or "").strip().lower()
    if action == "lockdown":
        return ip_access.set_lockdown(bool(payload.get("enabled")))
    which = (payload.get("list") or "").strip().lower()
    if which not in ("deny", "allow"):
        raise HTTPException(400, "list must be 'deny' or 'allow'")
    ip = (payload.get("ip") or "").strip()
    try:
        if action == "add":
            return ip_access.add(which, ip)
        if action == "remove":
            return ip_access.remove(which, ip)
    except ValueError as e:
        raise HTTPException(400, str(e))
    raise HTTPException(400, "action must be add, remove, or lockdown")


# --- Voice input (server-side STT -> normal chat) -- localhost-only -----------
@app.get("/api/voice/status")
async def api_voice_status(request: Request):
    if not _is_local_client(request):
        raise HTTPException(404)
    if voice_service is None:
        return {"available": False, "error": "voice service not initialised"}
    return {"available": True, **voice_service.status()}


@app.post("/api/voice/transcribe")
async def api_voice_transcribe(request: Request, payload: dict | None = None):
    """Push-to-talk: record one utterance on the host mic, transcribe, return the
    text. Runs in a threadpool so the blocking record/transcribe never stalls the
    event loop. Audio is in-memory only and discarded when this returns."""
    if not _is_local_client(request):
        raise HTTPException(404)
    if voice_service is None:
        raise HTTPException(503, "voice service unavailable")
    seconds = (payload or {}).get("seconds") if isinstance(payload, dict) else None
    try:
        text = await _run_in_threadpool(voice_service.transcribe_once, seconds)
    except Exception as e:
        raise HTTPException(503, str(e))
    return {"ok": True, "text": text or ""}


@app.post("/api/voice/wake")
async def api_voice_wake(request: Request, payload: dict):
    """Start/stop the OPT-IN always-listening wake-word loop (background thread)."""
    if not _is_local_client(request):
        raise HTTPException(404)
    if voice_service is None:
        raise HTTPException(503, "voice service unavailable")
    enabled = bool(payload.get("enabled"))
    try:
        if enabled:
            await _run_in_threadpool(voice_service.start_wake)
        else:
            voice_service.stop_wake()
    except Exception as e:
        raise HTTPException(503, str(e))
    return {"ok": True, "wake_active": voice_service.wake_active}


@app.get("/api/voice/poll")
async def api_voice_poll(request: Request):
    """Drain any wake-word-recognized commands for the UI to send as normal chat."""
    if not _is_local_client(request):
        raise HTTPException(404)
    if voice_service is None:
        return {"commands": [], "wake_active": False}
    return voice_service.poll()


# --- Socials (messaging channels: BitChat + Discord) -- localhost-only ---------
# v2.11.14: PER-PROFILE. v2.11.13's owner-only guard is gone — every profile
# now gets its own router/connectors/credentials/feed via
# _socials_router_for(request). Isolation is per profile: person A can never
# read person B's socials. BitChat (this machine's BLE radio) stays with the
# owner profile — one radio, one bridge.
def _socials_router_for(request: Request):
    """The caller's per-profile socials router. Owner / single-user -> the
    boot router; signed-in child -> lazily built + cached per namespace.
    None when socials are disabled entirely."""
    if socials_router is None:
        return None
    ns = _session_ns(request)
    if not ns:
        return socials_router
    router = _socials_routers.get(ns)
    if router is None:
        router = _build_socials_router(ns)
        _socials_routers[ns] = router
        print(f"[SOCIALS] per-profile router built for ns={ns} "
              f"(channels: {router.names()})")
    return router


@app.get("/api/socials/status")
async def api_socials_status(request: Request):
    if not _is_local_client(request):
        raise HTTPException(404)
    router = _socials_router_for(request)
    if router is None:
        return {"available": False}
    return {"available": True, **router.status()}


@app.post("/api/socials/connect")
async def api_socials_connect(request: Request, payload: dict):
    """Connect (default) or disconnect a channel; starts/stops its listen loop."""
    if not _is_local_client(request):
        raise HTTPException(404)
    router = _socials_router_for(request)
    if router is None:
        raise HTTPException(503, "socials unavailable")
    name = (payload.get("channel") or "").strip().lower()
    # BitChat = this machine's BLE radio; owner profile only (child routers
    # don't even register it, but fail loudly rather than silently).
    if name == "bitchat" and _session_ns(request) is not None:
        raise HTTPException(403, "BitChat uses this machine's radio and is "
                                 "available on the owner profile only")
    if payload.get("connect", True):
        if name == "bitchat":
            # Make sure the BLE gateway process is actually running first.
            try:
                await tier_lifecycle.ensure_bitchat_gateway()
            except Exception as exc:
                print(f"[socials] bitchat gateway start failed: {exc}")
        ok = await router.connect(name)
    else:
        ok = await router.disconnect(name)
        if name == "bitchat":
            # 'Off' means off: stop the gateway so BLE scanning fully ceases.
            try:
                await tier_lifecycle.stop_bitchat_gateway()
            except Exception as exc:
                print(f"[socials] bitchat gateway stop failed: {exc}")
    return {"ok": bool(ok), **router.status()}


@app.post("/api/socials/send")
async def api_socials_send(request: Request, payload: dict):
    if not _is_local_client(request):
        raise HTTPException(404)
    router = _socials_router_for(request)
    if router is None:
        raise HTTPException(503, "socials unavailable")
    name = (payload.get("channel") or "").strip().lower()
    text = (payload.get("text") or "").strip()
    room = (payload.get("room") or "general").strip()
    if not text:
        raise HTTPException(400, "empty message")
    ok = await router.send(name, text, channel=room)
    return {"ok": bool(ok)}


@app.get("/api/socials/recent")
async def api_socials_recent(request: Request):
    if not _is_local_client(request):
        raise HTTPException(404)
    router = _socials_router_for(request)
    if router is None:
        return {"messages": []}
    return {"messages": router.recent(limit=30)}


@app.post("/api/socials/clear")
async def api_socials_clear(request: Request, payload: dict):
    """Clear buffered Socials messages. Body: {channel: "discord"} clears that
    one thread; {all: true} clears every channel. The feed buffer is in-memory
    only (nothing persisted, nothing shared across user profiles)."""
    if not _is_local_client(request):
        raise HTTPException(404)
    router = _socials_router_for(request)
    if router is None:
        raise HTTPException(503, "socials unavailable")
    if payload.get("all"):
        removed = router.clear_recent(None)
        return {"ok": True, "removed": removed, "scope": "all"}
    name = (payload.get("channel") or "").strip().lower()
    if not name:
        raise HTTPException(400, "channel required (or pass all:true)")
    removed = router.clear_recent(name)
    return {"ok": True, "removed": removed, "scope": name}


@app.post("/api/socials/auto-reply")
async def api_socials_autoreply(request: Request, payload: dict):
    """Toggle opt-in wake-word auto-reply on connected channels (OFF by default)."""
    if not _is_local_client(request):
        raise HTTPException(404)
    router = _socials_router_for(request)
    if router is None:
        raise HTTPException(503, "socials unavailable")
    router.set_auto_reply(bool(payload.get("enabled")))
    return {"ok": True, "auto_reply": router.auto_reply}


@app.get("/api/socials/peers")
async def api_socials_peers(request: Request, channel: str = ""):
    if not _is_local_client(request):
        raise HTTPException(404)
    router = _socials_router_for(request)
    if router is None:
        return {"peers": []}
    return {"peers": await router.peers((channel or "").strip().lower())}


@app.get("/api/socials/config")
async def api_socials_config_get(request: Request):
    if not _is_local_client(request):
        raise HTTPException(404)
    router = _socials_router_for(request)
    if router is None:
        return {"config": {}}
    return {"config": router.config_snapshot()}


@app.post("/api/socials/config")
async def api_socials_config_set(request: Request, payload: dict):
    """Set or clear per-channel settings (e.g. a Discord bot token). Localhost-only.
    Body: {channel, settings:{...}} to set; {channel, clear:true[, keys:[...]]} to remove.
    Tokens are stored in sage_data and never returned (status shows only has_token)."""
    if not _is_local_client(request):
        raise HTTPException(404)
    router = _socials_router_for(request)
    if router is None:
        raise HTTPException(503, "socials unavailable")
    name = (payload.get("channel") or "").strip().lower()
    if not name:
        raise HTTPException(400, "channel required")
    if payload.get("clear"):
        router.clear_config(name, payload.get("keys"))
    else:
        router.set_config(name, payload.get("settings") or {})
    return {"ok": True, "config": router.config_snapshot()}
    
    
@app.get("/api/comfyui/setup-status")
async def comfyui_setup_status():
    """Returns whether ComfyUI is installed and ready for headless operation."""
    try:
        import comfyui_setup
        import comfyui_launcher
        import comfyui_models
        existing = comfyui_setup.detect_existing()
        installed_models = comfyui_models.list_installed(existing) if existing else []
        _sel = comfyui_models.get_selection()
        _gpu = comfyui_setup.detect_gpu()

        # Check if offload is enabled and a remote node is configured.
        # If so, local ComfyUI absence is not a problem.
        cfg = {}
        try:
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
        except Exception:
            pass
            
        offload_enabled    = bool(cfg.get("offload_enabled", False))
        remote_node_url    = (cfg.get("remote_node_url") or "").strip()
        routed_to_remote   = offload_enabled and bool(remote_node_url)
        
        return JSONResponse({
            "installed":        existing is not None,
            "comfy_home":       existing or "",
            "setup_required":   (existing is None) and not routed_to_remote,
            "headless_ready":   existing is not None,
            "has_model":        len(installed_models) > 0,
            "installed_models": installed_models,
            "models_catalog":   comfyui_models.catalog_public(),
            "selected_model":   _sel.get("key", ""),
            "selected_checkpoint": _sel.get("checkpoint", ""),
            "gpu":              _gpu,
            "routed_to_remote": routed_to_remote,
            "remote_node_url": remote_node_url if routed_to_remote else "",
            "launcher_status":  comfyui_launcher.status(),
        })
    except Exception as e:
        return JSONResponse({"installed": False, "error": str(e)})


# --------------------------------------------------------------------------- #
#  ComfyUI setup endpoints (wizard UI)
# --------------------------------------------------------------------------- #
import asyncio as _asyncio

_setup_progress_queue: _asyncio.Queue = None

@app.on_event("startup")
async def _init_setup_queue():
    global _setup_progress_queue
    _setup_progress_queue = _asyncio.Queue()

def _get_setup_queue():
    global _setup_progress_queue
    if _setup_progress_queue is None:
        _setup_progress_queue = _asyncio.Queue()
    return _setup_progress_queue


@app.post("/api/comfyui/run-setup")
async def comfyui_run_setup(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    install_dir = body.get("install_dir") or None
    queue = _get_setup_queue()
    # Drain any leftover events from a previous setup run so this run's SSE
    # stream starts clean. A stale terminal ("done") event could otherwise make
    # the wizard think a fresh install finished instantly and loop.
    try:
        while True:
            queue.get_nowait()
    except _asyncio.QueueEmpty:
        pass
    loop  = _asyncio.get_running_loop()  # capture the RUNNING loop, not get_event_loop()

    def progress_cb(message, percent=-1):
        done  = percent == 100
        event = {"message": message, "percent": percent,
                 "done": done, "success": True}
        try:
            loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception:
            pass

    def run():
        import comfyui_setup
        print("[DEBUG] background thread started", flush=True)
        try:
            result = comfyui_setup.run_setup(
                install_parent=install_dir,
                silent=True,
                progress_cb=progress_cb,
            )
            print(f"[DEBUG] run_setup returned: {result}", flush=True)
        except Exception as e:
            print(f"[DEBUG] run_setup CRASHED: {type(e).__name__}: {e}", flush=True)
            import traceback
            traceback.print_exc()
            loop.call_soon_threadsafe(queue.put_nowait, {
                "message": f"Setup crashed: {e}",
                "percent": 0,
                "done": True,
                "success": False,
                "error": str(e),
            })
            return
        done_event = {
            "message": result.get("error") or "Setup complete.",
            "percent": 100 if result.get("success") else 0,
            "done":    True,
            "success": result.get("success", False),
            "error":   result.get("error", ""),
        }
        try:
            loop.call_soon_threadsafe(queue.put_nowait, done_event)
        except Exception:
            pass

    import threading
    threading.Thread(target=run, daemon=True).start()
    return JSONResponse({"started": True})


@app.get("/api/comfyui/setup-progress")
async def comfyui_setup_progress():
    """SSE endpoint. Streams progress events from the setup background
    thread to the frontend wizard in real time."""
    queue = _get_setup_queue()

    async def event_stream():
        while True:
            try:
                event = await _asyncio.wait_for(queue.get(), timeout=60.0)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("done"):
                    break
            except _asyncio.TimeoutError:
                # Keep-alive ping so the browser doesn't drop the connection
                yield "data: {\"ping\": true}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
        },
    )        


@app.post("/api/comfyui/download-model")
async def comfyui_download_model(request: Request):
    """Download a catalog model into ComfyUI/models/checkpoints, streaming
    progress over the SAME SSE channel the setup wizard uses
    (/api/comfyui/setup-progress). Records the choice in config on success."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    key = (body.get("key") or "").strip()

    import comfyui_models
    import comfyui_setup
    if key not in comfyui_models.MODEL_CATALOG:
        return JSONResponse({"started": False, "error": f"Unknown model '{key}'."},
                            status_code=400)
    comfy_home = comfyui_setup.detect_existing()
    if not comfy_home:
        return JSONResponse({"started": False,
                             "error": "ComfyUI is not installed yet."},
                            status_code=400)

    queue = _get_setup_queue()
    # Drain stale events so this download's stream starts clean.
    try:
        while True:
            queue.get_nowait()
    except _asyncio.QueueEmpty:
        pass
    loop = _asyncio.get_running_loop()

    def progress_cb(message, percent=-1):
        # Non-terminal progress only; the terminal event is queued by run().
        event = {"message": message, "percent": percent,
                 "done": False, "success": True}
        try:
            loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception:
            pass

    def run():
        try:
            result = comfyui_models.download_model(key, comfy_home, progress_cb)
        except Exception as e:
            loop.call_soon_threadsafe(queue.put_nowait, {
                "message": f"Download crashed: {e}", "percent": 0,
                "done": True, "success": False, "error": str(e)})
            return
        if result.get("success"):
            # Record the choice (persisted in oracleai_config.json) so generation
            # uses this model + its params, even across restarts.
            try:
                comfyui_models.set_selection(key, result.get("filename", ""))
                config["comfyui_model_key"] = key
                config["comfyui_checkpoint"] = result.get("filename", "")
            except Exception:
                pass
        loop.call_soon_threadsafe(queue.put_nowait, {
            "message": result.get("error") or "Model ready.",
            "percent": 100 if result.get("success") else 0,
            "done":    True,
            "success": result.get("success", False),
            "error":   result.get("error", ""),
        })

    import threading
    threading.Thread(target=run, daemon=True).start()
    return JSONResponse({"started": True})


@app.post("/api/comfyui/select-model")
async def comfyui_select_model(request: Request):
    """Set the active model (must already be installed). Persists the choice so
    generation uses it + its params across restarts."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    checkpoint = (body.get("checkpoint") or "").strip()
    import comfyui_models
    import comfyui_setup
    home = comfyui_setup.detect_existing()
    installed = comfyui_models.list_installed(home) if home else []
    if checkpoint not in installed:
        return JSONResponse({"success": False,
                             "error": "That model is not installed."},
                            status_code=400)
    key = comfyui_models.key_for_checkpoint(checkpoint)
    comfyui_models.set_selection(key, checkpoint)
    config["comfyui_model_key"] = key
    config["comfyui_checkpoint"] = checkpoint
    return JSONResponse({"success": True, "checkpoint": checkpoint, "key": key})


@app.post("/api/comfyui/delete-model")
async def comfyui_delete_model(request: Request):
    """Delete an installed checkpoint to reclaim disk. If it was the active
    model, clear the selection so generation falls back cleanly."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    checkpoint = (body.get("checkpoint") or "").strip()
    import comfyui_models
    import comfyui_setup
    home = comfyui_setup.detect_existing()
    if not home:
        return JSONResponse({"success": False, "error": "ComfyUI is not installed."},
                            status_code=400)
    res = await asyncio.to_thread(comfyui_models.delete_model, checkpoint, home)
    if res.get("success"):
        if comfyui_models.get_selection().get("checkpoint") == checkpoint:
            comfyui_models.set_selection("", "")
            config["comfyui_model_key"] = ""
            config["comfyui_checkpoint"] = ""
    return JSONResponse(res, status_code=200 if res.get("success") else 400)


@app.post("/api/comfyui/enable-directml")
async def comfyui_enable_directml(request: Request):
    """OPT-IN: install torch-directml so AMD/Intel GPUs accelerate via --directml.
    REFUSES on NVIDIA (CUDA is already optimal; we never touch the CUDA build).
    Streams progress over the setup-progress SSE channel."""
    import comfyui_setup
    import comfyui_launcher
    gpu = comfyui_setup.detect_gpu()
    if gpu.get("vendor") == "nvidia":
        return JSONResponse({"started": False,
            "error": ("NVIDIA GPU detected — CUDA is already optimal. DirectML is "
                      "for AMD/Intel GPUs and will NOT be installed (it would "
                      "replace your CUDA build).")}, status_code=400)
    comfy_home = comfyui_setup.detect_existing()
    if not comfy_home:
        return JSONResponse({"started": False, "error": "ComfyUI is not installed yet."},
                            status_code=400)

    queue = _get_setup_queue()
    try:
        while True:
            queue.get_nowait()
    except _asyncio.QueueEmpty:
        pass
    loop = _asyncio.get_running_loop()

    def progress_cb(message, percent=-1):
        try:
            loop.call_soon_threadsafe(queue.put_nowait, {
                "message": message, "percent": percent,
                "done": False, "success": True})
        except Exception:
            pass

    def run():
        try:
            import comfyui_directml
            res = comfyui_directml.provision_directml(comfy_home, progress_cb)
        except Exception as e:
            loop.call_soon_threadsafe(queue.put_nowait, {
                "message": f"DirectML install crashed: {e}", "percent": 0,
                "done": True, "success": False, "error": str(e)})
            return
        if res.get("success"):
            # Force the launcher to re-resolve so the next launch uses --directml.
            try:
                comfyui_launcher._accel_cache = None
            except Exception:
                pass
        loop.call_soon_threadsafe(queue.put_nowait, {
            "message": res.get("error") or "DirectML enabled.",
            "percent": 100 if res.get("success") else 0,
            "done":    True,
            "success": res.get("success", False),
            "error":   res.get("error", ""),
        })

    import threading
    threading.Thread(target=run, daemon=True).start()
    return JSONResponse({"started": True})


@app.get("/api/models")
async def api_list_models():
    return {"models": await model_manager.list_models()}


@app.post("/api/models/load")
async def api_load_model(payload: dict):
    mid = payload.get("model_id", "")
    if not mid:
        raise HTTPException(400, "model_id required")
    return await model_manager.load_model(mid)


@app.post("/api/models/unload")
async def api_unload_model(payload: dict):
    mid = payload.get("model_id", "")
    await model_manager.unload_model(mid)
    return {"status": "unloaded", "model_id": mid}


# --- Tier management (Phase 1D Step 4) ------------------------------------
@app.get("/api/tiers")
async def api_tiers_list():
    """Return live state of each llama-server tier."""
    return {"tiers": tier_lifecycle.tier_status_snapshot()}


@app.get("/api/tiers/{name}/status")
async def api_tier_status(name: str):
    """Return live state of a single tier."""
    name = name.lower().strip()
    if name not in tier_lifecycle.TIER_PORTS:
        raise HTTPException(400, f"Unknown tier: {name!r}")
    snap = tier_lifecycle.tier_status_snapshot()
    return snap.get(name, {})


@app.post("/api/tiers/{name}/restart")
async def api_tier_restart(name: str):
    """Manually restart a single tier using its current desired ctx_size
    computed from config.json. Blocks for up to ~70 seconds while the
    new llama-server loads its model."""
    name = name.lower().strip()
    if name not in tier_lifecycle.TIER_PORTS:
        raise HTTPException(400, f"Unknown tier: {name!r}")
    from config import compute_sage_ctx, compute_daemon_ctx
    global_n_ctx = config.get("n_ctx")
    if name == "sage":
        desired = compute_sage_ctx(global_n_ctx)
    else:
        desired = compute_daemon_ctx(global_n_ctx)
    return await tier_lifecycle.restart_tier(name, desired)


@app.post("/api/models/refresh")
async def api_models_refresh():
    """Combined endpoint for the frontend Refresh Models button.
    1. Restart any llama-server tier whose ctx_size has changed
       since the last boot/restart (no-op if nothing changed).
    2. Return the fresh model list plus a list of tiers that
       were actually restarted this call."""
    tier_result = await tier_lifecycle.refresh_if_needed(config)
    models = await model_manager.list_models()
    return {
        "models": models,
        "restarted_tiers": tier_result.get("restarted", []),
        "warnings":        tier_result.get("warnings", []),
    }


@app.get("/api/config")
async def api_get_config(request: Request):
    # v2.11.13: non-owner users see the global config with their personal
    # overlay merged on top — their theme/model/prefs, everyone else's
    # system settings. Owner/single-user: unchanged.
    return _effective_config(_session_ns(request))


@app.post("/api/config")
async def api_update_config(payload: dict, request: Request):
    global config
    # #68 Phase E Step 3: validate against the allowlist BEFORE any merge
    # or save. Unknown keys → 400 with the bad key name in the message.
    # Defense against silent typos (audit Bug 6: typo'd keys were getting
    # merged into config.json and persisting forever, invisible to the
    # user). The allowlist is derived from to_flat_dict().keys() so it
    # stays automatically in sync with the schema.
    ok, err = validate_flat_payload(payload)
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    # v2.1.8 max_tokens=-1 trap fix: sanitize before merging so a buggy
    # client (or a curl from a test script) can't poison the runtime
    # config with 0 / negative / non-numeric max_tokens. The frontend
    # already sanitizes, but defense in depth.
    if "max_tokens" in payload:
        payload["max_tokens"] = _sanitize_max_tokens(payload["max_tokens"])

    # v2.11.13 per-user settings: a signed-in NON-owner writes only their
    # own overlay, and only PER_USER_KEYS. System keys from a non-owner →
    # 403 naming the key, so the UI can say why. The owner (and single-
    # user mode) keeps the existing global write path — including
    # multiuser_enabled itself, which is therefore owner-only by
    # construction.
    ns = _session_ns(request)
    if ns:
        bad = [k for k in payload.keys() if k not in PER_USER_KEYS]
        if bad:
            raise HTTPException(
                status_code=403,
                detail=f"setting {bad[0]!r} is managed by the owner profile")
        _save_user_overlay(ns, payload)
        return _effective_config(ns)

    config.update(payload)
    save_config(config)
    model_manager.config = config
    return config


# --- Plugin Routes ------------------------------------------------------------
@app.get("/api/plugins")
async def api_list_plugins():
    return {"plugins": plugin_manager.list_plugins()}

# Plugin toggle moved to v2 (below, with feature wiring)


# --- Sage / Agent Config Routes ----------------------------------------------
@app.get("/api/sage/config")
async def api_sage_config(request: Request):
    # v2.11.13: per-user view — a non-owner sees their own Sage toggles.
    eff = _effective_config(_session_ns(request))
    return {
        "sage_mode": eff.get("sage_mode", True),
        "agentic_mode": eff.get("agentic_mode", True),
        "web_search_enabled": eff.get("web_search_enabled", True),
        "code_exec_enabled": eff.get("code_exec_enabled", True),
    }


@app.post("/api/sage/config")
async def api_set_sage_config(payload: dict, request: Request):
    global config
    keys = ["sage_mode", "agentic_mode", "web_search_enabled", "code_exec_enabled"]
    ns = _session_ns(request)
    if ns:
        # Non-owner: write to the personal overlay only (all four keys are
        # in PER_USER_KEYS), never to the shared config.json.
        _save_user_overlay(ns, {k: bool(payload[k]) for k in keys if k in payload})
        return await api_sage_config(request)
    for k in keys:
        if k in payload:
            config[k] = bool(payload[k])
    save_config(config)
    return await api_sage_config(request)


# --- Developer Mode (hide/show log terminals) --------------------------------
# Simple on/off so normal users get a clean desktop; devs can reveal all the
# console windows (Ollama / Sage / Daemon / Overseer / model servers / etc.).
# State persists in sage_data/ui_prefs.json and is honored by daemon/model
# respawns too. Live toggle via ShowWindow; no restart needed.
@app.get("/api/devmode")
async def api_get_devmode():
    try:
        import devmode
        return {"enabled": devmode.is_enabled()}
    except Exception as e:
        return {"enabled": False, "error": str(e)}


@app.post("/api/devmode")
async def api_set_devmode(payload: dict):
    import devmode
    enabled = bool(payload.get("enabled"))
    devmode.set_enabled(enabled)
    result = devmode.set_consoles_visible(enabled)
    return {"enabled": enabled, "result": result}


@app.get("/api/devmode/diag")
async def api_devmode_diag():
    """Read-only: list terminal-ish windows (class/title/pid) so we can see why
    a console did/didn't hide. Open in a browser and share the JSON."""
    try:
        import devmode
        return devmode.diagnose()
    except Exception as e:
        return {"supported": False, "error": str(e)}


@app.get("/api/build/integrity")
async def api_build_integrity():
    """Build provenance: verify the signed build_manifest.json + re-hash shipped
    files. Returns official / modified / foreign_key / signature_invalid / no_manifest."""
    try:
        import build_integrity
        return build_integrity.verify()
    except Exception as e:
        return {"status": "error", "error": str(e)}


# --- Browser cookie persistence (opt-in, default OFF) -----------------------
@app.get("/api/browser/config")
async def api_get_browser_config():
    try:
        import ui_prefs
        return {"persist_cookies": bool(ui_prefs.get("browser_persist_cookies", False))}
    except Exception:
        return {"persist_cookies": False}


@app.post("/api/browser/config")
async def api_set_browser_config(payload: dict):
    import ui_prefs
    val = bool(payload.get("persist_cookies"))
    ui_prefs.set("browser_persist_cookies", val)
    return {"persist_cookies": val}


@app.on_event("startup")
async def _apply_devmode_on_startup():
    """Apply saved Developer Mode state so the log consoles start hidden
    (default) or visible per the user's last choice. Off-thread + best-effort
    so it never blocks or breaks startup."""
    try:
        import threading, time
        import devmode

        def _go():
            try:
                time.sleep(2.0)  # let start.bat's tier consoles finish opening
                devmode.apply_saved_state()
            except Exception:
                pass

        threading.Thread(target=_go, daemon=True, name="devmode_apply").start()
    except Exception:
        pass


# --- System Prompt File Routes (#68 Phase E Step 6) --------------------------
# Replace the inline "system_prompt" key in /api/config with a dedicated
# endpoint that reads/writes the prompt as a real file (prompts/system.txt).
# Settings.js, the inference path, and any future readers all go through
# this one place. After this lands, the system_prompt compat shim in
# config_store.py can be (and is) removed.
@app.get("/api/prompts/system")
async def api_get_system_prompt(request: Request):
    """Return the current system-prompt addendum. Per-user for a signed-in
    non-owner (sage_data/users/<ns>/prompts/system.txt); the owner / single-user
    reads the shared prompts.system_prompt_file."""
    _up = sage_engine.read_user_prompt(_session_ns(request))
    if _up is not None:
        return {"system_prompt": _up}
    oc = OracleConfig.load(CONFIG_FILE)
    return {"system_prompt": oc._read_prompt_file()}


@app.post("/api/prompts/system")
async def api_set_system_prompt(payload: dict, request: Request):
    """Write the addendum. Per-user for a non-owner; shared file for the owner.
    Empty writes allowed so a user can explicitly clear their own field."""
    text = payload.get("system_prompt")
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="system_prompt must be a string")
    if sage_engine.write_user_prompt(_session_ns(request), text):
        return {"system_prompt": text}
    oc = OracleConfig.load(CONFIG_FILE)
    oc._write_prompt_file(text)
    return {"system_prompt": oc._read_prompt_file()}


# --- Vibe Prompts -------------------------------------------------------------
@app.get("/api/vibe-prompts")
async def api_vibe_prompts():
    return sage_engine.VIBE_PROMPTS


# --- Tavily Key Management ---------------------------------------------------
@app.get("/api/tavily")
async def api_get_tavily():
    return sage_engine.get_tavily_key_info()


@app.post("/api/tavily")
async def api_set_tavily(payload: dict):
    key = payload.get("api_key", "")
    return sage_engine.set_tavily_key(key)


@app.delete("/api/tavily")
async def api_delete_tavily():
    return sage_engine.delete_tavily_key()


# --- Archive Routes -----------------------------------------------------------
def _session_ns(request: Request):
    """Per-user namespace for the signed-in NON-owner, so each user sees only their
    own conversations/archives. None for the owner or when multi-user is off ->
    the existing shared store, unchanged."""
    try:
        if not config.get("multiuser_enabled", False):
            return None
        import session as _session
        s = _session.get_session(request.cookies.get(_AUTH_COOKIE))
        if s and not s.get("is_owner"):
            return s.get("ns")
    except Exception:
        pass
    return None


def _is_owner(request: Request) -> bool:
    """True when the caller is the owner profile, or when multi-user is off
    (single-user = owner by definition). Used to gate system-level settings
    and the socials endpoints in multi-profile mode."""
    try:
        if not config.get("multiuser_enabled", False):
            return True
        import session as _session
        s = _session.get_session(request.cookies.get(_AUTH_COOKIE))
        return bool(s and s.get("is_owner"))
    except Exception:
        return False


# --- v2.11.13: per-user settings overlay -------------------------------------
# Settings no longer bleed between profiles. Each non-owner user gets an
# overlay file (sage_data/users/<ns>/settings.json) holding ONLY the keys in
# PER_USER_KEYS — chat + appearance preferences. Everything else (network,
# ports, tiers, hardware toggles, security, multiuser_enabled itself) stays
# in the global config.json and is OWNER-ONLY to change. The overlay is
# merged over the global config for reads, so a fresh user starts from the
# owner's defaults and diverges only where they change something.
PER_USER_KEYS = frozenset({
    "theme", "haptic", "default_model", "temperature", "max_tokens",
    "sage_mode", "agentic_mode", "web_search_enabled", "code_exec_enabled",
    "privacy_mode",
})


def _user_settings_file(ns) -> Path:
    return Path(DATA_DIR) / "users" / str(ns) / "settings.json"


def _load_user_overlay(ns) -> dict:
    """The user's saved preference overlay. Filtered to PER_USER_KEYS on read
    as well as write, so a hand-edited file can't smuggle system keys."""
    if not ns:
        return {}
    try:
        f = _user_settings_file(ns)
        if f.exists():
            raw = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {k: v for k, v in raw.items() if k in PER_USER_KEYS}
    except Exception:
        pass
    return {}


def _save_user_overlay(ns, patch: dict) -> dict:
    """Merge `patch` (already validated) into the user's overlay and persist."""
    overlay = _load_user_overlay(ns)
    overlay.update({k: v for k, v in patch.items() if k in PER_USER_KEYS})
    try:
        f = _user_settings_file(ns)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(overlay, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[USER SETTINGS] save failed for ns={ns}: {e}")
    return overlay


def _effective_config(ns) -> dict:
    """Global config with the user's overlay applied. ns=None -> global as-is."""
    if not ns:
        return config
    return {**config, **_load_user_overlay(ns)}


def _downloads_dir_for_ns(ns):
    """Per-user downloads (generated images / saved files), or the shared dir for
    owner / single-user. Created on demand."""
    base = sage_engine.user_data_dir(ns)
    d = (base / "downloads") if base else DOWNLOADS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _uploads_dir_for_ns(ns):
    base = sage_engine.user_data_dir(ns)
    d = (base / "uploads") if base else (BASE_DIR / "uploads")
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.get("/api/archives")
async def api_list_archives(request: Request):
    return {"archives": sage_engine.get_archives(_session_ns(request))}


@app.post("/api/archives/save")
async def api_archive_chat(request: Request):
    ns = _session_ns(request)
    history = sage_engine.load_chat_memory(ns)
    return sage_engine.archive_conversation(history, ns)


@app.post("/api/archives/load")
async def api_load_archive(payload: dict, request: Request):
    fn = payload.get("filename", "")
    if not fn:
        raise HTTPException(400, "filename required")
    return sage_engine.load_archive(fn, _session_ns(request))


@app.post("/api/archives/delete")
async def api_delete_archive(payload: dict, request: Request):
    fn = payload.get("filename", "")
    if not fn:
        raise HTTPException(400, "filename required")
    return sage_engine.delete_archive(fn, _session_ns(request))


# ============================================================================
# v2.2 (2026-05-31) -- OpenAI-compatible + MCP routes for Continue.dev / IDE
# integration. Session 1 scope: working MCP (HTTP + stdio share dispatcher),
# OpenAI /v1/models stub, OpenAI /v1/chat/completions as a thin Ollama
# pass-through (NO agentic loop yet -- that's Session 2 via fake-WebSocket
# bridge). All routes are additive; the existing WebSocket chat path is
# untouched.
# ============================================================================

import mcp_handlers as _mcp_handlers  # noqa: E402  -- import after app init
import _ws_bridge as _ws_bridge_mod    # noqa: E402  -- Session-2 pipeline bridge
import auth as _auth                    # noqa: E402  -- v2.3 bearer-token auth
from fastapi import Depends             # noqa: E402  -- v2.3 dependency wiring

# v2.3 (2026-05-31): initialise the bearer-token keystore at
# module load time so the first-boot banner appears in the
# same console window the user is already watching during
# start.bat. ensure_keystore() is idempotent on subsequent
# boots -- it just loads what already exists.
app.state.keystore = _auth.ensure_keystore()
import time as _oai_time               # used for response timestamps
import uuid as _oai_uuid
import json as _oai_json               # SSE chunk serialisation
from fastapi.responses import StreamingResponse as _SSEResponse  # noqa: E402


# ----- MCP HTTP route -------------------------------------------------------

@app.post(
    "/mcp/v1/jsonrpc",
    dependencies=[Depends(_auth.require_scope("mcp:*"))],
)
async def mcp_jsonrpc(payload: dict):
    """MCP (Model Context Protocol) over HTTP via JSON-RPC 2.0.

    Accepts either a single request envelope or a batch (list). Returns
    the matching response shape. Notifications (no id) get no response;
    in batch mode they are omitted from the result list.

    See backend/mcp_handlers.py for the dispatch logic. The stdio entry
    (backend/mcp_server.py) shares the same handler module so HTTP and
    stdio transports behave identically.
    """
    # Batch
    if isinstance(payload, list):
        responses = []
        for req in payload:
            if not isinstance(req, dict):
                continue
            r = _mcp_handlers.handle_jsonrpc(req)
            if r is not None:
                responses.append(r)
        return responses
    # Single
    if not isinstance(payload, dict):
        raise HTTPException(400, "JSON-RPC request must be object or array")
    resp = _mcp_handlers.handle_jsonrpc(payload)
    if resp is None:
        return {"status": "accepted"}  # notification, no protocol response
    return resp


# ----- OpenAI /v1/models stub -----------------------------------------------

@app.get(
    "/v1/models",
    dependencies=[Depends(_auth.require_scope("chat:read"))],
)
async def openai_list_models():
    """OpenAI-compatible models list. Continue.dev probes this on connect.

    v2.2 (2026-05-31): returns a single virtual model identifier
    'sage-pipeline' which represents the configured OracleAI inference
    chain (auto-route between primary + secondary per config.json --
    the request's `model` field is ignored). Once Session 2 lands the
    agentic-loop bridge, this same model id will route through the
    full Sage pipeline including tool dispatch.
    """
    return {
        "object": "list",
        "data": [
            {
                "id": "sage-pipeline",
                "object": "model",
                "created": int(_oai_time.time()),
                "owned_by": "oracleai",
            },
        ],
    }


# ----- OpenAI /v1/chat/completions (Session-2 full pipeline via bridge) -----

def _oai_chunk(cid: str, model_id: str, delta: dict,
               finish_reason: str | None = None) -> str:
    """Build a single OpenAI streaming chunk in SSE wire format."""
    chunk = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(_oai_time.time()),
        "model": model_id,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }
    return "data: " + _oai_json.dumps(chunk, separators=(",", ":")) + "\n\n"


@app.post(
    "/v1/chat/completions",
    dependencies=[Depends(_auth.require_scope("chat:write"))],
)
async def openai_chat_completions(payload: dict):
    """OpenAI-compatible chat completions, backed by the FULL Sage pipeline.

    v2.2 Session 2 (2026-05-31): every request flows through the same
    ws_chat handler the chat UI uses, via the _ws_bridge fake-WebSocket
    adapter. The pipeline includes:

      * Persona prompt (SAGE_SYSTEM_PROMPT lead + user-add-on)
      * Agentic loop with tool-tag dispatch
      * Procedural memory writes (chain-witnessed; source-tagged
        "openai_endpoint" via the agentic-loop's own metadata path)
      * Hash-chain logging via memory_logger
      * AIQNudge participation
      * Auto-route between primary + secondary slots per config.json

    The incoming `model` field is IGNORED -- config.json governs
    routing, per the Session-1 design decision. Continue.dev's model
    selector becomes cosmetic; the substrate decides per query.

    Supports stream=true via SSE chunks. Non-streaming returns the
    full assistant message in one response.
    """
    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(400, "messages array required")

    stream_requested = bool(payload.get("stream"))

    # Build options passed through to the handler. We only forward
    # the canonical OpenAI tunables; anything else is dropped to keep
    # the surface stable.
    pass_through_options = {
        k: payload[k] for k in ("temperature", "top_p", "max_tokens")
        if k in payload
    }

    cid = f"chatcmpl-{_oai_uuid.uuid4().hex[:24]}"
    # The visible model id in the response. We use a stable identifier
    # rather than whatever the router picked, so clients see the
    # SAME model id across turns (avoiding chat-history disruption in
    # IDEs that key conversation state on model name).
    response_model_id = "sage-pipeline"

    # ----- Non-streaming path: collect, assemble, return -----
    if not stream_requested:
        events = await _ws_bridge_mod.run_full_pipeline(
            user_messages=messages,
            model_id=None,             # let auto-route fire
            options=pass_through_options,
            on_token=None,
        )
        err = _ws_bridge_mod.extract_error(events)
        if err is not None:
            raise HTTPException(500, f"Pipeline error: {err}")
        assistant_text = _ws_bridge_mod.assemble_assistant_text(events)
        return {
            "id": cid,
            "object": "chat.completion",
            "created": int(_oai_time.time()),
            "model": response_model_id,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": assistant_text},
                "finish_reason": "stop",
            }],
            "usage": {},   # token accounting NYI; clients tolerate empty
        }

    # ----- Streaming path: SSE with delta chunks -----
    import asyncio as _aio

    chunk_queue: _aio.Queue = _aio.Queue()

    async def _on_token(ev: dict) -> None:
        """Bridge callback: forward each token chunk to the SSE queue."""
        content = ev.get("content")
        if content is None:
            return
        await chunk_queue.put(("delta", str(content)))

    async def _drive_pipeline() -> None:
        try:
            events = await _ws_bridge_mod.run_full_pipeline(
                user_messages=messages,
                model_id=None,
                options=pass_through_options,
                on_token=_on_token,
            )
            err = _ws_bridge_mod.extract_error(events)
            if err is not None:
                await chunk_queue.put(("error", err))
        except Exception as e:
            await chunk_queue.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            await chunk_queue.put(("done", None))

    async def _sse_stream():
        # Initial chunk: assistant role (OpenAI convention).
        yield _oai_chunk(cid, response_model_id, {"role": "assistant"})
        # Drive the pipeline as a background task; consume queue.
        driver = _aio.create_task(_drive_pipeline())
        try:
            while True:
                kind, value = await chunk_queue.get()
                if kind == "delta":
                    yield _oai_chunk(
                        cid, response_model_id, {"content": value},
                    )
                elif kind == "error":
                    # Surface error as a final chunk with content prefix.
                    yield _oai_chunk(
                        cid, response_model_id,
                        {"content": f"\n[PIPELINE ERROR] {value}"},
                    )
                elif kind == "done":
                    break
            # Final chunk: empty delta + finish_reason="stop".
            yield _oai_chunk(cid, response_model_id, {}, finish_reason="stop")
            yield "data: [DONE]\n\n"
        finally:
            if not driver.done():
                driver.cancel()

    return _SSEResponse(
        _sse_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disables nginx buffering if present
        },
    )


@app.get("/api/chat-memory")
async def api_get_memory(request: Request):
    return {"history": sage_engine.load_chat_memory(_session_ns(request))}


@app.post("/api/chat-memory")
async def api_save_memory(payload: dict, request: Request):
    sage_engine.save_chat_memory(payload.get("history", []), _session_ns(request))
    return {"success": True}


# --- Complexity Routing -------------------------------------------------------
@app.post("/api/route-query")
async def api_route_query(payload: dict):
    prompt = payload.get("prompt", "")
    return {
        "recommended_model": sage_engine.route_query(prompt),
        "complexity": sage_engine.analyze_complexity(prompt),
        "query_type": sage_engine.detect_query_type(prompt),
    }


# --- AIQNudge send (UI side-channel) ------------------------------------------
@app.post("/api/aiq-nudge")
async def api_aiq_nudge(payload: dict):
    """Sign + deposit a mid-run nudge for Sage from the chat UI — the
    button-driven equivalent of the aiq_nudge_send.py terminal helper, so the
    user never has to open a terminal to steer Sage mid-run.

    Localhost-only (deliberately NOT in the remote allowlist). Honors
    config['aiq_nudge_enabled']; the signed file lands in sage_data/nudges/
    and Sage consumes it on her next agentic step. Uses the shared
    AIQNudge.send() and the same sage_data-resolved key as the consumer, so
    UI nudges verify identically to terminal ones."""
    message = (payload.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "empty nudge")
    if not config.get("aiq_nudge_enabled", False):
        raise HTTPException(409, "AIQNudge is disabled (set aiq_nudge_enabled)")
    if aiq_nudge is None:
        raise HTTPException(503, "AIQNudge channel unavailable (key/setup error)")
    try:
        target = aiq_nudge.send(message)
    except Exception as e:
        raise HTTPException(500, f"could not write nudge: {e}")
    return {"success": True, "file": target.name, "length": len(message)}


# --- File Upload Route ---
@app.post("/api/upload")
async def api_upload(request: Request, file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "No file provided")

    import re
    import time
    safe_name = re.sub(
        r'[^\w\-.]', '_', file.filename
    ) or f"upload_{TimeManager.epoch_int()}"  # v2.1.6 unified

    dest = _uploads_dir_for_ns(_session_ns(request)) / safe_name
    dest.parent.mkdir(parents=True, exist_ok=True)

    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    size = dest.stat().st_size
    # Upload size cap removed per project policy: local system, hardware-limited,
    # no arbitrary size thresholds. User's disk/RAM is the only constraint.

    _result = sage_engine.process_upload(str(dest), safe_name)
    # v2.9 hardening: encrypt the at-rest upload copy. process_upload above read
    # the plaintext; afterward the file is only a record (vision uses the base64
    # in the response, never re-reads the disk file).
    try:
        import atrest as _atrest
        with open(dest, "rb") as _uf:
            _raw = _uf.read()
        if not _atrest.is_encrypted(_raw):
            with open(dest, "wb") as _uf:
                _uf.write(_atrest.encrypt_bytes(_raw))
    except Exception:
        pass
    return _result


# --- Image generation (manual trigger; ComfyUI) ------------------------------
async def _generate_full_routed(messages, model_id, options):
    """generate_full with Sage Network offload, used by the AGENTIC loop so its
    per-step inference can run on the remote node too (orchestration + tools stay
    local; inference goes remote). model_id=None on offload -> the desktop picks.
    Falls back to local generate_full on any failure. No recursion: the node
    /api/node/infer endpoint calls model_manager.generate_full directly."""
    try:
        if (bool(config.get("offload_enabled", False))
                and (config.get("remote_node_url") or "").strip()):
            import node_trust
            import node_client
            _remote = config.get("remote_node_url").strip()
            _token = node_trust.load_or_create_home_token(str(DATA_DIR))
            _ok, _res = await asyncio.to_thread(
                node_client.node_infer, _remote, _token, None, messages, options)
            if (_ok and isinstance(_res, dict) and _res.get("success")
                    and _res.get("content") is not None):
                _current_offload.set(_remote)
                return _res["content"]
    except Exception:
        pass
    return await model_manager.generate_full(messages, model_id, options)


async def _generate_image_routed(prompt, downloads_dir=None, **opts):
    """Generate an image, OFFLOADING to a configured remote node (the desktop)
    when one is set - so a GPU-less client still makes images, via the encrypted
    node channel. Falls back to local ComfyUI if no remote is set or it is
    unreachable. Always saves the result into THIS machine's downloads. Returns a
    comfyui_client-style result dict."""
    import node_trust
    _dl = Path(downloads_dir) if downloads_dir else DOWNLOADS_DIR
    remote = (config.get("remote_node_url") or "").strip()
    if not bool(config.get("offload_enabled", False)):
        remote = ""  # offload disabled in settings -> always run locally
    if remote:
        try:
            import node_client
            token = node_trust.load_or_create_home_token(str(DATA_DIR))
            ok, res = await asyncio.to_thread(
                node_client.node_generate_image, remote, token, prompt)
            if ok and isinstance(res, dict) and res.get("success") and res.get("data"):
                try:
                    import base64
                    import uuid
                    raw = base64.b64decode(res["data"])
                    fn = res.get("filename") or ("gen_remote_" + uuid.uuid4().hex[:8] + ".png")
                    _dl.mkdir(parents=True, exist_ok=True)
                    import atrest as _atrest
                    with open(_dl / fn, "wb") as _f:
                        _f.write(_atrest.encrypt_bytes(raw))
                    res["path"] = str(_dl / fn)
                    res["via"] = "remote"
                except Exception:
                    pass
                return res
            if ok and isinstance(res, dict):
                res.setdefault("via", "remote")
                return res  # remote reached but its ComfyUI failed
            # node unreachable -> fall through to local
        except Exception:
            pass
    # ── Local ComfyUI: on-demand lifecycle ─────────────────────────────────────
    # Ensure a headless server is up (start it if needed, regardless of the
    # autostart-at-boot setting), generate, then kill + respawn a CLEAN instance
    # in the background so the in-memory job queue (which holds the last image) is
    # destroyed for privacy and a fresh server is warm for the next request.
    import comfyui_launcher
    ensure = await asyncio.to_thread(comfyui_launcher.ensure_running, config)
    if not ensure.get("running"):
        return {"success": False,
                "error": ("ComfyUI could not be started automatically: "
                          + str(ensure.get("reason") or "unknown reason")
                          + ". Try again, or check Settings → Image Generation.")}
    import comfyui_client
    result = await asyncio.to_thread(
        comfyui_client.generate_image, prompt,
        downloads_dir=str(_dl), **opts)
    # Wipe-and-rewarm only after a real render (image now sits in Comfy's queue).
    if isinstance(result, dict) and result.get("success"):
        try:
            asyncio.create_task(asyncio.to_thread(comfyui_launcher.respawn, config))
        except Exception:
            pass
    return result


@app.post("/api/generate-image")
async def api_generate_image(payload: dict, request: Request):
    """Manually generate an image from a prompt via the local ComfyUI backend.
    Returns the comfyui_client result dict (success + path + base64 data, or a
    clear error). Runs in a thread so a long render never blocks the loop."""
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "No prompt provided")
    opts = {}
    for _k in ("negative", "width", "height", "steps", "cfg",
               "sampler", "scheduler", "seed", "checkpoint", "url"):
        if payload.get(_k) is not None:
            opts[_k] = payload[_k]
    # Apply the user's selected model: its checkpoint + architecture-specific
    # params (e.g. Flux schnell -> 4 steps, cfg 1.0) unless the caller overrode
    # them in the request. Falls back silently if no model has been chosen.
    try:
        import comfyui_models, comfyui_setup
        _home = comfyui_setup.detect_existing()
        _inst = comfyui_models.list_installed(_home) if _home else []
        # Active model: persisted selection -> only/first installed -> client auto.
        _ckpt = (comfyui_models.get_selection().get("checkpoint") or "").strip()
        if _ckpt and _ckpt not in _inst:
            _ckpt = ""                       # selection no longer on disk
        if not _ckpt and _inst:
            _ckpt = _inst[0]
        if _ckpt:
            opts.setdefault("checkpoint", _ckpt)
        # Architecture params (e.g. Flux schnell -> 4 steps / cfg 1.0), by filename.
        for _pk, _pv in comfyui_models.params_for(_ckpt).items():
            opts.setdefault(_pk, _pv)
    except Exception:
        pass
    return await _generate_image_routed(
        prompt, downloads_dir=str(_downloads_dir_for_ns(_session_ns(request))), **opts)


# --- Sage network: node surface (gated OFF by default; token + Fernet) --------
@app.post("/api/node/info")
async def api_node_info(request: Request):
    import node_server
    if not node_server.node_enabled(config):
        raise HTTPException(403, "node server disabled")
    token = node_server.get_token(str(DATA_DIR))
    ok, env = node_server.read_request(await request.body(), token)
    if not ok:
        raise HTTPException(403, "unauthorized")
    try:
        models = await model_manager.list_models()
        ids = [m.get("id") for m in (models or [])]
    except Exception:
        ids = []
    caps = node_server.capabilities(
        token, config.get("node_name", "oracle-node"), ids, True)
    return Response(content=node_server.seal_response(env, True, caps, token),
                    media_type="application/octet-stream")


def _is_gen_error(text):
    """True if `text` is a bracketed generation/model-load error string from
    model_manager (e.g. '[Oracle Ollama error 500: ... mllama ...]')."""
    if not isinstance(text, str):
        return False
    s = text.strip()
    return s.startswith("[") and (
        "Ollama error" in s or s.startswith("[Error:")
        or "not found on any running tier" in s)


def _sanitize_node_options(opts):
    """Bound a REMOTE node's supplied generation options to safe ranges so a
    malicious or buggy peer cannot exhaust this host (huge context window,
    runaway generation length, etc.). Unknown keys are dropped. Local requests
    are NOT passed through here -- only options that arrived over the wire."""
    if not isinstance(opts, dict):
        return {}
    out = {}

    def _num(key, lo, hi, cast=float):
        v = opts.get(key)
        if v is None:
            return
        try:
            v = cast(v)
        except (TypeError, ValueError):
            return
        out[key] = max(lo, min(hi, v))

    _num("temperature", 0.0, 2.0)
    _num("top_p", 0.0, 1.0)
    _num("top_k", 0, 1000, int)
    _num("repeat_penalty", 0.0, 5.0)
    # Context + length: hard cap to prevent OOM / runaway. A remote peer does
    # NOT get the -1 "unlimited" sentinel -- they get a bounded cap instead.
    for _k in ("num_ctx", "n_ctx"):
        _v = opts.get(_k)
        if _v is not None:
            try:
                out[_k] = max(0, min(131072, int(_v)))
            except (TypeError, ValueError):
                pass
    for _k in ("num_predict", "max_tokens"):
        _v = opts.get(_k)
        if _v is not None:
            try:
                _iv = int(_v)
                out[_k] = 4096 if _iv < 0 else max(0, min(131072, _iv))
            except (TypeError, ValueError):
                pass
    return out


def _node_model_candidates(msgs, primary, needs_vision):
    """Ordered model candidates for a node request: the routed pick first, then
    other capable slots (vision for an image turn, language otherwise), then any
    remaining slot as a last resort. Lets the node fall back when a model's
    architecture will not load -> 'plug-and-play' across mixed user setups."""
    _slots = [s for s in (config.get("default_model"),
                          config.get("secondary_model"),
                          config.get("tertiary_model")) if s]
    if needs_vision:
        _cap = [s for s in _slots if sage_engine.model_capabilities(s).get("vision")]
    else:
        _cap = [s for s in _slots
                if sage_engine.model_capabilities(s).get("language", True)]
    _ordered = ([primary] if primary else []) + _cap + _slots
    _seen = set()
    _out = []
    for _m in _ordered:
        if _m and _m not in _seen:
            _seen.add(_m)
            _out.append(_m)
    return _out


@app.post("/api/node/infer")
async def api_node_infer(request: Request):
    import node_server
    if not node_server.node_enabled(config):
        raise HTTPException(403, "node server disabled")
    token = node_server.get_token(str(DATA_DIR))
    ok, env = node_server.read_request(await request.body(), token)
    if not ok:
        raise HTTPException(403, "unauthorized")
    body = env["body"]
    _msgs = body.get("messages") or []
    _model = body.get("model_id") or body.get("model")
    _needs_vision = any(isinstance(_m, dict) and _m.get("images") for _m in _msgs)
    if not _model:
        # Capability-aware pick among THIS node's slots so an uploaded image gets
        # PROCESSED on the right model: image-bearing request -> a vision model,
        # text -> a language model. (Vision offload from a GPU-less client.)
        _slots = [s for s in (config.get("default_model"),
                              config.get("secondary_model"),
                              config.get("tertiary_model")) if s]
        if len(_slots) >= 2:
            _last = _msgs[-1].get("content", "") if _msgs else ""
            _model = sage_engine.route_query(_last, candidates=_slots,
                                             needs_vision=_needs_vision)
        else:
            _model = config.get("default_model")
    _opts = _sanitize_node_options(body.get("options") or {})
    # v2.11.13 urgency: remote requests ride the remote lanes (2 urgent /
    # 3 normal — local users always outrank both). Urgent is honored only
    # within the peer's rolling budget (urgent_quota): over budget the
    # request is DEMOTED to normal and logged — never rejected, so the
    # urgent lane cannot be Bogarted.
    _opts["_priority"] = 3
    if bool(body.get("urgent")):
        import urgent_quota
        _peer = str(env.get("user", "owner"))
        if urgent_quota.allow_urgent(_peer):
            _opts["_priority"] = 2
            print(f"[NODE URGENT] granted to {_peer!r} "
                  f"({urgent_quota.remaining(_peer)} left this hour)")
        else:
            print(f"[NODE URGENT] budget exhausted for {_peer!r} — demoted to normal")
    # Graceful fallback: if a model will not load (unsupported arch, e.g. mllama),
    # try the next capable slot so distribution "just works".
    text = None
    _err = ""
    _used = _model
    for _cand in _node_model_candidates(_msgs, _model, _needs_vision):
        try:
            _t = await model_manager.generate_full(_msgs, _cand, _opts)
        except Exception as e:
            _err = str(e)
            continue
        if _is_gen_error(_t):
            _err = _t
            continue
        text = _t
        _used = _cand
        break
    if text is not None:
        result = {"success": True, "content": text, "model": _used}
    else:
        result = {"success": False, "error": _err or "all candidate models failed"}
    return Response(
        content=node_server.seal_response(env, result.get("success", True), result, token),
        media_type="application/octet-stream")


@app.post("/api/node/infer-stream")
async def api_node_infer_stream(request: Request):
    import node_server
    import node_trust
    if not node_server.node_enabled(config):
        raise HTTPException(403, "node server disabled")
    token = node_server.get_token(str(DATA_DIR))
    ok, env = node_server.read_request(await request.body(), token)
    if not ok:
        raise HTTPException(403, "unauthorized")
    body = env["body"]
    _msgs = body.get("messages") or []
    _model = body.get("model_id") or body.get("model")
    _nv = any(isinstance(_m, dict) and _m.get("images") for _m in _msgs)
    if not _model:
        _slots = [s for s in (config.get("default_model"),
                              config.get("secondary_model"),
                              config.get("tertiary_model")) if s]
        _model = (sage_engine.route_query(
                      _msgs[-1].get("content", "") if _msgs else "",
                      candidates=_slots, needs_vision=_nv)
                  if len(_slots) >= 2 else config.get("default_model"))
    _opts = _sanitize_node_options(body.get("options") or {})
    # v2.11.13 urgency — same policy as /api/node/infer above.
    _opts["_priority"] = 3
    if bool(body.get("urgent")):
        import urgent_quota
        _peer = str(env.get("user", "owner"))
        if urgent_quota.allow_urgent(_peer):
            _opts["_priority"] = 2
            print(f"[NODE URGENT] granted to {_peer!r} "
                  f"({urgent_quota.remaining(_peer)} left this hour)")
        else:
            print(f"[NODE URGENT] budget exhausted for {_peer!r} — demoted to normal")
    _cands = _node_model_candidates(_msgs, _model, _nv)

    async def _stream():
        _last_err = ""
        for _cand in _cands:
            _first = True
            _failed = False
            _yielded = False
            try:
                async for _tok in model_manager.generate(_msgs, _cand, _opts):
                    if _first:
                        _first = False
                        if _is_gen_error(_tok):
                            _last_err = _tok
                            _failed = True
                            break
                    yield node_trust.encrypt_payload({"t": _tok}, token) + b"\n"
                    _yielded = True
            except Exception as _e:
                _last_err = str(_e)
                _failed = True
            if not _failed:
                yield node_trust.encrypt_payload({"end": True}, token) + b"\n"
                return
            if _yielded:
                # failure after real tokens already streamed -> cannot safely
                # restart on another model; end with what we have.
                yield node_trust.encrypt_payload({"end": True}, token) + b"\n"
                return
            # failed before any output -> try the next candidate
        yield node_trust.encrypt_payload(
            {"e": _last_err or "all candidate models failed"}, token) + b"\n"
        yield node_trust.encrypt_payload({"end": True}, token) + b"\n"

    return _SSEResponse(_stream(), media_type="application/octet-stream")


@app.post("/api/node/generate-image")
async def api_node_generate_image(request: Request):
    import node_server
    if not node_server.node_enabled(config):
        raise HTTPException(403, "node server disabled")
    token = node_server.get_token(str(DATA_DIR))
    ok, env = node_server.read_request(await request.body(), token)
    if not ok:
        raise HTTPException(403, "unauthorized")
    body = env["body"]
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        result = {"success": False, "error": "empty prompt"}
    else:
        import comfyui_client
        opts = {k: body[k] for k in ("negative", "width", "height", "steps",
                "cfg", "sampler", "scheduler", "seed", "checkpoint")
                if body.get(k) is not None}
        result = await asyncio.to_thread(
            comfyui_client.generate_image, prompt,
            downloads_dir=str(DOWNLOADS_DIR), **opts)
    return Response(
        content=node_server.seal_response(env, result.get("success", True), result, token),
        media_type="application/octet-stream")


# --- Sage network MANAGEMENT (localhost-only via the LAN-exposure guard) -------
@app.get("/api/sage-network/status")
async def api_sn_status():
    import node_trust
    token = node_trust.load_or_create_home_token(str(DATA_DIR))
    import socket
    _lan = "127.0.0.1"
    try:
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _s.connect(("8.8.8.8", 80))
        _lan = _s.getsockname()[0]
        _s.close()
    except Exception:
        pass
    try:
        from config import PORT_APP as _port
    except Exception:
        _port = 8000
    return {
        "node_server_enabled": bool(config.get("node_server_enabled", False)),
        "node_name": config.get("node_name", ""),
        "remote_node_url": config.get("remote_node_url", ""),
        "offload_enabled": bool(config.get("offload_enabled", False)),
        "host": config.get("host", "127.0.0.1"),
        "lan_ip": _lan,
        "app_port": _port,
        "fingerprint": node_trust.token_fingerprint(token),
    }


@app.get("/api/sage-network/token")
async def api_sn_token_reveal():
    # localhost-only (the LAN guard blocks this from remote). Reveals THIS node's
    # home token so the user can copy it to another machine they own.
    import node_trust
    token = node_trust.load_or_create_home_token(str(DATA_DIR))
    return {"token": token, "fingerprint": node_trust.token_fingerprint(token)}


@app.post("/api/sage-network/token")
async def api_sn_token_set(payload: dict):
    import node_trust
    tok = (payload.get("token") or "").strip()
    if not tok:
        raise HTTPException(400, "no token provided")
    if not node_trust.set_home_token(str(DATA_DIR), tok):
        raise HTTPException(500, "could not write token")
    return {"ok": True, "fingerprint": node_trust.token_fingerprint(tok)}


@app.post("/api/sage-network/token/reset")
async def api_sn_token_reset():
    import node_trust
    tok = node_trust.reset_home_token(str(DATA_DIR))
    if not tok:
        raise HTTPException(500, "could not reset token")
    return {"ok": True, "token": tok, "fingerprint": node_trust.token_fingerprint(tok)}


@app.post("/api/sage-network/pair-test")
async def api_sn_pair_test(payload: dict):
    import node_trust
    import node_client
    url = (payload.get("url") or config.get("remote_node_url") or "").strip()
    if not url:
        raise HTTPException(400, "no remote node url")
    token = node_trust.load_or_create_home_token(str(DATA_DIR))
    ok, result = await asyncio.to_thread(node_client.node_info, url, token)
    if ok:
        return {
            "ok": True,
            "remote": result,
            "fingerprint_match": (result.get("fingerprint")
                                  == node_trust.token_fingerprint(token)),
        }
    return {"ok": False, "error": result}


# --- Downloads (Sage output files) -------------------------------------------
@app.get("/api/downloads")
async def api_list_downloads(request: Request):
    files = []
    _dir = _downloads_dir_for_ns(_session_ns(request))
    if _dir.exists():
        for f in sorted(_dir.iterdir(), reverse=True):
            if f.is_file():
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "modified": f.stat().st_mtime,
                })
    return {"files": files}


@app.get("/api/downloads/{filename}")
async def api_download_file(filename: str, request: Request, dl: bool = False):
    name = Path(filename).name              # basename only -> no cross-user traversal
    path = _downloads_dir_for_ns(_session_ns(request)) / name
    if not path.exists():
        raise HTTPException(404, "File not found")
    import atrest as _atrest
    import mimetypes as _mt
    _data = _atrest.read_file_auto(str(path))     # decrypt for viewing / export
    _media = _mt.guess_type(name)[0] or "application/octet-stream"
    _disp = "attachment" if dl else "inline"      # ?dl=1 -> 'Save a copy' (plaintext download)
    return Response(content=_data, media_type=_media,
                    headers={"Content-Disposition": '%s; filename="%s"' % (_disp, name)})


@app.delete("/api/downloads/{filename}")
async def api_delete_download(filename: str, request: Request):
    path = _downloads_dir_for_ns(_session_ns(request)) / Path(filename).name
    if path.exists():
        path.unlink()
        return {"success": True}
    return {"success": False, "error": "File not found"}


@app.delete("/api/downloads")
async def api_clear_downloads(request: Request):
    count = 0
    _dir = _downloads_dir_for_ns(_session_ns(request))
    if _dir.exists():
        for f in _dir.iterdir():
            if f.is_file():
                f.unlink()
                count += 1
    return {"success": True, "deleted": count}


@app.post("/api/downloads/save")
async def api_save_to_downloads(payload: dict, request: Request):
    """Save text content as a file in downloads."""
    filename = payload.get("filename", "")
    content = payload.get("content", "")
    if not filename:
        raise HTTPException(400, "filename required")
    import re as _re
    safe_name = _re.sub(r'[^\w\-.]', '_', filename)
    path = _downloads_dir_for_ns(_session_ns(request)) / safe_name
    path.write_text(content, encoding="utf-8")
    return {"success": True, "filename": safe_name, "size": path.stat().st_size}


# --- Health Check ---
@app.get("/api/health")
async def health(request: Request):
    # Lock to localhost - external callers get nothing
    if request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=404)


# --- Per-user auth (Phase 2): accounts, login, sessions ---------------------
# A2a: the auth FLOW. Enforcement (gating middleware) is A2b and is OFF until
# config.multiuser_enabled is set, so this changes nothing for a single-user run.
_AUTH_COOKIE = "oai_session"
_AUTH_TTL = 7 * 24 * 3600
from wan_guard import AbuseGuard
_auth_guard = AbuseGuard(max_requests=30, window_sec=60, fail_threshold=6, ban_sec=900)


@app.get("/api/auth/status")
async def api_auth_status(request: Request):
    import users as _users
    import session as _session
    s = _session.get_session(request.cookies.get(_AUTH_COOKIE))
    _mu = bool(config.get("multiuser_enabled", False))
    if s:
        return {"authenticated": True, "username": s["username"],
                "is_owner": s["is_owner"], "needs_setup": False, "multiuser": _mu}
    return {"authenticated": False, "needs_setup": not _users.any_users(),
            "multiuser": _mu}


@app.post("/api/auth/setup")
async def api_auth_setup(payload: dict):
    import users as _users
    import session as _session
    if _users.any_users():
        raise HTTPException(409, "owner account already exists")
    r = _users.create_user(payload.get("username", ""), payload.get("password", ""),
                           is_owner=True)
    if not r.get("success"):
        raise HTTPException(400, r.get("error", "could not create account"))
    tok = _session.create_session(r, ttl=_AUTH_TTL)
    resp = JSONResponse({"success": True, "username": r["username"], "is_owner": True})
    # Session cookie (NO max_age) so it's discarded when the browser/app closes
    # -> reopening requires a fresh sign-in. Server session still expires via TTL.
    resp.set_cookie(_AUTH_COOKIE, tok, httponly=True, samesite="lax")
    return resp


@app.post("/api/auth/login")
async def api_auth_login(payload: dict, request: Request):
    import users as _users
    import session as _session
    _ip = request.client.host if request.client else "?"
    if _auth_guard.is_banned(_ip):
        raise HTTPException(429, "too many failed attempts; try again shortly")
    r = _users.verify_user(payload.get("username", ""), payload.get("password", ""))
    if not r.get("success"):
        _auth_guard.record_failure(_ip)
        raise HTTPException(401, "invalid credentials")
    _auth_guard.record_success(_ip)
    tok = _session.create_session(r, ttl=_AUTH_TTL)
    resp = JSONResponse({"success": True, "username": r["username"],
                         "is_owner": r["is_owner"]})
    # Session cookie (NO max_age) so it's discarded when the browser/app closes
    # -> reopening requires a fresh sign-in. Server session still expires via TTL.
    resp.set_cookie(_AUTH_COOKIE, tok, httponly=True, samesite="lax")
    return resp


@app.post("/api/auth/logout")
async def api_auth_logout(request: Request):
    import session as _session
    _session.destroy_session(request.cookies.get(_AUTH_COOKIE))
    resp = JSONResponse({"success": True})
    resp.delete_cookie(_AUTH_COOKIE)
    return resp


@app.post("/api/auth/change-password")
async def api_auth_change_password(payload: dict, request: Request):
    # Change the signed-in user's own password. Identity comes from the session
    # cookie (never from the request body), so a user can only change their own.
    import users as _users
    import session as _session
    s = _session.get_session(request.cookies.get(_AUTH_COOKIE))
    if s is None:
        raise HTTPException(401, "not signed in")
    username = s["username"]
    current = payload.get("current_password", "")
    new = payload.get("new_password", "")
    if not new:
        raise HTTPException(400, "new password required")
    # Re-verify the CURRENT password before allowing the change.
    chk = _users.verify_user(username, current)
    if not chk.get("success"):
        raise HTTPException(401, "current password is incorrect")
    r = _users.set_password(username, new)
    if not r.get("success"):
        raise HTTPException(400, r.get("error", "could not change password"))
    # Invalidate every existing session for this user (forces other devices to
    # re-login), then re-issue a fresh one so THIS tab stays signed in.
    _session.destroy_user_sessions(username)
    tok = _session.create_session(
        {"username": chk["username"], "is_owner": chk["is_owner"], "ns": chk.get("ns")},
        ttl=_AUTH_TTL)
    resp = JSONResponse({"success": True})
    # Session cookie (NO max_age) so it's discarded when the browser/app closes
    # -> reopening requires a fresh sign-in. Server session still expires via TTL.
    resp.set_cookie(_AUTH_COOKIE, tok, httponly=True, samesite="lax")
    return resp


# --- Owner-only user management (multi-user) ----------------------------------
def _require_owner(request: Request):
    """Return the session if it's a signed-in OWNER, else 404-cloak the surface."""
    import session as _session
    s = _session.get_session(request.cookies.get(_AUTH_COOKIE))
    if not s or not s.get("is_owner"):
        raise HTTPException(404)
    return s


@app.get("/api/auth/users")
async def api_auth_users_list(request: Request):
    _require_owner(request)
    import users as _users
    return {"users": _users.list_users()}


@app.post("/api/auth/users")
async def api_auth_users_create(request: Request, payload: dict):
    _require_owner(request)
    import users as _users
    r = _users.create_user((payload.get("username") or ""), (payload.get("password") or ""))
    if not r.get("success"):
        raise HTTPException(400, r.get("error", "could not create user"))
    return {"ok": True, "users": _users.list_users()}


@app.post("/api/auth/users/delete")
async def api_auth_users_delete(request: Request, payload: dict):
    s = _require_owner(request)
    import users as _users
    import session as _session
    target = (payload.get("username") or "").strip()
    if target.lower() == (s.get("username") or "").strip().lower():
        raise HTTPException(400, "you can't delete the account you're signed in as")
    r = _users.delete_user(target)
    if not r.get("success"):
        raise HTTPException(400, r.get("error", "could not delete user"))
    _session.destroy_user_sessions(target)   # kick any active sessions for that account
    wiped = False
    if payload.get("wipe_data") and r.get("ns"):
        # Erase the user's isolated data dir. Guarded: only ever removes a path
        # under sage_data/users/, never anything else.
        try:
            import shutil
            d = sage_engine.user_data_dir(r["ns"])
            users_root = str((DATA_DIR / "users").resolve())
            if d and d.exists() and str(d.resolve()).startswith(users_root):
                shutil.rmtree(d, ignore_errors=True)
                wiped = True
        except Exception as _wipe_err:
            print(f"[USERS] data wipe failed for {target}: {_wipe_err}")
    return {"ok": True, "wiped": wiped, "users": _users.list_users()}


@app.middleware("http")
async def _session_gate(request: Request, call_next):
    # Phase 2 A2b: require a valid login session for the app surface, but ONLY
    # when multi-user is enabled -> zero change to a single-user run (default).
    if not config.get("multiuser_enabled", False):
        return await call_next(request)
    _p = request.url.path
    # Always-open: the static UI shell (the login screen lives in the frontend and
    # polls /api/auth/status), the auth endpoints, health, and the node surface
    # (token-authenticated separately, not via login sessions).
    if (_p == "/" or _p == "/favicon.ico"
            or _p == "/manifest.webmanifest" or _p == "/sw.js"
            or _p.startswith("/static")
            or _p.startswith("/api/auth/")
            or _p == "/api/health"
            or _p.startswith("/api/node/")
            or _p == "/api/skills/catalog"
            or _p.startswith("/api/skills/object/")
            or _p.startswith("/api/relay/")
            or _p.startswith("/mcp/")):
        return await call_next(request)
    try:
        import session as _session
        _s = _session.get_session(request.cookies.get(_AUTH_COOKIE))
    except Exception:
        _s = None
    if _s is None:
        # Remote callers get the uniform 404 cloak (no hint that a login-gated
        # surface exists here); the local UI still gets a real 401 so its login
        # screen can prompt.
        if not _is_local_client(request):
            return _cloak_not_found()
        return JSONResponse(
            {"error": "authentication required", "needs_login": True},
            status_code=401)
    request.state.user = _s
    return await call_next(request)


# --- Exercise Tracker Routes ---
@app.get("/api/exercise")
async def api_exercise_log():
    return {"log": sage_engine.get_exercise_log(), "stats": sage_engine.get_exercise_stats()}


@app.post("/api/exercise")
async def api_exercise_add(payload: dict):
    return sage_engine.log_exercise(
        payload.get("date", ""), payload.get("activity", ""),
        payload.get("duration", 0), payload.get("intensity", 5),
        payload.get("note", ""))


@app.delete("/api/exercise")
async def api_exercise_clear():
    return sage_engine.clear_exercise_log()


# --- Plugin Feature Toggle Wiring ---
@app.post("/api/plugins/{plugin_id}/toggle")
async def api_toggle_plugin_v2(plugin_id: str):
    result = plugin_manager.toggle_plugin(plugin_id)
    # Wire toggle state to sage_engine feature flags
    feature_map = {
        "weather-tool": "weather",
        "semantic-search": "semantic_search",
        "exercise-tracker": "exercise_tracker",
        "complexity-detector": "complexity_detector",
        "task-prioritiser": "task_prioritiser",
        "browser-plugin": "browser",
    }
    if plugin_id in feature_map:
        sage_engine.set_feature(
            feature_map[plugin_id], result.get("enabled", True))
    return result


# --- WebSocket Chat (with full Sage agentic loop) ---------------------------
# === Symposium mode: a moderated 3-model debate ==========================
# The debate is driven by STANCE PROMPTS (each debater is assigned an opposing
# side); temperature is only flavor. Streams as one markdown document (speaker
# headers + each model's tokens) so the frontend renders it with zero new code.
_SYMP_OPEN_P = (
    "You are the PROPONENT in a structured, good-faith debate. Give your OPENING "
    "STATEMENT: the strongest, most rigorous case IN FAVOR of the stated "
    "proposition. Use clear reasoning and concrete evidence. Stay strictly on "
    "the proposition; never drift. Be substantive and concise -- no preamble.")
_SYMP_OPEN_O = (
    "You are the OPPONENT in a structured, good-faith debate. Give your OPENING "
    "STATEMENT: the strongest, most rigorous case AGAINST the stated "
    "proposition, directly rebutting the Proponent. Stay strictly on the "
    "proposition; never drift. Be substantive and concise -- no preamble.")
_SYMP_REBUT_P = (
    "You are the PROPONENT. REBUTTAL: directly counter the Opponent's last "
    "statement and reinforce your case FOR the proposition. Stay strictly on "
    "the proposition. Concede anything genuinely correct. Be concise.")
_SYMP_REBUT_O = (
    "You are the OPPONENT. REBUTTAL: directly counter the Proponent's last "
    "statement against the proposition. Stay strictly on the proposition. "
    "Concede anything genuinely correct. Be concise.")
_SYMP_CROSS_P = (
    "You are the PROPONENT. CROSS-EXAMINATION: pose two or three pointed "
    "questions that expose the weakest parts of the Opponent's position, and "
    "briefly answer the toughest question they would put to you. Stay strictly "
    "on the proposition. Be concise.")
_SYMP_CROSS_O = (
    "You are the OPPONENT. CROSS-EXAMINATION: answer the Proponent's challenges, "
    "then pose two or three pointed questions that expose the weakest parts of "
    "their position. Stay strictly on the proposition. Be concise.")
_SYMP_CLOSE_P = (
    "You are the PROPONENT. CLOSING ARGUMENT: summarize why the proposition "
    "holds, addressing the strongest objections raised. Introduce no new claims. "
    "Stay strictly on the proposition. Be concise.")
_SYMP_CLOSE_O = (
    "You are the OPPONENT. CLOSING ARGUMENT: summarize why the proposition "
    "fails, addressing the Proponent's strongest points. Introduce no new "
    "claims. Stay strictly on the proposition. Be concise.")
_SYMP_MODERATOR = (
    "You are the MODERATOR (Sage) of the debate that follows. Fairly summarize "
    "the strongest point from each side, identify the CRUX (the core "
    "disagreement), note what each side gets right, and give a balanced "
    "synthesis. Only name a stronger side if the case is decisive; otherwise "
    "lay out what evidence would settle it. Be concise and even-handed.")


def _symposium_roles():
    """Pick (proponent, opponent, moderator) from the configured slots. Sage
    (the primary/default) moderates; the secondary + tertiary debate. Works with
    sparse slots (a single model can play all three roles via the prompts)."""
    prim = config.get("default_model")
    sec = config.get("secondary_model")
    ter = config.get("tertiary_model")
    slots = [s for s in (prim, sec, ter) if s]
    if not slots:
        return None
    moderator = prim or slots[0]
    debaters = [s for s in (sec, ter) if s]
    _i = 0
    while len(debaters) < 2:
        debaters.append(slots[_i % len(slots)])
        _i += 1
    return debaters[0], debaters[1], moderator


async def _run_symposium(topic, options, watchdog, rounds=1):
    """Async-generator: yield the whole debate as one streamed markdown doc."""
    roles = _symposium_roles()
    if not roles:
        yield "[Symposium needs at least one model configured in a slot.]"
        return
    a, b, mod = roles
    base = options or {}
    try:
        _t = float(base.get("temperature", 0.7))
    except (TypeError, ValueError):
        _t = 0.7
    mod_opts = dict(base)
    mod_opts["temperature"] = min(_t, 0.5)  # moderator stays measured
    transcript = []

    async def _speak(model_id, system, context, header, opts):
        u = ("PROPOSITION (stay strictly on this single proposition; never "
             "change the topic): " + topic)
        if context:
            u += "  ||  " + context
        yield "\n\n### " + header + "\n\n"
        buf = ""
        async for tok in model_manager.generate(
                [{"role": "system", "content": system},
                 {"role": "user", "content": u}], model_id, opts):
            if model_manager._abort:
                break
            watchdog.record_token()
            buf += tok
            yield tok
        transcript.append((header, buf))

    yield "# Symposium\n\n**Proposition:** " + topic + "\n\n"
    yield ("> _Symposium is a structured reasoning exercise, not a source of "
           "verified facts. The debaters argue from memory and may cite events, "
           "dates, or figures without sources -- treat every empirical claim as "
           "unverified and check it independently._\n")

    # A COMPLETE debate in ONE turn: Opening -> Rebuttal -> Cross-Examination ->
    # Closing -> Verdict. No "continue" is ever needed, so Auto-Re-Prompt cannot
    # hijack a debate into a new proposition.
    phases = (
        ("Opening Statement", _SYMP_OPEN_P, _SYMP_OPEN_O),
        ("Rebuttal", _SYMP_REBUT_P, _SYMP_REBUT_O),
        ("Cross-Examination", _SYMP_CROSS_P, _SYMP_CROSS_O),
        ("Closing Argument", _SYMP_CLOSE_P, _SYMP_CLOSE_O),
    )
    for _label, _sys_p, _sys_o in phases:
        if model_manager._abort:
            return
        _prior = transcript[-1][1] if transcript else None
        _ctx_p = ("THE OPPONENT'S LAST STATEMENT:\n" + _prior) if _prior else None
        async for _x in _speak(a, _sys_p, _ctx_p,
                               _label + " - Proponent (" + a + ")", base):
            yield _x
        if model_manager._abort:
            return
        _ctx_o = "THE PROPONENT'S LAST STATEMENT:\n" + transcript[-1][1]
        async for _x in _speak(b, _sys_o, _ctx_o,
                               _label + " - Opponent (" + b + ")", base):
            yield _x
    if model_manager._abort:
        return
    _tx = "\n\n".join(_h + ":\n" + _c for _h, _c in transcript)
    async for _x in _speak(mod, _SYMP_MODERATOR,
                           "FULL DEBATE TRANSCRIPT:\n" + _tx,
                           "Moderator Verdict (" + mod + ")", mod_opts):
        yield _x


async def _handle_symposium(websocket, data):
    """WS handler for action=="symposium": stream a moderated debate. Fully
    isolated from the agentic loop. Reuses the standard token/done events so the
    frontend renders it as a normal streamed markdown message."""
    messages = data.get("messages") or []
    topic = str(data.get("topic") or "").strip()
    if not topic and messages:
        _last = messages[-1]
        if isinstance(_last, dict):
            topic = str(_last.get("content") or "").strip()
    options = data.get("options") or {}
    try:
        rounds = int(data.get("rounds", config.get("symposium_rounds", 1)))
    except (TypeError, ValueError):
        rounds = 1
    rounds = max(1, min(3, rounds))

    if not topic:
        await websocket.send_json({
            "type": "done",
            "content": "[Symposium: please type a proposition to debate.]",
            "done": True, "model": "Symposium", "ts": TimeManager.iso_z()})
        return

    class _NullWatchdog:
        def record_token(self):
            pass

    model_manager._abort = False
    full = ""
    try:
        async for _chunk in _run_symposium(topic, options, _NullWatchdog(),
                                           rounds=rounds):
            if model_manager._abort:
                break
            full += _chunk
            await websocket.send_json({
                "type": "token", "content": _chunk, "done": False})
    except Exception as _e:
        _emsg = "\n\n[Symposium error: " + str(_e) + "]"
        full += _emsg
        try:
            await websocket.send_json({
                "type": "token", "content": _emsg, "done": False})
        except Exception:
            pass
    await websocket.send_json({
        "type": "done", "content": full, "done": True,
        "model": "Symposium", "ts": TimeManager.iso_z()})
                
        
# === Build Battle mode: two models compete to produce the best code ==========
_BUILD_INIT_A = (
    "You are BUILDER A in a competitive coding challenge. "
    "Read the specification carefully and produce your INITIAL BUILD: "
    "a complete, working implementation. Include brief inline comments "
    "explaining key decisions. No preamble — output code first, then a short "
    "design rationale.")

_BUILD_INIT_B = (
    "You are BUILDER B in a competitive coding challenge. "
    "Read the specification carefully and produce your INITIAL BUILD: "
    "a complete, working implementation. Aim to differ meaningfully from "
    "obvious approaches — find a clever angle. Include brief inline comments. "
    "No preamble — output code first, then a short design rationale.")

_BUILD_CRITIQUE_A = (
    "You are BUILDER A. CRITIQUE & REFINE: "
    "First, identify 2-3 concrete bugs, edge cases, or weaknesses in "
    "Builder B's code. Then improve YOUR OWN build in response — fix anything "
    "their critique would expose in yours. Output your revised code, then "
    "your critique of their build.")

_BUILD_CRITIQUE_B = (
    "You are BUILDER B. CRITIQUE & REFINE: "
    "First, identify 2-3 concrete bugs, edge cases, or weaknesses in "
    "Builder A's code. Then improve YOUR OWN build in response — fix anything "
    "their critique would expose in yours. Output your revised code, then "
    "your critique of their build.")

_BUILD_FINAL_A = (
    "You are BUILDER A. FINAL SUBMISSION: produce your best, cleanest, "
    "most complete version of the implementation. Incorporate all lessons "
    "from the critique round. This is your definitive entry. "
    "No preamble — code first, then a one-paragraph summary of your approach.")

_BUILD_FINAL_B = (
    "You are BUILDER B. FINAL SUBMISSION: produce your best, cleanest, "
    "most complete version of the implementation. Incorporate all lessons "
    "from the critique round. This is your definitive entry. "
    "No preamble — code first, then a one-paragraph summary of your approach.")

_BUILD_JUDGE = (
    "You are the JUDGE (Sage) evaluating two competing code submissions "
    "for the same specification. Assess each on: "
    "(1) Correctness — does it meet the spec and handle edge cases? "
    "(2) Clarity — is it readable and well-commented? "
    "(3) Elegance — is the approach clean and non-redundant? "
    "(4) Robustness — does it handle errors and unexpected input? "
    "Cite specific lines or patterns from each submission. "
    "Declare a winner with clear reasoning, or a draw if genuinely equal. "
    "Be fair, precise, and concise.")
    
    
def _build_battle_roles():
    """Pick (builder_a, builder_b, judge) from configured slots.
    Sage (primary) judges; secondary and tertiary compete."""
    prim = config.get("default_model")
    sec  = config.get("secondary_model")
    ter  = config.get("tertiary_model")
    slots = [s for s in (prim, sec, ter) if s]
    if not slots:
        return None
    judge    = prim or slots[0]
    builders = [s for s in (sec, ter) if s]
    _i = 0
    while len(builders) < 2:
        builders.append(slots[_i % len(slots)])
        _i += 1
    return builders[0], builders[1], judge


# --- Build Battle execute-gate helpers (added) -----------------------------
def _bb_extract_code(text):
    """Pull the implementation out of a builder submission: prefer the largest
    fenced ``` block; fall back to the whole text (which will fail to import --
    a legitimate gate failure)."""
    import re as _re
    if not text:
        return ""
    blocks = _re.findall(r"```(?:[a-zA-Z0-9_+-]*)\n(.*?)```", text, _re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip()
    return text.strip()


def _bb_final_buf(transcript, who):
    """Latest 'Final Submission' buffer for a given builder label."""
    for _h, _c in reversed(transcript):
        if _h.startswith("Final Submission") and who in _h:
            return _c
    return ""


def _bb_resolve_gate_path(p):
    """Resolve a gate-test path: as-is, or relative to the backend dir."""
    import os as _os
    if not p:
        return None
    cands = [p]
    _f = globals().get("__file__")
    if _f:
        cands.append(_os.path.join(_os.path.dirname(_os.path.abspath(_f)), p))
    for cand in cands:
        if _os.path.isfile(cand):
            return cand
    return None


def _bb_gate_summary(raw):
    """One-line human summary from the gate subprocess output."""
    if "[TIMEOUT]" in raw:
        return "timed out"
    for line in raw.splitlines():
        if "passed" in line and "failed" in line:
            return line.strip()
        if "ALL PASS" in line:
            return "all checks passed"
    return "passed" if "GATE_RC=0" in raw else "failed (did not run clean / tests failed)"


async def _bb_run_gate(candidate_code, test_content, module_name, test_filename, timeout=60):
    """Run a gate test against candidate code in OracleAI's sandbox. Writes
    <module>.py + the test into a temp dir, runs the test as a subprocess, and
    returns (passed, raw_output). Off-thread so the event loop is not blocked."""
    import base64 as _b64
    import asyncio as _aio
    cb = _b64.b64encode((candidate_code or "").encode("utf-8")).decode("ascii")
    tb = _b64.b64encode((test_content or "").encode("utf-8")).decode("ascii")
    driver = (
        "import base64,os,sys,subprocess,tempfile\n"
        "cand=base64.b64decode('" + cb + "').decode('utf-8')\n"
        "test=base64.b64decode('" + tb + "').decode('utf-8')\n"
        "d=tempfile.mkdtemp(prefix='bbgate_')\n"
        "open(os.path.join(d," + repr(module_name + '.py') + "),'w',encoding='utf-8').write(cand)\n"
        "open(os.path.join(d," + repr(test_filename) + "),'w',encoding='utf-8').write(test)\n"
        "r=subprocess.run([sys.executable,'-X','utf8',os.path.join(d," + repr(test_filename) + ")],cwd=d,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=" + str(int(timeout)) + ")\n"
        "print('GATE_RC='+str(r.returncode))\n"
        "print((r.stdout or '')[-3000:])\n"
        "if r.stderr: print('[GATE_STDERR] '+r.stderr[-1500:])\n"
    )
    raw = await _aio.to_thread(sage_engine.execute_python, driver, int(timeout) + 30)
    return ("GATE_RC=0" in raw), raw


async def _run_build_battle(spec, options, watchdog, rounds=1, gate_test=None):
    """Async-generator: streams the full Build Battle as one markdown doc.
    `rounds` controls how many Critique & Refine passes run (1-3)."""
    roles = _build_battle_roles()
    if not roles:
        yield "[Build Battle needs at least one model configured in a slot.]"
        return

    a, b, judge = roles
    try:
        rounds = max(1, min(3, int(rounds)))
    except (TypeError, ValueError):
        rounds = 1
    base = options or {}
    try:
        _t = float(base.get("temperature", 0.7))
    except (TypeError, ValueError):
        _t = 0.7

    judge_opts = dict(base)
    judge_opts["temperature"] = min(_t, 0.4)  # judge stays cool-headed
    transcript = []
    # When an execute-gate is active, require finalists to emit ONE fenced code
    # block so _bb_extract_code reliably captures the whole implementation.
    _gate_note = (
        " IMPORTANT: an automated test gate will EXECUTE your code. Put your "
        "COMPLETE final implementation in a SINGLE ```python fenced code block "
        "(one block, nothing omitted), then put your one-paragraph summary AFTER "
        "the closing fence. Any code outside that single block will NOT be tested."
    ) if gate_test else ""

    async def _build(model_id, system, context, header, opts):
        u = ("SPECIFICATION (implement exactly this; do not change the "
             "challenge): " + spec)
        if context:
            u += "\n\n---\n\n" + context
        yield "\n\n### " + header + "\n\n"
        buf = ""
        async for tok in model_manager.generate(
                [{"role": "system", "content": system},
                 {"role": "user",   "content": u}],
                model_id, opts):
            if model_manager._abort:
                break
            watchdog.record_token()
            buf += tok
            yield tok
        transcript.append((header, buf))

    yield "# Build Battle\n\n**Specification:** " + spec + "\n\n"
    yield ("> _Build Battle is a creative coding exercise. "
           "Generated code may contain bugs or incomplete logic — "
           "review and test all output before use._\n")

    # Phase 1 — Initial Builds (no cross-context yet)
    async for _x in _build(a, _BUILD_INIT_A, None,
                            "Initial Build — Builder A (" + a + ")", base):
        yield _x
    if model_manager._abort:
        return

    async for _x in _build(b, _BUILD_INIT_B, None,
                            "Initial Build — Builder B (" + b + ")", base):
        yield _x
    if model_manager._abort:
        return

    # Phase 2 — Critique & Refine (repeatable). `rounds` controls how many
    # times each builder critiques the other and revises. Context is rebuilt
    # from the full transcript every pass, so each round sees all prior builds
    # + critiques and refinement compounds. rounds==1 reproduces the original
    # single-round output exactly (no round tag in the header).
    for _round in range(1, rounds + 1):
        _rtag = "" if rounds == 1 else " (Round " + str(_round) + ")"

        _ctx_a = (
            "FULL CONTEXT SO FAR:\n\n" +
            "\n\n---\n\n".join(_h + ":\n" + _c for _h, _c in transcript)
        )
        async for _x in _build(a, _BUILD_CRITIQUE_A, _ctx_a,
                                "Critique & Refine" + _rtag + " — Builder A (" + a + ")", base):
            yield _x
        if model_manager._abort:
            return

        _ctx_b = (
            "FULL CONTEXT SO FAR:\n\n" +
            "\n\n---\n\n".join(_h + ":\n" + _c for _h, _c in transcript)
        )
        async for _x in _build(b, _BUILD_CRITIQUE_B, _ctx_b,
                                "Critique & Refine" + _rtag + " — Builder B (" + b + ")", base):
            yield _x
        if model_manager._abort:
            return

    # Phase 3 — Final Submissions (full context: both builds + both critiques)
    _ctx_final_a = (
        "FULL CONTEXT SO FAR:\n\n" +
        "\n\n---\n\n".join(_h + ":\n" + _c for _h, _c in transcript)
    )
    async for _x in _build(a, _BUILD_FINAL_A + _gate_note, _ctx_final_a,
                            "Final Submission — Builder A (" + a + ")", base):
        yield _x
    if model_manager._abort:
        return

    _ctx_final_b = (
        "FULL CONTEXT SO FAR:\n\n" +
        "\n\n---\n\n".join(_h + ":\n" + _c for _h, _c in transcript)
    )
    async for _x in _build(b, _BUILD_FINAL_B + _gate_note, _ctx_final_b,
                            "Final Submission — Builder B (" + b + ")", base):
        yield _x
    if model_manager._abort:
        return

    # Phase 4 — Judge's Ruling
    # --- Execute-gate (Phase 3.5): when a gate_test is configured, actually RUN
    # each finalist's code against it in the sandbox so the Judge sees real
    # pass/fail. No gate_test => this block is skipped (behaves exactly as before).
    _gate_for_judge = ""
    if gate_test:
        _gpath = _bb_resolve_gate_path(gate_test)
        if not _gpath:
            yield "\n\n### Gate Test\n\n> _Gate test not found: " + str(gate_test) + " -- skipping the execution gate._\n"
        else:
            try:
                _test_src = open(_gpath, encoding="utf-8").read()
            except Exception as _ge:
                _test_src = ""
                yield "\n\n### Gate Test\n\n> _Could not read gate test: " + str(_ge) + " -- skipping._\n"
            if _test_src:
                import os as _os
                _tbase = _os.path.basename(_gpath)
                _module = _tbase[:-3] if _tbase.endswith(".py") else _tbase
                if _module.startswith("test_"):
                    _module = _module[5:]
                yield "\n\n### Gate Test Results\n\n"
                yield "_Running " + _tbase + " against each finalist in the sandbox (module " + _module + ")._\n\n"
                _gate_lines = []
                for _who, _label in ((a, "Builder A"), (b, "Builder B")):
                    _code = _bb_extract_code(_bb_final_buf(transcript, _label))
                    if not _code.strip():
                        _passed, _summary = False, "no code block could be extracted"
                    else:
                        _passed, _raw = await _bb_run_gate(_code, _test_src, _module, _tbase)
                        _summary = _bb_gate_summary(_raw)
                    _verdict = "PASS" if _passed else "FAIL"
                    yield "- **" + _label + " (" + _who + ")** -- **" + _verdict + "**: " + _summary + "\n"
                    _gate_lines.append(_label + " (" + _who + "): " + _verdict + " -- " + _summary)
                _gate_for_judge = ("\n\nGATE TEST RESULTS (real automated execution of "
                                   + _tbase + "):\n" + "\n".join(_gate_lines))

    # Phase 4 -- Judge's Ruling (gate results, when present, are decisive)
    _judge_system = _BUILD_JUDGE
    if _gate_for_judge:
        _judge_system = _BUILD_JUDGE + (
            " CRITICAL: the transcript ends with REAL automated gate-test results "
            "from executing each submission. A submission that FAILS the gate does "
            "not meet the specification and cannot win on style or readability. If "
            "exactly one submission passes, it wins unless it has a separate, "
            "clearly disqualifying flaw. Treat the gate results as decisive and "
            "weigh them above prose impressions.")
    _tx = "\n\n".join(_h + ":\n" + _c for _h, _c in transcript)
    async for _x in _build(judge, _judge_system,
                            "FULL BUILD BATTLE TRANSCRIPT:\n" + _tx + _gate_for_judge,
                            "Judge's Ruling (" + judge + ")", judge_opts):
        yield _x



async def _handle_build_battle(websocket, data):
    messages = data.get("messages") or []
    spec = str(data.get("topic") or "").strip()  # reuse 'topic' key from frontend
    if not spec and messages:
        _last = messages[-1]
        if isinstance(_last, dict):
            spec = str(_last.get("content") or "").strip()

    # ✂️ Strip optional user-typed prefixes before anything else sees the spec
    for _prefix in ("specification:", "spec:", "build:"):
        if spec.lower().startswith(_prefix):
            spec = spec[len(_prefix):].strip()
            break

    # Optional per-battle execute-gate: a line "GATE: <path-to-test>" anywhere in
    # the spec names a test the finalists must pass. Strip it so the builders
    # never see it as part of the coding challenge.
    _gate_from_spec = None
    _spec_lines = []
    for _ln in spec.splitlines():
        _sln = _ln.strip()
        if _sln.lower().startswith("gate:") and _gate_from_spec is None:
            _gate_from_spec = _sln.split(":", 1)[1].strip()
        else:
            _spec_lines.append(_ln)
    if _gate_from_spec:
        spec = "\n".join(_spec_lines).strip()

    options = data.get("options") or {}
    try:
        rounds = int(data.get("rounds", config.get("build_battle_rounds", 1)))
    except (TypeError, ValueError):
        rounds = 1
    rounds = max(1, min(3, rounds))

    if not spec:
        await websocket.send_json({
            "type": "done",
            "content": "[Build Battle: please describe the coding challenge.]",
            "done": True, "model": "Build Battle", "ts": TimeManager.iso_z()})
        return

    class _NullWatchdog:
        def record_token(self): pass

    model_manager._abort = False
    full = ""
    try:
        _gate = (data.get("gate_test") or _gate_from_spec
                 or config.get("build_battle_gate_test"))
        async for _chunk in _run_build_battle(spec, options, _NullWatchdog(), rounds=rounds, gate_test=_gate):
            if model_manager._abort:
                break
            full += _chunk
            await websocket.send_json({
                "type": "token", "content": _chunk, "done": False})
    except Exception as _e:
        _emsg = "\n\n[Build Battle error: " + str(_e) + "]"
        full += _emsg
        try:
            await websocket.send_json({
                "type": "token", "content": _emsg, "done": False})
        except Exception:
            pass

    await websocket.send_json({
        "type": "done", "content": full, "done": True,
        "model": "Build Battle", "ts": TimeManager.iso_z()})        


# --- Prompt-cache live diagnostic helpers ---------------------------------
# Per-session record of the prior turn's STABLE segment hashes, so analyze_prompt
# can flag when a "stable" prefix actually changed turn-to-turn (a KV cache miss).
_PROMPT_CACHE_HASHES = {}


def _prompt_cache_segments(messages):
    """Map a composed chat 'messages' list to prompt_cache_manager segments.
    Only the FRONT system message (messages[0]) counts as the stable, cacheable
    'system_prompt'; any later system messages (tail-injected procedural memory
    or warm-handoff context) are treated as dynamic. Returns (segments, user)."""
    front_system = ""
    others = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if not isinstance(content, str):
            content = "" if content is None else str(content)
        if role == "system" and not front_system and not others:
            front_system = content
        else:
            others.append((role, content))
    segs = {}
    if front_system:
        segs["system_prompt"] = front_system
    user_msg = ""
    if others:
        user_msg = others[-1][1]
        body = others[:-1]
        if body:
            segs["history"] = "\n".join(f"{r}: {c}" for r, c in body)
        segs["user_message"] = user_msg
    return segs, user_msg


def _log_prompt_cache(entry):
    """Append one Fernet-encrypted JSON line to sage_data/logs/prompt_cache.log
    using the same at-rest protocol as the rest of sage_data. Silent-fail so a
    diagnostic can never break a chat turn."""
    try:
        import json as _json
        import atrest as _atr
        from config import LOG_DIR
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        line = _atr.encrypt_bytes(_json.dumps(entry, default=str).encode("utf-8"))
        with open(LOG_DIR / "prompt_cache.log", "ab") as _f:
            _f.write(line + b"\n")
    except Exception as _e:
        print(f"[PROMPT-CACHE] log write failed: {_e}")


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    if config.get("multiuser_enabled", False):
        try:
            import session as _session
            if _session.get_session(websocket.cookies.get(_AUTH_COOKIE)) is None:
                await websocket.close(code=1008)  # policy violation
                return
        except Exception:
            await websocket.close(code=1008)
            return
    await websocket.accept()
    # Per-user namespace for this connection (non-owner -> isolated store; owner /
    # single-user -> shared). Used for the agent's memory recall + conversation save.
    _ws_ns = None
    try:
        if config.get("multiuser_enabled", False):
            import session as _session
            _wss = _session.get_session(websocket.cookies.get(_AUTH_COOKIE))
            if _wss and not _wss.get("is_owner"):
                _ws_ns = _wss.get("ns")
    except Exception:
        _ws_ns = None
    # Bind this connection's namespace for the browser plugin so each user's
    # Sage drives her own persistent profile (per-user isolation).
    try:
        sage_engine.set_browser_ns(_ws_ns)
    except Exception:
        pass
    # v2.11.13: this connection's personal settings overlay (empty for the
    # owner / single-user). Applied to generation options and Sage-mode
    # flags below so each user's saved preferences actually drive THEIR
    # inference, not just their settings screen.
    _ws_overlay = _load_user_overlay(_ws_ns)
    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action", "chat")

            if action == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            if action == "abort":
                model_manager.abort()
                await websocket.send_json({"type": "aborted"})
                continue
            if action == "symposium":
                await _handle_symposium(websocket, data)
                continue
            if action == "build_battle":
                await _handle_build_battle(websocket, data)
                continue                
                

            messages = data.get("messages", [])
            model_id = data.get("model_id")
            options = data.get("options", {})

            # v2.11.13 urgency: the composer's ⚡ flag. Local user -> local
            # priority lane (0 urgent / 1 normal). The _priority key is
            # server-assigned here; anything a client put there is replaced.
            options["_priority"] = 0 if options.pop("urgent", False) else 1

            # v2.11.13 per-user settings: fill generation options from this
            # user's overlay where the request didn't set them explicitly.
            # (model_manager falls back to the GLOBAL config for missing
            # keys, which would leak the owner's prefs onto this user.)
            if _ws_overlay:
                for _puk in ("temperature", "max_tokens"):
                    if options.get(_puk) is None and _puk in _ws_overlay:
                        options[_puk] = _ws_overlay[_puk]
                if not model_id and _ws_overlay.get("default_model"):
                    model_id = _ws_overlay["default_model"]

            # ── Text→image deterministic intercept ─────────────────────────────
            # Local models don't reliably emit [GENERATE_IMAGE:]; for an obvious
            # image request, dispatch generation directly (same path + image_generated
            # message the agentic tag uses) so it never falls through to a chatty or
            # code response. Skips vision turns (image attached) and code/how-to asks.
            try:
                _last = messages[-1] if isinstance(messages, list) and messages else None
                if (_last and _last.get("role") == "user"
                        and not _last.get("images")
                        and not _last.get("imagePreviews")):
                    _img_prompt = sage_engine.detect_image_request(_last.get("content") or "")
                    if _img_prompt:
                        await websocket.send_json({
                            "type": "tool_call", "tool": "generate_image",
                            "input": _img_prompt[:200],
                            "message": f"Generating image: {_img_prompt[:80]}",
                        })
                        try:
                            _img = await _generate_image_routed(_img_prompt, downloads_dir=str(_downloads_dir_for_ns(_ws_ns)))
                        except Exception as _ge:
                            _img = {"success": False, "error": str(_ge)}
                        if _img.get("success"):
                            _ack = (f"Here's your image — saved to downloads as "
                                    f"{_img.get('filename')}.")
                            await websocket.send_json({
                                "type": "token", "content": _ack, "done": False})
                            await websocket.send_json({
                                "type": "image_generated",
                                "filename": _img.get("filename"),
                                "path": _img.get("path"),
                                "mimetype": _img.get("mimetype", "image/png"),
                                "data": _img.get("data"),
                                "prompt": _img_prompt,
                                "seed": _img.get("seed"),
                            })
                            await websocket.send_json({
                                "type": "done", "content": _ack, "done": True})
                        else:
                            _err = (f"I couldn't generate that image: "
                                    f"{_img.get('error', 'unknown error')} "
                                    f"(tip: use the 🎨 button to pick or download a model).")
                            await websocket.send_json({
                                "type": "token", "content": _err, "done": False})
                            await websocket.send_json({
                                "type": "done", "content": _err, "done": True})
                        continue
            except Exception as _ie:
                print(f"[img-intercept] skipped: {_ie}", flush=True)
            # ── end text→image intercept ───────────────────────────────────────

            # Vision multi-turn memory (2026-06-09): keep `images` only on
            # recent, capped messages so a user can ask follow-ups about an image
            # without re-attaching, while bounding context. Within the last
            # VISION_IMAGE_TURNS messages, keep images on at most VISION_MAX_IMAGES
            # most-recent image-bearing messages; strip everywhere else. The
            # Fernet memory log still only sees text (the user-turn log below
            # records last["content"], never base64). Authoritative bound that
            # mirrors the frontend buildPayload().
            try:
                import os as _os  # main.py does not import os at module level
                if isinstance(messages, list) and messages:
                    _vwin = int(_os.environ.get("VISION_IMAGE_TURNS", 8))
                    _vcap = int(_os.environ.get("VISION_MAX_IMAGES", 2))
                    _n = len(messages)
                    _kept = 0
                    for _i in range(_n - 1, -1, -1):
                        _m = messages[_i]
                        if not isinstance(_m, dict):
                            continue
                        _within = (_n - _i) <= _vwin
                        if _m.get("images") and _within and _kept < _vcap:
                            _kept += 1
                        else:
                            _m.pop("images", None)
            except Exception:
                pass

            # v2.1.4 stop-button fix: reset abort flag exactly ONCE per turn,
            # here at the top. A fresh user message implies the user wants
            # this new turn to run; any stale abort from the previous turn
            # is now stale. All deeper loops (model_manager.generate, the
            # search intercept's follow-up generate) must NOT reset it —
            # otherwise they silently clobber a mid-turn Stop click.
            model_manager._abort = False

            # v2.1.8 #56 stall detection: instantiate the per-turn
            # watchdog and start its async task. Cleanup is in the
            # finally below — even on exception, we stop the watchdog
            # and cancel its task so we don't leak background coroutines
            # across turns.
            _stall_tok = float(config.get("stall_token_timeout_sec", 56000.0))
            _stall_tool = float(config.get("stall_tool_timeout_sec", 56000.0))
            watchdog = _StallWatchdog(_stall_tok, _stall_tool)

            async def _on_stall(reason):
                """Callback fired by the watchdog when a stall is detected.
                Sets the global abort so the generate/stream loops break
                naturally, and sends a structured WS message so the UI
                can render a banner. Send may fail if the socket already
                closed — that's fine, we still aborted."""
                model_manager._abort = True
                print(f"[STALL DETECTED] {reason}")
                try:
                    await websocket.send_json({
                        "type":   "stall_detected",
                        "reason": reason,
                    })
                except Exception:
                    pass

            watchdog_task = asyncio.create_task(watchdog.watch(_on_stall))
            # v2.1.8 #56: publish the watchdog to the ambient ContextVar
            # so _taskp_run_or_direct can call record_tool_call /
            # record_tool_result without having to be passed an arg.
            _watchdog_token = _current_watchdog.set(watchdog)
            _current_offload.set(None)

            # v2.1.4: log the user turn BEFORE system-prompt injection so the
            # chain captures what the user actually said (role='user'). The
            # assistant side is logged further down near full_response. Only
            # the LAST message from this payload is logged — prior turns are
            # already in the chain from their own round-trips.
            user_request_text = ""  # v2.1.5: captured for autolog procedure key
            try:
                if messages:
                    last = messages[-1]
                    if (isinstance(last, dict)
                            and last.get("role") == "user"
                            and last.get("content")):
                        user_request_text = str(last["content"])
                        memory_logger.log(
                            content=last["content"],
                            temperature=config.get("temperature", 0.5),
                            token_prob=None,  # user turns are not generated
                            metadata={"source": "ws_chat"},
                            role="user",
                        )
            except Exception as _e:
                # Logging failures must never break the chat flow
                print(f"[MEMORY LOGGER] user-turn log failed: {_e}")

            # v2.2 (2026-05-29): auto-route now routes AMONG the
            # user's configured slots (primary + secondary), not
            # across MODELS_DB. The v2.1.7 fix correctly stopped
            # the universe-wide override, but it gated routing on
            # an empty model_id -- which the UI never permits --
            # so auto-route never actually fired.
            #
            # Corrected semantics: enabling auto_route IS user
            # intent to delegate THE CHOICE AMONG MY SLOTS to the
            # router. The router chooses between the slots the
            # user picked; it never substitutes a model the user
            # did not configure.
            #
            # 2-slot routing today is the strict subset of the
            # future Symposium-mode 3-slot routing -- extending to
            # the 3rd slot will be additive only.
            primary   = model_id or config.get("default_model")
            secondary = config.get("secondary_model")
            tertiary  = config.get("tertiary_model")  # 3rd slot (vision / Symposium)
            auto      = bool(config.get("auto_route"))

            # Capability-aware routing (2026-06-09): does THIS turn carry an
            # image? If so the router must pick a vision-capable slot; otherwise
            # it prefers a language-capable, pure-text slot. The 3rd slot joins
            # the candidate pool when configured (additive).
            _needs_vision = any(
                isinstance(m, dict) and m.get("images") for m in (messages or []))
            _slots = [s for s in (primary, secondary, tertiary) if s]

            if auto and len(_slots) >= 2:
                candidates = _slots
                chosen = sage_engine.route_query(
                    messages[-1].get("content", "") if messages else "",
                    candidates=candidates,
                    needs_vision=_needs_vision,
                )
                print(
                    f"[AUTO-ROUTE] candidates={candidates} "
                    f"needs_vision={_needs_vision} -> chose={chosen!r}"
                )
                model_id = chosen
            elif auto and primary:
                # Auto-route is on but only the primary slot is
                # configured -- no routing decision to make. Log
                # so the user understands why nothing varied.
                print(
                    f"[AUTO-ROUTE] only one slot configured "
                    f"(primary={primary!r}); add a secondary "
                    f"model in settings to enable routing"
                )
                model_id = primary
            else:
                # Auto-route off, or no primary at all -- use
                # whatever the UI sent (model_id may be empty,
                # downstream handles that).
                model_id = primary if primary else model_id
            # v2.11.13: Sage-mode flags honor the connection's per-user
            # overlay first, then the global config (owner/single-user).
            _eff = {**config, **_ws_overlay}
            sage_mode = _eff.get("sage_mode", True)
            agentic = _eff.get("agentic_mode", True) and sage_mode
            web_ok = _eff.get("web_search_enabled", True)
            code_ok = _eff.get("code_exec_enabled", True)

            # ── Build system prompt -----------------------------------
            # #68 Phase E Step 6: prompt now lives in a file
            # (prompts.system_prompt_file in config.json, default
            # sage_data/prompts/system.txt). Reading fresh each inference
            # means UI edits via POST /api/prompts/system take effect
            # immediately without a restart. Empty file (or missing) is
            # fine — the sage_mode branch below substitutes SAGE_SYSTEM_PROMPT
            # so Sage never runs with zero priming.
            _up = sage_engine.read_user_prompt(_ws_ns)   # per-user addendum (owner -> shared)
            sys_prompt = _up if _up is not None else OracleConfig.load(CONFIG_FILE)._read_prompt_file()
            if sage_mode:
                # v2.1.8 #55 model-aware prompt tier. Default is auto-
                # detection via the model name; user can override by
                # setting config["force_prompt_tier"] = "small" or
                # "full" (any other value falls back to auto).
                _forced = config.get("force_prompt_tier")
                if _forced in ("small", "full"):
                    _tier = _forced
                else:
                    _tier = _model_size_hint(model_id)
                if _tier == "small":
                    _base_prompt = sage_engine.SAGE_SYSTEM_PROMPT_SMALL
                else:
                    _base_prompt = sage_engine.SAGE_SYSTEM_PROMPT
                # Diagnostic so the chosen tier shows up in console logs.
                # Concise on purpose — fires on every turn.
                print(
                    f"[PROMPT TIER] model={model_id!r} -> tier={_tier} "
                    f"(forced={_forced!r})"
                )
                sys_prompt = _base_prompt + "\n\n" + sys_prompt

            # v2.1.4: inject recent procedural memory so the model carries
            # hard-won lessons across turns without having to re-[RECALL:]
            # every time. 5 most recent successful (what worked) + 5 most
            # recent unsuccessful (dead-ends to avoid). Silent-fail on any
            # error — procedural injection must never break chat. Injected
            # section is prefixed with an internal header the model knows
            # not to echo (see SAGE_SYSTEM_PROMPT rule about never
            # displaying procedural context).
            _proc_block = ""
            try:
                kb = procedural.get_all()
                succ = kb.get("successful", {})
                unsucc = kb.get("unsuccessful", {})

                def _recent(bucket, n):
                    items = [
                        (k, v) for k, v in bucket.items()
                        if isinstance(v, dict)
                    ]
                    items.sort(
                        key=lambda kv: kv[1].get("timestamp", ""),
                        reverse=True,
                    )
                    return items[:n]

                recent_succ = _recent(succ, 5)
                recent_fail = _recent(unsucc, 5)

                if recent_succ or recent_fail:
                    lines = [
                        "",
                        "=== PROCEDURAL MEMORY (internal — do not display) "
                        "===",
                        "The following are approaches learned from prior "
                        "turns in this install. Use them to guide tool "
                        "choices; prefer successful patterns, avoid dead-"
                        "ends. Do not mention this section to the user.",
                    ]
                    if recent_succ:
                        lines.append("")
                        lines.append("What worked (successful procedures):")
                        for k, v in recent_succ:
                            val = str(v.get("value", ""))[:200]
                            lines.append(f"  - {k}: {val}")
                    if recent_fail:
                        lines.append("")
                        lines.append("Dead-ends (unsuccessful — do not retry "
                                     "unless context has changed):")
                        for k, v in recent_fail:
                            val = str(v.get("value", ""))[:200]
                            lines.append(f"  - {k}: {val}")
                    lines.append("=== END PROCEDURAL MEMORY ===")
                    _proc_block = "\n".join(lines)
            except Exception as _pm_e:
                print(f"[PROCEDURAL] system-prompt injection failed: "
                      f"{_pm_e}")

            if not messages or messages[0].get("role") != "system":
                messages = [
                    {"role": "system", "content": sys_prompt}] + messages

            # CRAIID warm-context resume (#69): if a fatigue handoff just
            # rotated the daemon, pull the verified, FRAMED warm-context and
            # inject it ONCE so the fresh instance resumes coherently. One-shot
            # (the daemon clears it after handing over); already HMAC-verified
            # and framed as data-not-instructions, so a hostile payload cannot
            # issue directives here. Best-effort - never blocks a real turn.
            _warm = ""
            try:
                _warm = sage_engine.consume_warm_handoff()
                if _warm:
                    print(f"[CRAIID] warm-context available ({len(_warm)} chars) - injecting at tail.")
                    try:
                        await websocket.send_json(
                            {"type": "warm_context_restored",
                             "chars": len(_warm), "summary": _warm}
                        )
                    except Exception:
                        pass
            except Exception as _wc_e:
                print(f"[CRAIID] warm-context resume check failed: {_wc_e}")

            # Inject volatile context (procedural memory + warm handoff) at the
            # TAIL -- just before the final user message -- so it never sits in
            # the cacheable front prefix. A change here then only re-processes
            # the tail, never the system+history prefix (KV-cache friendly).
            _late_ctx = []
            if _proc_block:
                _late_ctx.append({"role": "system", "content": _proc_block})
            if _warm:
                _late_ctx.append({"role": "system", "content": _warm})
            if _late_ctx:
                _tail_at = len(messages) - 1 if (messages and messages[-1].get("role") == "user") else len(messages)
                messages[_tail_at:_tail_at] = _late_ctx

            messages = plugin_manager.preprocess(messages)
            # --- Prompt-cache live diagnostic (KV-cache efficiency) ----------
            # Read-only analysis of the final composed prompt; appends one
            # at-rest-encrypted line to sage_data/logs/prompt_cache.log per turn.
            # Toggle via config["prompt_cache_diagnostics"] (default ON).
            # Silent-fail: a diagnostic must never break a turn.
            if config.get("prompt_cache_diagnostics", True):
                try:
                    import prompt_cache_manager as _pcm
                    _pc_segs, _pc_user = _prompt_cache_segments(messages)
                    if _pc_segs:
                        _pc_prev = _PROMPT_CACHE_HASHES.get(_ws_ns)
                        _pc_res = _pcm.analyze_prompt(_pc_segs, previous_hashes=_pc_prev)
                        _pc_changed = any("Hash mismatch" in _cb for _cb in _pc_res.cache_busters)
                        _PROMPT_CACHE_HASHES[_ws_ns] = {
                            _s.name: _s.hash for _s in _pc_res.segments if _s.stable
                        }
                        print(
                            f"[PROMPT-CACHE] eff={_pc_res.cache_efficiency:.0%} "
                            f"stable={_pc_res.stable_tokens} dyn={_pc_res.dynamic_tokens} "
                            f"optimal={_pc_res.is_optimal} "
                            f"busters={len(_pc_res.cache_busters)} "
                            f"prefix_changed={_pc_changed}"
                        )
                        _log_prompt_cache({
                            "ts": TimeManager.iso_z(),
                            "ns": str(_ws_ns),
                            "model": model_id,
                            "cache_efficiency": round(_pc_res.cache_efficiency, 4),
                            "stable_tokens": _pc_res.stable_tokens,
                            "dynamic_tokens": _pc_res.dynamic_tokens,
                            "total_tokens": _pc_res.total_tokens,
                            "is_optimal": _pc_res.is_optimal,
                            "recommended_order": _pc_res.recommended_order,
                            "stable_prefix_changed": _pc_changed,
                            "cache_busters": _pc_res.cache_busters,
                            "warnings": _pc_res.warnings,
                        })
                except Exception as _pc_e:
                    print(f"[PROMPT-CACHE] diagnostic skipped: {_pc_e}")


            full_response = ""
            aborted = False

            try:
                # ======================================================
                #  AGENTIC MODE — multi-step tool execution loop
                # ======================================================
                if agentic and (web_ok or code_ok):
                    max_steps = 27
                    tool_results_acc = {}

                    # v2.1.4: auto-capture of repeated tool failures.
                    # Tracks (action_type, content_key) → attempt count
                    # within THIS turn. When the 3rd repeat lands and the
                    # result still looks like failure/no-progress, we emit
                    # an unsuccessful procedure so the next turn's recall
                    # will warn the model off the dead-end. Inspired by
                    # Todd's DuckDuckGo-ban stress test (100+ identical
                    # searches that got us IP-blocked).
                    action_attempts: Dict[str, int] = {}
                    auto_failed_keys: set = set()

                    # v2.1.5 Phase A: ordered sequence of tool calls executed
                    # this turn. Persisted as one chain-witnessed successful
                    # procedure on [TASK_DONE] so future turns can reuse the
                    # winning approach. Excludes meta tags (remember /
                    # remember_fail / recall) — those aren't part of the
                    # task's working sequence.
                    turn_actions: list = []
                    _img_gen_count = 0
                    _max_imgs_per_turn = int(config.get("max_images_per_turn", 5))

                    def _looks_like_failure(text: str) -> bool:
                        t = (text or "").lower()
                        if not t.strip():
                            return True
                        fail_sigs = (
                            "error:", "error ", "blocked", "rate limit",
                            "timeout", "no results", "not found",
                            "failed", "no relevant", "429", "403",
                            "connection", "unable to", "captcha",
                        )
                        return any(s in t for s in fail_sigs)

                    sage_engine._tavily_call_count = 0  # line 375 — reset Tavily counter per response

                    for step in range(1, max_steps + 1):
                        if model_manager._abort:
                            aborted = True
                            break

                        # v2.1.10 #44 AIQNudge check — runs BETWEEN reasoning
                        # steps, BEFORE the next agent_step is announced. Any
                        # verified nudge becomes a system-role priority
                        # directive appended to the running messages list so
                        # Sage sees it on her next generate() call. Tampered
                        # or unsigned files are quarantined inside
                        # aiq_nudge.read_pending() — nothing reaches Sage
                        # without a valid HMAC. Skipped entirely when
                        # aiq_nudge_enabled is False (default) or the module
                        # failed to initialise at boot. Wrapped in try/except
                        # so a misbehaving nudge can't crash a real run.
                        if (aiq_nudge is not None
                                and config.get("aiq_nudge_enabled", False)):
                            try:
                                _pending = aiq_nudge.read_pending(
                                    config.get(
                                        "aiq_nudge_watch_pattern",
                                        "nudge_*.txt",
                                    )
                                )
                                for _entry in _pending:
                                    _body = _entry.get("content", "").strip()
                                    if not _body:
                                        continue
                                    messages.append({
                                        "role": "system",
                                        "content": (
                                            "[VERIFIED USER NUDGE — mid-run "
                                            "directive from Todd, HMAC-"
                                            "checked] "
                                        ) + _body,
                                    })
                                    # Surface to UI so Todd sees confirmation
                                    # it was picked up and is in play.
                                    await websocket.send_json({
                                        "type":    "aiq_nudge_received",
                                        "preview": _body[:200],
                                        "step":    step,
                                    })
                                    print(
                                        f"[AIQ_NUDGE INJECTED] step={step} "
                                        f"chars={len(_body)}"
                                    )
                            except Exception as _nudge_err:
                                # Loud-but-not-fatal: log and continue.
                                print(
                                    f"[AIQ_NUDGE] check failed at step {step}: "
                                    f"{_nudge_err}"
                                )

                        await websocket.send_json({
                            "type": "agent_step", "step": step,
                            "message": f"Thinking (step {step}/{max_steps})…",
                        })

                        # ── Inject accumulated tool results into msgs ─
                        # v2.1.3 FIX: The previous post-prompt told Sage
                        # "do not emit any more tool tags, just answer
                        # directly" after EVERY tool result — which killed
                        # multi-step workflows. She'd do Step 1 (e.g.
                        # listdir), get the results, then be ordered to
                        # "just answer" even though the task clearly
                        # required more steps (reading the file she just
                        # listed, saving a summary, etc.). Now she is
                        # explicitly told she CAN keep emitting tool tags
                        # if she still needs to, and MUST emit [TASK_DONE]
                        # when truly finished.
                        step_messages = list(messages)
                        if tool_results_acc:
                            tool_text = (
                                "\n\n[TOOL RESULTS SO FAR — use these to "
                                "continue the task]\n"
                            )
                            for k, v in tool_results_acc.items():
                                tool_text += f"--- {k} ---\n{v}\n\n"
                            tool_text += (
                                f"[You are on step {step} of "
                                f"{max_steps}. Review the tool results "
                                f"above. If the task is now fully "
                                f"complete, give the final answer and "
                                f"emit [TASK_DONE]. If you still need to "
                                f"run more tools (read a file, save a "
                                f"file, search, browse, etc.) to finish "
                                f"the task, emit the next tool tag NOW. "
                                f"Do not apologize that information is "
                                f"missing — instead, emit the tool tag "
                                f"that will get it. Chain as many "
                                f"[CODE:], [SAVE_FILE:], [SEARCH:], or "
                                f"other tool calls as you need.]\n"
                            )
                            step_messages.append({
                                "role": "user", "content": tool_text,
                            })

                        # ── Non-streaming call for this agentic step ──
                        try:
                            step_text = await _generate_full_routed(
                                step_messages, model_id, options)
                        except Exception as e:
                            await websocket.send_json({
                                "type": "error",
                                "content": f"Generation error: {e}",
                                "done": True,
                            })
                            break

                        # ── Parse tool actions from model output ------
                        # v2.1.5 FIX: also pull consumed_ranges so the
                        # final-answer cleanup can surgically remove every
                        # parsed tag span (not just [TASK_DONE] + [SEARCH_MEMORY:]).
                        actions, consumed_ranges = (
                            sage_engine.parse_agent_actions(
                                step_text, return_ranges=True,
                            )
                        )

                        # v2.1.5 DISPATCH-ORDER FIX: Previously, if [TASK_DONE]
                        # appeared in the actions list, the loop SHORTCUTTED
                        # straight to the final-answer streaming path WITHOUT
                        # dispatching the other actions in the same step. That
                        # meant [SAVE_FILE: foo.py|<script>] + [TASK_DONE] in
                        # one response → SAVE_FILE never fired, and the entire
                        # SAVE_FILE tag (with its body) leaked verbatim into
                        # chat as the "final answer." Now: dispatch all
                        # non-done actions FIRST, then handle the done-shortcut.
                        has_done = ("done", "") in actions
                        non_done_actions = [
                            (a, c) for a, c in actions if a != "done"
                        ]

                        def _stream_clean_final_answer():
                            """Strip every parsed tag span from step_text and
                            stream what's left as the final answer. Uses the
                            parser's consumed_ranges so SAVE_FILE/CODE bodies
                            (which can be arbitrarily long Python/JSON/etc.)
                            are removed cleanly without regex acrobatics."""
                            cleaned = step_text
                            for s, e in sorted(consumed_ranges, reverse=True):
                                if 0 <= s < e <= len(cleaned):
                                    cleaned = cleaned[:s] + cleaned[e:]
                            return cleaned.strip()

                        # If the model emitted nothing parseable, stream raw
                        # text as the final answer (matches old behavior).
                        if not actions:
                            clean = step_text.strip()
                            for chunk in _chunk_text(clean, 4):
                                if model_manager._abort:
                                    aborted = True
                                    break
                                full_response += chunk
                                await websocket.send_json({
                                    "type": "token",
                                    "content": chunk,
                                    "done": False,
                                })
                            break

                        # ── Execute each tool action ------------------
                        # Dispatches non-done actions even when [TASK_DONE]
                        # is also present in this step. After dispatch, the
                        # done-handler below will stream a clean final answer.
                        executed_any = False
                        # Capture the latest result text so the post-dispatch
                        # auto-failure detector can inspect it. Reset per
                        # action; stays None for tags that produce no result
                        # (done, unknown).
                        for action_type, content in non_done_actions:
                            if model_manager._abort:
                                aborted = True
                                break
                            result = None  # set by handlers; read after

                            if action_type in ("search",
                                               "search_general") and web_ok:
                                executed_any = True
                                stype = ("general"
                                         if action_type == "search_general"
                                         else "news")
                                # v2.1.8 Phase 2: prefix ⚡ when TaskP is on
                                # so the user can see TaskP is routing this.
                                _taskp_on = sage_engine.is_feature_enabled(
                                    "task_prioritiser")
                                _bolt = "⚡ " if _taskp_on else ""
                                await websocket.send_json({
                                    "type": "tool_call",
                                    "tool": "search",
                                    "input": content,
                                    "message":
                                        f"{_bolt}🔍 Searching: {content}",
                                })
                                try:
                                    result = await _taskp_run_or_direct(
                                        "search",
                                        lambda c=content, st=stype: (
                                            sage_engine.web_search(
                                                c, search_type=st,
                                            )
                                        ),
                                        importance=0.5,
                                    )
                                except Exception as e:
                                    result = f"Search error: {e}"
                                key = f"search:{content[:60]}"
                                tool_results_acc[key] = result
                                await websocket.send_json({
                                    "type": "tool_result",
                                    "tool": "search",
                                    "output": result[:500],
                                })

                            elif action_type == "weather":
                                executed_any = True
                                # v2.1.8 Phase 2 TaskP wrap (SAFE op)
                                _taskp_on = sage_engine.is_feature_enabled(
                                    "task_prioritiser")
                                _bolt = "⚡ " if _taskp_on else ""
                                await websocket.send_json({
                                    "type": "tool_call",
                                    "tool": "weather",
                                    "input": content,
                                    "message":
                                        f"{_bolt}🌤️ Getting weather: "
                                        f"{content}",
                                })
                                try:
                                    result = await _taskp_run_or_direct(
                                        "weather",
                                        lambda c=content: (
                                            sage_engine.get_weather(c)
                                        ),
                                        importance=0.5,
                                    )
                                except Exception as e:
                                    result = f"Weather error: {e}"
                                key = f"weather:{content[:60]}"
                                tool_results_acc[key] = result
                                await websocket.send_json({
                                    "type": "tool_result",
                                    "tool": "weather",
                                    "output": result,
                                })

                            elif action_type == "code" and code_ok:
                                executed_any = True
                                await websocket.send_json({
                                    "type": "tool_call",
                                    "tool": "code",
                                    "input": content[:200],
                                    "message": "⚙️ Running code…",
                                })
                                try:
                                    loop = asyncio.get_event_loop()
                                    result = await loop.run_in_executor(
                                        None,
                                        sage_engine.execute_python,
                                        content,
                                    )
                                except Exception as e:
                                    result = f"Code error: {e}"
                                # Key by step number so successive code calls
                                # don't overwrite each other's output.
                                code_key = f"code_output_step_{step}"
                                tool_results_acc[code_key] = result
                                await websocket.send_json({
                                    "type": "tool_result",
                                    "tool": "code",
                                    "output": result[:500],
                                })

                            elif action_type == "search_memory":
                                executed_any = True
                                # v2.1.8 Phase 2 TaskP wrap (SAFE op:
                                # memory READ only — write path is the
                                # REMEMBER tag handler below and uses
                                # the direct Fernet-aware writer).
                                _taskp_on = sage_engine.is_feature_enabled(
                                    "task_prioritiser")
                                _bolt = "⚡ " if _taskp_on else ""
                                await websocket.send_json({
                                    "type": "tool_call",
                                    "tool": "memory",
                                    "input": content,
                                    "message":
                                        f"{_bolt}🧠 Searching memory: "
                                        f"{content}",
                                })
                                try:
                                    def _search_mem(c=content):
                                        archives = sage_engine.get_archives(_ws_ns)
                                        mem = sage_engine.keyword_search(
                                            c, archives,
                                        )
                                        if mem:
                                            r = ""
                                            for mr in mem:
                                                for msg in mr.get(
                                                    "preview", [],
                                                ):
                                                    r += (
                                                        f"{msg['role']}: "
                                                        f"{msg['content'][:200]}"
                                                        "\n"
                                                    )
                                            return r
                                        return (
                                            "No relevant memories found."
                                        )

                                    result = await _taskp_run_or_direct(
                                        "search_memory",
                                        _search_mem,
                                        importance=0.4,
                                    )
                                except Exception as e:
                                    result = f"Memory error: {e}"
                                tool_results_acc["memory_search"] = result
                                await websocket.send_json({
                                    "type": "tool_result",
                                    "tool": "memory",
                                    "output": result[:500],
                                })

                            elif action_type == "browse":
                                executed_any = True
                                # v2.1.8 Phase 2 TaskP wrap (SAFE op)
                                _taskp_on = sage_engine.is_feature_enabled(
                                    "task_prioritiser")
                                _bolt = "⚡ " if _taskp_on else ""
                                await websocket.send_json({
                                    "type": "tool_call",
                                    "tool": "browse",
                                    "input": content,
                                    "message":
                                        f"{_bolt}🌐 Browsing: {content}",
                                })
                                try:
                                    # Browser sessions can take a while —
                                    # bump TaskP outer timeout so a slow
                                    # page load doesn't trip the helper.
                                    result = await _taskp_run_or_direct(
                                        "browse",
                                        lambda c=content: (
                                            sage_engine.browse_url(c)
                                        ),
                                        importance=0.5,
                                        timeout_seconds=120.0,
                                    )
                                except Exception as e:
                                    result = f"Browse error: {e}"
                                key = f"browse:{content[:60]}"
                                tool_results_acc[key] = result
                                await websocket.send_json({
                                    "type": "tool_result",
                                    "tool": "browse",
                                    "output": result[:500],
                                })

                            elif action_type == "web_search_browser":
                                executed_any = True
                                # v2.1.8 Phase 2 TaskP wrap (SAFE op)
                                _taskp_on = sage_engine.is_feature_enabled(
                                    "task_prioritiser")
                                _bolt = "⚡ " if _taskp_on else ""
                                await websocket.send_json({
                                    "type": "tool_call",
                                    "tool": "web_search",
                                    "input": content,
                                    "message":
                                        f"{_bolt}🌐 Web search: {content}",
                                })
                                try:
                                    result = await _taskp_run_or_direct(
                                        "web_search_browser",
                                        lambda c=content: (
                                            sage_engine.web_search_browser(c)
                                        ),
                                        importance=0.5,
                                        timeout_seconds=120.0,
                                    )
                                except Exception as e:
                                    result = f"Web search error: {e}"
                                key = f"web_search:{content[:60]}"
                                tool_results_acc[key] = result
                                await websocket.send_json({
                                    "type": "tool_result",
                                    "tool": "web_search",
                                    "output": result[:500],
                                })

                            elif action_type == "save_file":
                                executed_any = True
                                fname, fcontent = content if isinstance(
                                    content, tuple) else ("output.txt", str(content))
                                await websocket.send_json({
                                    "type": "tool_call",
                                    "tool": "save_file",
                                    "input": fname,
                                    "message": f"💾 Saving file: {fname}",
                                })
                                try:
                                    save_result = sage_engine.save_to_downloads(
                                        fname, fcontent)
                                    if save_result.get("success"):
                                        result = f"Saved {save_result['filename']} to downloads ({save_result['size']} bytes)"
                                    else:
                                        result = f"Save failed: {save_result.get('error', 'unknown')}"
                                except Exception as e:
                                    result = f"Save error: {e}"
                                tool_results_acc[f"save:{fname}"] = result
                                await websocket.send_json({
                                    "type": "tool_result",
                                    "tool": "save_file",
                                    "output": result,
                                })

                            elif action_type == "generate_image":
                                executed_any = True
                                if _img_gen_count >= _max_imgs_per_turn:
                                    _lim = (f"Image limit reached "
                                            f"({_max_imgs_per_turn} per turn). "
                                            f"Not generating more this turn.")
                                    tool_results_acc[f"image_limit:{step}"] = _lim
                                    await websocket.send_json({
                                        "type": "tool_result",
                                        "tool": "generate_image",
                                        "output": _lim,
                                    })
                                    continue
                                _img_gen_count += 1
                                _gen_prompt = (
                                    content if isinstance(content, str)
                                    else str(content)
                                ).strip()
                                await websocket.send_json({
                                    "type": "tool_call",
                                    "tool": "generate_image",
                                    "input": _gen_prompt[:200],
                                    "message": f"Generating image: {_gen_prompt[:80]}",
                                })
                                try:
                                    _img = await _generate_image_routed(_gen_prompt, downloads_dir=str(_downloads_dir_for_ns(_ws_ns)))
                                except Exception as _ge:
                                    _img = {"success": False, "error": str(_ge)}
                                if _img.get("success"):
                                    result = (
                                        f"Image generated and saved to downloads "
                                        f"as {_img['filename']} (seed "
                                        f"{_img.get('seed')}, checkpoint "
                                        f"{_img.get('checkpoint')})."
                                    )
                                    try:
                                        await websocket.send_json({
                                            "type": "image_generated",
                                            "filename": _img.get("filename"),
                                            "path": _img.get("path"),
                                            "mimetype": _img.get("mimetype", "image/png"),
                                            "data": _img.get("data"),
                                            "prompt": _gen_prompt,
                                            "seed": _img.get("seed"),
                                        })
                                    except Exception:
                                        pass
                                else:
                                    result = (
                                        f"Image generation failed: "
                                        f"{_img.get('error', 'unknown error')}"
                                    )
                                tool_results_acc[f"image:{_gen_prompt[:40]}"] = result
                                await websocket.send_json({
                                    "type": "tool_result",
                                    "tool": "generate_image",
                                    "output": result,
                                })

                            elif action_type == "save_file_error":
                                # v2.2 (2026-05-30): malformed SAVE_FILE
                                # surfaced by parse_agent_actions. The
                                # parser previously fell through silently
                                # and Sage would fabricate verification
                                # success. Now we push a clear tool_result
                                # so her loop sees the failure and can
                                # self-correct in the same turn. content
                                # is the raw malformed tag body (usually
                                # just a filename with no pipe).
                                executed_any = True
                                bad_content = (
                                    content if isinstance(content, str)
                                    else str(content)
                                )
                                err_msg = (
                                    f"[SAVE FAILED] Malformed [SAVE_FILE:] tag. "
                                    f"Missing `|` separator between filename "
                                    f"and content. Got: {bad_content!r}. "
                                    f"Required format: "
                                    f"[SAVE_FILE: name.ext|<entire file body>]. "
                                    f"The body MUST be INSIDE the brackets. "
                                    f"Anything outside the closing `]` is chat "
                                    f"text and is NOT saved. No file was "
                                    f"written. Please re-emit with the full "
                                    f"body inside the tag."
                                )
                                await websocket.send_json({
                                    "type": "tool_call",
                                    "tool": "save_file",
                                    "input": bad_content[:200],
                                    "message": (
                                        f"⚠️ SAVE_FILE malformed "
                                        f"(missing `|`): {bad_content[:60]}"
                                    ),
                                })
                                tool_results_acc[
                                    f"save_error:{bad_content[:60]}"
                                ] = err_msg
                                await websocket.send_json({
                                    "type": "tool_result",
                                    "tool": "save_file",
                                    "output": err_msg,
                                })

                            elif action_type == "verify_file":
                                # v2.1.5: [VERIFY_FILE: path] handler.
                                # Was previously documented in the prompt
                                # but UNWIRED (parser ignored, no dispatch
                                # handler), so Sage would hallucinate a
                                # verification result. Now actually calls
                                # verify_written_file() which checks
                                # os.path.exists() first and returns
                                # "[VERIFY FAILED] File not found: ..." on
                                # missing files. Truth, not vibes.
                                executed_any = True
                                vpath = str(content).strip()
                                await websocket.send_json({
                                    "type": "tool_call",
                                    "tool": "verify_file",
                                    "input": vpath,
                                    "message":
                                        f"🔎 Verifying file: {vpath}",
                                })
                                try:
                                    if not vpath:
                                        result = (
                                            "[VERIFY FAILED] empty path "
                                            "(use [VERIFY_FILE: path])"
                                        )
                                    else:
                                        result = sage_engine.verify_written_file(
                                            vpath
                                        )
                                except Exception as e:
                                    result = f"[VERIFY ERROR] {e}"
                                tool_results_acc[
                                    f"verify_file:{vpath[:60]}"] = result
                                await websocket.send_json({
                                    "type": "tool_result",
                                    "tool": "verify_file",
                                    "output": result,
                                })

                            elif action_type == "lint_expr":
                                # [LINT_EXPR: expr] -> static validation via
                                # expression_engine (no evaluation). Imported
                                # directly so it works even with sage plugins off.
                                executed_any = True
                                _xexpr = str(content).strip()
                                await websocket.send_json({
                                    "type": "tool_call",
                                    "tool": "lint_expr",
                                    "input": _xexpr,
                                    "message": f"Linting expression: {_xexpr[:80]}",
                                })
                                try:
                                    import json as _xjson
                                    import expression_engine as _xee
                                    result = _xjson.dumps(_xee.lint(_xexpr))
                                except Exception as e:
                                    result = f"[LINT ERROR] {e}"
                                tool_results_acc[
                                    f"lint_expr:{_xexpr[:60]}"] = result
                                await websocket.send_json({
                                    "type": "tool_result",
                                    "tool": "lint_expr",
                                    "output": result[:500],
                                })

                            elif action_type == "parse_expr":
                                # [PARSE_EXPR: expr] -> safe parse + evaluate via
                                # expression_engine (no Python eval).
                                executed_any = True
                                _xexpr = str(content).strip()
                                await websocket.send_json({
                                    "type": "tool_call",
                                    "tool": "parse_expr",
                                    "input": _xexpr,
                                    "message": f"Evaluating expression: {_xexpr[:80]}",
                                })
                                try:
                                    import expression_engine as _xee
                                    _xpr = _xee.parse(_xexpr)
                                    result = f"result={_xpr['result']} (type={_xpr['type']})"
                                    if _xpr.get("warnings"):
                                        result += f" | warnings: {_xpr['warnings']}"
                                except ZeroDivisionError as e:
                                    result = f"[ZeroDivisionError] {e}"
                                except Exception as e:
                                    result = f"[PARSE ERROR] {e}"
                                tool_results_acc[
                                    f"parse_expr:{_xexpr[:60]}"] = result
                                await websocket.send_json({
                                    "type": "tool_result",
                                    "tool": "parse_expr",
                                    "output": result[:500],
                                })

                            # ── v2.1.4 procedural memory tags ──────────
                            elif action_type == "remember":
                                # [REMEMBER:key|description]
                                # Successful procedure → chain-witnessed.
                                executed_any = True
                                parts = str(content).split("|", 1)
                                p_key = parts[0].strip()
                                p_desc = (parts[1].strip()
                                          if len(parts) > 1 else "")
                                await websocket.send_json({
                                    "type": "tool_call",
                                    "tool": "remember",
                                    "input": p_key,
                                    "message":
                                        f"📌 Remembering procedure: {p_key}",
                                })
                                try:
                                    if not p_key:
                                        result = (
                                            "REMEMBER failed: empty key. "
                                            "Use [REMEMBER:key|description]."
                                        )
                                    else:
                                        is_new = procedural.add_procedure(
                                            key=p_key,
                                            value=p_desc,
                                            success=True,
                                            metadata={
                                                "source": "tag",
                                                "step": step,
                                            },
                                        )
                                        entry = (
                                            procedural
                                            .get_procedure_with_metadata(
                                                p_key, category="successful",
                                            )
                                        )
                                        chash = (entry.get("chain_hash", "")
                                                 if entry else "")
                                        verb = ("stored new"
                                                if is_new else "updated")
                                        result = (
                                            f"REMEMBER ok: {verb} '{p_key}' "
                                            f"(chain_hash "
                                            f"{chash[:12] + '…' if chash else 'n/a'})"
                                        )
                                except Exception as e:
                                    result = f"REMEMBER error: {e}"
                                tool_results_acc[
                                    f"remember:{p_key[:60]}"] = result
                                await websocket.send_json({
                                    "type": "tool_result",
                                    "tool": "remember",
                                    "output": result,
                                })

                            elif action_type == "remember_fail":
                                # [REMEMBER_FAIL:key|reason]
                                # Unsuccessful — local-only, NOT chained.
                                executed_any = True
                                parts = str(content).split("|", 1)
                                p_key = parts[0].strip()
                                p_reason = (parts[1].strip()
                                            if len(parts) > 1 else "")
                                await websocket.send_json({
                                    "type": "tool_call",
                                    "tool": "remember_fail",
                                    "input": p_key,
                                    "message":
                                        f"⚠️ Recording dead-end: {p_key}",
                                })
                                try:
                                    if not p_key:
                                        result = (
                                            "REMEMBER_FAIL failed: empty key."
                                        )
                                    else:
                                        procedural.add_procedure(
                                            key=p_key,
                                            value=p_reason,
                                            success=False,
                                            metadata={
                                                "source": "tag",
                                                "step": step,
                                            },
                                        )
                                        result = (
                                            f"REMEMBER_FAIL ok: '{p_key}' "
                                            "logged as dead-end "
                                            "(not chain-witnessed)"
                                        )
                                except Exception as e:
                                    result = f"REMEMBER_FAIL error: {e}"
                                tool_results_acc[
                                    f"remember_fail:{p_key[:60]}"] = result
                                await websocket.send_json({
                                    "type": "tool_result",
                                    "tool": "remember_fail",
                                    "output": result,
                                })

                            elif action_type == "recall":
                                # [RECALL:query] — fuzzy substring match
                                # against successful procedure keys and
                                # descriptions. Returns top 10.
                                executed_any = True
                                q = str(content).strip().lower()
                                # v2.1.8 Phase 2 TaskP wrap (SAFE op:
                                # procedural memory READ. The WRITE
                                # side is REMEMBER / REMEMBER_FAIL and
                                # stays on the direct, chain-witnessed
                                # path).
                                _taskp_on = sage_engine.is_feature_enabled(
                                    "task_prioritiser")
                                _bolt = "⚡ " if _taskp_on else ""
                                await websocket.send_json({
                                    "type": "tool_call",
                                    "tool": "recall",
                                    "input": q,
                                    "message":
                                        f"{_bolt}🔎 Recalling procedure: "
                                        f"{q}",
                                })
                                try:
                                    def _recall(query=q):
                                        hits = []
                                        succ = procedural.get_all().get(
                                            "successful", {})
                                        for k, entry in succ.items():
                                            val = str(
                                                entry.get("value", ""))
                                            if (not query
                                                    or query in k.lower()
                                                    or query in val.lower()):
                                                hits.append((k, val))
                                            if len(hits) >= 10:
                                                break
                                        if not hits:
                                            return (
                                                f"No procedural memory "
                                                f"matched '{query}'."
                                            )
                                        lines = [
                                            f"- {k}: {v[:160]}"
                                            for k, v in hits
                                        ]
                                        return (
                                            f"RECALL hits ({len(hits)}):\n"
                                            + "\n".join(lines)
                                        )

                                    result = await _taskp_run_or_direct(
                                        "recall",
                                        _recall,
                                        importance=0.4,
                                    )
                                except Exception as e:
                                    result = f"RECALL error: {e}"
                                tool_results_acc[
                                    f"recall:{q[:60]}"] = result
                                await websocket.send_json({
                                    "type": "tool_result",
                                    "tool": "recall",
                                    "output": result[:500],
                                })

                            elif action_type == "prioritise":
                                # v2.1.5 Phase C: batched parallel
                                # dispatch via oracle_d. Pipe-separated
                                # subtasks get submitted in parallel,
                                # results concatenated + extractive-
                                # summarised into one tool result. ONE
                                # agentic step instead of N — and the
                                # prioritiser's worker threads run
                                # off-band from this WebSocket handler.
                                executed_any = True
                                subtasks_raw = [
                                    s.strip() for s in
                                    str(content).split("|")
                                    if s.strip()
                                ]
                                await websocket.send_json({
                                    "type": "tool_call",
                                    "tool": "prioritise",
                                    "input": (
                                        f"{len(subtasks_raw)} parallel "
                                        f"subtasks"
                                    ),
                                    "message": (
                                        f"⚡ Prioritising "
                                        f"{len(subtasks_raw)} "
                                        f"subtasks in parallel…"
                                    ),
                                })

                                def _classify_subtask(s: str):
                                    """Map a free-text subtask to a tool
                                    fn. Heuristic: keyword-match on the
                                    first word(s). Returns (kind, fn)
                                    or (None, fn) if not recognised
                                    (in which case fn just echoes)."""
                                    sl = s.lower()
                                    if (sl.startswith("search news")
                                            or sl.startswith("news ")):
                                        q = s.split(":", 1)[-1].strip(
                                            ) if ":" in s else s
                                        return ("news", lambda q=q: (
                                            sage_engine.web_search(
                                                q, search_type="news")))
                                    if (sl.startswith("search general")
                                            or sl.startswith("general ")
                                            or sl.startswith("search:")
                                            or sl.startswith("search ")):
                                        q = s.split(":", 1)[-1].strip(
                                            ) if ":" in s else s
                                        return ("general", lambda q=q: (
                                            sage_engine.web_search(
                                                q,
                                                search_type="general")))
                                    if sl.startswith("weather"):
                                        loc = s.split(
                                            None, 1)[-1] if " " in s \
                                            else s
                                        return ("weather", lambda l=loc: (
                                            sage_engine.get_weather(l)))
                                    if (sl.startswith("browse")
                                            or sl.startswith("http")):
                                        url = s.split(
                                            None, 1)[-1] if " " in s \
                                            else s
                                        return ("browse", lambda u=url: (
                                            sage_engine.browse_url(u)))
                                    return (None, lambda s=s: (
                                        f"(unrecognised subtask: "
                                        f"{s})"))

                                _pending_keys: list = []
                                _results_collect: dict = {}
                                done_evt = threading.Event()
                                _lock = threading.Lock()

                                def _collect(task_result):
                                    out = task_result.output
                                    if (isinstance(out, dict)
                                            and "key" in out):
                                        with _lock:
                                            _results_collect[
                                                out["key"]] = out["value"]
                                            if (len(_results_collect)
                                                    >= len(_pending_keys)):
                                                done_evt.set()

                                _orig_cb = (
                                    sage_engine.oracle_d._result_callback
                                )

                                def _patched_cb(tr):
                                    _collect(tr)
                                    try:
                                        _orig_cb(tr)
                                    except Exception:
                                        pass

                                sage_engine.oracle_d._result_callback = (
                                    _patched_cb
                                )

                                try:
                                    for idx, sub in enumerate(
                                            subtasks_raw):
                                        kind, fn = _classify_subtask(sub)
                                        key = f"sub{idx}_{kind or 'echo'}"
                                        _pending_keys.append(key)
                                        sage_engine.oracle_d.submit_raw_task({
                                            "type": kind or "echo",
                                            "importance": 0.7,
                                            "deadline": (
                                                TimeManager.epoch() + 30),
                                            "key": key,
                                            "fn": fn,
                                        })
                                    if _pending_keys:
                                        loop2 = (
                                            asyncio.get_event_loop())
                                        await loop2.run_in_executor(
                                            None,
                                            done_evt.wait,
                                            60.0,  # hard timeout
                                        )
                                    parts = []
                                    for k in _pending_keys:
                                        v = _results_collect.get(
                                            k, "(timed out)")
                                        parts.append(
                                            f"--- {k} ---\n"
                                            f"{str(v)[:600]}"
                                        )
                                    result = (
                                        f"PRIORITISE results "
                                        f"({len(_results_collect)}/"
                                        f"{len(_pending_keys)}):\n"
                                        + "\n\n".join(parts)
                                    )
                                except Exception as _pe:
                                    result = (
                                        f"PRIORITISE error: {_pe}"
                                    )
                                finally:
                                    sage_engine.oracle_d._result_callback = (
                                        _orig_cb
                                    )
                                tool_results_acc[
                                    f"prioritise:"
                                    f"{str(content)[:60]}"] = result
                                await websocket.send_json({
                                    "type": "tool_result",
                                    "tool": "prioritise",
                                    "output": result[:600],
                                })

                            # ── v2.1.4 auto-capture of repeats -------
                            # After every action executes, track repeats
                            # of (action_type, content_key). If the same
                            # call lands 3 times in a turn AND the most
                            # recent result looks like failure, log an
                            # unsuccessful procedure so future turns can
                            # warn the model off the dead-end. REMEMBER /
                            # REMEMBER_FAIL / RECALL are excluded — they
                            # are meta and shouldn't self-report.
                            if action_type not in (
                                "remember", "remember_fail", "recall",
                            ):
                                try:
                                    if isinstance(content, tuple):
                                        content_repr = "|".join(
                                            str(x) for x in content
                                        )
                                    else:
                                        content_repr = str(content)
                                    attempt_key = (
                                        f"{action_type}:{content_repr[:120]}"
                                    )
                                    # v2.1.5 Phase A: log this tool call into
                                    # the turn's ordered sequence for the
                                    # TASK_DONE autolog. Truncate aggressively
                                    # — long browser URLs / code blocks would
                                    # bloat the procedure record.
                                    turn_actions.append({
                                        "step": step,
                                        "action": action_type,
                                        "content": content_repr[:200],
                                        "result_shape": (
                                            "fail" if _looks_like_failure(
                                                str(result or "")
                                            ) else "ok"
                                        ),
                                    })
                                    action_attempts[attempt_key] = (
                                        action_attempts.get(attempt_key, 0) + 1
                                    )
                                    if (action_attempts[attempt_key] >= 3
                                            and attempt_key
                                            not in auto_failed_keys
                                            and _looks_like_failure(
                                                str(result or ""))):
                                        auto_failed_keys.add(attempt_key)
                                        procedural.add_procedure(
                                            key=attempt_key,
                                            value=(
                                                f"Auto-captured after 3 "
                                                f"consecutive attempts "
                                                f"with failure-shaped "
                                                f"result. Last result: "
                                                f"{str(result)[:300]}"
                                            ),
                                            success=False,
                                            metadata={
                                                "source": "auto_capture",
                                                "step": step,
                                                "attempts":
                                                    action_attempts[
                                                        attempt_key],
                                            },
                                        )
                                        # Surface it in tool_results_acc so
                                        # the next agentic step sees the
                                        # warning inline, not just on the
                                        # next turn.
                                        tool_results_acc[
                                            f"auto_dead_end:"
                                            f"{attempt_key[:60]}"
                                        ] = (
                                            f"[Auto-captured dead-end] "
                                            f"'{attempt_key}' has failed "
                                            f"{action_attempts[attempt_key]}"
                                            f" times this turn. "
                                            f"Try a different approach."
                                        )
                                        await websocket.send_json({
                                            "type": "tool_result",
                                            "tool": "auto_dead_end",
                                            "output": (
                                                f"Auto-logged dead-end: "
                                                f"{attempt_key}"
                                            ),
                                        })
                                except Exception as _ac_e:
                                    # Auto-capture must never break the loop
                                    print(
                                        f"[PROCEDURAL] auto-capture "
                                        f"failed: {_ac_e}"
                                    )

                        # ── v2.1.5 post-dispatch [TASK_DONE] handler ──
                        # After all non-done actions in this step have been
                        # dispatched, if [TASK_DONE] was also emitted in the
                        # same step, stream the cleaned final answer (with
                        # every parsed tag span surgically removed via
                        # consumed_ranges) and break out of the agentic loop.
                        # Also runs the chain-witnessed autolog of the
                        # turn's tool sequence.
                        #
                        # This branch replaces the v2.1.4 "shortcut" where
                        # any [TASK_DONE] in actions caused the loop to skip
                        # dispatch — which let [SAVE_FILE:] tag bodies leak
                        # verbatim into chat. With dispatch happening first,
                        # the file actually gets saved AND the streamed
                        # final answer is just prose.
                        if has_done:
                            clean = _stream_clean_final_answer()
                            for chunk in _chunk_text(clean, 4):
                                if model_manager._abort:
                                    aborted = True
                                    break
                                full_response += chunk
                                await websocket.send_json({
                                    "type": "token",
                                    "content": chunk,
                                    "done": False,
                                })
                            # Autolog the turn's tool sequence as a
                            # chain-witnessed successful procedure.
                            if (not aborted and turn_actions
                                    and user_request_text):
                                try:
                                    import hashlib as _hlib
                                    req_hash = _hlib.sha1(
                                        user_request_text.encode("utf-8")
                                    ).hexdigest()[:8]
                                    slug = re.sub(
                                        r"[^a-z0-9]+", "_",
                                        user_request_text.lower(),
                                    ).strip("_")[:40] or "task"
                                    proc_key = f"task:{req_hash}:{slug}"
                                    proc_value = {
                                        "user_request":
                                            user_request_text[:500],
                                        "steps_used": step,
                                        "max_steps": max_steps,
                                        "actions": turn_actions,
                                        "final_answer_preview":
                                            clean[:300],
                                    }
                                    procedural.add_procedure(
                                        key=proc_key,
                                        value=proc_value,
                                        success=True,
                                        metadata={
                                            "source": "auto_task_done",
                                            "tool_count": len(turn_actions),
                                        },
                                    )
                                    print(
                                        f"[PROCEDURAL] auto-logged "
                                        f"successful sequence: "
                                        f"{proc_key} "
                                        f"({len(turn_actions)} actions)"
                                    )
                                except Exception as _adone_e:
                                    print(
                                        f"[PROCEDURAL] TASK_DONE "
                                        f"autolog failed: {_adone_e}"
                                    )
                            break

                        if not executed_any:
                            # Tags present but no enabled tool matched —
                            # stream the raw text as the final answer
                            for chunk in _chunk_text(step_text, 4):
                                full_response += chunk
                                await websocket.send_json({
                                    "type": "token",
                                    "content": chunk,
                                    "done": False,
                                })
                            break
                        # Loop continues → next agentic step with results

                # ======================================================
                #  STANDARD MODE — streaming with search interception
                # ======================================================
                else:
                    search_buffer = ""
                    search_intercepted = False

                    # v2.1.8 #56: routed through _watched_generate so the
                    # stall watchdog's last-token timestamp is bumped on
                    # every yielded token. Falls through to the underlying
                    # model_manager.generate() — no behavior change beyond
                    # the side-effect of the watchdog notification.
                    async for token in _watched_generate(
                        messages, model_id, options, watchdog,
                    ):
                        if model_manager._abort:
                            aborted = True
                            break

                        search_buffer += token

                        # ── Intercept [SEARCH:] mid-stream ------------
                        if ("[SEARCH:" in search_buffer
                                and not search_intercepted and web_ok):
                            match = re.search(
                                r"\[SEARCH:\s*(.*?)(\]|$)",
                                search_buffer, re.I,
                            )
                            if match and match.group(2) == "]":
                                search_intercepted = True
                                query = match.group(1).strip()
                                # v2.1.6 fix: distinguish a self-abort
                                # (we're breaking out of the current
                                # stream so we can run the search and
                                # re-prompt) from a user-pressed-stop.
                                # Snapshot the current flag — if the
                                # user already pressed stop, that
                                # intent survives the followup. If
                                # not, we'll restore _abort=False
                                # before the followup so the
                                # follow-up generate() actually runs.
                                user_already_aborted = model_manager._abort
                                model_manager.abort()

                                await websocket.send_json({
                                    "type": "tool_call",
                                    "tool": "search",
                                    "input": query,
                                    "message":
                                        f"🔍 Searching: {query}",
                                })
                                loop = asyncio.get_event_loop()
                                try:
                                    sr = await loop.run_in_executor(
                                        None,
                                        sage_engine.web_search,
                                        query,
                                    )
                                except Exception as e:
                                    sr = f"Search error: {e}"

                                await websocket.send_json({
                                    "type": "tool_result",
                                    "tool": "search",
                                    "output": sr[:500],
                                })

                                # Re-query with search results
                                followup = list(messages) + [{
                                    "role": "user",
                                    "content": (
                                        f"\n\n[SEARCH RESULTS FOR: "
                                        f"{query}]\n{sr}\n\n"
                                        "Using ONLY the search results "
                                        "above, provide an accurate "
                                        "answer. Do not make up info "
                                        "not in the results."
                                    ),
                                }]
                                # v2.1.6 fix: if the user did NOT press
                                # stop before search interception fired,
                                # the abort we set above was a self-
                                # abort to break the prior stream — we
                                # need to clear it so the followup can
                                # actually run. If the user DID press
                                # stop, leave _abort=True so the
                                # followup respects their intent.
                                if not user_already_aborted:
                                    model_manager._abort = False
                                # v2.1.8 #56: search-intercept follow-up
                                # also goes through _watched_generate so
                                # stalls during the post-search summary
                                # get caught the same way.
                                async for t2 in _watched_generate(
                                    followup, model_id, options, watchdog,
                                ):
                                    if model_manager._abort:
                                        aborted = True
                                        break
                                    full_response += t2
                                    await websocket.send_json({
                                        "type": "token",
                                        "content": t2,
                                        "done": False,
                                    })
                                break
                            continue

                        if not search_intercepted:
                            full_response += token
                            await websocket.send_json({
                                "type": "token",
                                "content": token,
                                "done": False,
                            })

                # ── Wrap up -------------------------------------------
                if aborted:
                    await websocket.send_json({"type": "aborted"})
                else:
                    full_response = plugin_manager.postprocess(
                        full_response,
                    )
                    # Save to chat memory
                    try:
                        history = sage_engine.load_chat_memory(_ws_ns)
                        user_msgs = [
                            m for m in messages
                            if m.get("role") == "user"
                        ]
                        if user_msgs:
                            history.append({
                                "role": "user",
                                "content": user_msgs[-1]["content"],
                            })
                        if full_response.strip():
                            history.append({
                                "role": "assistant",
                                "content": full_response,
                            })
                        # Chat memory window cap removed per project policy:
                        # local system, hardware-limited, no arbitrary turn count.
                        # Full history is persisted; model context handled upstream.
                        sage_engine.save_chat_memory(history, _ws_ns)
                    except Exception:
                        pass

                    # v2.1.6: surface the model that produced this
                    # response and a canonical timestamp so the
                    # frontend can render a per-message model badge
                    # and a local-time stamp on each completed bubble.
                    await websocket.send_json({
                        "type": "done",
                        "content": full_response,
                        "done": True,
                        "model": model_id or "",
                        "offloaded": (_current_offload.get() or ""),
                        "ts": TimeManager.iso_z(),
                    })
                # Log the response to memory (v2.1.2: louder error reporting)
                # v2.1.4: now tags role='assistant' so the chain distinguishes
                # sides of the conversation.
                try:
                    memory_logger.log(
                        content=full_response,
                        temperature=config.get("temperature", 0.5),
                        token_prob=None,
                        metadata={"mode": "agentic"},
                        role="assistant",
                    )
                    print(f"[MEMORY LOGGER] Logged response (entry #{memory_logger.count_entries()})")
                except Exception as e:
                    # Loud failure: print to console AND write a breadcrumb file
                    # so silent errors cannot hide again
                    import traceback as _tb
                    err_msg = f"[MEMORY LOGGER ERROR] {type(e).__name__}: {e}"
                    print(err_msg)
                    print(_tb.format_exc())
                    try:
                        err_file = BASE_DIR / "memory_logger_errors.log"
                        with open(err_file, "a", encoding="utf-8") as _ef:
                            _ef.write(f"{err_msg}\n{_tb.format_exc()}\n---\n")
                    except Exception:
                        pass

            except Exception as e:
                await websocket.send_json({
                    "type": "error",
                    "content": f"Generation error: {e}",
                    "done": True,
                })

            # v2.1.8 #56 stall watchdog teardown. Runs at end of every
            # turn — normal completion, internal error, or stall. Stops
            # the watchdog's check loop so its task exits gracefully;
            # cancels the task as a fallback if stop() doesn't take
            # within 1.5s (the watchdog sleeps in 2.0s ticks so a
            # cancel-after-timeout is the right belt-and-suspenders).
            # Wrapped in try/except so a teardown failure can't kill
            # the WS handler — we'd rather have a tiny leaked task
            # than a dead chat.
            try:
                watchdog.stop()
                try:
                    await asyncio.wait_for(watchdog_task, timeout=1.5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    watchdog_task.cancel()
                # v2.1.8 #56: restore the ContextVar to its previous
                # value (None for the outer scope). Prevents the
                # next turn from accidentally seeing this turn's
                # already-stopped watchdog.
                try:
                    _current_watchdog.reset(_watchdog_token)
                except Exception:
                    pass
            except Exception as _wd_err:
                print(f"[STALL WATCHDOG] teardown error: {_wd_err}")

    except WebSocketDisconnect:
        # v2.1.8 cleanup: the previous version had `pass` followed by an
        # unreachable-looking `print(f"[WS Error] {e}")` where `e` was
        # undefined (no `as e` on this except clause). That print would
        # raise NameError on every disconnect, propagate out of ws_chat,
        # and get logged by Starlette as an unhandled coroutine error.
        # Latent bug from the Phase 2 splice. Now a clean pass — WS
        # disconnects are normal and don't need any error log.
        pass
    except Exception as e:
        print(f"[WS Error] {e}")


def _chunk_text(text: str, size: int = 4) -> list:
    """Break text into small chunks for a streaming-like effect."""
    return [text[i:i+size] for i in range(0, len(text), size)]


# Initialize memory logger (v2.1.2 fix: use BASE_DIR instead of hardcoded C: path)
# v2.1.4 Phase 0: use MEMORY_DIR from config.py. Previously wrote to
# BASE_DIR / "memory_log" (project folder) while the daemon reads from
# sage_data/memory_log -- two different files on disk. Now unified.
memory_logger = MemoryLogger(
    storage_dir=str(MEMORY_DIR),
    baseline_temp=0.5
)
print(f"[MEMORY LOGGER] Initialized at: {MEMORY_DIR}")
print(f"[MEMORY LOGGER] Chain head: {memory_logger.chain_head[:16]}...")
print(f"[MEMORY LOGGER] Entries on disk: {memory_logger.count_entries()}")

# v2.1.4: enable the previously-dormant procedural memory, wired to the
# memory_logger so successful procedures get chain-witnessed for provenance.
# Unsuccessful procedures are stored locally only (dead-end cache, no chain
# noise). See procedural_memory.verify_procedure_provenance() for the
# verification path.
procedural = ProceduralMemory(
    storage_dir=str(PROCEDURAL_DIR),
    memory_logger=memory_logger,
)
print(f"[PROCEDURAL] Initialized at: {PROCEDURAL_DIR}")
print(f"[PROCEDURAL] Successful: {len(procedural.list_procedures('successful'))} | "
      f"Unsuccessful: {len(procedural.list_procedures('unsuccessful'))}")

# v2.1.10 #44 — AIQNudge singleton. Key auto-created on first instantiation;
# watch directory is sage_data/nudges/. Failure to initialise is NON-FATAL:
# we log and set aiq_nudge to None so the agentic loop skips nudge checks
# entirely. The HMAC channel is opt-in via config["aiq_nudge_enabled"], so
# users who don't use it shouldn't see any noise here.
try:
    from secret_locator import resolve_secret_file as _rsf
    aiq_nudge = AIQNudge(
        key_file=_rsf(".aiq_nudge_key", DATA_DIR,
                      Path(__file__).resolve().parent),
        watch_dir=DATA_DIR / "nudges",
    )
    print(f"[AIQ_NUDGE] Initialised. Watch dir: {DATA_DIR / 'nudges'}")
    print(f"[AIQ_NUDGE] enabled in config: {config.get('aiq_nudge_enabled', False)}")
except NudgeError as _aiq_err:
    print(f"[AIQ_NUDGE] disabled due to setup error: {_aiq_err}")
    aiq_nudge = None
except Exception as _aiq_err:
    print(f"[AIQ_NUDGE] disabled due to unexpected error: {_aiq_err}")
    aiq_nudge = None

if __name__ == "__main__":
    uvicorn.run(
        "main:app", host=config.get("host", "127.0.0.1"), port=8000,
        reload=False, log_level="warning",
    )


# ===============================================================================
#  IPC BRIDGE — VISIBLE BROWSER LAUNCHER — v2.1.2 surgical addition
# ===============================================================================
# Adds a /api/launch-browser HTTP endpoint that spawns browser_tool.py
# as a separate visible process so the user can watch Sage browse in real time.
# The visible browser listens on localhost TCP 9999 via its built-in QTcpServer;
# browser_tool.py sends "navigate" and "search" IPC messages to it (best-effort,
# silent no-op when the visible browser is not running).
#
# This block is purely additive. It does NOT modify any existing function,
# route, config, or class. Nothing above this line has been changed.

import subprocess as _ipc_subprocess  # aliased to avoid any name collision

# Path to the visible browser script, alongside this file in backend/
_BROWSER_SCRIPT = Path(__file__).parent / "browser_tool.py"

# Track the spawned process so repeated calls don't flood the system
_visible_browser_proc = None


def launch_visible_browser() -> dict:
    """
    Start browser_tool.py as a separate visible process if it isn't
    already running. Returns a small status dict; never raises.

    On Windows, uses pythonw.exe when available (suppresses a console window).
    On other platforms, uses the current sys.executable. If the script is
    missing or launch fails, returns a status dict with an error field
    instead of raising, so the /api/launch-browser endpoint stays stable.
    """
    global _visible_browser_proc

    # Already running? Don't double-spawn.
    if _visible_browser_proc is not None and _visible_browser_proc.poll() is None:
        return {
            "status": "already_running",
            "pid": _visible_browser_proc.pid,
            "script": str(_BROWSER_SCRIPT),
        }

    if not _BROWSER_SCRIPT.exists():
        return {
            "status": "error",
            "error": f"Browser script not found: {_BROWSER_SCRIPT}",
        }

    # Pick the right Python executable
    python_exe = sys.executable
    if sys.platform.startswith("win"):
        pythonw = python_exe.replace("python.exe", "pythonw.exe")
        if Path(pythonw).exists():
            python_exe = pythonw

    # Windows-specific: don't pop a console window for the child
    popen_kwargs = {}
    if sys.platform.startswith("win"):
        # CREATE_NO_WINDOW = 0x08000000
        popen_kwargs["creationflags"] = 0x08000000

    try:
        _visible_browser_proc = _ipc_subprocess.Popen(
            [python_exe, str(_BROWSER_SCRIPT)],
            cwd=str(_BROWSER_SCRIPT.parent),
            **popen_kwargs,
        )
        return {
            "status": "launched",
            "pid": _visible_browser_proc.pid,
            "script": str(_BROWSER_SCRIPT),
            "python": python_exe,
        }
    except Exception as e:
        _visible_browser_proc = None
        return {
            "status": "error",
            "error": f"Failed to launch visible browser: {e}",
            "script": str(_BROWSER_SCRIPT),
        }


@app.get("/api/launch-browser")
async def api_launch_browser():
    """
    Launch (or report already-running) the visible privacy browser.
    The visible browser listens on localhost:9999 and receives IPC mirror
    messages ('navigate', 'search') from browser_tool.py automatically.
    """
    return launch_visible_browser()


@app.post("/api/abort")
async def api_abort():
    """
    v2.1.4 stop-button fix: out-of-band HTTP abort.

    The WebSocket receive loop serializes messages — while a chat turn
    is streaming, await websocket.receive_json() is not being called,
    so a WS-delivered {"action":"abort"} sits in the queue until the
    current turn finishes (or hits max_steps). That's the 10-15 second
    delay the user observed.

    HTTP bypasses the WS queue entirely. The frontend POSTs here the
    moment Stop is clicked; we flip model_manager._abort synchronously
    and the next per-token check in generate()/_gen_ollama/_gen_llama
    aborts the stream. Returns immediately.
    """
    model_manager.abort()
    return {"status": "aborted"}


