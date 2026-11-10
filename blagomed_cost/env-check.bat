@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

rem Каталог установки: явный BLAGOMED_BASE или каталог самого env-check.bat.
if not defined BLAGOMED_BASE set "BLAGOMED_BASE=%~dp0"
for %%I in ("%BLAGOMED_BASE%") do set "BLAGOMED_BASE=%%~fI"
pushd "%BLAGOMED_BASE%" >nul 2>&1
if errorlevel 1 goto :bad_base
set "BLAGOMED_BASE=%CD%"
popd

if not exist "%BLAGOMED_BASE%\app\web\server.py" goto :bad_base
if not exist "%BLAGOMED_BASE%\tools\check_env.py" (
  echo [ОШИБКА] Не найден "%BLAGOMED_BASE%\tools\check_env.py".
  pause
  exit /b 2
)

if /I "%~1"=="--check" (
  echo [OK] Каталог запуска: %BLAGOMED_BASE%
  exit /b 0
)

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
pause
exit /b 2

:python_ready
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"%PY%" %PY_ARGS% "%BLAGOMED_BASE%\tools\check_env.py" --base "%BLAGOMED_BASE%"
set "RC=%ERRORLEVEL%"
echo.
pause
endlocal
exit /b %RC%

:bad_base
echo [ОШИБКА] Не найден каталог установки: "%BLAGOMED_BASE%"
echo Нет файла "%BLAGOMED_BASE%\app\web\server.py".
echo Запускайте env-check.bat из корня приложения или задайте BLAGOMED_BASE.
pause
exit /b 2