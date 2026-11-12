@echo off
rem Windows: double-click this file
setlocal
cd /d "%~dp0"
title CUTWORK Clipper

set "PY=python"
where python >nul 2>nul || set "PY=py -3"
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python 3.10 or newer is needed and was not found.
  echo   Install it from https://www.python.org/downloads/ and tick "Add Python to PATH",
  echo   then double-click this file again.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo   Setting up for the first time. This takes a few minutes...
  %PY% -m venv .venv
  if errorlevel 1 ( echo   Could not create the Python environment. & pause & exit /b 1 )
)

rem Install or update packages only when requirements.txt has changed since the last good install
fc /b requirements.txt ".venv\requirements.installed" >nul 2>nul
if errorlevel 1 (
  echo   Installing packages...
  ".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo.
    echo   The packages could not be installed. Check the internet connection and try again.
    pause
    exit /b 1
  )
  copy /y requirements.txt ".venv\requirements.installed" >nul
)

".venv\Scripts\python.exe" app.py
echo.
echo   CUTWORK Clipper has stopped.
pause
