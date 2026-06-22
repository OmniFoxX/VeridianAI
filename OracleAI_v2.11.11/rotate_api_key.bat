@echo off
:: rotate_api_key.bat -- OracleAI API key rotation launcher
:: =========================================================
:: v2.3.1 (2026-06-06)
::
:: Run this from the OracleAI project root to rotate the default
:: bearer token. Wraps rotate_api_key.py with a friendly console
:: window that stays open so you can copy the new token.
::
:: Usage: double-click, or run from terminal:
::     rotate_api_key.bat

setlocal EnableDelayedExpansion

:: ---------------------------------------------------------------------------
:: Locate the project root (same folder as this .bat file)
:: ---------------------------------------------------------------------------
set "PROJECT_ROOT=%~dp0"
:: Strip trailing backslash
if "%PROJECT_ROOT:~-1%"=="\" set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"

set "SCRIPT=%PROJECT_ROOT%\rotate_api_key.py"
set "BACKEND=%PROJECT_ROOT%\backend"

:: ---------------------------------------------------------------------------
:: Sanity checks
:: ---------------------------------------------------------------------------
if not exist "%SCRIPT%" (
    echo.
    echo   ERROR: rotate_api_key.py not found at:
    echo       %SCRIPT%
    echo.
    echo   Make sure rotate_api_key.bat lives in the OracleAI project root.
    echo.
    goto :done
)

if not exist "%BACKEND%\auth.py" (
    echo.
    echo   ERROR: backend\auth.py not found at:
    echo       %BACKEND%\auth.py
    echo.
    echo   Cannot rotate without auth module.
    echo.
    goto :done
)

:: ---------------------------------------------------------------------------
:: Prefer the venv Python if present, fall back to system Python
:: ---------------------------------------------------------------------------
set "VENV_PYTHON=%PROJECT_ROOT%\venv\Scripts\python.exe"
set "VENV_PYTHON_ALT=%PROJECT_ROOT%\.venv\Scripts\python.exe"

if exist "%VENV_PYTHON%" (
    set "PYTHON=%VENV_PYTHON%"
) else if exist "%VENV_PYTHON_ALT%" (
    set "PYTHON=%VENV_PYTHON_ALT%"
) else (
    set "PYTHON=python"
)

:: ---------------------------------------------------------------------------
:: Run the rotator
:: ---------------------------------------------------------------------------
echo.
echo   OracleAI -- launching key rotator ...
echo   Python: %PYTHON%
echo.

cd /d "%PROJECT_ROOT%"
"%PYTHON%" "%SCRIPT%"

if errorlevel 1 (
    echo.
    echo   rotate_api_key.py exited with an error.
    echo   See output above for details.
)

:done
echo.
pause
endlocal