@echo off
setlocal EnableExtensions
title NUNES IndiaMART Automation V3.3 - System Test
pushd "%~dp0"
set "VENV_PYTHON=%LOCALAPPDATA%\NUNES_INDIAMART_AUTOMATION\venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
  echo ERROR: Run SETUP.bat first.
  popd
  pause
  exit /b 1
)

"%VENV_PYTHON%" diagnostics.py
set "TEST_EXIT=%ERRORLEVEL%"
if not "%TEST_EXIT%"=="0" goto done

"%VENV_PYTHON%" tests\sheet_dropdown_test.py
set "TEST_EXIT=%ERRORLEVEL%"

:done
popd
pause
exit /b %TEST_EXIT%
