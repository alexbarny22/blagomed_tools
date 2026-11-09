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
if not exist "%BLAGOMED_BASE%\app\web\server.py" (
  echo [ОШИБКА] Не найден каталог установки: "%BLAGOMED_BASE%"
  echo Укажите его: set BLAGOMED_BASE=D:\путь\SebestoimostMP
  pause
  exit /b 2
)

set "PY=python"
if exist "%BLAGOMED_BASE%\venv\Scripts\python.exe" set "PY=%BLAGOMED_BASE%\venv\Scripts\python.exe"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist "%BLAGOMED_BASE%\config.ini" (
  echo [ОШИБКА] Не найден config.ini.
  echo Скопируйте config.example.ini в config.ini и заполните [auth] и [1C]:
  echo   copy "%BLAGOMED_BASE%\config.example.ini" "%BLAGOMED_BASE%\config.ini"
  echo Хэш пароля: "%PY%" tools\hash_password.py "пароль"
  pause
  exit /b 2
)

echo Каталог: %BLAGOMED_BASE%
"%PY%" "%BLAGOMED_BASE%\tools\web_control.py" start --base "%BLAGOMED_BASE%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo Сервер не запущен. Проверьте окружение:
  echo   "%PY%" "%BLAGOMED_BASE%\tools\check_env.py" --base "%BLAGOMED_BASE%"
  echo Журнал: "%BLAGOMED_BASE%\logs\server.out"
  pause
  exit /b %RC%
)
echo.
echo Откройте http://127.0.0.1:8085 (порт — из config.ini).
echo Остановка: stop.bat
pause
endlocal
exit /b 0