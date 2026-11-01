@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0publisher_cleanup.ps1" %*
if errorlevel 1 (
    echo.
    echo 清理工具执行失败，请查看上面的提示。
    pause
)
