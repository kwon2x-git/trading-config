@echo off
REM run_daily_check.bat
REM 2026-09-23 생성
REM daily_check.py 실행 전, GitHub trading-config 레포의 최신 코드를 먼저 받아와
REM C:\trading\daily_check.py를 덮어씁니다. 이후 파이썬을 실행합니다.
REM 작업 스케줄러의 4개 작업은 이제 python.exe 대신 이 .bat 파일을 직접 실행하도록 바꿔주세요.

cd /d C:\trading

powershell -Command "try { Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/kwon2x-git/trading-config/main/daily_check.py' -OutFile 'daily_check.py.new' -TimeoutSec 15; Move-Item -Force 'daily_check.py.new' 'daily_check.py'; Write-Host '[OK] daily_check.py 최신본 다운로드 완료' } catch { Write-Host '[경고] 최신본 다운로드 실패, 기존 로컬 파일로 계속 진행' }"

python daily_check.py %*
