@echo off
REM Creates Desktop + Start Menu shortcuts for Interview Assistant.
REM Run from the shipping/client/ folder after building the exe.

set "EXE=%~dp0dist\InterviewAssistant.exe"
set "NAME=Interview Assistant"

if not exist "%EXE%" (
    echo ERROR: %EXE% not found. Run build_exe.bat first.
    pause
    exit /b 1
)

echo Creating Desktop shortcut...
powershell -Command "$s=(New-Object -COM WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\%NAME%.lnk'); $s.TargetPath='%EXE%'; $s.WorkingDirectory='%~dp0dist'; $s.Save()"

echo Creating Start Menu shortcut...
powershell -Command "$s=(New-Object -COM WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Programs')+'\%NAME%.lnk'); $s.TargetPath='%EXE%'; $s.WorkingDirectory='%~dp0dist'; $s.Save()"

echo Shortcuts created on Desktop and Start Menu.
pause
