@echo off
echo Starting license + key-issuance server on http://127.0.0.1:8000
echo Press Ctrl+C to stop.
cd /d "%~dp0"
..\.venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
pause
