@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

set "PY_BIN="

where python >nul 2>nul
if not errorlevel 1 (
    set "PY_BIN=python"
) else if exist "%USERPROFILE%\.pyenv\pyenv-win\shims\python.exe" (
    set "PY_BIN=%USERPROFILE%\.pyenv\pyenv-win\shims\python.exe"
) else if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    set "PY_BIN=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
)

if "%PY_BIN%"=="" (
    echo [ERREUR] Aucun interpreteur Python detecte.
    exit /b 1
)

"%PY_BIN%" backend\main.py %*
