@echo off
setlocal EnableExtensions EnableDelayedExpansion
title tvapp
cd /d "%~dp0"

set "PORT=8777"
set "URL=http://127.0.0.1:%PORT%/"

echo ============================================
echo   tvapp - live channel browser
echo ============================================
echo.

REM ---------- 1. find python ----------
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY ( where py >nul 2>&1 && set "PY=py -3" )
if not defined PY (
    echo [X] Python not found on PATH.
    echo     Install it from https://www.python.org/downloads/ and tick
    echo     "Add python.exe to PATH", then run this again.
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo [1/5] %%v found

REM ---------- 2. make sure the player library exists ----------
if not exist "hls.min.js" (
    echo [2/5] hls.min.js missing - downloading...
    curl -sL --max-time 90 -o "hls.min.js" "https://cdn.jsdelivr.net/npm/hls.js@1.5.20/dist/hls.min.js"
    if not exist "hls.min.js" (
        echo [X] Download failed. Check your connection.
        pause
        exit /b 1
    )
) else (
    echo [2/5] player library ok
)

REM ---------- 3. make sure there is a channel list ----------
if not exist "channels.json" (
    echo [3/5] channels.json missing - rebuilding from the harvest...
    %PY% finalize.py
    if errorlevel 1 (
        echo [X] Rebuild failed.
        pause
        exit /b 1
    )
    if exist "final_pass.py" %PY% final_pass.py
) else (
    echo [3/5] channel list ok
)

REM ---------- 4. clear a stale server on the port ----------
set "STALE="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:"LISTENING" ^| findstr ":%PORT% "') do set "STALE=%%p"
if defined STALE (
    echo [4/5] port %PORT% busy - restarting the old instance
    taskkill /PID !STALE! /F >nul 2>&1
    timeout /t 1 /nobreak >nul
) else (
    echo [4/5] port %PORT% free
)

REM ---------- 5. start the server, wait, open the browser ----------
echo [5/5] starting server...
set "PYW="
where pythonw >nul 2>&1 && set "PYW=pythonw"
if defined PYW (
    start "" /min %PYW% "server.py" --port %PORT%
) else (
    start "" /min %PY% "server.py" --port %PORT%
)

set "UP="
for /l %%i in (1,1,40) do (
    if not defined UP (
        curl -s -o nul --max-time 2 "%URL%api/channels" && set "UP=1"
        if not defined UP timeout /t 1 /nobreak >nul
    )
)

if not defined UP (
    echo.
    echo [X] Server did not answer on %URL%
    echo     Run this to see the error:
    echo         %PY% server.py --port %PORT%
    echo.
    pause
    exit /b 1
)

echo.
echo Ready: %URL%

REM ---- open as a desktop app window (no tabs, no URL bar) ----
set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"

if exist "%EDGE%" (
    start "" "%EDGE%" --app="%URL%" --window-size=1440,900
) else if exist "%CHROME%" (
    start "" "%CHROME%" --app="%URL%" --window-size=1440,900
) else (
    start "" "%URL%"
)
timeout /t 2 /nobreak >nul
exit /b 0
