@echo off
REM run_daily_check.bat (2026-09-23 v3 -- ASCII only, encoding issue fixed)
cd /d C:\trading

echo [download] fetching latest daily_check.py from GitHub...
curl.exe -sS -f -L -o daily_check.py.new "https://raw.githubusercontent.com/kwon2x-git/trading-config/main/daily_check.py"
if %ERRORLEVEL% EQU 0 (
    move /y daily_check.py.new daily_check.py >nul
    echo [OK] daily_check.py updated
) else (
    echo [WARN] download failed, code=%ERRORLEVEL%, using existing local daily_check.py
    if exist daily_check.py.new del daily_check.py.new
)

python daily_check.py %*

REM Uncomment the line below only when double-clicking manually to keep the window open.
REM pause
