@echo off
:: ============================================================================
:: prep_distribution.bat -- stage a clean OracleAI copy for distribution
::
:: Usage:
::   prep_distribution.bat                    -> stages to ..\<source_basename>_dist\
::   prep_distribution.bat C:\some\path       -> stages to that path
::
:: This script NEVER touches the source folder. It copies the project to a
:: staging folder, then strips user-personal artifacts (encryption keys,
:: chat archives, personal downloads, caches, migration backups, dev notes)
:: from the COPY. The result is what you zip / put on a USB / hand to a
:: tester.
::
:: Run from the OracleAI project root.
:: ============================================================================

setlocal EnableDelayedExpansion

:: Source = directory this script lives in (the project root)
set "SRC=%~dp0"
if "!SRC:~-1!"=="\" set "SRC=!SRC:~0,-1!"

:: Default destination = sibling folder <source_basename>_dist
:: v2.3 (2026-05-31): derived from source-folder basename rather than
:: hardcoded so future folder renames track without hand-editing.
if "%~1"=="" (
    for %%I in ("!SRC!") do set "DST=%%~dpI%%~nxI_dist"
) else (
    set "DST=%~1"
)

:: Safety: abort if destination would clobber source
if /I "!SRC!"=="!DST!" (
    echo  [X] Destination is the same as source. Aborting to prevent data loss.
    exit /b 1
)

echo.
echo  +======================================================+
echo  ^|  OracleAI Distribution Prep                          ^|
echo  +======================================================+
echo.
echo   Source:      !SRC!
echo   Destination: !DST!
echo.

if exist "!DST!" (
    echo  [!] Destination already exists.
    choice /C YN /M "  Overwrite it (this deletes the existing copy)? "
    if errorlevel 2 (
        echo  Aborted by user.
        exit /b 1
    )
    echo  Removing existing destination...
    rd /S /Q "!DST!" 2>nul
)

echo  Copying project tree to staging folder (this may take a minute)...
xcopy "!SRC!" "!DST!\" /E /I /Q /Y >nul
if errorlevel 1 (
    echo  [X] Copy failed.
    exit /b 1
)
echo  Copy complete.
echo.

echo  Stripping user-personal artifacts from the COPY...
echo.

:: --- Encryption keys (MOST IMPORTANT to remove) -----------------------------
:: These are per-install secrets. Shipping yours would let recipients decrypt
:: data encrypted with them.
call :delete_file "!DST!\backend\.fernet_key" "Fernet memory-log encryption key"
call :delete_file "!DST!\backend\.aiq_nudge_key" "AIQNudge HMAC signing key"
call :delete_file "!DST!\backend\.api_keystore.json" "v2.3 bearer-token keystore (per-install secret)"

:: --- Personal chat data -----------------------------------------------------
call :delete_dir "!DST!\archives" "personal chat archives"
call :delete_dir "!DST!\downloads" "personal downloaded files"

:: --- Build artifacts and caches ---------------------------------------------
call :delete_dir "!DST!\electron\node_modules" "Electron node_modules"
call :delete_pycache "!DST!"

:: --- Migration / repair-session orphans -------------------------------------
call :delete_file "!DST!\electron\main.js.tail-clean" "session orphan from repairs"
for %%F in ("!DST!\config.json.v1_backup_*.json") do call :delete_file "%%F" "config.json migration backup"
for %%F in ("!DST!\backend\main.py.bak_*") do call :delete_file "%%F" "main.py backup snapshot"
for %%F in ("!DST!\backend\main.py.truncated_state_for_audit") do call :delete_file "%%F" "audit snapshot"

:: --- Dev notes (internal only) ----------------------------------------------
call :delete_dir "!DST!\_oracle_dev" "internal dev notes"

:: --- This script itself (not for end users) ---------------------------------
call :delete_file "!DST!\prep_distribution.bat" "prep_distribution.bat (build tool, not for users)"

:: --- Reset config.json so recipient gets clean defaults on first boot -------
:: The dataclass defaults in config_store.py are distribution-safe (no
:: Todd-specific paths, models, or prompt). Deleting config.json forces the
:: backend to write a fresh v2 default on first boot via OracleConfig.save.
if exist "!DST!\config.json" (
    del /F /Q "!DST!\config.json" >nul 2>&1
    echo    [-] Deleted config.json  ^(backend will create v2 default on first boot^)
) else (
    echo    [.] No config.json to delete
)

echo.
echo  +======================================================+
echo  ^|  Done!                                                ^|
echo  +======================================================+
echo.
echo   Staging folder:  !DST!
echo.
echo   Items deliberately KEPT in the copy:
echo     - All source code (backend\, electron\, frontend\, start.bat, start.py)
echo     - BEFORE_RUNNING.txt
echo     - OracleAI_REFERENCE.md
echo     - electron\.backend_mode  (default vulkan, fine for any GPU)
echo     - Empty sage_data\ directories are NOT in the copy
echo       ^(sage_data lives outside the project; recipient's install creates
echo        its own at the same relative location^)
echo.
echo   Verify before shipping:
echo     1. Open !DST! in Explorer, eyeball the contents
echo     2. Confirm backend\.fernet_key is NOT there
    2b. Confirm backend\.api_keystore.json is NOT there
echo     3. Confirm archives\ and downloads\ are NOT there
echo     4. Confirm config.json is NOT there ^(or is a clean stub^)
echo     5. Zip the folder or copy to USB
echo     6. Hand to tester along with BEFORE_RUNNING.txt instructions
echo.
exit /b 0


:: ============================================================================
:: SUBROUTINES
:: ============================================================================

:delete_file
if exist "%~1" (
    del /F /Q "%~1" >nul 2>&1
    if exist "%~1" (
        echo    [!] Could not delete %~nx1
    ) else (
        echo    [-] Removed %~2
    )
) else (
    echo    [.] Not present: %~nx1
)
exit /b 0

:delete_dir
if exist "%~1\" (
    rd /S /Q "%~1" >nul 2>&1
    if exist "%~1\" (
        echo    [!] Could not delete %~nx1\
    ) else (
        echo    [-] Removed %~2
    )
) else (
    echo    [.] Not present: %~nx1\
)
exit /b 0

:delete_pycache
set "pcount=0"
for /d /r "%~1" %%D in (__pycache__) do (
    if exist "%%D\" (
        rd /S /Q "%%D" >nul 2>&1
        if not exist "%%D\" set /a pcount+=1
    )
)
echo    [-] Removed !pcount! __pycache__ directories
exit /b 0
