@echo off
REM Launch desktop overlay. LICENSE_SERVER_URL priority:
REM   1. env var LICENSE_SERVER_URL  2. GUI\license_server.txt  3. localhost
REM setup_paid.bat writes license_server.txt for you.
setlocal
if exist "%~dp0license_server.txt" (
  set /p _LSRV=<"%~dp0license_server.txt"
  if defined _LSRV set "LICENSE_SERVER_URL=%_LSRV%"
)
if defined LICENSE_SERVER_URL echo [run_gui] License server: %LICENSE_SERVER_URL%
"%~dp0..\ass_env\Scripts\python.exe" "%~dp0app.py" %*
