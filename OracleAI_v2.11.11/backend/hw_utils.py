"""
OracleAI Hardware Utility v2.1.10
Cross-platform detection for NVIDIA (CUDA), AMD (ROCm), Intel (Arc/XPU), and CPU.

v2.1.2 FIX: Intel GPU VRAM now reads from the Windows driver registry
(HardwareInformation.qwMemorySize — a 64-bit QWORD) instead of WMI's
AdapterRAM, which is a signed 32-bit field capped at ~2 GB. Cards with
more than 2 GB of VRAM (like the Arc B580 12GB) previously showed 2 GB.
"""

import os, sys, platform, shutil, subprocess, re
from typing import Any, Dict


def detect_hardware() -> Dict[str, Any]:
    nvidia = _detect_nvidia()
    amd    = _detect_amd()
    intel  = _detect_intel()

    if nvidia["available"]:
        rec_backend, rec_layers = "cuda", -1
    elif amd["available"]:
        rec_backend, rec_layers = "rocm", -1
    elif intel["available"]:
        rec_backend = "vulkan"
        rec_layers  = -1 if intel.get("arc_detected") else 20
    else:
        rec_backend, rec_layers = "cpu", 0

    return {
        "os": _detect_os(), "cpu": _detect_cpu(),
        "nvidia": nvidia, "amd": amd, "intel": intel,
        "recommended_backend": rec_backend, "recommended_layers": rec_layers,
    }


def _detect_os() -> Dict[str, Any]:
    return {
        "name": platform.system(), "version": platform.version(),
        "release": platform.release(), "machine": platform.machine(),
        "python": platform.python_version(),
        "is_windows": sys.platform == "win32",
        "is_linux": sys.platform.startswith("linux"),
        "is_mac": sys.platform == "darwin",
    }


def _detect_cpu() -> Dict[str, Any]:
    logical = os.cpu_count() or 1
    physical = logical  # fallback
    info: Dict[str, Any] = {
        "available": True, "name": "Unknown CPU",
        "cores": physical, "threads": logical,
        "avx2": False, "avx512": False,
    }
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                info["name"] = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()

            # Method 1: PowerShell Get-CimInstance
            got_cores = False
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "(Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum"],
                    capture_output=True, text=True, timeout=10, creationflags=0x08000000)
                v = r.stdout.strip()
                if v.isdigit() and int(v) > 0:
                    physical = int(v)
                    got_cores = True
            except Exception:
                pass

            # Method 2: wmic fallback
            if not got_cores:
                try:
                    r = subprocess.run(["wmic", "cpu", "get", "NumberOfCores", "/value"],
                                       capture_output=True, text=True, timeout=8,
                                       creationflags=0x08000000)
                    for line in r.stdout.splitlines():
                        line = line.strip()
                        if line.startswith("NumberOfCores="):
                            val = line.split("=")[1].strip()
                            if val.isdigit() and int(val) > 0:
                                physical = int(val)
                                got_cores = True
                except Exception:
                    pass

            # Method 3: Registry-based heuristic
            if not got_cores:
                try:
                    import winreg
                    i = 0
                    while True:
                        try:
                            winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                           rf"HARDWARE\DESCRIPTION\System\CentralProcessor\{i}")
                            i += 1
                        except OSError:
                            break
                    if i > 0 and "Intel" in info.get("name", ""):
                        physical = max(1, i // 2)
                        got_cores = True
                    elif i > 0:
                        physical = i
                except Exception:
                    pass

            # AVX detection via CPU name heuristics
            cpu_name_lower = info["name"].lower()
            if any(gen in cpu_name_lower for gen in ["13th gen", "14th gen", "12th gen",
                                                       "core ultra", "raptor", "alder"]):
                info["avx2"] = True
            if "avx-512" in cpu_name_lower or "sapphire" in cpu_name_lower:
                info["avx512"] = True

        elif sys.platform.startswith("linux"):
            core_ids = set()
            current_phys = "0"
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name") and info["name"] == "Unknown CPU":
                        info["name"] = line.split(":", 1)[1].strip()
                    if "avx2" in line.lower(): info["avx2"] = True
                    if "avx512" in line.lower(): info["avx512"] = True
                    if line.startswith("physical id"):
                        current_phys = line.split(":", 1)[1].strip()
                    if line.startswith("core id"):
                        core_ids.add(f"{current_phys}:{line.split(':', 1)[1].strip()}")
            if core_ids: physical = len(core_ids)

        elif sys.platform == "darwin":
            r = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0: info["name"] = r.stdout.strip()
            r = subprocess.run(["sysctl", "-n", "hw.physicalcpu"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip().isdigit():
                physical = int(r.stdout.strip())
    except Exception:
        pass

    info["cores"] = physical
    info["threads"] = logical
    return info


def _detect_nvidia() -> Dict[str, Any]:
    info = {"available": False, "gpus": [], "cuda_version": None,
            "driver_version": None, "error": None}
    smi = shutil.which("nvidia-smi")
    if not smi:
        for p in ["/usr/bin/nvidia-smi", r"C:\Windows\System32\nvidia-smi.exe",
                  r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"]:
            if os.path.exists(p): smi = p; break
    if not smi:
        info["error"] = "nvidia-smi not found"; return info
    try:
        r = subprocess.run(
            [smi, "--query-gpu=name,memory.total,driver_version,compute_cap",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            info["available"] = True
            for line in r.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                gpu = {"name": parts[0] if parts else "Unknown",
                       "vram_mb": _to_int(parts[1]) if len(parts) > 1 else 0,
                       "driver": parts[2] if len(parts) > 2 else "?",
                       "compute_cap": parts[3] if len(parts) > 3 else "?"}
                info["gpus"].append(gpu)
                info["driver_version"] = gpu["driver"]
    except Exception as e:
        info["error"] = str(e)
    if info["available"]:
        try:
            import torch; info["cuda_version"] = torch.version.cuda
        except ImportError:
            pass
    return info


def _detect_amd() -> Dict[str, Any]:
    info = {"available": False, "gpus": [], "rocm_version": None, "error": None}
    tool = shutil.which("rocm-smi") or shutil.which("rocminfo")
    if not tool:
        for p in ["/opt/rocm/bin/rocm-smi", "/opt/rocm/bin/rocminfo"]:
            if os.path.exists(p): tool = p; break
    if tool:
        try:
            args = [tool] + (["--showproductname"] if "rocm-smi" in tool else [])
            r = subprocess.run(args, capture_output=True, text=True, timeout=10)
            if r.returncode == 0: info["available"] = True
        except Exception as e:
            info["error"] = str(e)
    if not info["available"] and sys.platform.startswith("linux"):
        try:
            r = subprocess.run(["lsmod"], capture_output=True, text=True, timeout=5)
            if "amdgpu" in r.stdout: info["available"] = True
        except Exception:
            pass
    vf = "/opt/rocm/.info/version"
    if os.path.exists(vf):
        try:
            with open(vf) as f: info["rocm_version"] = f.read().strip()
        except Exception:
            pass
    return info


def _detect_intel() -> Dict[str, Any]:
    info = {"available": False, "gpus": [], "openvino": False,
            "xe_cores": 0, "arc_detected": False, "level_zero": False,
            "driver_info": None, "error": None}

    ARC_KEYWORDS = [
        "arc", "battlemage", "alchemist",
        "a770", "a750", "a580", "a380", "a310",
        "b580", "b570",
        "dg2", "dg1",
    ]

    def _is_arc(name: str) -> bool:
        nl = name.lower()
        return any(kw in nl for kw in ARC_KEYWORDS)

    # Linux: lspci
    if sys.platform.startswith("linux"):
        try:
            r = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
            for l in r.stdout.splitlines():
                if "Intel" in l and any(k in l for k in ["VGA", "Display", "3D"]):
                    info["available"] = True
                    gn = l.split(":", 2)[-1].strip() if ":" in l else l.strip()
                    info["gpus"].append({"name": gn})
                    if _is_arc(l): info["arc_detected"] = True
        except Exception:
            pass

    # Windows
    if sys.platform == "win32":
        names = []
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_VideoController | "
                 "Where-Object { $_.Name -like '*Intel*' } | "
                 "Select-Object -ExpandProperty Name"],
                capture_output=True, text=True, timeout=10,
                creationflags=0x08000000)
            for line in r.stdout.strip().splitlines():
                n = line.strip()
                if n and "Intel" in n: names.append(n)
        except Exception:
            pass

        if not names:
            try:
                r = subprocess.run(
                    ["wmic", "path", "win32_VideoController", "get", "name"],
                    capture_output=True, text=True, timeout=8,
                    creationflags=0x08000000)
                for line in r.stdout.splitlines():
                    n = line.strip()
                    if "Intel" in n and n != "Name": names.append(n)
            except Exception:
                pass

        if names:
            info["available"] = True
            for n in names:
                gpu_entry = {"name": n}
                if _is_arc(n):
                    info["arc_detected"] = True
                info["gpus"].append(gpu_entry)

            # --- VRAM detection (v2.1.2 FIX) -----------
            # WMI's AdapterRAM is a signed 32-bit field capped at ~2 GB.
            # Read from the driver registry instead, where
            # HardwareInformation.qwMemorySize is a 64-bit QWORD.
            # Path: HKLM:\SYSTEM\CurrentControlSet\Control\Class\
            #       {4d36e968-e325-11ce-bfc1-08002be10318}\*
            vram_map: Dict[str, int] = {}  # DriverDesc -> bytes
            try:
                ps_cmd = (
                    "Get-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\"
                    "{4d36e968-e325-11ce-bfc1-08002be10318}\\*' -ErrorAction SilentlyContinue | "
                    "Where-Object { $_.DriverDesc -like '*Intel*' } | "
                    "ForEach-Object { "
                    "  $qw = $_.'HardwareInformation.qwMemorySize'; "
                    "  $size = if ($qw) { $qw } else { 0 }; "
                    "  Write-Output \"$($_.DriverDesc)||$size\" "
                    "}"
                )
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True, text=True, timeout=10,
                    creationflags=0x08000000)
                for line in r.stdout.strip().splitlines():
                    line = line.strip()
                    if "||" not in line:
                        continue
                    desc, size_str = line.rsplit("||", 1)
                    desc = desc.strip()
                    size_str = size_str.strip()
                    try:
                        size_bytes = int(size_str)
                    except ValueError:
                        continue
                    if size_bytes > 0:
                        vram_map[desc] = size_bytes
            except Exception:
                pass

            # Match registry entries to detected GPU names
            for gpu in info["gpus"]:
                gpu_name = gpu["name"]
                if gpu_name in vram_map:
                    gpu["vram_mb"] = vram_map[gpu_name] // (1024 * 1024)
                    continue
                matched = False
                for desc, size_bytes in vram_map.items():
                    if desc in gpu_name or gpu_name in desc:
                        gpu["vram_mb"] = size_bytes // (1024 * 1024)
                        matched = True
                        break
                if not matched and vram_map:
                    if len(vram_map) == 1:
                        gpu["vram_mb"] = list(vram_map.values())[0] // (1024 * 1024)

            # Last-ditch fallback: AdapterRAM (may be truncated to 2GB)
            if not any(gpu.get("vram_mb") for gpu in info["gpus"]):
                try:
                    r = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         "Get-CimInstance Win32_VideoController | "
                         "Where-Object { $_.Name -like '*Intel*' } | "
                         "Select-Object -ExpandProperty AdapterRAM"],
                        capture_output=True, text=True, timeout=10,
                        creationflags=0x08000000)
                    lines = [l.strip() for l in r.stdout.strip().split('\n') if l.strip()]
                    for i, line in enumerate(lines):
                        if line.isdigit() and int(line) > 0 and i < len(info["gpus"]):
                            if not info["gpus"][i].get("vram_mb"):
                                info["gpus"][i]["vram_mb"] = int(line) // (1024 * 1024)
                                info["gpus"][i]["vram_note"] = "AdapterRAM fallback (may be truncated)"
                except Exception:
                    pass

            # Driver version
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-CimInstance Win32_VideoController | "
                     "Where-Object { $_.Name -like '*Intel*' } | "
                     "Select-Object -ExpandProperty DriverVersion"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=0x08000000)
                dv = r.stdout.strip().split('\n')[0].strip()
                if dv: info["driver_info"] = dv
            except Exception:
                pass

            # Xe core count estimation for Arc GPUs
            if info["arc_detected"]:
                for gpu in info["gpus"]:
                    name_lower = gpu["name"].lower()
                    if "a770" in name_lower: info["xe_cores"] = 32
                    elif "a750" in name_lower: info["xe_cores"] = 28
                    elif "a580" in name_lower: info["xe_cores"] = 24
                    elif "b580" in name_lower: info["xe_cores"] = 20
                    elif "b570" in name_lower: info["xe_cores"] = 18
                    elif "a380" in name_lower: info["xe_cores"] = 8
                    elif "a310" in name_lower: info["xe_cores"] = 6

    # OpenVINO
    try:
        import importlib.util
        if importlib.util.find_spec("openvino"):
            info["openvino"] = True; info["available"] = True
    except Exception:
        pass

    # Level-Zero / oneAPI
    for cmd in [shutil.which("ze_info"), shutil.which("sycl-ls")]:
        if cmd:
            try:
                r = subprocess.run([cmd], capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    info["level_zero"] = True; info["available"] = True
            except Exception:
                pass

    # Vulkan check
    if not info["level_zero"] and info["available"]:
        vulkan_info = shutil.which("vulkaninfo")
        if vulkan_info:
            try:
                r = subprocess.run([vulkan_info, "--summary"],
                                   capture_output=True, text=True, timeout=5)
                if "Intel" in r.stdout:
                    info["vulkan_supported"] = True
            except Exception:
                pass

    return info


def _to_int(s, default=0):
    try: return int(s)
    except (ValueError, TypeError): return default
