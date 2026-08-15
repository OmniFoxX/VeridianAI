# Troubleshooting — "VeridianAI won't start"

The most common failure is the backend not coming up: the window opens, stays
dark blue, and says the backend isn't running. This document tells you where to
look. **You do not need Developer Mode enabled** — the logs below are always
written.

---

## 1. The two log files

Both live in your temp folder. Paste `%TEMP%` into the Explorer address bar, or:

```powershell
notepad $env:TEMP\VeridianAI-boot.log
notepad $env:TEMP\VeridianAI-backend.log
```

| File | Written by | Contains |
|---|---|---|
| `VeridianAI-boot.log` | the app shell | Every step of startup, from the first line of code to the window opening |
| `VeridianAI-backend.log` | `start.bat` and the Python backend | Everything the backend printed, including any crash |

Both are rewritten on each launch, so they always describe the most recent
attempt.

> **Why these exist:** a packaged desktop app has nowhere to print. Its console
> output goes nowhere, so a backend that dies on startup used to produce
> *literally no evidence anywhere*. These files are that evidence.

---

## 2. Reading the boot log

A healthy launch looks roughly like this:

```
=== VeridianAI boot ===
electron=42.7.1 node=24.18.0 platform=win32 windowsStore=false
execPath=...\VeridianAI.exe
ensureBackendAvailable: APP_PORT=8000 HEALTH_URL=http://127.0.0.1:8000/api/health
isPackaged=true resolvedRoot=...
start.bat exists=true at ...\start.bat
root contents: backend, frontend, python, start.bat, ...
dataDir=...\sage_data (store=false) exists=true
devMode=false backendLog=...\VeridianAI-backend.log
CASE 3: port free -> startBackend()
spawning: cmd.exe /c "...\start.bat" --mode vulkan
spawned pid=12345
waiting for backend health...
backend healthy=true
createWindow()
```

### What each line rules out

| Line | If it's wrong |
|---|---|
| `start.bat exists=false` | The install is incomplete — reinstall. |
| `root contents:` missing `backend`/`python` | Same: the package didn't ship everything. |
| `dataDir=... exists=false` | The data folder couldn't be created. Check the path is writable. |
| `CASE 1: backend already healthy` | Another copy is already running. Not an error. |
| `CASE 2b: port bound, holder UNKNOWN` | **Nothing was launched.** Something else owns port 8000. |
| `start.bat EXITED code=N` **early** | The launcher gave up. `N` says why — check the backend log. |
| No `spawned pid=` line at all | The backend was never started. The `CASE` line above says which path bailed. |

**The single most useful line is `start.bat EXITED code=`.** If it appears
seconds after `spawned pid=`, the launcher ran and quit, and
`VeridianAI-backend.log` will contain the reason.

---

## 3. Common causes

### Something else is using port 8000

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
```

Anything listed there owns the port. Either stop it, or change
`network.ports.app` in `config.json` and relaunch.

### Orphan processes from a previous run

```powershell
Get-Process python,pythonw,ollama,llama-server,VeridianAI -ErrorAction SilentlyContinue | Stop-Process -Force
```

### Python is missing (portable build only)

The boot log shows the launcher starting and the backend log shows a Python
error. The portable build installs Python on first run; if that was declined or
failed, run `start.bat` by hand once to see the prompts. The Microsoft Store
build bundles its own Python and never needs this.

### The install folder path contains spaces

Fixed in v2.13, but worth knowing the signature: `start.bat` exits almost
immediately with code 0 and the backend log shows a command not being
recognised. This affected any install under `C:\Program Files\...`.

---

## 4. Developer Mode

Developer Mode makes `start.bat` open a **visible console** so you can watch the
tiers start in real time. It does not change what is logged — the files above
are written either way.

Turn it on in Settings → Hardware, or create the file directly:

```powershell
$d = "$env:APPDATA\VeridianAI\sage_data"      # Store build
# $d = "<install folder>\..\sage_data"        # portable build
New-Item -ItemType Directory -Path $d -Force | Out-Null
'{ "developer_mode": true }' | Set-Content "$d\ui_prefs.json" -Encoding ascii
```

> Use `-Encoding ascii`, **not** `utf8`. Windows PowerShell 5.1 writes UTF-8
> *with a BOM*, and a leading BOM used to break the JSON parse silently. v2.13
> tolerates it, but ascii avoids the question entirely.

---

## 5. Reporting a problem

Attach both log files. They contain paths and version numbers but no
conversation content, no keys and no personal data — the backend's own logs
live separately under `sage_data/logs/`. Email: "todd@mentispheresoftware.com"

Also useful:

```powershell
Get-ComputerInfo -Property OsName,OsVersion,CsTotalPhysicalMemory
```
