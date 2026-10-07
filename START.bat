@echo off
setlocal EnableExtensions
title NUNES IndiaMART Automation V2

pushd "%~dp0"
if errorlevel 1 (
  echo ERROR: Could not access project folder.
  pause
  exit /b 1
)

set "VENV_PYTHON=%LOCALAPPDATA%\NUNES_INDIAMART_AUTOMATION\venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
  echo ERROR: Local Python environment not found.
  echo Run SETUP.bat first.
  popd
  pause
  exit /b 1
)

start "" "http://127.0.0.1:5077"
"%VENV_PYTHON%" app.py
set "APP_EXIT=%ERRORLEVEL%"

popd
if not "%APP_EXIT%"=="0" (
  echo.
  echo Application exited with error code %APP_EXIT%.
)
pause
exit /b %APP_EXIT%
