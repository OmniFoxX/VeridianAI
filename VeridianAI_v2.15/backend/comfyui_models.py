#!/usr/bin/env python3
r"""
ComfyUI model catalog + downloader for OracleAI.
================================================
A small, curated set of text-to-image checkpoints a first-time user can pick
from, plus a defensive downloader that streams the chosen model into ComfyUI's
models/checkpoints folder.

DESIGN (mirrors comfyui_setup.py / comfyui_client.py discipline):
  * Distribution-safe: URLs are official Hugging Face 'resolve' links; nothing
    machine-specific. Sizes are advisory -- real progress uses Content-Length.
  * Fully defensive: every entry point returns a status dict and never raises
    into the app.
  * Architecture-aware: each model carries the generation params it expects
    (Flux schnell needs 4 steps / cfg 1.0), so the single ComfyUI workflow graph
    in comfyui_client.py renders SD1.5, SDXL and Flux alike -- no per-model graph.
  * NOTHING is hardcoded as a default. The user picks; we record the choice.
"""
from __future__ import annotations

import json
import os
import urllib.request
from net_guard import safe_urlopen
from typing import Callable, Optional

# Ordered light -> heavy so the picker can show them in that order.
MODEL_CATALOG = {
    "sd15": {
        "key":        "sd15",
        "label":      "Stable Diffusion 1.5",
        "blurb":      "Fast and light. Runs on modest GPUs (or CPU). A dependable all-rounder.",
        "size_label": "~2 GB",
        "vram_label": "~4 GB VRAM",
        "filename":   "v1-5-pruned-emaonly-fp16.safetensors",
        "url":        "https://huggingface.co/Comfy-Org/stable-diffusion-v1-5-archive/resolve/main/v1-5-pruned-emaonly-fp16.safetensors",
        "params":     {"width": 512, "height": 512, "steps": 25, "cfg": 7.0,
                       "sampler": "euler", "scheduler": "normal"},
    },
    "sdxl": {
        "key":        "sdxl",
        "label":      "Stable Diffusion XL",
        "blurb":      "Higher detail, 1024px native. Wants a capable GPU (~8 GB+ VRAM).",
        "size_label": "~6.5 GB",
        "vram_label": "~8 GB+ VRAM",
        "filename":   "sd_xl_base_1.0.safetensors",
        "url":        "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors",
        "params":     {"width": 1024, "height": 1024, "steps": 30, "cfg": 7.0,
                       "sampler": "euler", "scheduler": "normal"},
    },
    "flux-schnell": {
        "key":        "flux-schnell",
        "label":      "Flux.1 schnell (fp8)",
        "blurb":      "Top quality, 4-step. Best on a strong GPU with 12 GB+ VRAM. Large download.",
        "size_label": "~17 GB",
        "vram_label": "~12 GB+ VRAM",
        "filename":   "flux1-schnell-fp8.safetensors",
        "url":        "https://huggingface.co/Comfy-Org/flux1-schnell/resolve/main/flux1-schnell-fp8.safetensors",
        "params":     {"width": 1024, "height": 1024, "steps": 4, "cfg": 1.0,
                       "sampler": "euler", "scheduler": "simple"},
    },
}

CKPT_EXTS = (".safetensors", ".ckpt")


def _noop(message: str, percent: int = -1):
    if percent >= 0:
        print(f"[models] [{percent:3d}%] {message}")
    else:
        print(f"[models] {message}")


# --------------------------------------------------------------------------- #
#  paths / detection
# --------------------------------------------------------------------------- #
def checkpoints_dir(comfy_home: str) -> str:
    """ComfyUI/models/checkpoints under the app dir (where main.py lives)."""
    return os.path.join(comfy_home, "models", "checkpoints")


def list_installed(comfy_home: str) -> list:
    """Installed checkpoint filenames. Defensive: [] if the dir is missing."""
    try:
        d = checkpoints_dir(comfy_home)
        if not os.path.isdir(d):
            return []
        return sorted(f for f in os.listdir(d) if f.lower().endswith(CKPT_EXTS))
    except Exception:
        return []


def has_any(comfy_home: str) -> bool:
    return len(list_installed(comfy_home)) > 0


def catalog_public() -> list:
    """The catalog for the picker UI -- an ordered list of plain dicts."""
    return [dict(m) for m in MODEL_CATALOG.values()]


def params_for(checkpoint_or_key: Optional[str]) -> dict:
    """Generation params for a model identified by catalog key OR checkpoint
    filename. Empty dict if unknown (caller keeps its own defaults)."""
    if not checkpoint_or_key:
        return {}
    s = str(checkpoint_or_key)
    entry = MODEL_CATALOG.get(s)
    if entry:
        return dict(entry["params"])
    for entry in MODEL_CATALOG.values():
        if entry["filename"] == s:
            return dict(entry["params"])
    return {}


def key_for_checkpoint(checkpoint: str) -> str:
    """Catalog key whose filename matches `checkpoint`, or '' if not in catalog."""
    for k, m in MODEL_CATALOG.items():
        if m["filename"] == checkpoint:
            return k
    return ""


# --------------------------------------------------------------------------- #
#  persistent active-model selection
# --------------------------------------------------------------------------- #
# Stored in oracleai_config.json (the same raw store comfyui_setup writes) so it
# SURVIVES RESTARTS. OracleAI's main config (config.json) silently drops unknown
# keys via OracleConfig.from_flat_dict, so the choice cannot live there.
_SELECTION_CONFIG = "oracleai_config.json"


def _selection_config_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _SELECTION_CONFIG)


def get_selection() -> dict:
    """The persisted active-model choice: {'key':..., 'checkpoint':...} or {}."""
    try:
        p = _selection_config_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return {"key": cfg.get("comfyui_model_key", "") or "",
                    "checkpoint": cfg.get("comfyui_checkpoint", "") or ""}
    except Exception:
        pass
    return {}


def set_selection(key: str, checkpoint: str) -> bool:
    """Persist the active-model choice. Read-modify-write so we never clobber
    comfy_home / python_root that comfyui_setup stores in the same file."""
    try:
        p = _selection_config_path()
        cfg = {}
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
        cfg["comfyui_model_key"] = key or ""
        cfg["comfyui_checkpoint"] = checkpoint or ""
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        return True
    except Exception:
        return False


def delete_model(checkpoint: str, comfy_home: str) -> dict:
    """Delete an installed checkpoint file to reclaim disk. Safety: `checkpoint`
    must be a bare filename (no path separators / '..') and resolve to a real
    file inside the checkpoints dir. Returns {success}/{success:False,error}.
    Never raises."""
    try:
        if (not checkpoint or "/" in checkpoint or "\\" in checkpoint
                or ".." in checkpoint):
            return {"success": False, "error": "Invalid checkpoint name."}
        path = os.path.join(checkpoints_dir(comfy_home), checkpoint)
        if not os.path.isfile(path):
            return {"success": False, "error": "Checkpoint not found."}
        os.remove(path)
        return {"success": True, "filename": checkpoint}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
#  download
# --------------------------------------------------------------------------- #
def download_model(key: str, comfy_home: str,
                   progress_cb: Callable = _noop) -> dict:
    """Stream the chosen catalog model into ComfyUI/models/checkpoints.

    * Skips if already present and size-matched.
    * Atomic: writes to <file>.part then os.replace() on success.
    Returns {success, filename, path[, already_present]} or
    {success: False, error}. NEVER raises.
    """
    tmp = None
    try:
        entry = MODEL_CATALOG.get(key)
        if not entry:
            return {"success": False, "error": f"Unknown model '{key}'."}
        if not comfy_home or not os.path.isdir(comfy_home):
            return {"success": False, "error": f"ComfyUI home not found: {comfy_home}"}

        dest_dir = checkpoints_dir(comfy_home)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, entry["filename"])
        url = entry["url"]

        # Probe the expected size (for skip-check + a sane progress total).
        expected = 0
        try:
            head = urllib.request.Request(
                url, method="HEAD",
                headers={"User-Agent": "OracleAI-Setup/1.0"})
            with safe_urlopen(head, timeout=30) as r:
                expected = int(r.headers.get("Content-Length") or 0)
        except Exception:
            expected = 0

        # Already downloaded? (size within 1 MB of expected, or expected unknown)
        if os.path.exists(dest):
            have = os.path.getsize(dest)
            if expected <= 0 or abs(have - expected) < 1024 * 1024:
                progress_cb(f"{entry['label']} already installed.", 100)
                return {"success": True, "filename": entry["filename"],
                        "path": dest, "already_present": True}

        progress_cb(f"Downloading {entry['label']} ({entry['size_label']})...", 1)
        tmp = dest + ".part"
        req = urllib.request.Request(
            url, headers={"User-Agent": "OracleAI-Setup/1.0"})
        with safe_urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
            total = int(resp.headers.get("Content-Length") or expected or 0)
            done = 0
            last = 0
            block = 1024 * 512  # 512 KB chunks
            while True:
                chunk = resp.read(block)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total > 0:
                    pct = min(99, int(done / total * 100))
                    if pct > last:
                        mb, tmb = done / 1048576, total / 1048576
                        progress_cb(
                            f"Downloading {entry['label']}... {mb:.0f} / {tmb:.0f} MB",
                            pct)
                        last = pct
        os.replace(tmp, dest)
        progress_cb(f"{entry['label']} ready.", 100)
        return {"success": True, "filename": entry["filename"], "path": dest}
    except Exception as e:
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return {"success": False,
                "error": f"Model download failed: {type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
#  CLI diagnostic (python comfyui_models.py [list|download <key> <comfy_home>])
# --------------------------------------------------------------------------- #
if __name__ == "__main__":  # pragma: no cover
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        for m in catalog_public():
            print(f"{m['key']:14s} {m['label']:24s} {m['size_label']:8s} {m['vram_label']}")
    elif cmd == "download" and len(sys.argv) >= 4:
        print(download_model(sys.argv[2], sys.argv[3]))
    else:
        print("Usage: python comfyui_models.py [list | download <key> <comfy_home>]")
