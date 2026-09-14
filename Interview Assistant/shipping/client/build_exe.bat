@echo off
echo Building Interview Assistant into single .exe ...
cd /d "%~dp0"

REM Close any running instance first
taskkill /F /IM InterviewAssistant.exe 2>nul

REM Build
..\.venv\Scripts\python.exe -m PyInstaller --onefile --windowed --name InterviewAssistant ^
    --distpath dist ^
    --workpath build ^
    --specpath . ^
    --paths "..\shared" ^
    --paths ".." ^
    app.py

echo.
echo Copying .env to dist\ for frozen exe...
if exist "..\client\.env" copy "..\client\.env" "dist\.env" >nul
if exist "..\client\license_server.txt" copy "..\client\license_server.txt" "dist\license_server.txt" >nul

echo.
echo Done. Exe at: client\dist\InterviewAssistant.exe
echo.
echo To install Desktop shortcut: run install_shortcut.bat
pause
