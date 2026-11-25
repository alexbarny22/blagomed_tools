@echo off
chcp 866 >nul
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  echo [ОШИБКА] Сначала выполните INSTALL.cmd
  pause
  exit /b 1
)
echo Запуск сервера на http://127.0.0.1:8085 - окно не закрывайте.
"venv\Scripts\python.exe" run.py
pause
