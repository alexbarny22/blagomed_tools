@echo off
rem Server loop: update from GitHub, start web bot, restart on exit.
cd /d "%~dp0"
set "PY=python"
if exist python_path.txt set /p PY=<python_path.txt
:loop
"%PY%" update.py >> server_run.log 2>&1
"%PY%" web.py >> server_run.log 2>&1
timeout /t 5 /nobreak >nul
goto loop
