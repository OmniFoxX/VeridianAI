#!/usr/bin/env python3
"""
ComfyUI text-to-image client for OracleAI (inbound image GENERATION).
====================================================================

Ollama/llama.cpp generate text + read images, but cannot CREATE images, so
generation runs on a separate Stable-Diffusion backend. This module drives a
local ComfyUI server over its HTTP API: build a txt2img workflow graph, POST it
to /prompt, poll /history until it finishes, fetch the PNG via /view, and save it
into OracleAI's downloads folder.

DESIGN:
  * Synchronous core (urllib) so it is trivial to unit-test with a mocked
    urlopen. Call it from async code via `asyncio.to_thread(generate_image, ...)`
    so a 10-60s render never blocks the event loop.
  * FULLY DEFENSIVE: ComfyUI down / timeout / bad checkpoint / malformed
    response all return {"success": False, "error": "..."} - this NEVER raises.
  * Config-driven, distribution-safe: every parameter falls back to an env var
    then a sane default; nothing is hardcoded to one machine. If no checkpoint
    is given, the first one ComfyUI reports via /object_info is used, so it works
    out of the box.
"""
from __future__ import annotations

import base64
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from net_guard import safe_urlopen
from pathlib import Path
from typing import Any, Dict, Optional


# --------------------------------------------------------------------------- #
#  defensive HTTP helpers (urllib; short, bounded timeouts)
# --------------------------------------------------------------------------- #
def _post_json(url: str, payload: dict, timeout: float) -> Optional[dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with safe_urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, timeout: float) -> Optional[dict]:
    with safe_urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_bytes(url: str, timeout: float) -> bytes:
    with safe_urlopen(url, timeout=timeout) as resp:
        return resp.read()


# --------------------------------------------------------------------------- #
#  config resolution (explicit arg -> env -> default)
# --------------------------------------------------------------------------- #
def _opt(explicit, env_name, default):
    if explicit is not None:
        return explicit
    v = os.environ.get(env_name)
    return v if v not in (None, "") else default


def _resolve_checkpoint(base: str, explicit: Optional[str], timeout: float):
    """Return (checkpoint_name, error). If explicit/env is set, use it. Else ask
    ComfyUI which checkpoints exist and take the first - so generation works with
    whatever model the user already has, and a missing-model case is explained."""
    ckpt = _opt(explicit, "COMFYUI_CHECKPOINT", None)
    if ckpt:
        return ckpt, None
    try:
        info = _get_json(base + "/object_info/CheckpointLoaderSimple", timeout)
        choices = (((info or {}).get("CheckpointLoaderSimple", {})
                    .get("input", {}).get("required", {})
                    .get("ckpt_name", [[]]))[0]) or []
        if choices:
            return choices[0], None
        return None, ("ComfyUI has no checkpoints installed. Add a model to "
                      "ComfyUI/models/checkpoints, or set COMFYUI_CHECKPOINT.")
    except Exception as e:
        return None, (f"Could not query ComfyUI for checkpoints ({type(e).__name__}). "
                      "Is ComfyUI running? Set COMFYUI_CHECKPOINT to skip discovery.")


# --------------------------------------------------------------------------- #
#  workflow graph (canonical ComfyUI txt2img, API format)
# --------------------------------------------------------------------------- #
def _build_workflow(prompt, negative, width, height, steps, cfg, sampler,
                    scheduler, seed, checkpoint, filename_prefix):
    return {
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": int(width), "height": int(height),
                         "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative or "", "clip": ["4", 1]}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": int(seed), "steps": int(steps),
                         "cfg": float(cfg), "sampler_name": sampler,
                         "scheduler": scheduler, "denoise": 1.0,
                         "model": ["4", 0], "positive": ["6", 0],
                         "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": filename_prefix, "images": ["8", 0]}},
    }


# --------------------------------------------------------------------------- #
#  public API
# --------------------------------------------------------------------------- #
def generate_image(prompt: str, *, negative: Optional[str] = None,
                   width=None, height=None, steps=None, cfg=None, sampler=None,
                   scheduler=None, seed=None, checkpoint=None, url=None,
                   downloads_dir=None, timeout=None,
                   poll_interval: float = 1.0) -> Dict[str, Any]:
    """Generate one image from `prompt` via a local ComfyUI server and save it to
    the downloads folder. Returns a result dict; NEVER raises.

    Success: {"success": True, "path", "filename", "data" (base64 PNG),
              "mimetype": "image/png", "seed", "prompt", "checkpoint"}
    Failure: {"success": False, "error": "<human-readable reason>"}
    """
    try:
        if not prompt or not str(prompt).strip():
            return {"success": False, "error": "empty image prompt"}
        prompt = str(prompt).strip()

        base = str(_opt(url, "COMFYUI_URL", "http://127.0.0.1:8188")).rstrip("/")
        width = int(_opt(width, "COMFYUI_WIDTH", 1024))
        height = int(_opt(height, "COMFYUI_HEIGHT", 1024))
        steps = int(_opt(steps, "COMFYUI_STEPS", 25))
        cfg = float(_opt(cfg, "COMFYUI_CFG", 7.0))
        sampler = str(_opt(sampler, "COMFYUI_SAMPLER", "euler"))
        scheduler = str(_opt(scheduler, "COMFYUI_SCHEDULER", "normal"))
        # 600s default: enough for a slower local GPU/CPU to return an image or a
        # clear error on reasonable hardware. Power users can override via env.
        timeout = float(_opt(timeout, "COMFYUI_TIMEOUT_SEC", 600))
        if seed is None:
            seed = random.randint(0, 2**32 - 1)
        ddir = Path(downloads_dir) if downloads_dir else \
            Path(_opt(None, "ORACLEAI_DOWNLOADS", str(Path(__file__).resolve().parent.parent / "downloads")))

        ckpt, ckpt_err = _resolve_checkpoint(base, checkpoint, min(timeout, 15))
        if not ckpt:
            return {"success": False, "error": ckpt_err}

        workflow = _build_workflow(prompt, negative, width, height, steps, cfg,
                                   sampler, scheduler, seed, ckpt, "OracleAI")
        client_id = str(_uuid())

        # 1) submit
        try:
            sub = _post_json(base + "/prompt",
                             {"prompt": workflow, "client_id": client_id},
                             min(timeout, 30))
        except urllib.error.URLError as e:
            return {"success": False,
                    "error": f"Could not reach ComfyUI at {base} ({e}). Is it running?"}
        if not isinstance(sub, dict) or not sub.get("prompt_id"):
            node_err = (sub or {}).get("node_errors") or sub
            return {"success": False, "error": f"ComfyUI rejected the workflow: {node_err}"}
        prompt_id = sub["prompt_id"]

        # 2) poll history until this prompt produces outputs
        deadline = time.time() + timeout
        outputs = None
        while time.time() < deadline:
            try:
                hist = _get_json(base + "/history/" + urllib.parse.quote(prompt_id),
                                 min(timeout, 30))
            except Exception:
                hist = None
            entry = (hist or {}).get(prompt_id)
            if entry and entry.get("outputs"):
                outputs = entry["outputs"]
                break
            time.sleep(poll_interval)
        if outputs is None:
            return {"success": False,
                    "error": f"Generation timed out after {int(timeout)}s "
                             f"(prompt still running or ComfyUI stalled)."}

        # 3) find the produced image
        img = None
        for node in outputs.values():
            for im in (node.get("images") or []):
                if im.get("filename"):
                    img = im
                    break
            if img:
                break
        if not img:
            return {"success": False, "error": "ComfyUI finished but returned no image."}

        # 4) fetch + save
        q = urllib.parse.urlencode({
            "filename": img["filename"],
            "subfolder": img.get("subfolder", ""),
            "type": img.get("type", "output"),
        })
        png = _get_bytes(base + "/view?" + q, min(timeout, 60))
        # Privacy (best-effort): drop this prompt+image from ComfyUI's /history so
        # it does not linger in the web UI / for others on a shared node. The
        # image is already fetched; any failure here is harmless.
        try:
            _dreq = urllib.request.Request(
                base + "/history",
                data=json.dumps({"delete": [prompt_id]}).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            safe_urlopen(_dreq, timeout=10).read()
        except Exception:
            pass
        ddir.mkdir(parents=True, exist_ok=True)
        out_name = f"gen_{int(time.time())}_{seed}.png"
        out_path = ddir / out_name
        import atrest
        with open(out_path, "wb") as f:
            f.write(atrest.encrypt_bytes(png))

        # Privacy: remove ComfyUI's PLAINTEXT original on disk so the only copy
        # left is the encrypted, per-user one in downloads. Best-effort: any
        # failure here must never break a successful generation.
        try:
            import comfyui_setup as _cs
            _home = _cs.detect_existing()
            if _home and img.get("filename"):
                _orig = (Path(_home) / (img.get("type") or "output")
                         / (img.get("subfolder") or "") / img["filename"])
                if _orig.exists():
                    _orig.unlink()
        except Exception:
            pass

        return {
            "success": True,
            "path": str(out_path),
            "filename": out_name,
            "data": base64.b64encode(png).decode("utf-8"),
            "mimetype": "image/png",
            "seed": seed,
            "prompt": prompt,
            "checkpoint": ckpt,
            "size": len(png),
        }
    except Exception as e:
        return {"success": False, "error": f"generation failed: {type(e).__name__}: {e}"}


def _uuid():
    import uuid
    return uuid.uuid4()


if __name__ == "__main__":  # pragma: no cover
    import sys
    p = " ".join(sys.argv[1:]) or "a watercolor fox in a snowy forest"
    print(json.dumps({k: (v[:40] + "..." if k == "data" and isinstance(v, str) else v)
                      for k, v in generate_image(p).items()}, indent=2))
