@echo off
echo Starting Interview Assistant GUI...
cd /d "%~dp0"
..\.venv\Scripts\python.exe app.py %*
pause
