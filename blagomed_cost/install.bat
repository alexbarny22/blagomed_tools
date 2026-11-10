@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

rem Установка зависимостей в venv рядом с приложением.
rem База данных не создаётся и config.ini не перезаписывается.
if not defined BLAGOMED_BASE set "BLAGOMED_BASE=%~dp0"
for %%I in ("%BLAGOMED_BASE%") do set "BLAGOMED_BASE=%%~fI"
pushd "%BLAGOMED_BASE%" >nul 2>&1
if errorlevel 1 goto :bad_base
set "BLAGOMED_BASE=%CD%"
popd

if not exist "%BLAGOMED_BASE%\app\web\server.py" (
  echo [ОШИБКА] Не найден "%BLAGOMED_BASE%\app\web\server.py".
  echo Запускайте install.bat из корня приложения.
  pause
  exit /b 2
)
if not exist "%BLAGOMED_BASE%\requirements.txt" (
  echo [ОШИБКА] Не найден "%BLAGOMED_BASE%\requirements.txt".
  pause
  exit /b 2
)
if not exist "%BLAGOMED_BASE%\config.example.ini" (
  echo [ОШИБКА] Не найден "%BLAGOMED_BASE%\config.example.ini".
  pause
  exit /b 2
)

set "PY=python"
set "PY_ARGS="
where python >nul 2>&1
if not errorlevel 1 goto :have_python
set "PY=py"
set "PY_ARGS=-3"
where py >nul 2>&1
if not errorlevel 1 goto :have_python
echo [ОШИБКА] Python не найден в PATH.
echo Установите Python 3.10+ с python.org и включите "Add Python to PATH".
pause
exit /b 2

:have_python
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
echo Каталог: %BLAGOMED_BASE%
echo Python: %PY% %PY_ARGS%

set "VPY=%BLAGOMED_BASE%\venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo Создаю venv...
  "%PY%" %PY_ARGS% -m venv "%BLAGOMED_BASE%\venv"
  if errorlevel 1 goto :failed
) else (
  echo venv уже существует, использую его.
)

echo Обновляю pip...
"%VPY%" -m pip install --upgrade pip
if errorlevel 1 goto :failed

echo Устанавливаю зависимости из requirements.txt...
"%VPY%" -m pip install -r "%BLAGOMED_BASE%\requirements.txt"
if errorlevel 1 goto :failed

if not exist "%BLAGOMED_BASE%\config.ini" (
  copy /Y "%BLAGOMED_BASE%\config.example.ini" "%BLAGOMED_BASE%\config.ini" >nul
  echo Создан config.ini из config.example.ini.
) else (
  echo config.ini уже существует, не изменяю.
)

echo.
echo Установка завершена. Дальше:
echo 1. Создайте хэш пароля:
echo    "%VPY%" "%BLAGOMED_BASE%\tools\hash_password.py"
echo 2. Вставьте хэш и логин в "%BLAGOMED_BASE%\config.ini" ([auth]).
echo 3. Проверьте окружение: env-check.bat
echo 4. Запустите: start.bat
echo База данных создаётся только вручную на странице "База данных" после входа.
pause
exit /b 0

:failed
echo.
echo [ОШИБКА] Установка не завершена. Проверьте интернет/доступ к pip и повторите.
pause
exit /b 1

:bad_base
echo [ОШИБКА] Не найден каталог установки: "%BLAGOMED_BASE%"
echo Запускайте install.bat из корня приложения.
pause
exit /b 2