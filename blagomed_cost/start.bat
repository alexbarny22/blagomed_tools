@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

rem 1) Каталог установки: явный BLAGOMED_BASE или каталог самого start.bat.
rem Автоподстановка другой установленной копии (C:\Blagomed\...) запрещена.
if not defined BLAGOMED_BASE set "BLAGOMED_BASE=%~dp0"
for %%I in ("%BLAGOMED_BASE%") do set "BLAGOMED_BASE=%%~fI"
pushd "%BLAGOMED_BASE%" >nul 2>&1
if errorlevel 1 goto :bad_base
set "BLAGOMED_BASE=%CD%"
popd

if not exist "%BLAGOMED_BASE%\app\web\server.py" goto :bad_base

for %%F in ("app\web\server.py" "tools\web_control.py" "tools\check_env.py" "config.example.ini" "requirements.txt") do (
  if not exist "%BLAGOMED_BASE%\%%~F" (
    echo [ОШИБКА] В каталоге "%BLAGOMED_BASE%" не найден "%%~F".
    echo Это неполная копия приложения. Распакуйте проект целиком.
    pause
    exit /b 2
  )
)

if /I "%~1"=="--check" (
  echo [OK] Каталог запуска: %BLAGOMED_BASE%
  if exist "%BLAGOMED_BASE%\config.ini" (
    echo [OK] config.ini найден
  ) else (
    echo [ПРЕДУПРЕЖДЕНИЕ] config.ini не найден
  )
  exit /b 0
)

rem 2) Python: сначала venv рядом с приложением, затем системный Python.
set "PY=%BLAGOMED_BASE%\venv\Scripts\python.exe"
set "PY_ARGS="
if exist "%PY%" goto :python_ready
goto :find_python

:find_python
set "PY=python"
where python >nul 2>&1
if not errorlevel 1 goto :python_ready
set "PY=py"
set "PY_ARGS=-3"
where py >nul 2>&1
if not errorlevel 1 goto :python_ready
echo [ОШИБКА] Python не найден в PATH.
echo Установите Python 3.10+ или запустите install.bat.
pause
exit /b 2

:python_ready
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"%PY%" %PY_ARGS% --version >nul 2>&1
if errorlevel 1 (
  echo [ОШИБКА] Не удалось запустить Python: "%PY%" %PY_ARGS%
  echo Установите Python 3.10+ или запустите install.bat.
  pause
  exit /b 2
)

rem 3) Конфигурация: не создаём БД и не перезаписываем config.ini.
if not exist "%BLAGOMED_BASE%\config.ini" (
  echo [ОШИБКА] Не найден config.ini.
  echo Запустите install.bat или скопируйте config.example.ini в config.ini:
  echo   copy "%BLAGOMED_BASE%\config.example.ini" "%BLAGOMED_BASE%\config.ini"
  echo Хэш пароля: "%PY%" %PY_ARGS% "%BLAGOMED_BASE%\tools\hash_password.py" "пароль"
  pause
  exit /b 2
)

echo Каталог: %BLAGOMED_BASE%
"%PY%" %PY_ARGS% "%BLAGOMED_BASE%\tools\web_control.py" start --base "%BLAGOMED_BASE%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo Сервер не запущен. Проверьте окружение:
  echo   "%PY%" %PY_ARGS% "%BLAGOMED_BASE%\tools\check_env.py" --base "%BLAGOMED_BASE%"
  echo Журнал: "%BLAGOMED_BASE%\logs\server.out"
  pause
  exit /b %RC%
)
echo.
echo Откройте адрес из строки "Сервер запущен" выше (порт — из config.ini).
echo Остановка: stop.bat
pause
endlocal
exit /b 0

:bad_base
echo [ОШИБКА] Не найден каталог установки: "%BLAGOMED_BASE%"
echo Нет файла "%BLAGOMED_BASE%\app\web\server.py".
echo Запускайте start.bat из корня приложения или задайте:
echo   set BLAGOMED_BASE=D:\путь\SebestoimostMP
pause
exit /b 2