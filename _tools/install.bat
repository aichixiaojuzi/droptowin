@echo off
setlocal
title DropToWin Setup
chcp 936 >nul 2>&1
cd /d "%~dp0"

rem ============================================================
rem  Step 1: make sure we run as administrator (needed for firewall)
rem  fltmc fails for normal users, succeeds for admins.
rem  The "elevated" marker prevents an endless re-launch loop.
rem
rem  NOTE: keep this file pure ASCII. cmd.exe reads .bat in the OEM
rem  codepage, so UTF-8 Chinese here would show up as garbage.
rem  All user-facing Chinese lives in install.ps1 (UTF-8 with BOM).
rem ============================================================

if /i "%~1"=="elevated" goto run

rem DryRun does not touch the firewall, so it needs no elevation.
if /i "%~1"=="-DryRun" goto run

fltmc >nul 2>&1
if not errorlevel 1 goto run

echo.
echo    Administrator rights are needed to configure the firewall.
echo    Requesting elevation ...
echo.

powershell -NoProfile -Command "try { Start-Process -FilePath '%~f0' -ArgumentList 'elevated %*' -Verb RunAs -ErrorAction Stop } catch { exit 1 }"

if errorlevel 1 (
    echo.
    echo    Elevation was cancelled.
    echo    Run this file again and click "Yes" on the UAC prompt.
    echo.
    timeout /t 6 >nul
)
exit /b

rem ============================================================

:run
rem Strip the "elevated" marker but keep every other argument
rem (-DryRun / -Lang en ...), so they reach install.ps1 intact.
set "ARGS="

:argloop
if "%~1"=="" goto argdone
if /i not "%~1"=="elevated" set "ARGS=%ARGS% %~1"
shift
goto argloop

:argdone
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"%ARGS%
echo.
pause
