@echo off
REM One-shot setup for paid licensing ($9.99/mo, 1 seat).
REM Does: install server deps, create server\.env, point client at server.
REM Usage:
REM   setup_paid.bat                                   (local mock dev)
REM   setup_paid.bat https://YOURHOST                  (live server URL)
REM   setup_paid.bat https://YOURHOST <api-key> <ipn-secret>
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set HOST=%1
set APIKEY=%2
set IPNSECRET=%3
if "%HOST%"=="" set HOST=http://127.0.0.1:8000

echo [1/4] Installing server deps...
"ass_env\Scripts\python.exe" -m pip install -r server\requirements.txt
if %errorlevel% neq 0 (
  echo.
  echo FAILED: pip install failed. See error above.
  echo Try running this file from a terminal to see full output.
  pause
  exit /b 1
)

echo [2/4] server\.env ...
if not exist "server\.env" ( copy /y "server\.env.example" "server\.env" >nul & echo created server\.env )
if not "%APIKEY%"=="" (
  powershell -NoProfile -Command "(Get-Content 'server\.env') -replace '^NOWPAYMENTS_API_KEY=.*','NOWPAYMENTS_API_KEY=%APIKEY%' -replace '^NOWPAYMENTS_IPN_SECRET=.*','NOWPAYMENTS_IPN_SECRET=%IPNSECRET%' -replace '^MOCK_NOWPAYMENTS=.*','MOCK_NOWPAYMENTS=0' -replace '^IPN_CALLBACK_URL=.*','IPN_CALLBACK_URL=%HOST%/ipn' | Set-Content 'server\.env'"
  echo live keys written, MOCK off, IPN=%HOST%/ipn
) else (
  echo mock mode kept. To go live re-run: setup_paid.bat https://YOURHOST key secret
)

echo [3/4] Pointing desktop client at %HOST% ...
echo %HOST%> "GUI\license_server.txt"
echo wrote GUI\license_server.txt

echo [4/4] Done.
echo.
echo  Local dev : start server in one window:  server\run_server.bat
echo              then in another window:       GUI\run_gui.bat
echo  Deploy    : push to GitHub, then Render.com -^> New Web Service -^> this repo
echo              (render.yaml is included). Set env vars from server\.env there.
echo              Then: setup_paid.bat https://YOURHOST.onrender.com [key secret]
echo  NOWPayments dashboard: set IPN callback URL to %HOST%/ipn
echo.
pause
