@echo off
REM Azure Access Manager - one-click start for Windows.
REM First run: creates .venv and installs dependencies. Then opens the UI in your browser.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating Python virtual environment...
    py -3 -m venv .venv 2>nul
    if not exist ".venv\Scripts\python.exe" python -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo.
        echo Python 3.11 or newer is required. Install it from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH" during installation, then run this file again.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" -c "import streamlit, azure.identity, azure.mgmt.authorization, yaml" 2>nul
if errorlevel 1 (
    echo Installing dependencies - this takes a minute the first time...
    ".venv\Scripts\python.exe" -m pip --version >nul 2>&1 || ".venv\Scripts\python.exe" -m ensurepip --upgrade
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
    if errorlevel 1 (
        echo Dependency installation failed. See the messages above.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" azure_manager.py ui %*
