@echo off
REM Run license server locally (dev/mock mode).
REM Usage: server\run_server.bat [port]
REM NOTE: run from a terminal (cmd) so errors stay visible. Do not double-click blind.
setlocal
set PORT=%1
if "%PORT%"=="" set PORT=8000
cd /d "%~dp0"

if not exist ".env" (
  echo [run_server] No server\.env found - copying from .env.example for MOCK mode.
  copy /y ".env.example" ".env" >nul
)

echo [run_server] Installing deps...
"..\ass_env\Scripts\python.exe" -m pip install -r requirements.txt
if %errorlevel% neq 0 (
  echo.
  echo FAILED: pip install failed. See error above.
  pause
  exit /b 1
)

echo [run_server] Checking uvicorn...
"..\ass_env\Scripts\python.exe" -c "import fastapi, uvicorn; print('fastapi+uvicorn OK')"
if %errorlevel% neq 0 (
  echo.
  echo FAILED: fastapi/uvicorn not importable in ass_env even after install.
  pause
  exit /b 1
)

echo [run_server] Starting on http://127.0.0.1:%PORT% (Ctrl+C to stop)...
"..\ass_env\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port %PORT%
echo.
echo [run_server] Server stopped (exit code %errorlevel%).
pause
