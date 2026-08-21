@echo off
setlocal
title NyaaReader - Docker launcher
cd /d "%~dp0"

echo ============================================
echo   🐾 NyaaReader - Docker launcher
echo ============================================
echo.

REM Check Docker is available
docker --version >nul 2>&1
if errorlevel 1 (
    echo [!] Docker is not installed or not in PATH.
    echo     Please install Docker Desktop and make sure it is running.
    echo.
    pause
    exit /b 1
)

REM Check Docker daemon is running
docker info >nul 2>&1
if errorlevel 1 (
    echo [!] Docker daemon is not running.
    echo     Please start Docker Desktop first, then run this again.
    echo.
    pause
    exit /b 1
)

REM Combine public code with the private scraper vault (if present).
REM This overlays your private plugins into scrapers/ so the build has them,
REM while the public repo stays clean.
echo [*] Combining private scraper plugins (nyaareader-scrapper)...
python combine.py
if errorlevel 3 (
    REM exit code 3 = no private vault configured - the normal public case
    echo [i] No private vault found - building with public plugins only.
) else if errorlevel 1 (
    echo [!] combine step failed - continuing anyway (public build only).
    echo     Set up the private vault to get your private site plugins.
)
echo.

echo [*] Building and starting containers...
echo     (first build may take a few minutes)
echo.
docker compose up --build

echo.
echo [*] Containers stopped. Goodbye!
echo.
pause
