@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0publisher_cleanup.ps1" %*
if errorlevel 1 (
    echo.
    echo Cleanup tool failed; review the message above.
    pause
)
