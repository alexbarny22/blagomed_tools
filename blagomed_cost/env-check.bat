@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

if not defined BLAGOMED_BASE (
  if exist "C:\Blagomed\SebestoimostMP\app\web\server.py" (
    set "BLAGOMED_BASE=C:\Blagomed\SebestoimostMP"
  ) else (
    set "BLAGOMED_BASE=%~dp0"
  )
)
set "PY=python"
if exist "%BLAGOMED_BASE%\venv\Scripts\python.exe" set "PY=%BLAGOMED_BASE%\venv\Scripts\python.exe"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"%PY%" "%BLAGOMED_BASE%\tools\check_env.py" --base "%BLAGOMED_BASE%"
set "RC=%ERRORLEVEL%"
echo.
pause
endlocal
exit /b %RC%