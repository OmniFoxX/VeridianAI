#!/usr/bin/env python3
r"""
DirectML image engine provisioner for OracleAI (AMD / Intel GPUs, Windows).
===========================================================================

ComfyUI's official Windows portable ships Python 3.13, but Microsoft's
``torch-directml`` (the cross-vendor GPU path for AMD *and* Intel on Windows)
only publishes wheels up to **Python 3.12 / PyTorch 2.3.1**. So we cannot just
``pip install torch-directml`` into the NVIDIA portable.

This module provisions a SEPARATE, self-contained Python 3.12 environment that
runs the *already-installed* ComfyUI source with ``--directml``:

    <portable_parent>/python_directml/python.exe   (Python 3.12 + torch-directml)
    <portable_parent>/ComfyUI/ComfyUI/main.py       (the existing source, reused)

DESIGN
  * SEPARATE & non-destructive. The NVIDIA/CPU portable and its Python are never
    touched. NVIDIA users never reach this code (the endpoint + launcher gate it).
  * ENGINE-CHOICE-READY. This is one selectable image backend. CUDA/CPU is another;
    a future in-house engine is a third. The launcher picks among them by hardware
    + availability, so adding an engine later is additive, not a rewrite.
  * HONEST CEILING. torch-directml == torch 2.3.1 == ~2024-era ComfyUI features.
    Good for SD 1.5 / SDXL; Flux is not expected to work on DirectML.
  * FULLY DEFENSIVE. Every step returns a status dict with a clear, actionable
    message; nothing raises into the app. Idempotent + resumable where possible.
  * DISTRIBUTION-SAFE. Official python.org embeddable + official get-pip; no
    third-party forks, no hardcoded machine paths.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import urllib.request
import zipfile
from typing import Callable, Optional

# --------------------------------------------------------------------------- #
#  constants  (pinned, distribution-safe URLs)
# --------------------------------------------------------------------------- #
PY_EMBED_VERSION = "3.12.7"
PY_EMBED_URL = (f"https://www.python.org/ftp/python/{PY_EMBED_VERSION}/"
                f"python-{PY_EMBED_VERSION}-embed-amd64.zip")
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
DML_DIRNAME = "python_directml"   # sibling of the ComfyUI portable folder


def _noop(message: str, percent: int = -1):
    if percent >= 0:
        print(f"[directml] [{percent:3d}%] {message}")
    else:
        print(f"[directml] {message}")


# --------------------------------------------------------------------------- #
#  paths / detection
# --------------------------------------------------------------------------- #
def directml_root(comfy_home: str) -> Optional[str]:
    """Folder that holds the dedicated DirectML Python (sibling of the portable).
    comfy_home is the inner ComfyUI dir (where main.py lives)."""
    if not comfy_home:
        return None
    # comfy_home = <root>/ComfyUI/ComfyUI ; we want <root>/ComfyUI/python_directml
    # (i.e. a sibling of the portable's python_embeded, one level above main.py).
    parent = os.path.dirname(comfy_home.rstrip("\\/"))
    return os.path.join(parent, DML_DIRNAME)


def directml_python(comfy_home: str) -> Optional[str]:
    root = directml_root(comfy_home)
    return os.path.join(root, "python.exe") if root else None


def _can_import_directml(py: str) -> bool:
    try:
        if not py or not os.path.exists(py):
            return False
        r = subprocess.run([py, "-c", "import torch_directml"],
                           capture_output=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def is_provisioned(comfy_home: str) -> bool:
    """True when the DirectML env exists AND torch_directml imports in it."""
    py = directml_python(comfy_home)
    return bool(py) and os.path.exists(py) and _can_import_directml(py)


# --------------------------------------------------------------------------- #
#  download helper (streamed, with progress)
# --------------------------------------------------------------------------- #
def _download(url: str, dest: str, progress_cb: Callable, lo: int, hi: int,
              label: str) -> bool:
    """Stream `url` to `dest`, mapping progress into the [lo, hi] band."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OracleAI-Setup/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            last = lo
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total > 0:
                    pct = lo + int((done / total) * (hi - lo))
                    if pct > last:
                        progress_cb(f"{label}… {done // 1048576} / {total // 1048576} MB", pct)
                        last = pct
        return True
    except Exception as e:
        progress_cb(f"Download failed ({label}): {type(e).__name__}: {e}", -1)
        return False


def _enable_site(dml_root: str) -> None:
    """Uncomment 'import site' in pythonXYZ._pth so pip + site-packages work in
    the embeddable distribution (this is what the official portable does too)."""
    for fn in os.listdir(dml_root):
        if fn.lower().startswith("python") and fn.lower().endswith("._pth"):
            p = os.path.join(dml_root, fn)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
                out = []
                for ln in lines:
                    s = ln.strip()
                    if s in ("#import site", "# import site"):
                        out.append("import site")
                    else:
                        out.append(ln)
                if not any(l.strip() == "import site" for l in out):
                    out.append("import site")
                with open(p, "w", encoding="utf-8") as f:
                    f.write("\n".join(out) + "\n")
            except Exception:
                pass
            return


# --------------------------------------------------------------------------- #
#  main entry: provision the DirectML engine
# --------------------------------------------------------------------------- #
def provision_directml(comfy_home: str, progress_cb: Callable = _noop) -> dict:
    """Create a Python 3.12 + torch-directml environment that runs the EXISTING
    ComfyUI source with --directml. Caller MUST gate this off NVIDIA. Idempotent:
    re-running on a complete env is a fast no-op. Returns {success, python} or
    {success: False, error}. Never raises.
    """
    try:
        if not comfy_home or not os.path.isdir(comfy_home):
            return {"success": False, "error": f"ComfyUI not found at {comfy_home}."}

        req_file = os.path.join(comfy_home, "requirements.txt")
        if not os.path.exists(req_file):
            return {"success": False,
                    "error": "ComfyUI requirements.txt not found — install ComfyUI first."}

        root = directml_root(comfy_home)
        py = os.path.join(root, "python.exe")

        # Already done? (fast path)
        if os.path.exists(py) and _can_import_directml(py):
            progress_cb("DirectML engine already provisioned.", 100)
            return {"success": True, "python": py, "already_present": True}

        os.makedirs(root, exist_ok=True)

        # 1) Python 3.12 embeddable -------------------------------------------
        if not os.path.exists(py):
            progress_cb(f"Downloading Python {PY_EMBED_VERSION} (embeddable)…", 5)
            zip_path = os.path.join(root, "python_embed.zip")
            if not _download(PY_EMBED_URL, zip_path, progress_cb, 5, 15,
                             "Python 3.12"):
                return {"success": False,
                        "error": "Could not download the Python 3.12 embeddable package."}
            progress_cb("Extracting Python 3.12…", 16)
            try:
                with zipfile.ZipFile(zip_path) as z:
                    z.extractall(root)
                os.remove(zip_path)
            except Exception as e:
                return {"success": False, "error": f"Python extract failed: {e}"}
            _enable_site(root)

        if not os.path.exists(py):
            return {"success": False, "error": "Python 3.12 executable missing after extract."}

        # 2) bootstrap pip -----------------------------------------------------
        chk = subprocess.run([py, "-m", "pip", "--version"],
                             capture_output=True, text=True)
        if chk.returncode != 0:
            progress_cb("Bootstrapping pip…", 20)
            getpip = os.path.join(root, "get-pip.py")
            if not _download(GET_PIP_URL, getpip, progress_cb, 20, 24, "get-pip"):
                return {"success": False, "error": "Could not download get-pip.py."}
            r = subprocess.run([py, getpip, "--no-warn-script-location"],
                               capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                return {"success": False,
                        "error": f"pip bootstrap failed: {(r.stderr or '')[-600:]}"}

        # 3) torch-directml FIRST (pins torch 2.3.1 so step 4 won't upgrade it)
        progress_cb("Installing torch-directml (PyTorch 2.3.1 — several minutes)…", 30)
        r = subprocess.run([py, "-m", "pip", "install", "torch-directml"],
                           capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "")
            return {"success": False,
                    "error": f"torch-directml install failed: {err[-700:]}"}

        # 4) the rest of ComfyUI's requirements (torch already satisfied) ------
        progress_cb("Installing ComfyUI dependencies for DirectML…", 70)
        r = subprocess.run(
            [py, "-m", "pip", "install", "-r", req_file],
            capture_output=True, text=True, timeout=3600, cwd=comfy_home)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "")
            return {"success": False,
                    "error": f"ComfyUI dependency install failed: {err[-700:]}"}

        # 5) verify ------------------------------------------------------------
        progress_cb("Verifying DirectML…", 95)
        if not _can_import_directml(py):
            return {"success": False,
                    "error": "Provisioned, but torch_directml could not be imported."}

        progress_cb("DirectML engine ready.", 100)
        return {"success": True, "python": py}

    except Exception as e:
        return {"success": False,
                "error": f"DirectML provisioning failed: {type(e).__name__}: {e}"}


def remove_directml(comfy_home: str) -> dict:
    """Delete the DirectML env to reclaim space / start clean. Never raises."""
    try:
        root = directml_root(comfy_home)
        if root and os.path.isdir(root):
            shutil.rmtree(root, ignore_errors=True)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


# --------------------------------------------------------------------------- #
#  CLI diagnostic
# --------------------------------------------------------------------------- #
if __name__ == "__main__":  # pragma: no cover
    import sys
    home = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("COMFYUI_HOME", "")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print("provisioned:", is_provisioned(home))
        print("python:", directml_python(home))
    elif cmd == "provision":
        print(provision_directml(home))
    elif cmd == "remove":
        print(remove_directml(home))
    else:
        print("Usage: python comfyui_directml.py [status|provision|remove] <comfy_home>")
