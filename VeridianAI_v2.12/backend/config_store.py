#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config_store.py — OracleAI unified config layer (#68, schema v2)
-----------------------------------------------------------------
Single source of truth for user-tunable runtime configuration.

This module owns config.json. config.py owns *structural* concerns
(paths, log filenames, llama-server command builder, ctx-tier compute
helpers, FERNET_KEY_FILE). The two compose: when a path field in
OracleConfig is null, the accessor falls through to the matching
constant in config.py.

Migration story:
    - schema_version: 2  (this file)
    - schema_version: 1  (old flat dict, your existing config.json)
    - Missing key       (very first boot; treated as v1 empty dict)
The migrator (Phase D, migrate_config.py) handles v1 → v2 with backups.

Backward compatibility:
    OracleConfig.to_flat_dict() returns the old flat shape so call sites
    that do `cfg.get("temperature", 0.5)` keep working unchanged during
    Phase E's gradual migration. Once all read sites are on typed
    accessors we can deprecate the flat view.

Safety rules followed (project constants — see MEMORY.md):
    - DOES NOT touch Fernet key, hash chain, procedural memory, or any
      memory-integrity surface. This layer only reads/writes config.json.
    - All writes are atomic (tmp file + os.replace) so a crash mid-save
      can't corrupt the user's config.
    - All defaults are distribution-safe (no Todd-specific paths/keys).
    - Schema validation rejects unknown keys at /api/config boundary
      (defense against silent typos).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict, fields, is_dataclass
from pathlib import Path
from typing import Any, Optional, Dict, Tuple
import json
import os
import tempfile


# --- Schema version --------------------------------------------------------
# Bump this whenever the on-disk JSON shape changes in a non-backward-
# -compatible way. The migrator (Phase D) reads this to decide whether
# to run the upgrade transform.
SCHEMA_VERSION = 2


# --- Section dataclasses ---------------------------------------------------

@dataclass
class UISection:
    theme: str = "dark"                # "light" | "dark"
    haptic: bool = True


@dataclass
class ElectronWindow:
    width: int = 1280
    height: int = 820
    min_width: int = 900
    min_height: int = 600


@dataclass
class ElectronHealthProbe:
    # See electron/main.js #72 (Arc B580 driver tuning).
    poll_ms: int = 1500
    probe_timeout_ms: int = 5000
    total_timeout_ms: int = 90_000


@dataclass
class ElectronSection:
    backend_mode: str = "vulkan"       # "vulkan" | "ipex"
    window: ElectronWindow = field(default_factory=ElectronWindow)
    health_probe: ElectronHealthProbe = field(default_factory=ElectronHealthProbe)


@dataclass
class NetworkPorts:
    app: int = 8000
    ipc_browser: int = 9999
    sage_daemon: int = 9998
    ollama_oracle: int = 11434
    llama_sage: int = 11435
    llama_daemon: int = 11436
    llama_embed: int = 11437
    npu_llm: int = 11438       # v2.11.12: Ryzen AI NPU tier (Lemonade Server)


@dataclass
class NetworkSection:
    ports: NetworkPorts = field(default_factory=NetworkPorts)
    ollama_url: str = "http://localhost:11434"
    host: str = "127.0.0.1"
    # Sage network (node-to-node). OFF by default; LAN-only when enabled.
    node_server_enabled: bool = False
    node_name: str = ""
    remote_node_url: str = ""
    offload_enabled: bool = False
    multiuser_enabled: bool = False   # Phase 2: gate the app behind per-user login
    skill_share_enabled: bool = False  # Aether: serve/share signed skills (OFF)
    relay_server_enabled: bool = False  # Aether: host the relay broker (OFF)
    relay_source_enabled: bool = False  # Aether: serve my skills over a relay (OFF)
    relay_url: str = ""                 # relay to dial out to (client/source side)
    relay_peer_id: str = ""             # my id on the relay (so peers can address me)
    comfyui_autostart_enabled: bool = False  # spawn+reap ComfyUI with the app (OFF)
    comfyui_launch_cmd: str = ""        # explicit ComfyUI launcher; auto-detect if empty


@dataclass
class PathsSection:
    # null means "resolve from config.py defaults relative to project root."
    # Distribution-safe: a fresh install gets the canonical sage_data layout
    # without any Todd-specific overrides.
    models_dir: Optional[str] = None
    snapshots_dir: Optional[str] = None
    logs_dir: Optional[str] = None


@dataclass
class InferenceSection:
    backend: str = "ollama"
    default_model: Optional[str] = None
    secondary_model: str = ""
    tertiary_model: str = ""
    max_images_per_turn: int = 5
    build_battle_rounds: int = 1       # Build Battle: Critique & Refine rounds (1-3)
    auto_route: bool = True
    gpu_acceleration: bool = True
    # v2.11.12 hardware-acceleration toggles. These existed in the UI
    # (hardware.js) for a while but the keys were never allowlisted, so
    # every POST /api/config with them 400'd and nothing consumed them —
    # the switches were cosmetic. Now they persist here and are consumed:
    #   cuda/rocm/vulkan_enabled -> model_manager gates Ollama GPU offload
    #     (num_gpu: 0 when the machine's GPU brand is toggled off);
    #   npu_enabled -> gates the NPU tier (Lemonade Server on Ryzen AI):
    #     tier_launcher decides whether to spawn it at boot, and
    #     model_manager includes/excludes it from routing LIVE.
    cuda_enabled: bool = True
    rocm_enabled: bool = True
    vulkan_enabled: bool = True
    openvino_enabled: bool = True
    xe_cores_enabled: bool = True
    # v2.12.2 scaling: aging-fair request scheduler (request_scheduler.py).
    # aging_rate 0.05 == 40s provable max-wait ceiling ((3-1)/rate) — the
    # value the starvation sim validated (observed 22s max under sustained
    # over-capacity). queue_limit 24 ~= 3x the ~8-concurrent latency knee.
    scheduler_enabled: bool = True
    scheduler_aging_rate: float = 0.05
    scheduler_queue_limit: int = 24
    npu_enabled: bool = True
    n_gpu_layers: int = -1
    temperature: float = 0.5
    max_tokens: int = -1               # -1 = unlimited sentinel
    n_ctx: Optional[int] = None        # None = adaptive sizing


@dataclass
class ContextSizingSection:
    hard_cap_ctx: bool = True
    ctx_min: int = 8192
    ctx_response_headroom: int = 1500
    ctx_padding_factor: float = 1.0    # 1.0 = no effect (additive headroom is primary)


@dataclass
class TimeoutsSection:
    ollama_read_sec: int = 54000        # 15 hrs — covers cold-load + big prompts + complex research/architecture tasks
    stall_token_sec: int = 36000         # 10 hrs between tokens = stall
    stall_tool_sec: int = 36000          # 10 hrs for a tool result = stall


@dataclass
class PromptsSection:
    # Relative paths resolve from project root. Absolute paths used as-is.
    # The migrator (Phase D) writes the user's existing system_prompt blob
    # to this file the first time it runs.
    system_prompt_file: str = "../sage_data/prompts/system.txt"
    force_prompt_tier: Optional[str] = None   # null | "small" | "full"


@dataclass
class AiqNudgeSection:
    # AIQNudge HMAC-signed mid-run side-channel (#44).
    # Off by default for distribution safety; user opts in.
    enabled: bool = True
    watch_pattern: str = "nudge_*.txt"


@dataclass
class SageSection:
    sage_mode: bool = True
    agentic_mode: bool = True
    web_search_enabled: bool = True
    code_exec_enabled: bool = True
    privacy_mode: bool = False
    # v2.12.1 personalization: the assistant's NAME (persona self-reference)
    # and the voice/socials WAKE WORD. assistant_name is per-user capable
    # (PER_USER_KEYS in main.py) so each profile can name their own Toga;
    # voice_wake_word stays owner-level — there's one microphone.
    assistant_name: str = "Toga"
    voice_wake_word: str = "Toga"
    # v2.12.8 session provenance: when True, loading an archived chat appends
    # a persistent "=== SESSION BOUNDARY ===" system marker to the restored
    # history so the model can distinguish a reloaded prior session from a
    # live continuous one (fixes source-misattribution after restarts).
    # The marker text is fixed at load time and persisted with the history,
    # so it is KV-cache-stable (never regenerated per turn).
    session_boundary_markers: bool = True


@dataclass
class HandoffSecuritySection:
    # CRAIID handoff hardening (#69). Cadence thresholds feed the rapid-
    # rotation alarm; toggles default to the SAFE values so a fresh install
    # is hardened out of the box without any config.json edits.
    cadence_max: int = 5                 # triggers within the window ...
    cadence_window_sec: float = 300.0    # ... before the alarm fires
    require_socket_auth: bool = False    # F5: token handshake on 9998 (opt-in)
    verify_respawn_hash: bool = True     # F2: hash-check respawn target
    strict_respawn: bool = False         # F2: refuse (vs warn) on hash change


@dataclass
class OracleConfig:
    schema_version: int = SCHEMA_VERSION
    ui: UISection = field(default_factory=UISection)
    electron: ElectronSection = field(default_factory=ElectronSection)
    network: NetworkSection = field(default_factory=NetworkSection)
    paths: PathsSection = field(default_factory=PathsSection)
    inference: InferenceSection = field(default_factory=InferenceSection)
    context_sizing: ContextSizingSection = field(default_factory=ContextSizingSection)
    timeouts: TimeoutsSection = field(default_factory=TimeoutsSection)
    prompts: PromptsSection = field(default_factory=PromptsSection)
    aiq_nudge: AiqNudgeSection = field(default_factory=AiqNudgeSection)
    sage: SageSection = field(default_factory=SageSection)
    handoff_security: HandoffSecuritySection = field(default_factory=HandoffSecuritySection)

    # --- Persistence ---------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "OracleConfig":
        """Load config from disk. Handles three cases:

            1. File missing: return all defaults (first boot).
            2. File present, schema_version == SCHEMA_VERSION: parse v2 nested.
            3. File present, no schema_version OR schema_version < 2: treat as
               v1 flat shape, convert via from_flat_dict().

        Any parse failure returns defaults (logged to stderr). The file is
        NEVER deleted or overwritten by load() — that's save()'s job.
        """
        if not path.exists():
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[config_store] WARN: could not read {path}: {e}; using defaults",
                  flush=True)
            return cls()
        if not isinstance(raw, dict):
            print(f"[config_store] WARN: {path} is not a JSON object; using defaults",
                  flush=True)
            return cls()

        ver = raw.get("schema_version")
        if ver == SCHEMA_VERSION:
            return cls._from_nested_dict(raw)
        # Anything else is treated as the old v1 flat shape. The migrator
        # writes v2 the next time save() runs, so v1 files self-upgrade.
        return cls.from_flat_dict(raw)

    def save(self, path: Path) -> None:
        """Atomic write: tmp file in the same directory, then os.replace().
        A crash before os.replace leaves the original file intact."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_nested_dict()
        # tempfile in the SAME directory so os.replace is a same-filesystem
        # rename (atomic on Windows + POSIX). delete=False because we'll
        # rename it; encoding-mode=w-text is fine for JSON.
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=path.name + ".",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(path))
        except Exception:
            # Clean up the tmp file on failure so we don't litter
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # --- Serialization helpers -----------------------------------------

    def to_nested_dict(self) -> dict:
        """Return the nested v2 shape suitable for json.dump."""
        return asdict(self)

    # --- Prompt file I/O helpers ----------------------------------------
    # The system prompt lives in a real text file (path stored under
    # prompts.system_prompt_file). These helpers are the canonical readers
    # and writer used by main.py's GET/POST /api/prompts/system endpoints
    # and by the inference path on every chat turn.
    #
    # History: introduced in #68 Phase E Step 0 as a compat shim that
    # exposed file content under a "system_prompt" key in to_flat_dict.
    # Step 6 removed the shim — the key is no longer in the flat dict and
    # /api/config rejects it — but the helpers themselves graduated into
    # the public API surface.

    def _resolve_prompt_path(self) -> Path:
        """Resolve prompts.system_prompt_file to an absolute Path.
        Relative paths resolve from project root (one level up from this
        file's directory)."""
        p = Path(self.prompts.system_prompt_file)
        if p.is_absolute():
            return p
        project_root = Path(__file__).resolve().parent.parent
        return (project_root / p).resolve()

    def _read_prompt_file(self) -> str:
        """Read the system prompt from disk. Best-effort: returns empty
        string if missing or unreadable. Never raises — a missing prompt
        file MUST NOT block config load."""
        try:
            path = self._resolve_prompt_path()
            if path.exists() and path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"[config_store] WARN: could not read prompt file: {e}", flush=True)
        return ""

    def _write_prompt_file(self, text: str) -> None:
        """Write the system prompt to the file at prompts.system_prompt_file.
        Best-effort: failure logs but does not raise (this is a compat
        write path, not a structural one). Trailing newline normalized."""
        try:
            path = self._resolve_prompt_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text((text or "").rstrip() + "\n", encoding="utf-8")
        except OSError as e:
            print(f"[config_store] WARN: could not write prompt file: {e}", flush=True)

    def to_flat_dict(self) -> dict:
        """Backward-compat: return a flat dict matching the OLD v1 shape so
        existing call sites (config.get('temperature')) keep working.

        This is a READ view only — writing back via the flat shape requires
        going through /api/config which maps flat-style payloads onto the
        nested schema before storing.
        """
        return {
            # ui
            "theme":              self.ui.theme,
            "haptic":             self.ui.haptic,
            # electron
            "backend_mode":       self.electron.backend_mode,
            # network
            "ollama_url":         self.network.ollama_url,
            "host":               self.network.host,
            "node_server_enabled": self.network.node_server_enabled,
            "node_name":          self.network.node_name,
            "remote_node_url":    self.network.remote_node_url,
            "offload_enabled":    self.network.offload_enabled,
            "multiuser_enabled":  self.network.multiuser_enabled,
            "skill_share_enabled": self.network.skill_share_enabled,
            "relay_server_enabled": self.network.relay_server_enabled,
            "relay_source_enabled": self.network.relay_source_enabled,
            "relay_url":          self.network.relay_url,
            "relay_peer_id":      self.network.relay_peer_id,
            "comfyui_autostart_enabled": self.network.comfyui_autostart_enabled,
            "comfyui_launch_cmd": self.network.comfyui_launch_cmd,
            # paths (None = let downstream code resolve from config.py)
            "models_dir":         self.paths.models_dir,
            # inference
            "backend":            self.inference.backend,
            "default_model":      self.inference.default_model,
            "secondary_model":    self.inference.secondary_model,
            "tertiary_model":     self.inference.tertiary_model,
            "max_images_per_turn": self.inference.max_images_per_turn,
            "build_battle_rounds": self.inference.build_battle_rounds,
            "auto_route":         self.inference.auto_route,
            # v2.12.2 aging-fair scheduler knobs
            "scheduler_enabled":  self.inference.scheduler_enabled,
            "scheduler_aging_rate": self.inference.scheduler_aging_rate,
            "scheduler_queue_limit": self.inference.scheduler_queue_limit,
            "gpu_acceleration":   self.inference.gpu_acceleration,
            # v2.11.12 hardware-acceleration toggles (see InferenceSection)
            "cuda_enabled":       self.inference.cuda_enabled,
            "rocm_enabled":       self.inference.rocm_enabled,
            "vulkan_enabled":     self.inference.vulkan_enabled,
            "openvino_enabled":   self.inference.openvino_enabled,
            "xe_cores_enabled":   self.inference.xe_cores_enabled,
            "npu_enabled":        self.inference.npu_enabled,
            "n_gpu_layers":       self.inference.n_gpu_layers,
            "temperature":        self.inference.temperature,
            "max_tokens":         self.inference.max_tokens,
            "n_ctx":              self.inference.n_ctx,
            # context_sizing
            "hard_cap_ctx":           self.context_sizing.hard_cap_ctx,
            "ctx_min":                self.context_sizing.ctx_min,
            "ctx_response_headroom":  self.context_sizing.ctx_response_headroom,
            "ctx_padding_factor":     self.context_sizing.ctx_padding_factor,
            # timeouts
            "ollama_read_timeout_sec":  self.timeouts.ollama_read_sec,
            "stall_token_timeout_sec":  self.timeouts.stall_token_sec,
            "stall_tool_timeout_sec":   self.timeouts.stall_tool_sec,
            # prompts
            "system_prompt_file":  self.prompts.system_prompt_file,
            "force_prompt_tier":   self.prompts.force_prompt_tier,
            # #68 Phase E Step 6 (this commit): the legacy "system_prompt"
            # key was here as a compat shim that read the prompt file's
            # content. It's gone now — settings.js uses GET/POST
            # /api/prompts/system instead. validate_flat_payload no
            # longer accepts "system_prompt" in POST /api/config bodies.
            # aiq_nudge
            "aiq_nudge_enabled":       self.aiq_nudge.enabled,
            "aiq_nudge_watch_pattern": self.aiq_nudge.watch_pattern,
            # sage
            "sage_mode":               self.sage.sage_mode,
            "agentic_mode":            self.sage.agentic_mode,
            "web_search_enabled":      self.sage.web_search_enabled,
            "code_exec_enabled":       self.sage.code_exec_enabled,
            "privacy_mode":            self.sage.privacy_mode,
            # v2.12.1 personalization
            "assistant_name":          self.sage.assistant_name,
            "voice_wake_word":         self.sage.voice_wake_word,
            # v2.12.8 session provenance
            "session_boundary_markers": self.sage.session_boundary_markers,
        }

    @classmethod
    def from_flat_dict(cls, flat: dict) -> "OracleConfig":
        """Migrator entry point: takes a v1-shape flat dict, returns v2.

        Unknown keys in the input are SILENTLY DROPPED here (the migrator
        explicitly does not preserve garbage). Known keys whose names
        changed (e.g. ollama_read_timeout_sec → timeouts.ollama_read_sec)
        are remapped. Missing keys get dataclass defaults.

        The reverse of to_flat_dict for known keys, plus a handful of
        v1-only key renames documented inline.
        """
        cfg = cls()

        def _g(key, default):
            v = flat.get(key, default)
            return v if v is not None else default

        # ui
        cfg.ui.theme  = str(_g("theme",  cfg.ui.theme))
        cfg.ui.haptic = bool(_g("haptic", cfg.ui.haptic))

        # electron (v1 had only "backend_mode" at top level; window/probe new)
        if "backend_mode" in flat:
            cfg.electron.backend_mode = str(flat["backend_mode"]).lower()

        # network (v1 had no ports section; preserve ollama_url override)
        if "ollama_url" in flat and flat["ollama_url"]:
            cfg.network.ollama_url = str(flat["ollama_url"])
        if "host" in flat and flat["host"]:
            cfg.network.host = str(flat["host"])
        if "node_server_enabled" in flat:
            cfg.network.node_server_enabled = bool(flat["node_server_enabled"])
        if "node_name" in flat:
            cfg.network.node_name = str(flat["node_name"] or "")
        if "remote_node_url" in flat:
            cfg.network.remote_node_url = str(flat["remote_node_url"] or "")
        if "offload_enabled" in flat:
            cfg.network.offload_enabled = bool(flat["offload_enabled"])
        if "multiuser_enabled" in flat:
            cfg.network.multiuser_enabled = bool(flat["multiuser_enabled"])
        if "skill_share_enabled" in flat:
            cfg.network.skill_share_enabled = bool(flat["skill_share_enabled"])
        if "relay_server_enabled" in flat:
            cfg.network.relay_server_enabled = bool(flat["relay_server_enabled"])
        if "relay_source_enabled" in flat:
            cfg.network.relay_source_enabled = bool(flat["relay_source_enabled"])
        if "relay_url" in flat:
            cfg.network.relay_url = str(flat["relay_url"] or "")
        if "relay_peer_id" in flat:
            cfg.network.relay_peer_id = str(flat["relay_peer_id"] or "")
        if "comfyui_autostart_enabled" in flat:
            cfg.network.comfyui_autostart_enabled = bool(flat["comfyui_autostart_enabled"])
        if "comfyui_launch_cmd" in flat:
            cfg.network.comfyui_launch_cmd = str(flat["comfyui_launch_cmd"] or "")

        # paths — drop Todd-specific models_dir override unless absolute & valid
        # The decision was: migrator sets paths.models_dir to null. If user
        # had a working absolute override they want to keep, they can re-add
        # it manually post-migration.
        cfg.paths.models_dir = None

        # inference
        cfg.inference.backend          = str(_g("backend", cfg.inference.backend))
        cfg.inference.default_model    = flat.get("default_model") or None
        cfg.inference.secondary_model  = str(_g("secondary_model", cfg.inference.secondary_model))
        cfg.inference.tertiary_model   = str(_g("tertiary_model", cfg.inference.tertiary_model))
        cfg.inference.max_images_per_turn = int(_g("max_images_per_turn", cfg.inference.max_images_per_turn))
        cfg.inference.build_battle_rounds = int(_g("build_battle_rounds", cfg.inference.build_battle_rounds))
        cfg.inference.auto_route       = bool(_g("auto_route", cfg.inference.auto_route))
        # v2.12.2 aging-fair scheduler knobs
        cfg.inference.scheduler_enabled = bool(_g("scheduler_enabled", cfg.inference.scheduler_enabled))
        try:
            cfg.inference.scheduler_aging_rate = max(
                0.001, float(_g("scheduler_aging_rate", cfg.inference.scheduler_aging_rate)))
        except (TypeError, ValueError):
            pass
        try:
            cfg.inference.scheduler_queue_limit = max(
                1, int(_g("scheduler_queue_limit", cfg.inference.scheduler_queue_limit)))
        except (TypeError, ValueError):
            pass
        cfg.inference.gpu_acceleration = bool(_g("gpu_acceleration", cfg.inference.gpu_acceleration))
        # v2.11.12 hardware-acceleration toggles
        cfg.inference.cuda_enabled     = bool(_g("cuda_enabled", cfg.inference.cuda_enabled))
        cfg.inference.rocm_enabled     = bool(_g("rocm_enabled", cfg.inference.rocm_enabled))
        cfg.inference.vulkan_enabled   = bool(_g("vulkan_enabled", cfg.inference.vulkan_enabled))
        cfg.inference.openvino_enabled = bool(_g("openvino_enabled", cfg.inference.openvino_enabled))
        cfg.inference.xe_cores_enabled = bool(_g("xe_cores_enabled", cfg.inference.xe_cores_enabled))
        cfg.inference.npu_enabled      = bool(_g("npu_enabled", cfg.inference.npu_enabled))
        cfg.inference.n_gpu_layers     = int(_g("n_gpu_layers", cfg.inference.n_gpu_layers))
        cfg.inference.temperature      = float(_g("temperature", cfg.inference.temperature))
        cfg.inference.max_tokens       = _sanitize_max_tokens(_g("max_tokens", -1))
        n_ctx = flat.get("n_ctx")
        cfg.inference.n_ctx = int(n_ctx) if (n_ctx is not None and n_ctx > 0) else None

        # context_sizing
        cfg.context_sizing.hard_cap_ctx          = bool(_g("hard_cap_ctx", cfg.context_sizing.hard_cap_ctx))
        cfg.context_sizing.ctx_min               = int(_g("ctx_min", cfg.context_sizing.ctx_min))
        cfg.context_sizing.ctx_response_headroom = int(_g("ctx_response_headroom", cfg.context_sizing.ctx_response_headroom))
        cfg.context_sizing.ctx_padding_factor    = float(_g("ctx_padding_factor", cfg.context_sizing.ctx_padding_factor))

        # timeouts — key renames: ollama_read_timeout_sec → ollama_read_sec, etc.
        cfg.timeouts.ollama_read_sec  = int(_g("ollama_read_timeout_sec", cfg.timeouts.ollama_read_sec))
        cfg.timeouts.stall_token_sec  = int(_g("stall_token_timeout_sec", cfg.timeouts.stall_token_sec))
        cfg.timeouts.stall_tool_sec   = int(_g("stall_tool_timeout_sec",  cfg.timeouts.stall_tool_sec))

        # prompts — v1 stored full system_prompt as inline string. The
        # migrator (Phase D) writes that string to a file and stores the
        # path here. from_flat_dict ITSELF doesn't write the file because
        # this method might be called in contexts where filesystem writes
        # aren't appropriate (e.g. /api/config payload validation).
        if "system_prompt_file" in flat and flat["system_prompt_file"]:
            cfg.prompts.system_prompt_file = str(flat["system_prompt_file"])
        cfg.prompts.force_prompt_tier = flat.get("force_prompt_tier") or None

        # #68 Phase E Step 6: legacy "system_prompt" inline-text handling
        # is removed. v1 config.json files that still carry that key get
        # their prompt extracted to the file by migrate_config.py during
        # the v1->v2 transition (see _extract_system_prompt). Any
        # post-migration POSTs to /api/config that include "system_prompt"
        # are now rejected by validate_flat_payload (the key is not in
        # to_flat_dict() anymore, so it's not allowlisted). settings.js
        # is wired to GET/POST /api/prompts/system instead.

        # aiq_nudge — key renames: aiq_nudge_enabled → aiq_nudge.enabled, etc.
        cfg.aiq_nudge.enabled       = bool(_g("aiq_nudge_enabled",       cfg.aiq_nudge.enabled))
        cfg.aiq_nudge.watch_pattern = str(_g("aiq_nudge_watch_pattern",  cfg.aiq_nudge.watch_pattern))

        # sage
        cfg.sage.sage_mode          = bool(_g("sage_mode",          cfg.sage.sage_mode))
        cfg.sage.agentic_mode       = bool(_g("agentic_mode",       cfg.sage.agentic_mode))
        cfg.sage.web_search_enabled = bool(_g("web_search_enabled", cfg.sage.web_search_enabled))
        cfg.sage.code_exec_enabled  = bool(_g("code_exec_enabled",  cfg.sage.code_exec_enabled))
        cfg.sage.privacy_mode       = bool(_g("privacy_mode",       cfg.sage.privacy_mode))
        # v2.12.1 personalization — sanitized: printable, no quotes/brackets
        # (they'd break prompt framing and tag parsing), max 24 chars.
        def _clean_name(v, fallback):
            try:
                s = "".join(ch for ch in str(v) if ch.isprintable()
                            and ch not in '"\'[]{}<>|')
                s = s.strip()[:24]
                return s or fallback
            except Exception:
                return fallback
        cfg.sage.assistant_name  = _clean_name(_g("assistant_name",  cfg.sage.assistant_name), "Toga")
        cfg.sage.voice_wake_word = _clean_name(_g("voice_wake_word", cfg.sage.voice_wake_word), "Toga")
        # v2.12.8 session provenance
        cfg.sage.session_boundary_markers = bool(
            _g("session_boundary_markers", cfg.sage.session_boundary_markers))

        return cfg

    @classmethod
    def _from_nested_dict(cls, raw: dict) -> "OracleConfig":
        """Parse a v2 nested dict back into an OracleConfig. Unknown keys
        at any level are silently ignored (forward-compat for v2.x minor
        bumps that add fields). Missing keys get dataclass defaults."""
        cfg = cls()
        _hydrate_section(cfg.ui,              raw.get("ui", {}))
        _hydrate_section(cfg.electron.window, raw.get("electron", {}).get("window", {}))
        _hydrate_section(cfg.electron.health_probe, raw.get("electron", {}).get("health_probe", {}))
        # electron.backend_mode is a sibling, not nested deeper
        if "electron" in raw and isinstance(raw["electron"], dict):
            if "backend_mode" in raw["electron"]:
                cfg.electron.backend_mode = str(raw["electron"]["backend_mode"]).lower()
        _hydrate_section(cfg.network.ports,   raw.get("network", {}).get("ports", {}))
        if "network" in raw and isinstance(raw["network"], dict):
            if "ollama_url" in raw["network"]:
                cfg.network.ollama_url = str(raw["network"]["ollama_url"])
            if "host" in raw["network"]:
                cfg.network.host = str(raw["network"]["host"])
            if "node_server_enabled" in raw["network"]:
                cfg.network.node_server_enabled = bool(raw["network"]["node_server_enabled"])
            if "node_name" in raw["network"]:
                cfg.network.node_name = str(raw["network"]["node_name"] or "")
            if "remote_node_url" in raw["network"]:
                cfg.network.remote_node_url = str(raw["network"]["remote_node_url"] or "")
            if "offload_enabled" in raw["network"]:
                cfg.network.offload_enabled = bool(raw["network"]["offload_enabled"])
            if "multiuser_enabled" in raw["network"]:
                cfg.network.multiuser_enabled = bool(raw["network"]["multiuser_enabled"])
            if "skill_share_enabled" in raw["network"]:
                cfg.network.skill_share_enabled = bool(raw["network"]["skill_share_enabled"])
            if "relay_server_enabled" in raw["network"]:
                cfg.network.relay_server_enabled = bool(raw["network"]["relay_server_enabled"])
            if "relay_source_enabled" in raw["network"]:
                cfg.network.relay_source_enabled = bool(raw["network"]["relay_source_enabled"])
            if "relay_url" in raw["network"]:
                cfg.network.relay_url = str(raw["network"]["relay_url"] or "")
            if "relay_peer_id" in raw["network"]:
                cfg.network.relay_peer_id = str(raw["network"]["relay_peer_id"] or "")
            if "comfyui_autostart_enabled" in raw["network"]:
                cfg.network.comfyui_autostart_enabled = bool(raw["network"]["comfyui_autostart_enabled"])
            if "comfyui_launch_cmd" in raw["network"]:
                cfg.network.comfyui_launch_cmd = str(raw["network"]["comfyui_launch_cmd"] or "")
        _hydrate_section(cfg.paths,           raw.get("paths", {}))
        _hydrate_section(cfg.inference,       raw.get("inference", {}))
        _hydrate_section(cfg.context_sizing,  raw.get("context_sizing", {}))
        _hydrate_section(cfg.timeouts,        raw.get("timeouts", {}))
        _hydrate_section(cfg.prompts,         raw.get("prompts", {}))
        _hydrate_section(cfg.aiq_nudge,       raw.get("aiq_nudge", {}))
        _hydrate_section(cfg.sage,            raw.get("sage", {}))
        _hydrate_section(cfg.handoff_security, raw.get("handoff_security", {}))
        # Final pass: max_tokens sanitizer (defense in depth, matches v1 behavior)
        cfg.inference.max_tokens = _sanitize_max_tokens(cfg.inference.max_tokens)
        return cfg


# --- Sanitizers ------------------------------------------------------------

def _sanitize_max_tokens(v) -> int:
    """v1's _sanitize_max_tokens, lifted here. Backend canonical sentinel
    for unlimited is -1. Any other non-positive value coerces to -1."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return -1
    if n == -1:
        return -1
    if n <= 0:
        return -1
    return n


def _hydrate_section(section_obj: Any, raw: dict) -> None:
    """Copy known fields from raw into section_obj in place. Unknown keys
    silently ignored (forward-compat). Type coercion is intentionally
    light — JSON gives us bool/int/float/str already; we only coerce when
    the field type and value type genuinely differ."""
    if not is_dataclass(section_obj) or not isinstance(raw, dict):
        return
    field_names = {f.name for f in fields(section_obj)}
    for k, v in raw.items():
        if k in field_names:
            setattr(section_obj, k, v)


# --- Allowed-key validation for /api/config --------------------------------

# Flat-key allowlist matches every key produced by to_flat_dict(). The
# /api/config POST handler uses this to reject typo'd or unknown payload
# keys with HTTP 400. Defense against silent typos (Bug 6 in audit).
_FLAT_ALLOWED_KEYS = frozenset(OracleConfig().to_flat_dict().keys())


def validate_flat_payload(payload: dict) -> Tuple[bool, Optional[str]]:
    """Return (ok, error_message). On success, error_message is None.
    On unknown key, returns (False, "unknown key: <name>").
    Empty payload is OK (no-op POST)."""
    if not isinstance(payload, dict):
        return False, "payload must be a JSON object"
    for k in payload.keys():
        if k not in _FLAT_ALLOWED_KEYS:
            return False, f"unknown config key: {k!r}"
    return True, None


# --- Singleton accessor (lazy) ---------------------------------------------

_singleton: Optional[OracleConfig] = None
_singleton_path: Optional[Path] = None


def get_config(path: Optional[Path] = None, force_reload: bool = False) -> OracleConfig:
    """Module-level lazy singleton. Most call sites get the cached instance;
    /api/config POST passes force_reload=True after writing to refresh.

    path is only honored on first load (or force_reload=True). Subsequent
    calls reuse the cached instance regardless of path arg.
    """
    global _singleton, _singleton_path
    if _singleton is None or force_reload:
        if path is None:
            # Default location: project root / config.json
            backend_dir = Path(__file__).resolve().parent
            path = backend_dir.parent / "config.json"
        _singleton = OracleConfig.load(path)
        _singleton_path = path
    return _singleton


def save_config(cfg: OracleConfig, path: Optional[Path] = None) -> None:
    """Save and refresh the singleton in one shot."""
    global _singleton, _singleton_path
    if path is None:
        path = _singleton_path or (Path(__file__).resolve().parent.parent / "config.json")
    cfg.save(path)
    _singleton = cfg
    _singleton_path = path


# --- Module-level smoke test -----------------------------------------------

if __name__ == "__main__":
    # Quick sanity check: defaults instantiate, roundtrip nested dict,
    # roundtrip flat dict. Run with `python config_store.py`.
    c = OracleConfig()
    nested = c.to_nested_dict()
    assert nested["schema_version"] == SCHEMA_VERSION
    assert nested["network"]["ports"]["app"] == 8000
    flat = c.to_flat_dict()
    assert flat["temperature"] == 0.5
    assert flat["max_tokens"] == -1
    c2 = OracleConfig.from_flat_dict(flat)
    assert c2.inference.temperature == 0.5
    assert c2.inference.max_tokens == -1
    c3 = OracleConfig._from_nested_dict(nested)
    assert c3.network.ports.app == 8000
    ok, err = validate_flat_payload({"temperature": 0.7})
    assert ok and err is None
    ok, err = validate_flat_payload({"nonsense_key": 1})
    assert not ok and "unknown" in err
    # #68 Phase E Step 6: system_prompt compat shim REMOVED.
    # to_flat_dict no longer surfaces "system_prompt" as a key, and
    # validate_flat_payload rejects it in POST /api/config bodies.
    # Callers must use GET/POST /api/prompts/system instead.
    assert "system_prompt" not in flat, "system_prompt should be gone from to_flat_dict"
    assert "system_prompt" not in _FLAT_ALLOWED_KEYS, "system_prompt should not be allowlisted anymore"
    ok, err = validate_flat_payload({"system_prompt": "anything"})
    assert not ok and "system_prompt" in (err or ""), "POST should reject system_prompt key"
    # _write_prompt_file / _read_prompt_file are still part of the API
    # surface (used by main.py's /api/prompts/system endpoints + inference
    # path) — keep verifying they work.
    import tempfile as _tf
    _tmp_dir = Path(_tf.mkdtemp())
    _tmp_path = _tmp_dir / "test_prompt.txt"
    c_io = OracleConfig()
    c_io.prompts.system_prompt_file = str(_tmp_path)
    c_io._write_prompt_file("HELLO WORLD")
    assert _tmp_path.exists(), "non-empty write should create file"
    assert c_io._read_prompt_file().strip() == "HELLO WORLD"
    c_io._write_prompt_file("")  # explicit clear must work via the API
    assert _tmp_path.exists(), "empty write should still leave file in place"
    assert c_io._read_prompt_file().strip() == "", "empty write should empty the file"
    _tmp_path.unlink()
    _tmp_dir.rmdir()
    print("config_store.py smoke test: OK")
