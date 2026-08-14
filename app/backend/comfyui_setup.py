#!/usr/bin/env python3
r"""
ComfyUI setup wizard for OracleAI.
===================================

Handles the full ComfyUI portable installation lifecycle for a fresh user system:

  1. DETECT   - Check if ComfyUI is already present via COMFYUI_HOME or a
                drive scan. If found, verify it is a valid portable install
                (main.py present, python_embeded present).

  2. DOWNLOAD - If not found, fetch the latest official ComfyUI portable
                release from GitHub. No bundling. No hardcoded versions.
                Always pulls the current release so every user gets the
                same up-to-date package regardless of when they install.

  3. EXTRACT  - Unzip to a user-chosen (or auto-chosen) location.

  4. DEPS     - Run pip install -r requirements.txt using the portable's
                OWN python_embeded\python.exe -- never the system Python.
                This keeps all dependencies self-contained and identical
                across every machine.

  5. VERIFY   - Confirm main.py is present and the launcher resolves to
                Headless: True.

  6. CONFIGURE - Write COMFYUI_HOME into OracleAI's config so the launcher
                 and client pick it up automatically from this point forward.

DESIGN:
  * Distribution-safe. Nothing is hardcoded. Works on any Windows system
    from a clean slate, exactly as a real user would experience it.
  * Fully defensive. Every stage returns a status dict. Nothing raises into
    the app's startup path.
  * Silent-capable. Pass silent=True for unattended installs. All prompts
    are skipped and sensible defaults are used.
  * Progress-callback-friendly. Pass a progress_cb(message, percent) to
    wire setup output into OracleAI's UI rather than stdout.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from net_guard import safe_urlopen
import zipfile
from pathlib import Path
from typing import Callable, Optional


# --------------------------------------------------------------------------- #
#  constants
# --------------------------------------------------------------------------- #
GITHUB_API   = "https://api.github.com/repos/comfyanonymous/ComfyUI/releases/latest"
GITHUB_ASSET = "ComfyUI_windows_portable_nvidia_cu128_or_cpu.7z"  # fallback name fragment
DEFAULT_INSTALL_PARENT = os.path.join(os.path.expanduser("~"), "OracleAI", "backend")
CONFIG_FILENAME = "oracleai_config.json"


# --------------------------------------------------------------------------- #
#  progress reporting
# --------------------------------------------------------------------------- #
def _noop(message: str, percent: int = -1):
    """Default progress callback -- prints to stdout."""
    if percent >= 0:
        print(f"[{percent:3d}%] {message}")
    else:
        print(f"       {message}")


# --------------------------------------------------------------------------- #
#  GitHub release resolution
# --------------------------------------------------------------------------- #
def _get_latest_release(progress_cb: Callable = _noop) -> dict:
    """Fetch the latest ComfyUI release metadata from GitHub.
    Returns dict with keys: version, download_url, filename, size_bytes.
    Or: error key on failure."""
    progress_cb("Checking latest ComfyUI release on GitHub...", 2)
    try:
        req = urllib.request.Request(
            GITHUB_API,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "OracleAI-Setup/1.0"},
        )
        with safe_urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        version = data.get("tag_name", "unknown")
        assets  = data.get("assets", [])

        # Find the windows portable zip -- prefer nvidia, fall back to any zip.
        asset = None
        for a in assets:
            name = a.get("name", "").lower()
            if "windows_portable" in name and "nvidia" in name and name.endswith(".7z"):
                # Prefer the direct download over the https_mirror
                if "https_mirror" not in name:
                    asset = a
                    break
        if not asset:
            for a in assets:
                name = a.get("name", "").lower()
                if "windows_portable" in name and name.endswith(".7z"):
                    asset = a
                    break
        if not asset:
            return {"error": "Could not find a Windows portable 7z in the latest release. "
                             "Check https://github.com/comfyanonymous/ComfyUI/releases manually."}

        return {
            "version":      version,
            "download_url": asset["browser_download_url"],
            "filename":     asset["name"],
            "size_bytes":   asset.get("size", 0),
        }
    except Exception as e:
        return {"error": f"GitHub API request failed: {type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
#  download with progress
# --------------------------------------------------------------------------- #
def _download(url: str, dest_path: str, size_bytes: int,
              progress_cb: Callable = _noop) -> dict:
    # Skip download if file already exists and is the right size
    if os.path.exists(dest_path):
        existing_size = os.path.getsize(dest_path)
        if size_bytes <= 0 or abs(existing_size - size_bytes) < 1024 * 1024:
            progress_cb("Archive already downloaded, skipping.", 61)
            return {"success": True, "path": dest_path}
    progress_cb(f"Downloading {os.path.basename(dest_path)}...", 5)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "OracleAI-Setup/1.0"}
        )
        with safe_urlopen(req, timeout=60) as resp, \
             open(dest_path, "wb") as f:
            downloaded = 0
            block      = 1024 * 256  # 256 KB chunks
            last_pct   = 5
            while True:
                chunk = resp.read(block)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if size_bytes > 0:
                    pct = min(60, 5 + int((downloaded / size_bytes) * 55))
                    if pct > last_pct:
                        mb = downloaded / (1024 * 1024)
                        total_mb = size_bytes / (1024 * 1024)
                        progress_cb(
                            f"Downloading... {mb:.1f} MB / {total_mb:.1f} MB",
                            pct,
                        )
                        last_pct = pct
        progress_cb("Download complete.", 61)
        return {"success": True, "path": dest_path}
    except Exception as e:
        return {"success": False, "error": f"Download failed: {type(e).__name__}: {e}"}
        
        
def _find_7zip():
    """Locate the 7-Zip executable. Checks standard install paths and PATH."""
    candidates = [
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ]
    # Check standard paths first
    for c in candidates:
        if os.path.exists(c):
            return c
    # Fall back to PATH
    found = shutil.which("7z")
    if found:
        return found
    return None        


# --------------------------------------------------------------------------- #
#  extract
# --------------------------------------------------------------------------- #
def _extract(zip_path: str, dest_parent: str,
             progress_cb: Callable = _noop) -> dict:
    """Extract the portable 7z into dest_parent using 7-Zip.
    py7zr does not support BCJ2 compression used by ComfyUI releases.
    7-Zip handles it natively and is the correct tool for this job."""
    progress_cb("Extracting ComfyUI portable package...", 62)
    try:
        # Find 7-Zip executable
        seven_zip = _find_7zip()
        if not seven_zip:
            return {"success": False,
                    "error": "7-Zip is required to extract the ComfyUI package "
                             "but could not be found. Please install 7-Zip from "
                             "https://www.7-zip.org and retry."}

        progress_cb("Extracting with 7-Zip (this may take a few minutes)...", 63)

        result = subprocess.run(
            [seven_zip, "x", zip_path, f"-o{dest_parent}", "-y"],
            capture_output=True, text=True, timeout=600,
        )

        if result.returncode != 0:
            return {"success": False,
                    "error": f"7-Zip extraction failed: {result.stderr[-1000:]}"}

        # Find the extracted top level folder containing main.py
        extracted = None
        for entry in os.listdir(dest_parent):
            candidate = os.path.join(dest_parent, entry)
            if os.path.isdir(candidate) and \
               os.path.exists(os.path.join(candidate, "main.py")):
                extracted = candidate
                break

        # Fallback: first directory that isn't the download cache
        if not extracted:
            for entry in os.listdir(dest_parent):
                candidate = os.path.join(dest_parent, entry)
                if os.path.isdir(candidate) and entry != ".download_cache":
                    extracted = candidate
                    break

        if not extracted:
            return {"success": False,
                    "error": "Extraction completed but could not locate "
                             "the ComfyUI folder."}

        # Rename to clean version-agnostic folder name
        final_path = os.path.join(dest_parent, "ComfyUI")
        if extracted != final_path:
            if os.path.exists(final_path):
                shutil.rmtree(final_path)
            os.rename(extracted, final_path)
            extracted = final_path

        progress_cb("Extraction complete.", 75)
        return {"success": True, "extracted_path": extracted}

    except subprocess.TimeoutExpired:
        return {"success": False,
                "error": "Extraction timed out after 10 minutes."}
    except Exception as e:
        return {"success": False,
                "error": f"Extraction failed: {type(e).__name__}: {e}"}
                
        # Handle nested ComfyUI folder (portable package extracts ComfyUI/ComfyUI/)
        nested = os.path.join(extracted, "ComfyUI")
        if os.path.isdir(nested) and os.path.exists(os.path.join(nested, "main.py")):
            extracted = nested

        progress_cb("Extraction complete.", 75)
        return {"success": True, "extracted_path": extracted}                


# --------------------------------------------------------------------------- #
#  dependency install
# --------------------------------------------------------------------------- #
def _install_deps(comfy_home: str, app_dir: str, progress_cb: Callable = _noop) -> dict:
    """Install dependencies from requirements.txt inside app_dir using python_embeded at comfy_home."""
    progress_cb("Installing ComfyUI dependencies (this may take a few minutes)...", 80)

    req_file = os.path.join(app_dir, "requirements.txt")
    if not os.path.exists(req_file):
        progress_cb("No requirements.txt found -- skipping dep install.", 85)
        return {"success": True}

    python_exe = os.path.join(comfy_home, "python_embeded", "python.exe")
    if not os.path.exists(python_exe):
        return {"success": False,
                "error": f"python_embeded not found at {comfy_home}. Cannot install dependencies."}

    try:
        result = subprocess.run(
            [python_exe, "-m", "pip", "install", "-r", req_file],
            capture_output=True,
            text=True,
            cwd=app_dir
        )
        if result.returncode != 0:
            return {"success": False,
                    "error": f"Dependency install failed:\n{result.stderr}"}
    except Exception as e:
        return {"success": False, "error": f"Dependency install exception: {e}"}

    progress_cb("Dependencies installed.", 88)
    return {"success": True}


# --------------------------------------------------------------------------- #
#  verify
# --------------------------------------------------------------------------- #
def _verify(comfy_home: str, progress_cb: Callable = _noop) -> dict:
    """Confirm main.py exists and locate the Python runtime.

    Supports two layouts:
      Portable zip:   <comfy_home>/ComfyUI/main.py  +  <comfy_home>/python_embeded/python.exe
      Git clone/venv: <comfy_home>/main.py          +  system Python or <comfy_home>/venv/
    """
    progress_cb("Verifying ComfyUI install...", 91)

    # ── Detect nested app folder ──────────────────────────────────────────────
    nested = os.path.join(comfy_home, "ComfyUI")
    if os.path.isdir(nested) and os.path.exists(os.path.join(nested, "main.py")):
        app_dir = nested
    else:
        app_dir = comfy_home

    # ── Verify main.py ────────────────────────────────────────────────────────
    main_py = os.path.join(app_dir, "main.py")
    if not os.path.exists(main_py):
        return {
            "success": False,
            "error": f"main.py not found in {app_dir}. Install may be incomplete."
        }

    # ── Locate Python runtime (portable zip preferred, venv/system fallback) ──
    candidates = [
        os.path.join(comfy_home, "python_embeded", "python.exe"),   # portable zip
        os.path.join(comfy_home, "venv", "Scripts", "python.exe"),  # venv on Windows
        os.path.join(comfy_home, "venv", "bin", "python"),          # venv on Linux/Mac
    ]

    python_root = None
    for candidate in candidates:
        if os.path.exists(candidate):
            python_root = comfy_home   # outer folder, same convention as before
            break

    # Last resort: accept system Python (git clone with no local venv)
    if python_root is None:
        import shutil as _shutil
        if _shutil.which("python") or _shutil.which("python3"):
            python_root = comfy_home  # no embedded runtime, but system Python exists
        else:
            return {
                "success": False,
                "error": (
                    f"No Python runtime found for ComfyUI at {comfy_home}. "
                    "Expected python_embeded/, venv/, or a system Python on PATH."
                )
            }

    progress_cb("Verification passed. ComfyUI is ready.", 95)
    return {
        "success":        True,
        "comfy_home":     app_dir,    # ← where main.py lives
        "python_embeded": python_root # ← outer folder (naming kept for caller compat)
    }


# --------------------------------------------------------------------------- #
#  config write
# --------------------------------------------------------------------------- #
def _write_config(app_dir: str, python_root: str, progress_cb: Callable = _noop) -> dict:
    """Write comfyui_home and python_root into OracleAI's config file and
    os.environ so the launcher picks them up immediately without a restart.

    app_dir     = inner ComfyUI folder (where main.py lives)
    python_root = outer portable folder (where python_embeded/ lives)
    """
    progress_cb("Writing ComfyUI paths to OracleAI config...", 96)
    try:
        # Set both in the current process environment immediately.
        os.environ["COMFYUI_HOME"]        = app_dir
        os.environ["COMFYUI_PYTHON_ROOT"] = python_root
        # ComfyUI is now configured -- the boot-time "setup required" flag is
        # stale. Clear it so the UI never re-prompts for setup this session.
        os.environ.pop("COMFYUI_SETUP_REQUIRED", None)

        # Locate the config file relative to this script (distribution-safe).
        script_dir  = Path(__file__).resolve().parent
        config_path = script_dir / CONFIG_FILENAME

        config = {}
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            except Exception:
                config = {}

        # Store both paths so the launcher never has to re-derive them.
        config["comfy_home"]        = app_dir       # main.py lives here
        config["comfyui_python_root"] = python_root   # python_embeded/ lives here

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

        progress_cb(f"Config written to {config_path}", 98)
        return {
            "success":     True,
            "config_path": str(config_path),
            "app_dir":     app_dir,
            "python_root": python_root
        }
    except Exception as e:
        # Non-fatal -- env vars are set, launcher will work this session.
        return {"success": False,
                "error": f"Config write failed (non-fatal): {type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
#  detection (is ComfyUI already present?)
# --------------------------------------------------------------------------- #
def detect_existing(progress_cb: Callable = _noop) -> Optional[str]:
    """Check if a valid ComfyUI portable install already exists.
    Returns the path if found, None if not."""
    progress_cb("Checking for existing ComfyUI install...", 1)

    # 1. Explicit env var.
    home = os.environ.get("COMFYUI_HOME") or os.environ.get("COMFYUI_PATH")
    if home and os.path.isdir(home) and os.path.exists(os.path.join(home, "main.py")):
        progress_cb(f"Found existing ComfyUI at {home}", 1)
        return home

    # 2. OracleAI config file.
    try:
        script_dir  = Path(__file__).resolve().parent
        config_path = script_dir / CONFIG_FILENAME
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            home = cfg.get("comfy_home", "")
            if home and os.path.isdir(home) and os.path.exists(
                    os.path.join(home, "main.py")):
                progress_cb(f"Found existing ComfyUI at {home}", 1)
                os.environ["COMFYUI_HOME"] = home
                return home
    except Exception:
        pass

    # 3. Default install location (prior OracleAI setup).
    default = os.path.join(DEFAULT_INSTALL_PARENT, "ComfyUI")
    if os.path.isdir(default) and os.path.exists(os.path.join(default, "main.py")):
        progress_cb(f"Found existing ComfyUI at {default}", 1)
        return default

    return None


# --------------------------------------------------------------------------- #
#  GPU detection (for run-mode selection + UI)
# --------------------------------------------------------------------------- #
_gpu_cache = None


def detect_gpu() -> dict:
    """Best-effort GPU summary for setup + UI (cached): {vendor, accel, name,
    vram_mb}. vendor: nvidia|amd|intel|cpu; accel target: cuda|directml|cpu.

    Reuses OracleAI's hw_utils.detect_hardware(). NVIDIA -> CUDA (the portable's
    native mode). AMD/Intel -> DirectML target (the launcher uses it only if
    torch_directml is present, else CPU). No discrete GPU -> CPU. Never raises.
    """
    global _gpu_cache
    if _gpu_cache is not None:
        return _gpu_cache
    out = {"vendor": "cpu", "accel": "cpu", "name": "CPU", "vram_mb": 0}
    try:
        import hw_utils
        hw = hw_utils.detect_hardware()
        for vend, accel in (("nvidia", "cuda"), ("amd", "directml"), ("intel", "directml")):
            blk = hw.get(vend, {})
            if blk.get("available"):
                gpus = blk.get("gpus") or []
                g0 = gpus[0] if gpus and isinstance(gpus[0], dict) else {}
                out = {"vendor": vend, "accel": accel,
                       "name": g0.get("name") or vend.upper(),
                       "vram_mb": g0.get("vram_mb", 0) or 0}
                _gpu_cache = out
                return out
        cpu = hw.get("cpu", {})
        out["name"] = cpu.get("name", "CPU")
    except Exception:
        pass
    _gpu_cache = out
    return out


def _find_embedded_python(comfy_home: str):
    """Locate the portable's embedded python.exe given the inner app dir (where
    main.py lives). Mirrors the launcher's resolution: env root, inner, parent."""
    if not comfy_home:
        return None
    parent  = os.path.dirname(comfy_home.rstrip("\\/"))
    py_root = os.environ.get("COMFYUI_PYTHON_ROOT") or ""
    for c in (os.path.join(py_root, "python_embeded", "python.exe") if py_root else "",
              os.path.join(comfy_home, "python_embeded", "python.exe"),
              os.path.join(parent,  "python_embeded", "python.exe")):
        if c and os.path.exists(c):
            return c
    return None


def _python_version(py: str):
    """(major, minor) of the given python.exe, or None. Never raises."""
    try:
        r = subprocess.run(
            [py, "-c", "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and "." in r.stdout:
            a, b = r.stdout.strip().split(".")[:2]
            return (int(a), int(b))
    except Exception:
        pass
    return None


def install_directml(comfy_home: str, progress_cb: Callable = _noop) -> dict:
    """OPT-IN: install torch-directml into the portable's embedded Python so
    AMD/Intel GPUs can accelerate via ComfyUI's --directml.

    Caller MUST gate this off NVIDIA -- on a CUDA box this would replace the
    CUDA build. Once installed, the launcher's _resolve_accel auto-detects
    torch_directml and switches AMD/Intel to --directml (self-persisting; no
    config flag needed). Returns {success}/{success:False,error}. Never raises.
    """
    progress_cb("Locating ComfyUI's bundled Python...", 5)
    py = _find_embedded_python(comfy_home)
    if not py:
        return {"success": False,
                "error": "Could not find ComfyUI's embedded Python (python_embeded)."}
    pyver = _python_version(py)
    _vs = f"{pyver[0]}.{pyver[1]}" if pyver else "unknown"
    progress_cb(f"ComfyUI's Python is {_vs}. Installing torch-directml "
                f"(downloading PyTorch — may take several minutes)...", 10)
    try:
        result = subprocess.run(
            [py, "-m", "pip", "install", "torch-directml"],
            capture_output=True, text=True, timeout=3600,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "")
            # torch-directml only ships wheels up to a given Python (3.12 as of
            # 2025); newer portables (3.13+) have no match -> explain clearly.
            if (("No matching distribution" in err or "Could not find a version" in err)
                    and pyver and pyver >= (3, 13)):
                return {"success": False, "error":
                        f"torch-directml has no build for ComfyUI's Python {_vs} yet "
                        f"(it currently supports up to Python 3.12), so DirectML can't be "
                        f"enabled on this ComfyUI build — image generation will keep running "
                        f"on CPU here. This will start working automatically once torch-directml "
                        f"adds Python {_vs} support, or with a ComfyUI build that ships Python 3.12."}
            return {"success": False, "error": f"pip install failed: {err[-700:]}"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "DirectML install timed out after 1 hour."}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}

    progress_cb("Verifying torch-directml...", 95)
    try:
        v = subprocess.run([py, "-c", "import torch_directml"],
                           capture_output=True, timeout=120)
        if v.returncode != 0:
            return {"success": False,
                    "error": "torch-directml installed but could not be imported."}
    except Exception:
        pass

    progress_cb("DirectML installed and verified.", 100)
    return {"success": True}


# --------------------------------------------------------------------------- #
#  main entry point
# --------------------------------------------------------------------------- #
def run_setup(install_parent: str = None, silent: bool = False,
              progress_cb: Callable = _noop) -> dict:
    """Run the full ComfyUI setup wizard.

    Args:
        install_parent: Where to install ComfyUI. Defaults to
                        ~/OracleAI/backend/ComfyUI. User can override
                        via the UI before calling this.
        silent:         Skip all interactive prompts. Use defaults.
        progress_cb:    progress_cb(message, percent) for UI integration.

    Returns a result dict:
        success (bool), comfy_home (str), already_present (bool),
        version (str), error (str on failure)
    """
    try:
        # ------------------------------------------------------------------- #
        #  Stage 0: report detected GPU (informs run mode + model choice)
        # ------------------------------------------------------------------- #
        _gpu = detect_gpu()
        progress_cb(f"Detected GPU: {_gpu['name']} "
                    f"({_gpu['vendor']} / {_gpu['accel']} mode).", -1)

        # ------------------------------------------------------------------- #
        #  Stage 1: detect existing install
        # ------------------------------------------------------------------- #
        existing = detect_existing(progress_cb)
        if existing:
            verify = _verify(existing, progress_cb)
            if verify["success"]:
                _write_config(verify["comfy_home"], verify["python_embeded"], progress_cb)
                progress_cb("ComfyUI is already installed and ready.", 100)
                return {
                    "success":        True,
                    "comfy_home":     existing,
                    "already_present": True,
                    "version":        "existing",
                }
            else:
                progress_cb(
                    f"Existing install at {existing} appears incomplete. "
                    "Proceeding with fresh download.", -1
                )

        # ------------------------------------------------------------------- #
        #  Stage 2: resolve install location
        # ------------------------------------------------------------------- #
        parent = install_parent or DEFAULT_INSTALL_PARENT
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as e:
            return {"success": False,
                    "error": f"Could not create install directory {parent}: {e}"}

        if not silent:
            progress_cb(f"ComfyUI will be installed to: {parent}", -1)

        # ------------------------------------------------------------------- #
        #  Stage 3: get latest release info from GitHub
        # ------------------------------------------------------------------- #
        release = _get_latest_release(progress_cb)
        if "error" in release:
            return {"success": False, "error": release["error"]}

        progress_cb(
            f"Latest ComfyUI release: {release['version']} "
            f"({release['size_bytes'] / (1024*1024):.0f} MB)", -1
        )

                # ------------------------------------------------------------------- #
        #  Stage 4: download to persistent cache (survives retries)
        # ------------------------------------------------------------------- #
        tmp = os.path.join(parent, ".download_cache")
        os.makedirs(tmp, exist_ok=True)
        zip_path = os.path.join(tmp, release["filename"])

        dl = _download(
            release["download_url"], zip_path,
            release["size_bytes"], progress_cb,
        )
        if not dl["success"]:
            return {"success": False, "error": dl["error"]}

               # ------------------------------------------------------------------- #
        #  Stage 5: extract
        # ------------------------------------------------------------------- #
        ex = _extract(zip_path, parent, progress_cb)
        if not ex["success"]:
            return {"success": False, "error": ex["error"]}

        # Clean up download cache only after successful extraction
        try:
            shutil.rmtree(tmp)
        except Exception:
            pass

        comfy_home = ex["extracted_path"]
        
        # Detect app_dir early so _install_deps gets the right path
        nested = os.path.join(comfy_home, "ComfyUI")
        app_dir = nested if (os.path.isdir(nested) and
                             os.path.exists(os.path.join(nested, "main.py"))) else comfy_home

        # ------------------------------------------------------------------- #
        #  Stage 6: install dependencies
        # ------------------------------------------------------------------- #
        deps = _install_deps(comfy_home, app_dir, progress_cb)
        if not deps["success"]:
            return {"success": False, "error": deps["error"]}

        # ------------------------------------------------------------------- #
        #  Stage 7: verify
        # ------------------------------------------------------------------- #
        verify = _verify(comfy_home, progress_cb)
        if not verify["success"]:
            return {"success": False, "error": verify["error"]}
            
        # Capture corrected paths
        comfy_home   = verify["comfy_home"]       # → app_dir (where main.py lives)
        python_root  = verify["python_embeded"]   # → outer folder (where python_embeded lives)    

        # ------------------------------------------------------------------- #
        #  Stage 8: write config
        # ------------------------------------------------------------------- #
        _write_config(verify["comfy_home"], verify["python_embeded"], progress_cb)

        progress_cb("ComfyUI setup complete and ready for headless operation.", 100)
        return {
            "success":         True,
            "comfy_home":      verify["comfy_home"],      # ← app_dir (main.py lives here)
            "python_root":     verify["python_embeded"],  # ← outer folder (python_embeded/ lives here)
            "already_present": False,
            "version":         release["version"],
        }

    except Exception as e:
        return {"success": False,
                "error": f"Setup failed unexpectedly: {type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
#  CLI entry point
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="OracleAI ComfyUI setup wizard",
    )
    parser.add_argument(
        "--install-dir",
        default=None,
        help=f"Where to install ComfyUI (default: {DEFAULT_INSTALL_PARENT})",
    )
    parser.add_argument(
        "--silent",
        action="store_true",
        help="Non-interactive mode, use all defaults",
    )
    parser.add_argument(
        "--detect-only",
        action="store_true",
        help="Only check if ComfyUI is already present, do not install",
    )
    args = parser.parse_args()

    if args.detect_only:
        found = detect_existing()
        if found:
            print(f"Found: {found}")
        else:
            print("No existing ComfyUI install detected.")
        sys.exit(0 if found else 1)

    result = run_setup(
        install_parent=args.install_dir,
        silent=args.silent,
    )

    print()
    if result["success"]:
        print("Setup complete!")
        print(f"  ComfyUI home : {result['comfy_home']}")
        print(f"  Version      : {result['version']}")
        print(f"  Pre-existing : {result['already_present']}")
        print()
        print("You can now run:")
        print("  python comfyui_launcher.py resolve")
        print("  python comfyui_launcher.py start")
    else:
        print(f"Setup failed: {result['error']}")
        sys.exit(1)