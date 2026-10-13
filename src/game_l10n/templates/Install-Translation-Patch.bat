@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-Translation-Patch.ps1" %*
set "PARR0T_EXIT=%ERRORLEVEL%"
echo.
if not "%PARR0T_EXIT%"=="0" echo The patch operation failed. No unrelated game files were managed.
pause
exit /b %PARR0T_EXIT%
