@echo off
REM run_daily_check.bat (2026-09-23 v2 -- curl 기반으로 변경, 오류 확인 가능)
cd /d C:\trading

echo [다운로드 시도] daily_check.py 최신본...
curl.exe -sS -f -L -o daily_check.py.new "https://raw.githubusercontent.com/kwon2x-git/trading-config/main/daily_check.py"
if %ERRORLEVEL% EQU 0 (
    move /y daily_check.py.new daily_check.py >nul
    echo [OK] 최신본 다운로드 및 교체 완료
) else (
    echo [경고] 다운로드 실패^(코드 %ERRORLEVEL%^) - 기존 로컬 daily_check.py로 계속 진행
    if exist daily_check.py.new del daily_check.py.new
)

python daily_check.py %*

REM 스케줄러가 아닌 더블클릭으로 직접 테스트할 때만 아래 줄 주석 해제해서 창이 안 닫히게 하세요.
REM pause
