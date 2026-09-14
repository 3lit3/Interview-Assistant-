@echo off
echo ==========================================
echo  Interview Assistant — First-Time Setup
echo ==========================================
echo.

cd /d "%~dp0"

echo [1/4] Creating Python virtual environment...
if not exist ".venv" (
    python -m venv .venv
    echo       Created .venv
) else (
    echo       .venv already exists — skipping
)

echo.
echo [2/4] Installing Python dependencies (client + server)...
.venv\Scripts\pip.exe install -r requirements.txt --quiet
.venv\Scripts\pip.exe install -r server\requirements.txt --quiet

echo.
echo [3/4] Setting up server environment...
if not exist "server\.env" (
    copy "server\.env.example" "server\.env" >nul
    echo       Created server\.env from template
    echo       *** Edit server\.env with your LLM_API_KEY and KEY_ISSUANCE_SECRET ***
) else (
    echo       server\.env already exists — skipping
)

echo.
echo [4/4] Setting up client environment...
if not exist "client\.env" (
    copy ".env.example" "client\.env" >nul
    echo       Created client\.env from template
) else (
    echo       client\.env already exists — skipping
)

echo.
echo ==========================================
echo  Setup complete.
echo.
echo  NEXT STEPS:
echo    1. Edit server\.env — set LLM_API_KEY and KEY_ISSUANCE_SECRET
echo    2. Start server:  server\run_server.bat
echo    3. Start client:  client\run_gui.bat
echo ==========================================
pause
