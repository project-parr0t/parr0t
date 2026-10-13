@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-Translation-Patch.ps1" %*
set "PARR0T_EXIT=%ERRORLEVEL%"
pause
exit /b %PARR0T_EXIT%
