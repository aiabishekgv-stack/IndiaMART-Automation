@echo off
setlocal EnableExtensions
title NUNES IndiaMART Automation V2 - LAN Server

pushd "%~dp0"
if errorlevel 1 (
  echo ERROR: Could not access project folder.
  pause
  exit /b 1
)

set "VENV_PYTHON=%LOCALAPPDATA%\NUNES_INDIAMART_AUTOMATION\venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
  echo ERROR: Run SETUP.bat first on this server PC.
  popd
  pause
  exit /b 1
)

set "APP_HOST=0.0.0.0"
set "APP_PORT=5077"

echo.
echo NUNES V2 LAN SERVER MODE
echo Other office computers can use:
echo http://SERVER-PC-IP:5077
echo.
echo The IndiaMART browser opens on THIS server PC.
echo.
start "" "http://127.0.0.1:5077"
"%VENV_PYTHON%" app.py
set "APP_EXIT=%ERRORLEVEL%"
popd
pause
exit /b %APP_EXIT%
