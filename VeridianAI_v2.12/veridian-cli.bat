@echo off
:: veridian-cli.bat — launch the VeridianAI terminal client.
:: The backend stack must already be running (start.bat / VeridianAI.exe).
:: All arguments pass through, e.g.:  veridian-cli --list-models
setlocal
set "SCRIPT=%~dp0tools\veridian_cli.py"
set "PYTHON_CMD="
py --version >nul 2>&1
if %errorlevel%==0 set PYTHON_CMD=py
if "%PYTHON_CMD%"=="" (
    python --version >nul 2>&1
    if %errorlevel%==0 set PYTHON_CMD=python
)
if "%PYTHON_CMD%"=="" (
    echo [veridian-cli] ERROR: Python not found. Install Python 3.10+
    exit /b 1
)
%PYTHON_CMD% "%SCRIPT%" %*
endlocal
