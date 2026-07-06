@echo off
setlocal EnableDelayedExpansion
title OracleAI v2.11.11 - Startup

:: ---------------------------------------------------------------
:: Non-interactive mode for Electron (and future self-update use)
:: Usage: start.bat --mode vulkan
::        start.bat --mode ipex
:: When called by Electron, the prompt is skipped entirely.
:: Human double-click with no args = menu shows as normal.
:: ---------------------------------------------------------------
set ELECTRON_MODE=0
if /I "%~1"=="--mode" (
    set ELECTRON_MODE=1
    if /I "%~2"=="ipex" (
        echo.
        echo  [OracleAI] Non-interactive: IPEX-LLM SYCL selected
        set SYCL_PI_LEVEL_ZERO_USE_IMMEDIATE_COMMANDLISTS=1
        set LLAMA_BACKEND=backend\llama-cpp-ipex-llm-2.3.0b20250424-win\llama-server.exe
        goto :start_tiers
    ) else (
        echo.
        echo  [OracleAI] Non-interactive: Vulkan selected
        set LLAMA_BACKEND=backend\llama-server.exe
        goto :start_tiers
    )
)

:: --- Interactive path (human double-click) ---------------------
echo.
echo  +===========================================+
echo  ^|       O R A C L E  A I  v2.11.11        ^|
echo  +===========================================+
echo.
echo  Select backend for this session:
echo.
echo     Vulkan    (recommended default)
echo     IPEX-LLM  (Intel SYCL, legacy)
echo.
choice /C 12 /N /T 10 /D 1 /M "  Your choice [Vulkan in 10s]: "

if !errorlevel!==2 (
    echo.
    echo  [OracleAI] Backend: IPEX-LLM SYCL selected
    set SYCL_PI_LEVEL_ZERO_USE_IMMEDIATE_COMMANDLISTS=1
    set LLAMA_BACKEND=backend\llama-cpp-ipex-llm-2.3.0b20250424-win\llama-server.exe
) else (
    echo.
    echo  [OracleAI] Backend: Vulkan selected
    set LLAMA_BACKEND=backend\llama-server.exe
)

:start_tiers
title OracleAI v2.11.11
echo.
echo  +===========================================+
echo  ^|       O R A C L E  A I  v2.11.11        ^|
echo  +===========================================+
echo.
:: ============================================================================
:: OracleAI v2.1.5+ launcher -- Phase 1A (deterministic three-tier startup)
::
:: Tiers:
::   Oracle  -> Ollama          on 127.0.0.1:11434  (heavy reasoning, GPU)
::   Sage    -> llama-server    on 127.0.0.1:11435  (fast chat, CPU)
::   Daemon  -> llama-server    on 127.0.0.1:11436  (mechanics, CPU, tiny)
::
:: This script starts each tier ONCE, then polls each port via curl until it
:: answers. If any tier fails to come online within PROBE_TIMEOUT_SEC, the
:: script aborts BEFORE launching the FastAPI backend so you see exactly
:: which tier failed and why. No silent partial startup.
:: ============================================================================

:: -- Port defaults (overridable via env var OR config.json network.ports.*).
:: These are the LAST-RESORT fallbacks if config.json + env var resolution
:: both fail. _tier_config_reader.py reads the canonical values from
:: config.json and overwrites these via the for /f loop further down.
set APP_PORT=8000
set OLLAMA_ORACLE_PORT=11434
set LLAMA_SAGE_PORT=11435
set LLAMA_DAEMON_PORT=11436
set DAEMON_PORT=9998

:: -- Tunables
set PROBE_TIMEOUT_SEC=90
:: v2.1.5: backend selected at startup via choice menu (Vulkan or IPEX-LLM).
:: %~dp0 expands to the directory this .bat file lives in (with trailing
:: backslash), so renaming the project folder never breaks llama-server
:: startup. LLAMA_BACKEND is set above by the choice block.
set LLAMA_SERVER=%~dp0!LLAMA_BACKEND!

:: v2.2 fix (2026-05-29): MODELS_DIR is now self-locating. Previously
:: hardcoded to Todd's E:\sage_data\models, which broke any install on
:: another drive. %~dp0 resolves to the project directory; ..\sage_data
:: walks up one level (sage_data lives ALONGSIDE OracleAI_v2.7, not
:: inside it -- see BEFORE_RUNNING.txt). Works on any drive, any path.
set MODELS_DIR=%~dp0..\sage_data\models

:: v2.2: model filenames are now env vars (still defaults, but no longer
:: hidden in Python source). config.py reads SAGE_MODEL_FILE and
:: DAEMON_MODEL_FILE to build its MODEL_SAGE / MODEL_DAEMON paths.
:: v2.11.12e: SAGE_MODEL_FILE is now a DEFAULT CANDIDATE, not a requirement.
:: Pre-set the env var to use a different gguf; if the file doesn't exist
:: the Sage tier is simply skipped (see preflight below) — a fresh install
:: needs NO specific model to start. The old behavior hard-aborted the
:: entire launch when this exact file was missing, which (a) blocked fresh
:: installs behind one arbitrary model and (b) name-dropped a third party
:: (All Hands / OpenHands is their real project) as if it were required.
if "%SAGE_MODEL_FILE%"=="" set SAGE_MODEL_FILE=all_hands_openhands_lm_7b_v0_1_Q6_K_L.gguf
set DAEMON_MODEL_FILE=qwen2.5_coder_1.5b_base.gguf

set SAGE_MODEL=%MODELS_DIR%\%SAGE_MODEL_FILE%
set DAEMON_MODEL=%MODELS_DIR%\%DAEMON_MODEL_FILE%

:: v2.2: bundled_models\ at project root is a fallback for the daemon
:: model so a fresh distribution install can launch the daemon tier
:: without the user having to download anything. Sage is not bundled
:: (model file too large -- ~6 GB); for Sage, MODELS_DIR is the only
:: lookup location. If the user has not pulled a Sage model yet, the
:: preflight check below will fail loudly with a clear message --
:: better than silent partial startup.
set BUNDLED_DAEMON_MODEL=%~dp0bundled_models\%DAEMON_MODEL_FILE%

:: -- Per-tier context sizes (Phase 1D Step 1) ----------------------------
:: These control llama-server working memory per tier. Cannot be changed
:: while the process is running -- a restart is required. The UI restart
:: endpoints added in Step 4 will kill and respawn the relevant server
:: when the user clicks Refresh Models after changing values.
::
:: Defaults chosen for the shipped models:
::   Sage   : OpenHands 7B, trained on 32768. 16384 is half of trained
::            window with room for a long document + system prompt +
::            several turns. KV cost ~900 MB on top of the 6.2 GB model.
::   Daemon : Qwen 1.5B. 8192 is plenty for log summarization and
::            mechanical tasks. KV cost ~224 MB on top of 940 MB model.
set SAGE_CTX_SIZE=256000
set DAEMON_CTX_SIZE=4096

:: -- Preflight: curl must exist (Windows 10 1803+ ships with it)
where curl.exe >nul 2>&1
if !errorlevel! neq 0 (
    echo [OracleAI] ERROR: curl.exe not found on PATH.
    echo           curl ships with Windows 10 1803+. Install curl or upgrade Windows.
    pause
    exit /b 1
)

:: -- Preflight: llama-server.exe must exist
if not exist "%LLAMA_SERVER%" (
    echo [OracleAI] ERROR: llama-server.exe not found at:
    echo           %LLAMA_SERVER%
    pause
    exit /b 1
)

:: -- Preflight: daemon model — try sage_data first, then bundled_models
:: v2.2: if user doesn't have a daemon model in sage_data, fall back to
:: the bundled copy under the project. Sage model preflight is deferred
:: until after we know whether Sage tier is even launching (see backend
:: branch below). If neither location has the daemon model, we let the
:: launcher proceed and the daemon tier will simply skip — daemon is
:: non-critical (background mechanics).
if not exist "%DAEMON_MODEL%" (
    if exist "%BUNDLED_DAEMON_MODEL%" (
        echo [OracleAI] Daemon model not in sage_data; using bundled copy.
        set DAEMON_MODEL=%BUNDLED_DAEMON_MODEL%
        set DAEMON_MODEL_PRESENT=1
    ) else (
        echo [OracleAI] Daemon model not found in sage_data or bundled_models.
        echo           Daemon tier will be skipped ^(mechanics background work
        echo           will be reduced but the rest of OracleAI is unaffected^).
        set DAEMON_MODEL_PRESENT=0
    )
) else (
    set DAEMON_MODEL_PRESENT=1
)

:: ============================================================================
:: Phase 1D Step 3: Python detection moved EARLY so we can call
:: _tier_config_reader.py to read live ctx sizes from config.json before
:: spawning the llama-server tiers. Falls through to the hardcoded
:: SAGE_CTX_SIZE/DAEMON_CTX_SIZE defaults if Python or the helper fails.
:: ============================================================================
set PYTHON_CMD=
py --version >nul 2>&1
if !errorlevel!==0 set PYTHON_CMD=py
if "!PYTHON_CMD!"=="" (
    python --version >nul 2>&1
    if !errorlevel!==0 set PYTHON_CMD=python
)
if "!PYTHON_CMD!"=="" (
    python3 --version >nul 2>&1
    if !errorlevel!==0 set PYTHON_CMD=python3
)
if "!PYTHON_CMD!"=="" (
    echo [OracleAI] ERROR: Python not found. Install Python 3.10+
    pause
    exit /b 1
)
echo [OracleAI] Python: !PYTHON_CMD!

:: ============================================================================
:: v2.11.12 zombie-process fix: reap anything a previous session left behind
:: BEFORE launching tiers. Kills only processes recorded in .oracle_pids.json
:: (identity-verified) plus stack processes running from backend\. A user's
:: own Ollama is never touched. This is what makes restart work on try #1
:: instead of try #3-5 — stale port-holders on 11434/11435/11436 die here.
:: ============================================================================
set "OAI_ROOT=%~dp0"
echo [OracleAI] Cleaning up any processes left from a previous session ...
!PYTHON_CMD! "%~dp0backend\shutdown_cleanup.py" --quiet

:: Read n_ctx + ports + backend from config.json via _tier_config_reader.py.
:: Output: SAGE_CTX,DAEMON_CTX,APP_PORT,OLLAMA_ORACLE_PORT,LLAMA_SAGE_PORT,LLAMA_DAEMON_PORT,INFERENCE_BACKEND
:: If the helper fails for any reason, the for /f loop body simply does
:: not execute and the tunables-block defaults set above (8000 / 11434 /
:: 11435 / 11436 and INFERENCE_BACKEND default below) take effect.
set INFERENCE_BACKEND=ollama
for /f "tokens=1,2,3,4,5,6,7 delims=," %%a in ('!PYTHON_CMD! "%~dp0backend\_tier_config_reader.py" 2^>nul') do (
    set "SAGE_CTX_SIZE=%%a"
    set "DAEMON_CTX_SIZE=%%b"
    set "APP_PORT=%%c"
    set "OLLAMA_ORACLE_PORT=%%d"
    set "LLAMA_SAGE_PORT=%%e"
    set "LLAMA_DAEMON_PORT=%%f"
    set "INFERENCE_BACKEND=%%g"
)
echo [OracleAI] Tier ctx: Sage=!SAGE_CTX_SIZE!, Daemon=!DAEMON_CTX_SIZE!
echo [OracleAI] Tier ports: App=!APP_PORT! Oracle=!OLLAMA_ORACLE_PORT! Sage=!LLAMA_SAGE_PORT! Daemon=!LLAMA_DAEMON_PORT!
echo [OracleAI] Inference backend: !INFERENCE_BACKEND!

:: v2.2 corrected semantics (2026-05-29): inference.backend controls
:: which tier USER CHAT routes to, NOT which tiers launch. All three
:: inference tiers (Oracle, Sage, Daemon) always come up because each
:: serves a distinct role -- Oracle = heavy reasoning, Sage = agentic
:: engine (interprets tool tags, runs multi-step plans), Daemon =
:: mechanics. These are the CRAIID substrate (Archivist/Journalist/
:: Author -- see oracleai_roadmap_craiid_v2.md). Skipping a tier
:: because user chat happens to route through another tier is a
:: category error -- the skipped tier still has its own role.
:: INFERENCE_BACKEND is read here for log/diagnostic clarity; future
:: routing code may use it, but tier launch is unconditional.

:: Sage model preflight -- unconditional (Sage tier always launches).
:: v2.2 (2026-05-30) error message: spells out the resolved sage_data
:: location so the user can see exactly where to put the gguf, and
:: explains the sibling-not-inside-project layout so they do not
:: intuitively create sage_data inside OracleAI_v2.7 (which would
:: not be found AND would break Trinity separation -- see
:: BEFORE_RUNNING.txt step 3 for the canonical layout).
:: v2.11.12e: missing Sage model is NO LONGER FATAL. Mirror the daemon
:: tier's graceful skip: warn, blank SAGE_MODEL so tier_launcher skips the
:: tier, and continue the launch. Chat routes through the Oracle tier
:: (Ollama) with whatever models the user actually has — no baked-in
:: model requirement on a fresh install.
if not exist "%SAGE_MODEL%" (
    echo.
    echo [OracleAI] Sage model not found -- Sage tier will be SKIPPED.
    echo    Looked for: %SAGE_MODEL_FILE%
    echo    in:         %MODELS_DIR%
    echo    OracleAI runs fine without it: chat routes through the
    echo    Oracle tier ^(Ollama^). To enable the Sage tier later, put
    echo    any .gguf in the models dir and set SAGE_MODEL_FILE to its
    echo    filename ^(or use the default name above^), then restart.
    echo.
    set SAGE_MODEL_PRESENT=0
    set "SAGE_MODEL="
) else (
    set SAGE_MODEL_PRESENT=1
)

:: -- Tiers + daemons launch via tier_launcher.py so console VISIBILITY follows
:: the Developer Mode toggle: Dev ON = each gets its own titled console (as
:: before); Dev OFF (the default) = spawned WINDOWLESS for a clean desktop,
:: regardless of Windows Terminal. Restart-to-apply. The launcher reads the
:: resolved paths/ports/models from the environment populated above.
::   Oracle = Ollama  |  Sage + Daemon = llama-server  |  Sage-Daemon/Overseer = Python
set "OAI_ROOT=%~dp0"
echo [OracleAI] Launching tiers + daemons (Developer Mode controls visibility) ...
!PYTHON_CMD! "%~dp0backend\tier_launcher.py"
:: Soft delay to let ports begin binding before the readiness probes below.
timeout /t 3 /nobreak >nul

:: -- Probe each tier for readiness
echo.
echo [OracleAI] Waiting for tiers to come online (max %PROBE_TIMEOUT_SEC%s each)...
echo.

:: v2.11.12c: tier probe failures are FATAL only in interactive mode.
:: In Electron mode (ELECTRON_MODE=1) a slow tier no longer aborts the
:: whole launch. Rationale (Todd's Ryzen AI laptop, 2026-07-02): on a
:: cold boot, reading the 6 GB Sage model off disk on a low-power chip
:: can exceed the probe window; the old `goto fail_*` then aborted
:: BEFORE FastAPI ever launched, so Electron waited forever on a backend
:: that was never started ("first start fails, immediate second start
:: works" — the second try hit a warm file cache). Now we log a warning
:: and continue: the tier keeps loading in the background, llama-server/
:: Ollama answer when ready, and model_manager routes to whatever tiers
:: are up. Interactive (double-click) runs keep the loud fail+pause.

:: Oracle uses Ollama's /api/tags endpoint
call :probe_tier "Oracle" !OLLAMA_ORACLE_PORT! "http://127.0.0.1:!OLLAMA_ORACLE_PORT!/api/tags"
if !errorlevel! neq 0 (
    if !ELECTRON_MODE!==1 (
        echo [OracleAI] WARNING: Oracle tier not ready yet -- continuing. It may finish warming in the background.
    ) else (
        goto fail_oracle
    )
)

:: Sage uses llama-server's OpenAI-compatible /v1/models endpoint.
:: v2.11.12e: probe only when the tier was actually launched (model file
:: present). A fresh install without a Sage gguf skips both launch+probe.
if !SAGE_MODEL_PRESENT!==1 (
    call :probe_tier "Sage  " !LLAMA_SAGE_PORT! "http://127.0.0.1:!LLAMA_SAGE_PORT!/v1/models"
    if !errorlevel! neq 0 (
        if !ELECTRON_MODE!==1 (
            echo [OracleAI] WARNING: Sage tier not ready yet -- continuing. It may finish warming in the background.
        ) else (
            goto fail_sage
        )
    )
) else (
    echo [OracleAI] Sage tier skipped ^(no model^) -- probe skipped.
)

:: Daemon probe — only if daemon model was found and tier was launched.
if !DAEMON_MODEL_PRESENT!==1 (
    call :probe_tier "Daemon" !LLAMA_DAEMON_PORT! "http://127.0.0.1:!LLAMA_DAEMON_PORT!/v1/models"
    if !errorlevel! neq 0 (
        if !ELECTRON_MODE!==1 (
            echo [OracleAI] WARNING: Daemon tier not ready yet -- continuing. It may finish warming in the background.
        ) else (
            goto fail_daemon
        )
    )
)

echo.
echo [OracleAI] Tiers ready. Launching backend...
echo.

:run

:run
:: v2.1.11 fix: do NOT pass %* to start.py.
:: %* contains whatever arguments start.bat was called with, including
:: --mode vulkan when Electron is the launcher. start.py's argparse
:: only knows --port / --host / --no-browser and crashes with
:: "unrecognized arguments: --mode vulkan", which means uvicorn never
:: runs and Electron's /api/health probe fails forever. The 5 tier
:: windows already opened (they spawn BEFORE this line), so the user
:: sees a half-up stack with no FastAPI behind it.
::
:: Resolution: pass start.py only arguments it actually understands.
:: When Electron is the launcher, also pass --no-browser so Brave
:: doesn't auto-open on top of the Electron window.
if !ELECTRON_MODE!==1 (
    !PYTHON_CMD! start.py --port !APP_PORT! --no-browser
) else (
    !PYTHON_CMD! start.py --port !APP_PORT!
)
if !errorlevel! neq 0 ( echo [OracleAI] Backend exited with error. & pause )
exit /b 0

:: ============================================================================
:: SUBROUTINE: probe_tier <label> <port> <url>
::   Polls <url> every second until curl succeeds OR PROBE_TIMEOUT_SEC elapses.
::   Returns errorlevel 0 on success, 1 on timeout.
:: ============================================================================
:probe_tier
set "LABEL=%~1"
set "PORT=%~2"
set "URL=%~3"
set /a count=0
:probe_loop
curl -fsS --max-time 2 -o nul "%URL%" >nul 2>&1
if !errorlevel!==0 (
    echo [OracleAI] !LABEL! ^(:!PORT!^) READY after !count!s
    exit /b 0
)
set /a count+=1
if !count! geq %PROBE_TIMEOUT_SEC% (
    echo [OracleAI] !LABEL! ^(:!PORT!^) FAILED -- no response after !count!s
    exit /b 1
)
:: Progress dots every 5 seconds
set /a mod=count %% 5
if !mod!==0 echo [OracleAI] !LABEL! ^(:!PORT!^) still waiting ... !count!s
timeout /t 1 /nobreak >nul
goto probe_loop

:: ============================================================================
:: Failure handlers -- each prints a specific hint and pauses
:: ============================================================================
:fail_oracle
echo.
echo [OracleAI] ============================================================
echo [OracleAI] ORACLE TIER FAILED TO START (Ollama on :!OLLAMA_ORACLE_PORT!)
echo [OracleAI] ============================================================
echo [OracleAI] Check the "Ollama-Oracle" window for errors. Common causes:
echo [OracleAI]   - ollama.exe not installed or not on PATH
echo [OracleAI]   - port !OLLAMA_ORACLE_PORT! already in use by another process
echo [OracleAI]   - GPU driver / VRAM issue on first model load
echo [OracleAI] Run `ollama serve` manually in a terminal to see the error.
pause
exit /b 1

:fail_sage
echo.
echo [OracleAI] ============================================================
echo [OracleAI] SAGE TIER FAILED TO START (llama-server on :!LLAMA_SAGE_PORT!)
echo [OracleAI] ============================================================
echo [OracleAI] Check the "Llama-Sage" window for errors. Common causes:
echo [OracleAI]   - model file not found: %SAGE_MODEL%
echo [OracleAI]   - port !LLAMA_SAGE_PORT! already in use
echo [OracleAI]   - insufficient RAM for the model
echo [OracleAI] Alternative: set inference.backend to "ollama" in config.json
echo [OracleAI] and OracleAI will serve Sage chat through the Oracle tier.
pause
exit /b 1

:fail_daemon
echo.
echo [OracleAI] ============================================================
echo [OracleAI] DAEMON TIER FAILED TO START (llama-server on :!LLAMA_DAEMON_PORT!)
echo [OracleAI] ============================================================
echo [OracleAI] Check the "Llama-Daemon" window for errors. Common causes:
echo [OracleAI]   - model file not loadable: %DAEMON_MODEL%
echo [OracleAI]   - port !LLAMA_DAEMON_PORT! already in use
echo [OracleAI]   - insufficient RAM (~1.5 GB needed)
pause
exit /b 1
