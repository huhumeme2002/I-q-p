@echo off
title iFlow Proxy for Claude Code
cd /d "%~dp0"

echo ============================================
echo   iFlow Proxy for Claude Code
echo ============================================

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+
    pause
    exit /b 1
)

:: Install dependencies if needed
if not exist ".deps_installed" (
    echo Installing dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies
        pause
        exit /b 1
    )
    echo. > .deps_installed
)

echo Starting proxy on port 8083...
echo Admin panel: http://localhost:8083/admin
echo.
python proxy.py

pause
