@echo off
setlocal enabledelayedexpansion
title iPhone-PC Bridge - Firewall Setup

net session >nul 2>&1
if not "%errorlevel%"=="0" (
    echo.
    echo   Requesting administrator rights, please click YES in the UAC dialog...
    echo.
    powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo.
echo   ============================================
echo    iPhone - PC Bridge : firewall setup
echo   ============================================
echo.

echo   [1/3] Removing old rule if any...
netsh advfirewall firewall delete rule name="DropToWin-8899" >nul 2>&1

echo   [2/3] Adding inbound rule for TCP 8899...
netsh advfirewall firewall add rule name="DropToWin-8899" dir=in action=allow protocol=TCP localport=8899 profile=private,domain >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [X] FAILED to add the rule.
    echo       Please right-click this file and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)

echo   [3/3] Verifying...
netsh advfirewall firewall show rule name="DropToWin-8899" | findstr /C:"8899" >nul
if errorlevel 1 (
    echo.
    echo   [X] Rule was not created properly.
    echo.
    pause
    exit /b 1
)

echo.
echo   [OK] Port 8899 is now open for private networks.
echo.
echo   --- Your IPv4 addresses ---
ipconfig | findstr /C:"IPv4"
echo.
echo   Open this on your iPhone Safari:
echo       http://^<the-192.168.x.x-address-above^>:8899
echo.
echo   If it still fails, check that the phone is on the same Wi-Fi,
echo   and that the router has "AP isolation" turned OFF.
echo.
pause
