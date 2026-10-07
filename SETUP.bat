@echo off
setlocal EnableExtensions

title NUNES IndiaMART Automation V3.0 - Setup

pushd "%~dp0"
if errorlevel 1 (
    echo.
    echo ERROR: Could not access the project folder.
    echo %~dp0
    pause
    exit /b 1
)

echo.
echo ======================================================
echo  NUNES INDIAMART AUTOMATION V3.0 - SETUP
echo ======================================================
echo Project: %CD%
echo.

if not exist "requirements.txt" goto missing_project
if not exist "app.py" goto missing_project

set "LOCAL_ROOT=%LOCALAPPDATA%\NUNES_INDIAMART_AUTOMATION"
set "VENV_DIR=%LOCAL_ROOT%\venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"

if not exist "%LOCAL_ROOT%" mkdir "%LOCAL_ROOT%"

echo Local Python environment:
echo %VENV_DIR%
echo.

if exist "%VENV_PYTHON%" goto venv_ready

where py >nul 2>&1
if errorlevel 1 goto try_python

echo Creating local virtual environment with Python Launcher...
py -3 -m venv "%VENV_DIR%"
if errorlevel 1 goto venv_fail
goto venv_ready

:try_python
where python >nul 2>&1
if errorlevel 1 goto python_missing

echo Creating local virtual environment with python.exe...
python -m venv "%VENV_DIR%"
if errorlevel 1 goto venv_fail

:venv_ready
echo Updating pip...
"%VENV_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 goto fail

echo.
echo Installing Python requirements...
"%VENV_PYTHON%" -m pip install -r "%CD%\requirements.txt"
if errorlevel 1 goto fail

echo.
echo Installing Playwright Chromium...
"%VENV_PYTHON%" -m playwright install chromium
if errorlevel 1 goto fail

set "CONFIG_DIR=%LOCAL_ROOT%\config"
set "LOCAL_ENV=%CONFIG_DIR%\.env"
if not exist "%CONFIG_DIR%" mkdir "%CONFIG_DIR%"
if not exist "%LOCAL_ENV%" (
    copy /Y ".env.example" "%LOCAL_ENV%" >nul
    echo Created private config: %LOCAL_ENV%
)

echo.
echo Checking Python source files...
"%VENV_PYTHON%" -m py_compile app.py automation.py product_record.py ai_client.py public_indiamart.py image_acquirer.py image_studio.py pdf_builder.py indiamart.py sheets_client.py config_manager.py diagnostics.py production_manager.py
if errorlevel 1 goto fail

echo.
where ollama >nul 2>&1
if errorlevel 1 goto ollama_missing

echo Ollama found. Checking/pulling qwen3:1.7b...
ollama list | findstr /I "qwen3:1.7b" >nul 2>&1
if not errorlevel 1 goto setup_done
ollama pull qwen3:1.7b
if errorlevel 1 echo WARNING: Qwen model pull did not finish. You can run: ollama pull qwen3:1.7b
goto setup_done

:ollama_missing
echo.
echo WARNING: Ollama is not installed or not in PATH.
echo Install Ollama, then run:
echo     ollama pull qwen3:1.7b

goto setup_done

:setup_done
echo.
echo ======================================================
echo  SETUP COMPLETED
echo ======================================================
echo Project: %CD%
echo Python : %VENV_PYTHON%
echo.
echo Next:
echo   1. Run TEST.bat.
echo   2. Run START.bat.
echo   3. Google Sheet link is preconfigured; no Apps Script token is needed for loading.
echo      Private config: %LOCAL_ENV%
echo.
popd
pause
exit /b 0

:missing_project
echo ERROR: This does not look like the V2 project folder.
popd
pause
exit /b 1

:python_missing
echo ERROR: Python 3 was not found. Install Python 3.11+ and rerun SETUP.bat.
popd
pause
exit /b 1

:venv_fail
echo ERROR: Could not create the local virtual environment.
popd
pause
exit /b 1

:fail
echo.
echo SETUP FAILED. Review the error above.
popd
pause
exit /b 1
