@echo off
setlocal EnableDelayedExpansion
title VeridianAI - Version Bump

:: ============================================================================
:: bump_version.bat -- VeridianAI version-string bumper
::
:: v2.3.0 (2026-05-31)
::
:: Updates every canonical version-string location across the project in
:: ONE pass. The real work is in _bump_version.py; this wrapper handles
:: argument parsing, prompts, and Python detection.
::
:: USAGE:
::   bump_version.bat                       -> interactive prompts
::   bump_version.bat 2.3 2.4               -> non-interactive bump
::   bump_version.bat 2.3 2.4 --dry-run     -> preview without writing
::   bump_version.bat 2.3 2.4 --verbose     -> show no-ops too
::
:: Version forms accepted: 2.3, v2.3, 2.3.0, V2.3.0
::
:: WHAT THIS SCRIPT DOES NOT DO:
:: - Rename the project folder (intentional separate step; breaks
::   Continue.dev configs, cowork mounts, shortcuts that point at the
::   old name)
:: - Touch dated historical fix-marker comments
:: - Modify backup files (.bak_*, .repaired, etc.)
:: ============================================================================

:: -- Python detection (matches start.bat's approach)
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
    echo [ERROR] Python not found. Install Python 3.10+ and ensure it's on PATH.
    pause
    exit /b 1
)

:: -- Argument handling
set "OLD_VER=%~1"
set "NEW_VER=%~2"
set "EXTRA_ARGS="

:: Collect any remaining flags (--dry-run, --verbose) into EXTRA_ARGS
shift
shift
:collect_flags
if "%~1"=="" goto :flags_done
set "EXTRA_ARGS=!EXTRA_ARGS! %~1"
shift
goto :collect_flags
:flags_done

:: -- Interactive prompts if not provided
if "!OLD_VER!"=="" (
    echo.
    echo  +===========================================+
    echo  ^|     VeridianAI Version Bump                 ^|
    echo  +===========================================+
    echo.
    set /p OLD_VER="  Current version (e.g. 2.3 or v2.3): "
)
if "!NEW_VER!"=="" (
    set /p NEW_VER="  New version     (e.g. 2.4 or v2.4): "
)
if "!OLD_VER!"=="" (
    echo [ERROR] Old version required.
    pause
    exit /b 1
)
if "!NEW_VER!"=="" (
    echo [ERROR] New version required.
    pause
    exit /b 1
)

:: -- Run the worker
echo.
!PYTHON_CMD! "%~dp0_bump_version.py" "!OLD_VER!" "!NEW_VER!"!EXTRA_ARGS!
set "EXITCODE=!errorlevel!"

echo.
if !EXITCODE!==0 (
    echo  [OK] Version bump completed cleanly.
) else if !EXITCODE!==2 (
    echo  [WARN] Version bump completed with warnings ^(missing anchors^).
    echo         Inspect the report above before relying on the result.
) else (
    echo  [ERROR] Version bump failed ^(exit code !EXITCODE!^).
)

:: -- If invoked interactively (no args), pause so user can read output.
if "%~1"=="" pause
exit /b !EXITCODE!
