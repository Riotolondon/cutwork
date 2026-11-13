@echo off
rem Windows: double-click this file to start the CUTWORK link helper.
rem It uses the clipper's Python packages, so run cutwork-clipper\run.bat once first.
setlocal
cd /d "%~dp0"
title CUTWORK link helper
if not exist "..\cutwork-clipper\.venv\Scripts\python.exe" (
  echo.
  echo   Start the clipper once first: double-click cutwork-clipper\run.bat
  echo.
  pause
  exit /b 1
)
"..\cutwork-clipper\.venv\Scripts\python.exe" server.py
echo.
echo   The CUTWORK link helper has stopped.
pause
