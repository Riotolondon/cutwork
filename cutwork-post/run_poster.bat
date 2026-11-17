@echo off
rem Windows: double-click to open CUTWORK Poster
setlocal
cd /d "%~dp0"
title CUTWORK Poster
set "PY=python"
where python >nul 2>nul || set "PY=py -3"
%PY% poster.py
echo.
echo   CUTWORK Poster has stopped.
pause
