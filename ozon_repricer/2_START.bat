@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist venv\Scripts\pythonw.exe (
  echo Сначала запустите 1_INSTALL.bat
  pause
  exit /b 1
)
rem обновление с GitHub проверяется внутри программы при запуске
start "" "venv\Scripts\pythonw.exe" "app.py"
