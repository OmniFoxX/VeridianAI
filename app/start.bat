@echo off
setlocal EnableDelayedExpansion
title VeridianAI v2.17 - Startup

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
        echo  [VeridianAI] Non-interactive: IPEX-LLM SYCL selected
        set SYCL_PI_LEVEL_ZERO_USE_IMMEDIATE_COMMANDLISTS=1
        set LLAMA_BACKEND=backend\llama-cpp-ipex-llm-2.3.0b20250424-win\llama-server.exe
        goto :start_tiers
    ) else (
        echo.
        echo  [VeridianAI] Non-interactive: Vulkan selected
        set LLAMA_BACKEND=backend\llama-server.exe
        goto :start_tiers
    )
)

:: --- Interactive path (human double-click) ---------------------
echo.
echo  +===============================================+
echo  ^|       V E R I D I A N  A I  v2.17           ^|
echo  +===============================================+
echo.
echo  Select backend for this session:
echo.
echo     Vulkan    (recommended default)
echo     IPEX-LLM  (Intel SYCL, legacy)
echo.
choice /C 12 /N /T 10 /D 1 /M "  Your choice [Vulkan in 10s]: "

if !errorlevel!==2 (
    echo.
    echo  [VeridianAI] Backend: IPEX-LLM SYCL selected
    set SYCL_PI_LEVEL_ZERO_USE_IMMEDIATE_COMMANDLISTS=1
    set LLAMA_BACKEND=backend\llama-cpp-ipex-llm-2.3.0b20250424-win\llama-server.exe
) else (
    echo.
    echo  [VeridianAI] Backend: Vulkan selected
    set LLAMA_BACKEND=backend\llama-server.exe
)

:start_tiers
title VeridianAI v2.17
echo.
echo  +===============================================+
echo  ^|       V E R I D I A N  A I  v2.17           ^|
echo  +===============================================+
echo.
:: ============================================================================
:: VeridianAI v2.1.5+ launcher -- Phase 1A (deterministic three-tier startup)
::
:: Tiers:
::   Oracle  -> Ollama          on 127.0.0.1:11434  (heavy reasoning, GPU)
::   Toga    -> llama-server    on 127.0.0.1:11435  (fast chat, CPU)
::   Daemon  -> llama-server    on 127.0.0.1:11436  (mechanics, CPU, tiny)
::
:: This script starts each tier ONCE, then polls each port via curl until it
:: answers. A tier that does not answer within PROBE_TIMEOUT_SEC prints a
:: loud diagnostic and the launch CONTINUES -- see the :warn_* handlers at
:: the bottom. It used to abort instead, which meant a cold Ollama or a
:: machine with no network never got a backend at all (v2.12.17).
:: The tiers keep warming in the background and model_manager routes to
:: whichever are live.
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
:: v2.12.17: this is PER TIER and the three probes run serially, so 90 meant
:: a 270s worst case -- past Electron's backend health timeout, guaranteeing
:: the 'backend is slow' dialog on any machine where the tiers are cold or
:: absent. Now that a slow tier only warns, Electron-launched runs use a much
:: shorter budget: FastAPI comes up promptly and the tiers join when ready.
:: Interactive runs keep the longer window, where a human is watching output.
set PROBE_TIMEOUT_SEC=90
if !ELECTRON_MODE!==1 set PROBE_TIMEOUT_SEC=20
:: v2.1.5: backend selected at startup via choice menu (Vulkan or IPEX-LLM).
:: %~dp0 expands to the directory this .bat file lives in (with trailing
:: backslash), so renaming the project folder never breaks llama-server
:: startup. LLAMA_BACKEND is set above by the choice block.
set LLAMA_SERVER=%~dp0!LLAMA_BACKEND!

:: v2.2 fix (2026-05-29): MODELS_DIR is now self-locating. Previously
:: hardcoded to Todd's E:\sage_data\models, which broke any install on
:: another drive. %~dp0 resolves to the project directory; ..\sage_data
:: walks up one level (sage_data lives ALONGSIDE the app folder, not
:: inside it -- see BEFORE_RUNNING.txt). Works on any drive, any path.
set MODELS_DIR=%~dp0..\sage_data\models

:: v2.2: model filenames are now env vars (still defaults, but no longer
:: hidden in Python source). config.py reads SAGE_MODEL_FILE and
:: DAEMON_MODEL_FILE to build its MODEL_SAGE / MODEL_DAEMON paths.
:: v2.11.12e: SAGE_MODEL_FILE is now a DEFAULT CANDIDATE, not a requirement.
:: Pre-set the env var to use a different gguf; if the file doesn't exist
:: the Toga tier is simply skipped (see preflight below) — a fresh install
:: needs NO specific model to start. The old behavior hard-aborted the
:: entire launch when this exact file was missing, which (a) blocked fresh
:: installs behind one arbitrary model and (b) name-dropped a third party
:: (All Hands / OpenHands is their real project) as if it were required.
if "%SAGE_MODEL_FILE%"=="" set SAGE_MODEL_FILE=qwen2.5_coder_1.5b_instruct.gguf
:: v2.14.1: CANDIDATE LISTS, mirroring store_launch.py exactly.
::
:: These were single hardcoded filenames, and both named the model that was
:: SUPERSEDED when the bundled chat model changed from the base checkpoint to
:: the instruct one. store_launch.py was updated then; this file was not. The
:: result: the MSIX found bundled_models\qwen2.5_coder_1.5b_instruct.gguf and
:: the portable looked for ..._base.gguf, found nothing, skipped the tier, and
:: therefore had no local model to preselect -- while the same folder, in the
:: same package, plainly contained a usable model.
::
:: Two sources of truth for one fact, one of them updated. A list makes the
:: next swap survivable: add the new name at the front, the old one still
:: resolves for anyone who has it.
:: v2.13: embed tier (nomic-embed). Serves CRAIID's warm-handoff turn
:: selection AND sage_rag semantic search via backend\embeddings.py.
set "DAEMON_CANDS=qwen2.5_coder_1.5b_instruct.gguf qwen2.5_coder_1.5b_base.gguf"
set "EMBED_CANDS=nomic_embed_text_v2_moe.gguf nomic_embed_text_latest.gguf"
:: An explicit env override is tried FIRST, then the shipped candidates.
if not "%DAEMON_MODEL_FILE%"=="" set "DAEMON_CANDS=%DAEMON_MODEL_FILE% %DAEMON_CANDS%"
if not "%EMBED_MODEL_FILE%"==""  set "EMBED_CANDS=%EMBED_MODEL_FILE% %EMBED_CANDS%"

set SAGE_MODEL=%MODELS_DIR%\%SAGE_MODEL_FILE%

:: Resolve daemon + embed: EVERY candidate in the user's models dir first,
:: then every candidate in bundled_models -- the same precedence
:: store_launch.py uses, so a user-supplied model is never overridden by a
:: bundled second choice.
::
:: `if not defined` is evaluated per-iteration at run time, so this needs no
:: delayed expansion; the first hit wins and later iterations are no-ops.
set "DAEMON_MODEL="
set "EMBED_MODEL="
for %%C in (%DAEMON_CANDS%) do (
    if not defined DAEMON_MODEL if exist "%MODELS_DIR%\%%C" (
        set "DAEMON_MODEL=%MODELS_DIR%\%%C"
        set "DAEMON_MODEL_FILE=%%C"
    )
)
for %%C in (%DAEMON_CANDS%) do (
    if not defined DAEMON_MODEL if exist "%~dp0bundled_models\%%C" (
        set "DAEMON_MODEL=%~dp0bundled_models\%%C"
        set "DAEMON_MODEL_FILE=%%C"
    )
)
for %%C in (%EMBED_CANDS%) do (
    if not defined EMBED_MODEL if exist "%MODELS_DIR%\%%C" (
        set "EMBED_MODEL=%MODELS_DIR%\%%C"
        set "EMBED_MODEL_FILE=%%C"
    )
)
for %%C in (%EMBED_CANDS%) do (
    if not defined EMBED_MODEL if exist "%~dp0bundled_models\%%C" (
        set "EMBED_MODEL=%~dp0bundled_models\%%C"
        set "EMBED_MODEL_FILE=%%C"
    )
)

:: v2.2: bundled_models\ at project root is a fallback for the daemon and
:: embed models, so a fresh distribution install can launch those tiers
:: without downloading anything. Toga is NOT bundled (~6 GB), so for Toga
:: MODELS_DIR is the only lookup location and the tier is simply skipped
:: when it is absent -- a fresh install needs no specific model to start.
::
:: v2.14.1: the resolution itself moved up to the candidate-list loops
:: above. BUNDLED_DAEMON_MODEL used to be built here from a single
:: filename; it is gone because that single filename was the bug.

:: -- Per-tier context sizes (Phase 1D Step 1) ----------------------------
:: These control llama-server working memory per tier. Cannot be changed
:: while the process is running -- a restart is required. The UI restart
:: endpoints added in Step 4 will kill and respawn the relevant server
:: when the user clicks Refresh Models after changing values.
::
:: These are FALLBACKS ONLY. _tier_config_reader.py normally overrides them
:: from config.json a few hundred lines below (it currently returns 32768 for
:: Toga). They take effect exactly when that helper CANNOT run -- Python not
:: found, config.json unreadable -- which is to say, when something has already
:: gone wrong. A fallback must therefore be the SAFEST value, not the largest.
::
:: v2.15.2 -- WHY THESE CHANGED. This block said:
::
::     Toga : OpenHands 7B, trained on 32768. 16384 is half of trained
::            window ... KV cost ~900 MB on top of the 6.2 GB model.
::     Daemon : Qwen 1.5B. 8192 is plenty ...
::
:: while the line under it read `set SAGE_CTX_SIZE=256000`. The reasoning was
:: sound and the number did not match it -- the same comment-vs-value drift
:: that left a 15.5-hour timeout sitting under a comment saying "5 min".
::
:: 256000 was not merely inconsistent, it was the known-bad value. See
:: config.SAGE_CTX_MAX: an unclamped ctx of this magnitude "made the CPU Toga
:: tier try a ~30 GB KV-cache allocation at every boot (intermittent boot
:: failure + system-wide thrash)", which is why the 65536 clamp exists at all.
:: This fallback would have reproduced that incident on the one path where the
:: clamp is not consulted.
::
:: Now matched to config.SAGE_CTX_DEFAULT / DAEMON_CTX_DEFAULT, which is also
:: what the helper returns -- so a helper failure changes WHERE the number came
:: from, not what it is. OpenHands 7B is trained on 32768, so this is its full
:: trained window and asking for more buys nothing but RAM.
set SAGE_CTX_SIZE=32768
set DAEMON_CTX_SIZE=4096
set EMBED_CTX_SIZE=2048

:: -- Preflight: curl must exist (Windows 10 1803+ ships with it)
where curl.exe >nul 2>&1
if !errorlevel! neq 0 (
    echo [VeridianAI] curl.exe not found on PATH -- readiness probes will be
    echo           SKIPPED. curl ships with Windows 10 1803+.
    echo           VeridianAI will still start; tiers warm unobserved.
    set NO_CURL=1
)

:: -- Preflight: llama-server.exe
:: v2.12.17: no longer fatal. tier_launcher.py already skips the Toga and
:: Daemon tiers when LLAMA_SERVER is blank, exactly as it does for a missing
:: model file. Aborting here meant that picking the IPEX backend on a build
:: without that folder killed the whole app instead of dropping two tiers.
if not exist "%LLAMA_SERVER%" (
    echo [VeridianAI] llama-server.exe not found at:
    echo           %LLAMA_SERVER%
    echo           Toga and Daemon tiers will be SKIPPED. Chat routes through
    echo           the Oracle tier ^(Ollama^). VeridianAI will still start.
    set "LLAMA_SERVER="
)

:: -- Preflight: embed model -- sage_data first, then the bundled copy.
:: Not fatal: without it, semantic features fall back to lexical (and
:: backend\embeddings.py logs that once).
if defined EMBED_MODEL (
    echo [VeridianAI] Embed model: %EMBED_MODEL_FILE%
) else (
    echo [VeridianAI] Embed model not found -- semantic search and CRAIID
    echo           handoff selection will use lexical matching.
    echo           Looked for: %EMBED_CANDS%
    echo           in:         %MODELS_DIR%
    echo           and:        %~dp0bundled_models
)

:: -- Preflight: daemon model — try sage_data first, then bundled_models
:: v2.2: if user doesn't have a daemon model in sage_data, fall back to
:: the bundled copy under the project. Toga model preflight is deferred
:: until after we know whether Toga tier is even launching (see backend
:: branch below). If neither location has the daemon model, we let the
:: launcher proceed and the daemon tier will simply skip — daemon is
:: non-critical (background mechanics).
if defined DAEMON_MODEL (
    echo [VeridianAI] Daemon model: %DAEMON_MODEL_FILE%
    set DAEMON_MODEL_PRESENT=1
) else (
    echo [VeridianAI] Daemon model not found in sage_data or bundled_models.
    echo           Daemon tier will be skipped ^(mechanics background work
    echo           will be reduced but the rest of VeridianAI is unaffected^).
    echo           Looked for: %DAEMON_CANDS%
    echo           in:         %MODELS_DIR%
    echo           and:        %~dp0bundled_models
    set DAEMON_MODEL_PRESENT=0
)

:: ============================================================================
:: Phase 1D Step 3: Python detection moved EARLY so we can call
:: _tier_config_reader.py to read live ctx sizes from config.json before
:: spawning the llama-server tiers. Falls through to the hardcoded
:: SAGE_CTX_SIZE/DAEMON_CTX_SIZE defaults if Python or the helper fails.
:: ============================================================================
set PYTHON_CMD=
:: (Bundled-Python check moved BELOW the explicit pins -- see v2.13.18 note.)
:: v2.13.18 -- INTERPRETER SELECTION, most explicit first.
::
:: Order matters and got this wrong once already. A bundled python\ folder used
:: to win unconditionally, including over a pin the user had deliberately set,
:: and including in the PORTABLE build. That is fine for the Store package,
:: where the bundled interpreter is the only one we are permitted to use -- but
:: on a portable install it silently moved the app off the system Python the
:: user had installed their optional packages into. Speech recognition stopped
:: being found, with no error and no obvious cause, immediately after an
:: unrelated update. Auto-detection must never outrank an explicit choice.
::
:: `py` is the Windows launcher and it selects the NEWEST installed Python, not
:: the one this install was set up against. Install a new Python for any reason
:: and every package pip-installed under the old one leaves the app's view at
:: once -- nothing was uninstalled, a different interpreter is simply looking
:: somewhere else. Optional extras (speech recognition) stop working, and it
:: presents as "the app broke", usually days later, with no obvious connection
:: to the Python install that caused it.
::
:: Two ways to pin, checked in order. Both are opt-in; behaviour is unchanged
:: for anyone who sets neither.
::
::   1. VERIDIAN_PYTHON       environment variable -- full path to python.exe
::   2. python_pin.txt        beside start.bat, one line, same content
::
:: A pin that no longer exists is IGNORED with a warning rather than being
:: obeyed into a failure: a stale pin should degrade to the old behaviour, not
:: stop the app from starting.
if "!PYTHON_CMD!"=="" (
    if defined VERIDIAN_PYTHON (
        if exist "!VERIDIAN_PYTHON!" (
            set "PYTHON_CMD=!VERIDIAN_PYTHON!"
            echo [VeridianAI] Using pinned Python from VERIDIAN_PYTHON
        ) else (
            echo [VeridianAI] WARNING: VERIDIAN_PYTHON is set but not found:
            echo               !VERIDIAN_PYTHON!
            echo               Ignoring it and detecting Python normally.
        )
    )
)
if "!PYTHON_CMD!"=="" (
    if exist "%~dp0python_pin.txt" (
        set /p _PIN=<"%~dp0python_pin.txt"
        if exist "!_PIN!" (
            set "PYTHON_CMD=!_PIN!"
            echo [VeridianAI] Using pinned Python from python_pin.txt
        ) else (
            echo [VeridianAI] WARNING: python_pin.txt points at a missing file:
            echo               !_PIN!
            echo               Ignoring it and detecting Python normally.
        )
        set "_PIN="
    )
)
:: Bundled interpreter: mandatory for the Store build (an MSIX app may not
:: install anything at runtime, so this is the only one we may depend on), and
:: a sensible default for a portable install that has one -- but only after the
:: user's own explicit pin has had its say.
:: Built by tools\bundle_python.ps1.
if "!PYTHON_CMD!"=="" (
    if exist "%~dp0python\python.exe" (
        set "PYTHON_CMD=%~dp0python\python.exe"
        echo [VeridianAI] Using bundled Python
    )
)
if "!PYTHON_CMD!"=="" (
    py --version >nul 2>&1
    if !errorlevel!==0 set PYTHON_CMD=py
)
if "!PYTHON_CMD!"=="" (
    python --version >nul 2>&1
    if !errorlevel!==0 set PYTHON_CMD=python
)
if "!PYTHON_CMD!"=="" (
    python3 --version >nul 2>&1
    if !errorlevel!==0 set PYTHON_CMD=python3
)
if "!PYTHON_CMD!"=="" (
    echo [VeridianAI] ERROR: Python not found. Install Python 3.10+
    pause
    exit /b 1
)
echo [VeridianAI] Python: !PYTHON_CMD!

:: Playwright's bundled Chromium, when this tree has one.
:: Only when it EXISTS -- pointing PLAYWRIGHT_BROWSERS_PATH at an empty or
:: missing folder does not degrade gracefully. It tells Playwright the browsers
:: live there, finds none, and then refuses to fall back to the Chrome or Edge
:: it would otherwise have used. Built by tools\bundle_playwright.ps1.
if exist "%~dp0playwright-browsers\*" (
    set "PLAYWRIGHT_BROWSERS_PATH=%~dp0playwright-browsers"
    echo [VeridianAI] Browser: bundled Chromium
) else (
    echo [VeridianAI] Browser: none bundled - will use an installed browser
)

:: ============================================================================
:: v2.11.12 zombie-process fix: reap anything a previous session left behind
:: BEFORE launching tiers. Kills only processes recorded in .oracle_pids.json
:: (identity-verified) plus stack processes running from backend\. A user's
:: own Ollama is never touched. This is what makes restart work on try #1
:: instead of try #3-5 — stale port-holders on 11434/11435/11436 die here.
:: ============================================================================
set "OAI_ROOT=%~dp0"
echo [VeridianAI] Cleaning up any processes left from a previous session ...
"!PYTHON_CMD!" "%~dp0backend\shutdown_cleanup.py" --quiet

:: Read n_ctx + ports + backend from config.json via _tier_config_reader.py.
:: Output: SAGE_CTX,DAEMON_CTX,APP_PORT,OLLAMA_ORACLE_PORT,LLAMA_SAGE_PORT,LLAMA_DAEMON_PORT,INFERENCE_BACKEND
:: If the helper fails for any reason, the for /f loop body simply does
:: not execute and the tunables-block defaults set above (8000 / 11434 /
:: 11435 / 11436 and INFERENCE_BACKEND default below) take effect.
set INFERENCE_BACKEND=ollama
for /f "usebackq tokens=1,2,3,4,5,6,7 delims=," %%a in (`""!PYTHON_CMD!" "%~dp0backend\_tier_config_reader.py"" 2^>nul`) do (
    set "SAGE_CTX_SIZE=%%a"
    set "DAEMON_CTX_SIZE=%%b"
    set "APP_PORT=%%c"
    set "OLLAMA_ORACLE_PORT=%%d"
    set "LLAMA_SAGE_PORT=%%e"
    set "LLAMA_DAEMON_PORT=%%f"
    set "INFERENCE_BACKEND=%%g"
)
echo [VeridianAI] Tier ctx: Toga=!SAGE_CTX_SIZE!, Daemon=!DAEMON_CTX_SIZE!
echo [VeridianAI] Tier ports: App=!APP_PORT! Oracle=!OLLAMA_ORACLE_PORT! Toga=!LLAMA_SAGE_PORT! Daemon=!LLAMA_DAEMON_PORT!
echo [VeridianAI] Inference backend: !INFERENCE_BACKEND!

:: v2.2 corrected semantics (2026-05-29): inference.backend controls
:: which tier USER CHAT routes to, NOT which tiers launch. All three
:: inference tiers (Oracle, Toga, Daemon) always come up because each
:: serves a distinct role -- Oracle = heavy reasoning, Toga = agentic
:: engine (interprets tool tags, runs multi-step plans), Daemon =
:: mechanics. These are the CRAIID substrate (Archivist/Journalist/
:: Author -- see oracleai_roadmap_craiid_v2.md). Skipping a tier
:: because user chat happens to route through another tier is a
:: category error -- the skipped tier still has its own role.
:: INFERENCE_BACKEND is read here for log/diagnostic clarity; future
:: routing code may use it, but tier launch is unconditional.

:: Toga model preflight -- unconditional (Toga tier always launches).
:: v2.2 (2026-05-30) error message: spells out the resolved sage_data
:: location so the user can see exactly where to put the gguf, and
:: explains the sibling-not-inside-project layout so they do not
:: intuitively create sage_data inside the app folder (which would
:: not be found AND would break Trinity separation -- see
:: BEFORE_RUNNING.txt step 3 for the canonical layout).
:: v2.11.12e: missing Toga model is NO LONGER FATAL. Mirror the daemon
:: tier's graceful skip: warn, blank SAGE_MODEL so tier_launcher skips the
:: tier, and continue the launch. Chat routes through the Oracle tier
:: (Ollama) with whatever models the user actually has — no baked-in
:: model requirement on a fresh install.
if not exist "%SAGE_MODEL%" (
    echo.
    echo [VeridianAI] Toga model not found -- Toga tier will be SKIPPED.
    echo    Looked for: %SAGE_MODEL_FILE%
    echo    in:         %MODELS_DIR%
    echo    VeridianAI runs fine without it: chat routes through the
    echo    Oracle tier ^(Ollama^). To enable the Toga tier later, put
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
::   Oracle = Ollama  |  Toga + Daemon = llama-server  |  Toga-Daemon/Overseer = Python
:: v2.12.17: no llama-server means the Toga and Daemon tiers cannot launch,
:: whatever the model preflights above concluded. Clear the flags HERE, after
:: those checks have run, so tier_launcher skips both tiers and we do not
:: then sit in a readiness probe waiting for something that was never spawned.
if not defined LLAMA_SERVER (
    set SAGE_MODEL_PRESENT=0
    set DAEMON_MODEL_PRESENT=0
)

set "OAI_ROOT=%~dp0"
echo [VeridianAI] Launching tiers + daemons (Developer Mode controls visibility) ...
"!PYTHON_CMD!" "%~dp0backend\tier_launcher.py"
:: Soft delay to let ports begin binding before the readiness probes below.
:: v2.12.17: was `timeout /t 3 /nobreak`. timeout.exe refuses to run with
:: redirected stdin -- which is exactly how Electron spawns this script when
:: Developer Mode is off -- so this delay silently never happened. ping is
:: stdin-agnostic. (-n 4 = 3 gaps = ~3s.)
ping -n 4 127.0.0.1 >nul 2>&1

:: -- Probe each tier for readiness
echo.
echo [VeridianAI] Waiting for tiers to come online (max %PROBE_TIMEOUT_SEC%s each)...
echo.

:: v2.11.12c: tier probe failures are FATAL only in interactive mode.
:: In Electron mode (ELECTRON_MODE=1) a slow tier no longer aborts the
:: whole launch. Rationale (Todd's Ryzen AI laptop, 2026-07-02): on a
:: cold boot, reading the 6 GB Toga model off disk on a low-power chip
:: can exceed the probe window; the old `goto fail_*` then aborted
:: BEFORE FastAPI ever launched, so Electron waited forever on a backend
:: that was never started ("first start fails, immediate second start
:: works" — the second try hit a warm file cache). Now we log a warning
:: and continue: the tier keeps loading in the background, llama-server/
:: Ollama answer when ready, and model_manager routes to whatever tiers
:: are up.
::
:: v2.12.17: interactive runs no longer abort either. They still print the
:: full diagnostic (see the :warn_* handlers at the bottom) -- they just no
:: longer `pause` + `exit` before FastAPI is launched. A local-inference app
:: must reach its own UI with no network and no tiers.

:: Oracle uses Ollama's /api/tags endpoint
call :probe_tier "Oracle" !OLLAMA_ORACLE_PORT! "http://127.0.0.1:!OLLAMA_ORACLE_PORT!/api/tags"
if !errorlevel! neq 0 (
    if !ELECTRON_MODE!==1 (
        echo [VeridianAI] WARNING: Oracle tier not ready yet -- continuing. It may finish warming in the background.
    ) else (
        call :warn_oracle
    )
)

:: Toga uses llama-server's OpenAI-compatible /v1/models endpoint.
:: v2.11.12e: probe only when the tier was actually launched (model file
:: present). A fresh install without a Toga gguf skips both launch+probe.
if !SAGE_MODEL_PRESENT!==1 (
    call :probe_tier "Toga  " !LLAMA_SAGE_PORT! "http://127.0.0.1:!LLAMA_SAGE_PORT!/v1/models"
    if !errorlevel! neq 0 (
        if !ELECTRON_MODE!==1 (
            echo [VeridianAI] WARNING: Toga tier not ready yet -- continuing. It may finish warming in the background.
        ) else (
            call :warn_sage
        )
    )
) else (
    echo [VeridianAI] Toga tier skipped ^(no model^) -- probe skipped.
)

:: Daemon probe — only if daemon model was found and tier was launched.
if !DAEMON_MODEL_PRESENT!==1 (
    call :probe_tier "Daemon" !LLAMA_DAEMON_PORT! "http://127.0.0.1:!LLAMA_DAEMON_PORT!/v1/models"
    if !errorlevel! neq 0 (
        if !ELECTRON_MODE!==1 (
            echo [VeridianAI] WARNING: Daemon tier not ready yet -- continuing. It may finish warming in the background.
        ) else (
            call :warn_daemon
        )
    )
)

echo.
echo [VeridianAI] Tier probes done. Launching backend...
echo.

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
    "!PYTHON_CMD!" start.py --port !APP_PORT! --no-browser
) else (
    "!PYTHON_CMD!" start.py --port !APP_PORT!
)
set "RC=!errorlevel!"
:: v2.13: `pause` is a NO-OP when stdin is redirected -- which is exactly
:: how Electron spawns us with Developer Mode off. The old line therefore
:: swallowed the failure and fell through to `exit /b 0`, so Electron was
:: told the backend had exited SUCCESSFULLY. Propagate the real code.
if !RC! neq 0 (
    echo [VeridianAI] Backend exited with error, code !RC!
    if !ELECTRON_MODE!==0 pause
    exit /b !RC!
)
exit /b 0

:: ============================================================================
:: SUBROUTINE: probe_tier <label> <port> <url>
::   Polls <url> every second until curl succeeds OR PROBE_TIMEOUT_SEC elapses.
::   Returns errorlevel 0 on success, 1 on timeout.
:: ============================================================================
:probe_tier
:: No curl means no way to observe readiness. Report ready and move on --
:: the probes are advisory now, so a false 'ready' costs nothing.
if defined NO_CURL exit /b 0
set "LABEL=%~1"
set "PORT=%~2"
set "URL=%~3"
set /a count=0
:probe_loop
curl -fsS --max-time 2 -o nul "%URL%" >nul 2>&1
if !errorlevel!==0 (
    echo [VeridianAI] !LABEL! ^(:!PORT!^) READY after !count!s
    exit /b 0
)
set /a count+=1
if !count! geq %PROBE_TIMEOUT_SEC% (
    echo [VeridianAI] !LABEL! ^(:!PORT!^) FAILED -- no response after !count!s
    exit /b 1
)
:: Progress dots every 5 seconds
set /a mod=count %% 5
if !mod!==0 echo [VeridianAI] !LABEL! ^(:!PORT!^) still waiting ... !count!s
:: v2.12.17: ping instead of timeout.exe -- see the note at the 3s delay
:: above. With timeout.exe this loop span at full speed under Electron,
:: so PROBE_TIMEOUT_SEC counted iterations, not seconds.
ping -n 2 127.0.0.1 >nul 2>&1
goto probe_loop

:: ============================================================================
:: Tier-not-ready handlers.
::
:: v2.12.17 (2026-07-30, the "no internet on the drive to the doctor" fix):
:: these used to `pause` + `exit /b 1`, which aborted the launch BEFORE the
:: :run block ever started FastAPI. A user with no network -- or simply a
:: cold/slow Ollama -- got no backend at all, and eventually the offline
:: screen. VeridianAI is a LOCAL inference app; a slow or absent tier must
:: degrade it, not kill it.
::
:: Electron mode already warned-and-continued (see the comment block above
:: the probes, added 2026-07-02 for the Ryzen AI cold-boot case). These are
:: now `call`ed subroutines so the interactive double-click path behaves the
:: same way: print the same diagnostics, then carry on to :run.
:: ============================================================================
:warn_oracle
echo.
echo [VeridianAI] ============================================================
echo [VeridianAI] ORACLE TIER NOT READY (Ollama on :!OLLAMA_ORACLE_PORT!) -- CONTINUING
echo [VeridianAI] ============================================================
echo [VeridianAI] Check the "Ollama-Oracle" window for errors. Common causes:
echo [VeridianAI]   - ollama.exe not installed or not on PATH
echo [VeridianAI]   - port !OLLAMA_ORACLE_PORT! already in use by another process
echo [VeridianAI]   - GPU driver / VRAM issue on first model load
echo [VeridianAI] Run `ollama serve` manually in a terminal to see the error.
echo [VeridianAI] Continuing anyway -- the backend WILL start; this tier joins
echo [VeridianAI] in as soon as it answers.
exit /b 0

:warn_sage
echo.
echo [VeridianAI] ============================================================
echo [VeridianAI] TOGA TIER NOT READY (llama-server on :!LLAMA_SAGE_PORT!) -- CONTINUING
echo [VeridianAI] ============================================================
echo [VeridianAI] Check the "Llama-Toga" window for errors. Common causes:
echo [VeridianAI]   - model file not found: %SAGE_MODEL%
echo [VeridianAI]   - port !LLAMA_SAGE_PORT! already in use
echo [VeridianAI]   - insufficient RAM for the model
echo [VeridianAI] Alternative: set inference.backend to "ollama" in config.json
echo [VeridianAI] and VeridianAI will serve Toga chat through the Oracle tier.
echo [VeridianAI] Continuing anyway -- the backend WILL start; this tier joins
echo [VeridianAI] in as soon as it answers.
exit /b 0

:warn_daemon
echo.
echo [VeridianAI] ============================================================
echo [VeridianAI] DAEMON TIER NOT READY (llama-server on :!LLAMA_DAEMON_PORT!) -- CONTINUING
echo [VeridianAI] ============================================================
echo [VeridianAI] Check the "Llama-Daemon" window for errors. Common causes:
echo [VeridianAI]   - model file not loadable: %DAEMON_MODEL%
echo [VeridianAI]   - port !LLAMA_DAEMON_PORT! already in use
echo [VeridianAI]   - insufficient RAM (~1.5 GB needed)
echo [VeridianAI] Continuing anyway -- daemon work is background mechanics.
exit /b 0
