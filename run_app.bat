@echo off
setlocal

set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"
set "PYTHON_EXE=%VENV%\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo [setup] Creating local virtual environment at "%VENV%"
    py -3.11 -m venv "%VENV%"
    if errorlevel 1 (
        echo [error] Could not create venv with py -3.11.
        echo [hint] Install Python 3.11 or edit run_app.bat to target your installed Python.
        exit /b 1
    )

    echo [setup] Installing dependencies...
    "%PYTHON_EXE%" -m pip install --upgrade pip
    if errorlevel 1 exit /b 1
    "%PYTHON_EXE%" -m pip install -r "%ROOT%requirements.txt"
    if errorlevel 1 exit /b 1
)

echo [run] Using Python: %PYTHON_EXE%
"%PYTHON_EXE%" "%ROOT%app.py"
